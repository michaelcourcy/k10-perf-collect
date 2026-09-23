#!/usr/bin/env python3
"""Generate the K10 export topology of a cluster as JSON.

Everything hangs off what is actually in the Kopia repositories on the object stores:
a namespace that has never been exported does not appear. repo_checker is the primary
source; the K10 API objects, K10's own Prometheus and cluster monitoring (cAdvisor +
kube-state-metrics) complete the picture.

    ExportTopology
      helmLimiters
      policies[]
        profile, frequency / subFrequency, exportFrequency, retention
        namespaces[]
          actionPodSpecs[]
          repository (id, maintenance)
          exports[]  (ExportAction windows, datamover peak cpu/memory)
          pvcs[]
            fileCount, totalSizeBytes, averageFileSizeBytes, sizeHistogram
            lastChangeRate, lastMaintenance
            snapshots[] (files, size, start/end, change rate, datamover peak cpu/memory)

Requirements: python3 (stdlib only), kubectl in PATH, helm in PATH (repo_checker
insists on it), network access to the cluster. Run:

    ./generate-export-topology.py --context <kube-context> -o export-topology.json

Everything the script learnt the hard way is in guides/ and CLAUDE.md of this repo; the
comments below point at the relevant guide where it matters.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- constants

REPO_CHECKER_URL = "https://docs.kasten.io/downloads/{version}/tools/k10_repo_checker.sh"

# Worker pods K10 creates for an export. Validated on 9.0.5 (guide 09).
DATAMOVER_POD_REGEX = r"data-mover.*|copy-vol-data.*|create-repo.*|repository-server.*|restore-data.*"

# k10-config keys that govern datamover behaviour (guide 10 step 3).
LIMITER_KEY_REGEX = re.compile(
    r"^(K10Limiter|k10DataStore|WorkerPod|K10EphemeralPVCOverhead|K10BackupBufferFileHeadroomFactor"
    r"|K10Timeout|csiSnapshot|workerPodResourcesCRDEnabled)"
)

# File-size buckets for the histogram (guide 05 step C).
HISTOGRAM_BUCKETS = [
    ("lt4KiB", 4 * 1024),
    ("4KiB_64KiB", 64 * 1024),
    ("64KiB_1MiB", 1024 * 1024),
    ("1MiB_16MiB", 16 * 1024 * 1024),
    ("16MiB_256MiB", 256 * 1024 * 1024),
    ("gt256MiB", None),
]

# Padding around a Kopia snapshot window when reading cAdvisor. The kubelet scrapes
# every 30 s and the datamover pod starts before Kopia starts hashing.
METRICS_PAD_SECONDS = 45

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print(f"  ! {msg}", file=sys.stderr)


START_TIME = time.monotonic()
VERBOSE = False
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def log(msg):
    """Progress goes to stderr with the elapsed time, so a user watching a long run can tell
    the tool is still working (inventory and tree listings take minutes with no other output)."""
    e = int(time.monotonic() - START_TIME)
    print(f"[{e // 60:02d}:{e % 60:02d}] {msg}", file=sys.stderr, flush=True)


class Step:
    """Context manager that logs how long a sub-step took: `with Step("content list") as st: ...;
    st.note = f"{n} contents"` prints `    content list: 12345 contents (41s)`."""

    HEARTBEAT = 60

    def __init__(self, label, indent=4):
        self.label, self.indent, self.note = label, indent, ""
        self.done = threading.Event()

    def _beat(self):
        # a single kubectl exec (content list, tree listing) can run for many minutes with
        # no output; say so, or a slow step is indistinguishable from a hung one
        while not self.done.wait(self.HEARTBEAT):
            log(f"{' ' * self.indent}{self.label} still running ({int(time.monotonic() - self.t0)}s) ...")

    def __enter__(self):
        self.t0 = time.monotonic()
        log(f"{' ' * self.indent}{self.label} ...")
        threading.Thread(target=self._beat, daemon=True).start()
        return self

    def __exit__(self, et, ev, tb):
        self.done.set()
        if et is None:
            d = time.monotonic() - self.t0
            log(f"{' ' * self.indent}{self.label}: {self.note} ({d:.0f}s)".replace(":  (", " ("))


# --------------------------------------------------------------------------- helpers

def parse_rfc3339(s):
    """Kopia emits nanoseconds; datetime accepts at most microseconds."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$", s)
    if not m:
        return None
    base, frac, tz = m.groups()
    frac = (frac or ".0")[:7].ljust(7, "0")  # keep 6 digits
    if tz == "Z":
        tz = "+00:00"
    return dt.datetime.fromisoformat(f"{base}{frac}{tz}")


def epoch(d):
    return d.timestamp() if d else None


def iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if d else None


def safe_div(a, b):
    return (a / b) if b else None


# --------------------------------------------------------------------------- kubectl

class Kube:
    def __init__(self, context, k10ns):
        self.context = context
        self.k10ns = k10ns

    def run(self, *args, check=True, stdin=None, timeout=600):
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += list(args)
        r = subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:6])}...: {r.stderr.strip()[:300]}")
        return r

    def json(self, *args):
        r = self.run(*args, "-o", "json", check=False)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return None

    def get(self, kind, name=None, ns=None, all_ns=False):
        args = ["get", kind]
        if name:
            args.append(name)
        if all_ns:
            args.append("-A")
        elif ns:
            args += ["-n", ns]
        return self.json(*args)

    def exec(self, ns, pod, script, timeout=1800):
        """Run a shell command inside the pod; only stdout is returned. The command goes as
        an argument (sh -c), not on stdin: with `kubectl exec -i` and piped stdin, large
        outputs came back truncated or garbled (a 100 KB `kopia snapshot list --json`
        broke mid-document on a lab cluster, intermittently)."""
        r = self.run("-n", ns, "exec", pod, "--", "sh", "-c", script, check=False, timeout=timeout)
        return r.stdout, r.stderr, r.returncode


# --------------------------------------------------------------------------- prometheus

class Prom:
    """Cluster monitoring (cAdvisor + kube-state-metrics). OpenShift Thanos by default;
    any Prometheus with --prom-url. Reproduces lib/prometheus.sh (guide 00 section 5)."""

    def __init__(self, kube, url=None, token=None, prom_ns="openshift-monitoring", header=None):
        self.kube = kube
        self.url = url
        self.token = token
        self.prom_ns = prom_ns
        self.header = header
        self.flavour = "custom" if url else "openshift-thanos"
        self.available = False
        self.retention_seconds = None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def init(self):
        if not self.url:
            r = self.kube.run("-n", self.prom_ns, "get", "route", "thanos-querier",
                              "-o", "jsonpath={.spec.host}", check=False)
            host = r.stdout.strip()
            if not host:
                warn(f"no thanos-querier route in {self.prom_ns}; pass --prom-url for a non-OpenShift Prometheus. "
                     "Datamover metrics will be absent.")
                return False
            self.url = f"https://{host}"
            self._mint_token()
        self.available = self._probe()
        self._retention()
        return self.available

    def _mint_token(self):
        r = self.kube.run("-n", self.prom_ns, "create", "token", "prometheus-k8s",
                          "--duration=6h", check=False)
        if r.returncode == 0 and r.stdout.strip():
            self.token = r.stdout.strip()
        else:
            warn(f"could not mint a token for prometheus-k8s in {self.prom_ns}: {r.stderr.strip()[:120]}")

    def _probe(self):
        try:
            v = self.query("count(container_cpu_usage_seconds_total{namespace=\"%s\"})" % self.kube.k10ns)
            n = int(float(v[0]["value"][1])) if v else 0
            if n == 0:
                warn(f"cluster monitoring reachable but has no cAdvisor series for {self.kube.k10ns}")
            return n > 0
        except Exception as e:  # noqa: BLE001
            warn(f"cluster monitoring not usable: {e}")
            return False

    def _retention(self):
        """Guide 00 section 6: declared retention, capped by replica uptime when the TSDB is
        an emptyDir. Used to mark snapshots that predate the metrics we still have."""
        cr = self.kube.get("prometheus", "k8s", ns=self.prom_ns)
        if not cr:
            return
        ret = (cr.get("spec") or {}).get("retention") or "15d"
        m = re.match(r"^(\d+)([dhw])$", ret)
        secs = {"d": 86400, "h": 3600, "w": 604800}
        ret_s = int(m.group(1)) * secs[m.group(2)] if m else 15 * 86400
        persistent = bool((cr.get("spec") or {}).get("storage"))
        if not persistent:
            pods = self.kube.get("pods", ns=self.prom_ns) or {"items": []}
            up = 0
            for p in pods["items"]:
                if (p["metadata"].get("labels") or {}).get("app.kubernetes.io/name") != "prometheus":
                    continue
                st = parse_rfc3339(p["status"].get("startTime"))
                if st:
                    up = max(up, time.time() - st.timestamp())
            ret_s = min(ret_s, up) if up else ret_s
        self.retention_seconds = int(ret_s)

    def _http(self, path, params, retry=True):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.url}{path}", data=data, method="POST")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if self.header:
            k, _, v = self.header.partition(":")
            req.add_header(k.strip(), v.strip())
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and retry and self.flavour == "openshift-thanos":
                self._mint_token()
                return self._http(path, params, retry=False)
            raise RuntimeError(f"HTTP {e.code} from {self.url}{path}")
        out = json.loads(body)
        if out.get("status") != "success":
            raise RuntimeError(out.get("error", "prometheus error"))
        return out["data"]["result"]

    def query(self, q):
        return self._http("/api/v1/query", {"query": q})

    def query_range(self, q, start, end, step="15s"):
        return self._http("/api/v1/query_range", {"query": q, "start": start, "end": end, "step": step})


_QUANTITY = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15,
             "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5}


def parse_quantity(q):
    """Kubernetes quantity -> float in base units (cores, bytes). '7500m' -> 7.5, '32Gi' -> bytes,
    '1091738596n' -> 1.09 cores, '446301077993' -> 446301077993."""
    if q is None:
        return None
    m = re.fullmatch(r"\s*([0-9.]+)\s*([a-zA-Z]*)\s*", str(q))
    if not m or m.group(2) not in _QUANTITY:
        return None
    return float(m.group(1)) * _QUANTITY[m.group(2)]


def collect_nodes(kube):
    """Capacity and allocatable from the Node objects; usage at this instant from the
    kubelet stats summary (/api/v1/nodes/<n>/proxy/stats/summary: CPU, working-set memory
    and the root filesystem that backs ephemeral storage, one call per node), falling back
    to the metrics API (/apis/metrics.k8s.io/v1beta1/nodes, CPU and memory only) when
    nodes/proxy is not permitted. A saturated node explains a slow datamover better than
    any repository figure, so this sits in the report header."""
    nodes = (kube.get("nodes") or {"items": []})["items"]
    out = {"sampledAt": iso(dt.datetime.now(dt.timezone.utc)), "items": [], "usageSources": [], "notes": []}
    metrics_api = None
    for n in nodes:
        md, st = n["metadata"], n.get("status") or {}
        lab = md.get("labels") or {}
        cap, alloc = st.get("capacity") or {}, st.get("allocatable") or {}
        item = {
            "name": md["name"],
            "roles": sorted(k.split("/", 1)[1] for k in lab if k.startswith("node-role.kubernetes.io/")),
            "instanceType": lab.get("node.kubernetes.io/instance-type"),
            "kubeletVersion": (st.get("nodeInfo") or {}).get("kubeletVersion"),
            "pressure": sorted(c["type"] for c in st.get("conditions") or [] if c.get("type", "").endswith("Pressure") and c.get("status") == "True"),
            "unschedulable": bool((n.get("spec") or {}).get("unschedulable")),
            "cpu": {"capacityCores": parse_quantity(cap.get("cpu")), "allocatableCores": parse_quantity(alloc.get("cpu"))},
            "memory": {"capacityBytes": parse_quantity(cap.get("memory")), "allocatableBytes": parse_quantity(alloc.get("memory"))},
            "ephemeralStorage": {"capacityBytes": parse_quantity(cap.get("ephemeral-storage")),
                                 "allocatableBytes": parse_quantity(alloc.get("ephemeral-storage"))},
            "podCapacity": parse_quantity(cap.get("pods")),
            "usageSource": None,
        }
        r = kube.run("get", "--raw", f"/api/v1/nodes/{md['name']}/proxy/stats/summary", check=False, timeout=60)
        summ = None
        if r.returncode == 0 and r.stdout.strip():
            try:
                summ = (json.loads(r.stdout).get("node") or {})
            except ValueError:
                summ = None
        if summ:
            item["usageSource"] = "kubelet stats/summary"
            cpu, mem, fs, ifs = summ.get("cpu") or {}, summ.get("memory") or {}, summ.get("fs") or {}, (summ.get("runtime") or {}).get("imageFs") or {}
            if cpu.get("usageNanoCores") is not None:
                item["cpu"]["usedCores"] = round(cpu["usageNanoCores"] / 1e9, 3)
            item["memory"]["workingSetBytes"] = mem.get("workingSetBytes")
            item["memory"]["availableBytes"] = mem.get("availableBytes")
            item["ephemeralStorage"].update({"usedBytes": fs.get("usedBytes"), "availableBytes": fs.get("availableBytes"),
                                             "fsCapacityBytes": fs.get("capacityBytes"), "imageFsUsedBytes": ifs.get("usedBytes"),
                                             "inodesUsed": fs.get("inodesUsed"), "inodesFree": fs.get("inodesFree")})
        else:
            if metrics_api is None:
                m = kube.run("get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes", check=False, timeout=60)
                try:
                    metrics_api = {x["metadata"]["name"]: x for x in json.loads(m.stdout).get("items", [])} if m.returncode == 0 else {}
                except ValueError:
                    metrics_api = {}
                if not metrics_api:
                    out["notes"].append("neither nodes/proxy stats/summary nor metrics.k8s.io is available: node usage not collected")
            u = (metrics_api.get(md["name"]) or {}).get("usage") or {}
            if u:
                item["usageSource"] = "metrics.k8s.io"
                item["cpu"]["usedCores"] = round(parse_quantity(u.get("cpu")) or 0, 3)
                item["memory"]["workingSetBytes"] = parse_quantity(u.get("memory"))
                out["notes"].append(f"{md['name']}: ephemeral storage usage needs nodes/proxy (stats/summary); not collected") \
                    if not any(md["name"] in x for x in out["notes"]) else None
        c, mm, e = item["cpu"], item["memory"], item["ephemeralStorage"]
        if c.get("usedCores") is not None and c.get("allocatableCores"):
            c["usedPctOfAllocatable"] = round(100 * c["usedCores"] / c["allocatableCores"], 1)
        if mm.get("workingSetBytes") is not None and mm.get("allocatableBytes"):
            mm["usedPctOfAllocatable"] = round(100 * mm["workingSetBytes"] / mm["allocatableBytes"], 1)
        if e.get("usedBytes") is not None and (e.get("fsCapacityBytes") or e.get("capacityBytes")):
            e["usedPct"] = round(100 * e["usedBytes"] / (e.get("fsCapacityBytes") or e["capacityBytes"]), 1)
        if item["usageSource"] and item["usageSource"] not in out["usageSources"]:
            out["usageSources"].append(item["usageSource"])
        out["items"].append(item)
    out["items"].sort(key=lambda x: (0 if "worker" in x["roles"] else 1, x["name"]))
    tot = {"nodes": len(out["items"]),
           "cpuAllocatableCores": sum(x["cpu"].get("allocatableCores") or 0 for x in out["items"]),
           "cpuUsedCores": sum(x["cpu"].get("usedCores") or 0 for x in out["items"]),
           "memoryAllocatableBytes": sum(x["memory"].get("allocatableBytes") or 0 for x in out["items"]),
           "memoryWorkingSetBytes": sum(x["memory"].get("workingSetBytes") or 0 for x in out["items"]),
           "ephemeralCapacityBytes": sum(x["ephemeralStorage"].get("fsCapacityBytes") or x["ephemeralStorage"].get("capacityBytes") or 0 for x in out["items"]),
           "ephemeralUsedBytes": sum(x["ephemeralStorage"].get("usedBytes") or 0 for x in out["items"])}
    out["totals"] = tot
    return out


def ksm_pod_labels_exposed(prom, k10ns):
    """Does kube-state-metrics expose pod labels (metric-labels-allowlist) for the K10
    namespace? Any kube_pod_labels series there carrying a label_* label means yes. None
    when the query itself fails. Without it, datamover usage cannot be attributed to a
    namespace or policy (README, "Datamover CPU/memory")."""
    try:
        res = prom.query('kube_pod_labels{namespace="%s"}' % k10ns)
    except Exception:  # noqa: BLE001
        return None
    if not res:
        return None
    return any(k.startswith("label_") for r in res for k in (r.get("metric") or {}))


def datamover_metrics(prom, k10ns, pod_regex, start, end, namespace=None):
    """Datamover memory peak (of the sum) and CPU-seconds in [start, end], per-pod figures
    attributed to a namespace through kube_pod_labels (guide 09).

    With `namespace`, the headline figures cover only the pods working for that namespace
    (plus pods that could not be attributed, flagged); pods of other namespaces alive in
    the same window are listed as concurrent load with their own peak, because a policy
    exporting three namespaces at once otherwise shows the same six pods under each.
    Not per PVC: copy-vol-data pods mount an ephemeral clone, not the source. The join on
    kube_pod_labels gives namespace and job, which is what OpenShift's KSM exposes thanks
    to --metric-labels-allowlist=pods=[*]."""
    if not (prom and prom.available):
        return None
    s = start - METRICS_PAD_SECONDS
    e = end + METRICS_PAD_SECONDS
    sel = 'namespace="%s",pod=~"%s",container!="",container!="POD"' % (k10ns, pod_regex)
    out = {"windowStart": iso(dt.datetime.fromtimestamp(s, dt.timezone.utc)),
           "windowEnd": iso(dt.datetime.fromtimestamp(e, dt.timezone.utc)),
           "samples": 0, "pods": []}
    try:
        mem = prom.query_range("sum by (pod) (container_memory_working_set_bytes{%s})" % sel, s, e)
        cpu = prom.query_range("sum by (pod) (container_cpu_usage_seconds_total{%s})" % sel, s, e)
        labels = prom.query_range('kube_pod_labels{namespace="%s",pod=~"%s"}' % (k10ns, pod_regex), s, e)
    except Exception as ex:  # noqa: BLE001
        out["error"] = str(ex)
        return out

    # attribution: job_id -> app namespace, from the data-mover-svc pods that carry app-name
    job_ns = {}
    pod_labels = {}
    for r in labels:
        m = r["metric"]
        pod_labels[m["pod"]] = m
        if m.get("label_k10_kasten_io_job_id") and m.get("label_app_name"):
            job_ns.setdefault(m["label_k10_kasten_io_job_id"], m["label_app_name"])

    # memory: per-pod series (to sum over any subset of pods) and per-pod peak
    series, pods = {}, {}
    for r in mem:
        pod = r["metric"]["pod"]
        vals = [(float(t), float(v)) for t, v in r["values"]]
        if not vals:
            continue
        pods.setdefault(pod, {})["peakMemoryBytes"] = int(max(v for _, v in vals))
        series[pod] = vals
    for r in cpu:
        pod = r["metric"]["pod"]
        vals = [float(v) for _, v in r["values"]]
        if len(vals) >= 2:
            pods.setdefault(pod, {})["cpuSeconds"] = round(max(vals) - min(vals), 3)
        elif vals:
            pods.setdefault(pod, {})["cpuSeconds"] = 0.0

    def attribution(pod):
        lab = pod_labels.get(pod, {})
        return lab.get("label_app_name") or job_ns.get(lab.get("label_k10_kasten_io_job_id"))

    def peak_of_sum(pod_names):
        sum_at = {}
        for pod in pod_names:
            for t, v in series.get(pod, []):
                sum_at[t] = sum_at.get(t, 0.0) + v
        return (int(max(sum_at.values())) if sum_at else None), len(sum_at)

    live = [pod for pod, p in pods.items() if p.get("peakMemoryBytes") or p.get("cpuSeconds")]
    if namespace:
        own = [pod for pod in live if attribution(pod) in (namespace, None)]
        others = [pod for pod in live if attribution(pod) not in (namespace, None)]
    else:
        own, others = live, []

    peak, nsamples = peak_of_sum(own)
    out["samples"] = nsamples
    out["peakSumMemoryBytes"] = peak
    # CPU: total seconds across the namespace's pods, and the average rate over the window
    total_cpu = sum(pods[pod].get("cpuSeconds", 0.0) for pod in own)
    out["cpuSecondsTotal"] = round(total_cpu, 3)
    out["avgCpuCores"] = round(total_cpu / max(e - s, 1), 4)
    out["scope"] = (f"datamover pods attributed to namespace {namespace} (plus unattributed ones)" if namespace
                    else "every datamover pod alive in the window")
    if namespace and any(attribution(pod) is None for pod in own):
        out["unattributedIncluded"] = sorted(pod for pod in own if attribution(pod) is None)
    if others:
        # what else was running: contention, and the cluster-wide load in this window
        all_peak, _ = peak_of_sum(live)
        out["concurrent"] = {"pods": [{"pod": pod, "appNamespace": attribution(pod)} for pod in sorted(others)],
                             "namespaces": sorted({attribution(pod) for pod in others}),
                             "allDatamoversPeakSumMemoryBytes": all_peak,
                             "allDatamoversCpuSecondsTotal": round(sum(pods[pod].get("cpuSeconds", 0.0) for pod in live), 3)}
    out["podsWithoutSamples"] = []
    for pod, p in sorted(pods.items()):
        lab = pod_labels.get(pod, {})
        # a pod that only ever shows a 0 working set lived less than one kubelet scrape
        # interval; listing it as a "peak of 0" would be misleading (guide 09)
        if pod not in live:
            out["podsWithoutSamples"].append(pod)
            continue
        out["pods"].append({
            "pod": pod,
            "appNamespace": attribution(pod),
            "policy": lab.get("label_policy_name"),
            "jobId": lab.get("label_k10_kasten_io_job_id"),
            "own": pod in own,
            "peakMemoryBytes": p.get("peakMemoryBytes"),
            "cpuSeconds": p.get("cpuSeconds"),
        })
    if not live:
        out["note"] = ("no cAdvisor samples in this window: the datamover pod lived less than one "
                       "scrape interval, or the window predates the metrics retention")
    return out


def k10tools_cause_chain(text):
    """k10tools prints `Error: {"message":...,"cause":{...}}`; causes nest, and any level may be
    a JSON document encoded as a string or a message that is itself JSON. Return the flat
    list of messages, outermost first."""
    m = re.search(r"^Error: (\{.*)$", ANSI_RE.sub("", text), re.M)
    if not m:
        return []
    out = []

    def walk(o, depth=0):
        if depth > 12:
            return
        if isinstance(o, str):
            st = o.strip()
            if st.startswith("{"):
                try:
                    walk(json.loads(st), depth + 1)
                    return
                except ValueError:
                    pass
            out.append(st)
            return
        if isinstance(o, dict):
            if "message" in o:
                walk(o["message"], depth + 1)
            if "cause" in o:
                walk(o["cause"], depth + 1)
    walk(m.group(1))
    return out


def enrich_orphans(kube, orphans, profiles, cluster_uid=None):
    """repo_checker only names a repository it cannot inventory. The StorageRepository CR
    knows the rest: owning namespace, profile label, content type and the object-store
    location. "failed to find a profile with given location information" does not always
    mean the profile is gone - on a lab cluster 4 of 5 had a live profile whose path prefix
    k10tools could not match to the repository path - so say which case it is."""
    crs = (kube.get("storagerepositories.repositories.kio.kasten.io", ns=kube.k10ns) or {"items": []})["items"]
    by_name = {c["metadata"]["name"]: c for c in crs}
    for o in orphans:
        cr = by_name.get(o["repository"])
        if not cr:
            o["note"] = "no StorageRepository CR with this name in the K10 namespace"
            continue
        lab = cr["metadata"].get("labels") or {}
        st = cr.get("status") or {}
        store = ((st.get("location") or {}).get("objectStore") or {})
        prof = lab.get("k10.kasten.io/exportProfile")
        o.update({"namespace": lab.get("k10.kasten.io/appName"), "profile": prof,
                  "profileExistsOnCluster": prof in profiles if prof else None,
                  "contentType": st.get("contentType"), "backendType": st.get("backendType"),
                  "objectStoreType": store.get("objectStoreType") or o.get("objectStoreType"),
                  "bucket": store.get("name") or o.get("bucket"), "path": store.get("path") or o.get("path"),
                  "region": store.get("region") or o.get("region"), "fileStore": (st.get("location") or {}).get("fileStore")})
        m = re.search(r"k10/([0-9a-f-]{36})/", o.get("path") or "")
        foreign = m.group(1) if (m and cluster_uid and m.group(1) != cluster_uid) else None
        if foreign:
            o["clusterUidInPath"] = foreign
        chain = o.get("k10toolsError") or []
        # innermost cause, with its parent when it is too terse to stand alone ("no profile")
        said = chain[-1] if chain else None
        if said and len(said) < 30 and len(chain) > 1:
            said = f"{chain[-2]}: {said}"
        facts = []
        if foreign:
            facts.append(f"written by another K10 installation (cluster UID {foreign} in the path)")
        if prof and prof in profiles:
            facts.append(f"profile {prof} still exists")
        elif prof:
            facts.append(f"profile {prof} no longer exists")
        else:
            facts.append("no profile label on the StorageRepository")
        o["reason"] = (f"k10tools: {said}" if said else "repo_checker cannot inventory it") + " — " + "; ".join(facts)
    return orphans


def exported_history(kube):
    """What HAS been exported, from two cluster-scoped K10 APIs that survive the deletion of
    the policy, the namespace and the application.

    `policy_targets` answers the forward question - what a live export policy selects - and
    is blind to data whose policy or namespace is gone. That data is still on the object
    store and still costing money, so it has to be discovered from history instead:

      restorepointcontents   cluster-scoped, one per restore point, carrying the
                             appNamespace / policyName / exportProfile labels. The
                             exportProfile label is what separates an EXPORT from a
                             backup-only snapshot (54 of 101 had no export on the reference
                             cluster). Its /details subresource additionally holds the PVC,
                             the Kopia snapshot id, the sizes and the Kubernetes manifests -
                             not needed for discovery, but the only way to describe a
                             repository whose PROFILE is gone and which therefore cannot be
                             opened at all.
      storagerepositories    one per Kopia repository, with the owning namespace
                             (k10.kasten.io/appName), the profile and status.location.

    Returns (triples, repos) where triples maps (namespace, policy, profile) to the restore
    points seen for it, and repos maps (namespace, profile) to the StorageRepository facts."""
    rpcs = (kube.get("restorepointcontents.apps.kio.kasten.io") or {"items": []})["items"]
    crs = (kube.get("storagerepositories.repositories.kio.kasten.io", ns=kube.k10ns) or {"items": []})["items"]

    triples = {}
    for r in rpcs:
        lab = r["metadata"].get("labels") or {}
        prof = lab.get("k10.kasten.io/exportProfile")
        ns = lab.get("k10.kasten.io/appNamespace") or lab.get("k10.kasten.io/appName")
        if not prof or not ns:
            continue  # backup-only restore point: no export, nothing on an object store
        key = (ns, lab.get("k10.kasten.io/policyName") or "-", prof)
        t = triples.setdefault(key, {"restorePoints": 0, "oldest": None, "newest": None, "names": []})
        t["restorePoints"] += 1
        ts = r["metadata"].get("creationTimestamp")
        if ts:
            t["oldest"] = min(t["oldest"] or ts, ts)
            t["newest"] = max(t["newest"] or ts, ts)
        if len(t["names"]) < 200:
            t["names"].append(r["metadata"]["name"])

    repos = {}
    for c in crs:
        lab = c["metadata"].get("labels") or {}
        st = c.get("status") or {}
        ns, prof = lab.get("k10.kasten.io/appName"), lab.get("k10.kasten.io/exportProfile")
        if not ns or not prof:
            continue
        store = ((st.get("location") or {}).get("objectStore") or {})
        repos[(ns, prof)] = {"repository": c["metadata"]["name"], "contentType": st.get("contentType"),
                             "bucket": store.get("name"), "path": store.get("path"),
                             "objectStoreType": store.get("objectStoreType"), "region": store.get("region"),
                             "fileStore": (st.get("location") or {}).get("fileStore")}
    return triples, repos


def restore_points_from_details(kube, names, max_n=40):
    """Describe an unopenable export from restorepointcontents/<name>/details alone.

    `repo_checker -o connect -a <namespace>` resolves the repository from the LIVE namespace
    UID, so once the namespace is deleted the repository cannot be opened at all - measured:
    "Failed to get application namespace UID -> namespaces \"large-test\" not found". A gone
    profile blocks it for a different reason (no credentials). In both cases the /details
    subresource is the only remaining description of the data, and it is a good one: per
    restore point and per PVC it carries the PVC name, its storage class, the Kopia snapshot
    id, the file count, the logical and physical sizes and the upload time.

    Returns per-PVC rows shaped like the Kopia-derived ones so the report reads the same."""
    by_pvc = {}
    fetched = errors = 0
    for n in names[:max_n]:
        r = kube.run("get", "--raw",
                     f"/apis/apps.kio.kasten.io/v1alpha1/restorepointcontents/{n}/details",
                     check=False, timeout=120)
        if r.returncode != 0 or not r.stdout.strip():
            errors += 1
            continue
        try:
            d = json.loads(r.stdout)
        except ValueError:
            errors += 1
            continue
        fetched += 1
        for a in (((d.get("status") or {}).get("restorePointContentDetails") or {}).get("artifacts") or []):
            kan = ((a.get("meta") or {}).get("kanister") or {})
            vol = ((kan.get("meta") or {}).get("k8sVolume") or {})
            pvc = vol.get("pvcName")
            if not pvc:
                continue
            vals = {v.get("key"): v.get("value") for v in (kan.get("values") or [])}
            usage = (kan.get("meta") or {}).get("storageUsage") or {}
            e = by_pvc.setdefault(pvc, {"name": pvc, "storageClass": vol.get("storageClassName"),
                                        "existsOnCluster": False, "snapshots": [],
                                        "source": "restorepointcontent details"})
            def _int(x):
                try:
                    return int(x)
                except (TypeError, ValueError):
                    return None
            e["snapshots"].append({
                "restorePointContent": n,
                "endTime": (kan.get("meta") or {}).get("uploadEndTime"),
                "kopiaSnapshotId": vals.get("backupIdentifier"),
                "objectStorePath": vals.get("objectStorePath"),
                "fileCount": _int(vals.get("fileCount")),
                "totalSizeBytes": usage.get("logical"),
                "physicalBytes": usage.get("physical"),
                "sizeText": vals.get("size"), "physicalText": vals.get("phySize")})
    out = []
    for e in by_pvc.values():
        e["snapshots"].sort(key=lambda x: x.get("endTime") or "")
        last = e["snapshots"][-1] if e["snapshots"] else {}
        e.update({"snapshotCount": len(e["snapshots"]), "fileCount": last.get("fileCount"),
                  "totalSizeBytes": last.get("totalSizeBytes"),
                  "lastSnapshotTime": last.get("endTime"),
                  "averageFileSizeBytes": (int(last["totalSizeBytes"] / last["fileCount"])
                                           if last.get("fileCount") and last.get("totalSizeBytes") else None),
                  "mode": None, "sizeHistogram": {"skipped": "repository cannot be opened; "
                                                             "described from restore point details"}})
        out.append(e)
    out.sort(key=lambda x: x["name"])
    return out, {"restorePointsRead": fetched, "restorePointsFailed": errors,
                 "restorePointsAvailable": len(names)}


# --------------------------------------------------------------------------- repo_checker

class ConnectError(RuntimeError):
    """repo_checker -o connect reported a failure (cause chain in the message)."""


class ImagePullError(SystemExit):
    """A pod repo_checker or k10tools created cannot pull its image. Fatal for the whole run:
    every later call would fail the same way, and repo_checker itself would wait forever
    ("Waiting for K10 tools pod to be Running" is a `while true`)."""


PULL_FAILURE_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ImageInspectError", "ErrImageNeverPull"}


def k10_image_registry(k10cfg):
    """Registry/prefix K10 itself pulls from, e.g. 'registry.connect.redhat.com/kasten' from
    KanisterToolsImage=registry.connect.redhat.com/kasten/kanister-tools@sha256:... - the
    natural value for repo_checker -i on an air-gapped cluster."""
    img = (k10cfg or {}).get("KanisterToolsImage") or ""
    if "/" not in img:
        return None
    return img.rsplit("/", 1)[0]


class RepoChecker:
    def __init__(self, kube, path, version, workdir, keep_pods=False, image_registry=None, image_tag=None):
        self.kube = kube
        self.workdir = workdir
        self.keep_pods = keep_pods
        # repo_checker defaults to gcr.io/kasten-images and to the newest kasten/k10 chart in
        # the LOCAL helm repo - on a 9.0.1 cluster it happily runs k10tools:9.0.5. Pin the tag
        # to the cluster's version; the registry stays gcr.io unless asked (air gap).
        self.image_registry = image_registry
        self.image_tag = image_tag or version
        self.created_pods = []
        self.deleting_pods = []
        self.path = path or self._download(version)

    def _download(self, version):
        dst = os.path.join(self.workdir, "k10_repo_checker.sh")
        url = REPO_CHECKER_URL.format(version=version)
        log(f"downloading {url}")
        try:
            urllib.request.urlretrieve(url, dst)
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"cannot download repo_checker for K10 {version}: {e}. Use --repo-checker PATH.")
        os.chmod(dst, 0o755)
        return dst

    def _pull_failure(self):
        """(pod, image, reason, message) for the first k10tools-*/debug-kopia-* pod stuck on an
        image pull in the K10 namespace, else None."""
        pods = self.kube.get("pods", ns=self.kube.k10ns) or {"items": []}
        for p in pods.get("items", []):
            name = p["metadata"]["name"]
            if not (name.startswith("k10tools-") or name.startswith("debug-kopia-")):
                continue
            for cs in (p.get("status") or {}).get("containerStatuses") or []:
                w = (cs.get("state") or {}).get("waiting") or {}
                if w.get("reason") in PULL_FAILURE_REASONS:
                    return name, cs.get("image"), w["reason"], (w.get("message") or "").strip()
        return None

    def _abort_on_pull_failure(self, proc, hit):
        pod, image, reason, message = hit
        proc.kill()
        # wait: the message below promises the pod is gone, and a user checks right away
        self.kube.run("-n", self.kube.k10ns, "delete", "pod", pod, "--grace-period=1", "--wait=true", "--timeout=60s", check=False)
        which = "k10tools" if pod.startswith("k10tools-") else "datamover (debug-kopia)"
        detected = getattr(self, "detected_registry", None)
        if detected and self.image_registry == detected:
            hint = (f" This is the registry K10 itself pulls from, so k10tools:{self.image_tag} is probably not mirrored there: "
                    f"mirror it (and datamover, kanister-tools), or pass --image-registry gcr.io/kasten-images if this cluster can reach gcr.io.")
        elif detected:
            hint = f" K10 itself pulls from {detected}: try --image-registry {detected}."
        else:
            hint = ""
        raise ImagePullError(
            f"\nAUDIT ABORTED: the {which} pod {pod} in {self.kube.k10ns} cannot pull its image\n"
            f"  image : {image}\n  reason: {reason}" + (f" - {message[:300]}" if message else "") + "\n"
            f"  fix   : make k10tools, datamover and kanister-tools at tag {self.image_tag} pullable from this cluster "
            f"(mirror them into a registry it can reach, with the pull secret K10 uses), then re-run with "
            f"--image-registry <registry/prefix>.{hint}"
            f" If docs.kasten.io is unreachable too, download k10_repo_checker.sh elsewhere and pass --repo-checker PATH.\n"
            f"  the pod was deleted; no other resource was created.")

    def _run(self, *args, timeout=1800):
        env = dict(os.environ)
        if self.kube.context:
            # repo_checker calls bare kubectl; honour the requested context without a kubeconfig edit
            r = self.kube.run("config", "view", "--minify", "--flatten", "--raw", check=False)
            if r.returncode == 0:
                kc = os.path.join(self.workdir, "kubeconfig")
                with open(kc, "w") as f:
                    f.write(r.stdout)
                os.chmod(kc, 0o600)
                env["KUBECONFIG"] = kc
        # repo_checker prints k10tools progress (catalog scan, repository connects) as coloured
        # log lines; stream them so a multi-minute run is visibly alive. Quiet by default:
        # a heartbeat every 30 s with the last line seen; --verbose echoes every line.
        # repo_checker writes its pod manifest (repo-checker.yaml) to the current directory and
        # deletes it afterwards; run it in this run's private workdir so two generators on one
        # machine (or a stray file in the caller's directory) cannot interfere
        extra = []
        if self.image_tag:
            extra += ["-t", self.image_tag]
        if self.image_registry:
            extra += ["-i", self.image_registry]
        proc = subprocess.Popen([os.path.abspath(self.path), *args, *extra], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=self.workdir)
        lines, last = [], {"line": "", "seen": time.monotonic(), "printed": time.monotonic()}

        def reader():
            for line in proc.stdout:
                lines.append(line)
                clean = ANSI_RE.sub("", line).rstrip()
                if clean and not clean.startswith(("{", "}", '"', " ")):
                    last["line"], last["seen"] = clean, time.monotonic()
                    if VERBOSE:
                        log(f"    repo_checker> {clean[:160]}")

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        t0 = time.monotonic()
        last_pull_check = t0
        while th.is_alive():
            th.join(1.0)
            if time.monotonic() - t0 > timeout:
                proc.kill()
                raise RuntimeError(f"repo_checker {' '.join(args)} exceeded {timeout}s")
            if time.monotonic() - last_pull_check >= 10:
                last_pull_check = time.monotonic()
                hit = self._pull_failure()
                if hit:
                    self._abort_on_pull_failure(proc, hit)
            if not VERBOSE and time.monotonic() - last["printed"] >= 30:
                last["printed"] = time.monotonic()
                log(f"    repo_checker running for {int(time.monotonic() - t0)}s - last: {last['line'][:120] or '(no output yet)'}")
        proc.wait()
        return "".join(lines)

    @staticmethod
    def _extract_json(out):
        # the JSON is wrapped in coloured progress lines (guide 12 step 1)
        lines = out.splitlines()
        try:
            a = next(i for i, l in enumerate(lines) if l.strip() == "{")
            b = max(i for i, l in enumerate(lines) if l.strip() == "}")
        except (StopIteration, ValueError):
            return None
        try:
            return json.loads("\n".join(lines[a:b + 1]))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _orphan_from_error(out):
        """k10tools aborts the whole inventory on the first repository whose Location Profile
        no longer exists ("failed to find a profile with given location information").
        Pull the repository name and its object-store location out of the error, because
        that repository is itself a finding: data on a store that nothing references."""
        flat = out.replace('\\"', '"')
        m = re.search(r"failed to list snapshots for repository ([A-Za-z0-9-]+)", flat)
        if not m:
            return None
        o = {"repository": m.group(1), "reason": "repo_checker cannot inventory this repository",
             "k10toolsError": k10tools_cause_chain(out)}
        loc = re.search(r'"objectStore":\{([^}]*)\}', flat)
        if loc:
            for k in ("name", "objectStoreType", "path", "region"):
                mm = re.search(r'"%s":"([^"]*)"' % k, loc.group(1))
                if mm:
                    o[{"name": "bucket"}.get(k, k)] = mm.group(1)
        return o

    def inventory(self, profile_names, full=True, repos_by_profile=None, per_repository=False):
        """Full inventory when it works; otherwise one inventory per existing profile,
        merged. Repositories k10tools cannot open are returned separately as orphans.

        The per-profile fallback is partial, not a fix: k10tools aborts the whole inventory
        on the first repository it cannot open, and `-p <profile>` aborts the same way when
        that repository is on that profile. Measured on the reference cluster with a deleted
        policy: full, `-p`, and `-R <the broken repository>` all exit 1; only
        `-R <a healthy repository>` returns JSON, which is the escalation below.

        Scope therefore never depends on this: it comes from the export policies and from
        the exported history (exported_history). A focused run (--namespace/--policy) passes
        only the profiles it needs and full=False: every inventory re-scans the whole
        catalog, so eight of them cost nine minutes for a single pair on a lab cluster."""
        orphans = []
        if full:
            out = self._run("-r", "inventory", "-F", "json", "-n", self.kube.k10ns)
            js = self._extract_json(out)
            if js is not None:
                return js, []
            o = self._orphan_from_error(out)
            if o:
                orphans.append(o)
                warn(f"repository {o['repository']} cannot be opened by k10tools, which aborts the "
                     f"WHOLE inventory on it (buildAllInventories has no continue-on-error) - "
                     f"falling back to one inventory per profile. Note this only helps when the "
                     f"broken repository is on a DIFFERENT profile: measured on the reference "
                     f"cluster, -p on the same profile aborts identically and only "
                     f"-R <healthy repository> gets through. The pair is still reported from the "
                     f"exported history. See orphanedRepositories for the cause.")
            else:
                warn("full inventory produced no JSON; falling back to one inventory per profile. Tail:\n" + out[-800:])
        merged = {"repositories": [], "timestamp": None}
        for prof in profile_names:
            log(f"  inventory for profile {prof} ...")
            out = self._run("-r", "inventory", "-F", "json", "-p", prof, "-n", self.kube.k10ns)
            js = self._extract_json(out)
            if js is None:
                if "No storage repositories found" in out:
                    continue
                o = self._orphan_from_error(out)
                if o and o not in orphans:
                    orphans.append(o)
                bad = o["repository"] if o else None
                warn(f"inventory for profile {prof} produced no JSON" +
                     (f" - k10tools cannot open repository {bad} and aborts the profile "
                      f"with it" if bad else ""))
                # Escalate to one inventory per repository. `-R` is the only filter that
                # survives a broken repository: measured on the reference cluster, full and
                # `-p` both exit 1 while `-R <a healthy repository>` returns JSON. Costs one
                # catalog re-scan each, so it is a last resort, not the default.
                names = [n for n in (repos_by_profile or {}).get(prof, []) if n != bad]
                if not names:
                    continue
                if not per_repository:
                    # Each one re-scans the whole catalog (~45 s on the reference cluster), and
                    # all it adds is the orphan/dangling/not-synced counts - the repository
                    # names and locations are already in the StorageRepository CRs, and the
                    # scope does not depend on any of it. Opt in with --inventory-per-repository.
                    log(f"  skipping the per-repository fallback for {prof}: {len(names)} "
                        f"repositories x one catalog re-scan each. It would only add orphan and "
                        f"dangling counts; pass --inventory-per-repository to collect them.")
                    continue
                log(f"  escalating to {len(names)} per-repository inventories on {prof} ...")
                got = 0
                for name in sorted(names):
                    out_r = self._run("-r", "inventory", "-F", "json", "-p", prof,
                                      "-R", name, "-n", self.kube.k10ns)
                    js_r = self._extract_json(out_r)
                    if js_r is None:
                        o_r = self._orphan_from_error(out_r)
                        if o_r and o_r not in orphans:
                            orphans.append(o_r)
                        continue
                    merged["repositories"].extend(js_r.get("repositories") or [])
                    got += 1
                log(f"  {got}/{len(names)} repositories on {prof} inventoried individually")
                continue
            merged["repositories"].extend(js.get("repositories") or [])
            merged["timestamp"] = js.get("timestamp") or merged["timestamp"]
        if not merged["repositories"] and not orphans:
            raise SystemExit("repo_checker produced no inventory for any profile")
        return merged, orphans

    def _debug_pods(self):
        r = self.kube.run("-n", self.kube.k10ns, "get", "pods", "-o", "name", check=False)
        return {l.split("/", 1)[1] for l in r.stdout.split() if "debug-kopia" in l}

    @staticmethod
    def _connect_cause(out):
        """k10tools reports connect failures as a JSON status block whose StatusMessage embeds
        an escaped error chain. Return the chain's messages, innermost last."""
        m = re.search(r'"StatusMessage":\s*"(.*?)"\s*\n', out, re.S)
        if not m:
            return []
        try:
            inner = m.group(1).encode().decode("unicode_escape")
            j = inner[inner.find("{"): inner.rfind("}") + 1]
            d = json.loads(j)
            chain = []
            while isinstance(d, dict):
                if d.get("message"):
                    chain.append(d["message"])
                d = d.get("cause")
            return chain
        except Exception:  # noqa: BLE001
            return [m.group(1)[:200]]

    def connect(self, application, profile):
        """Leave a debug-kopia pod connected read-only to the application repository.
        Returns the pod name. repo_checker does not clean it up; we do - including the pod
        it leaves behind when the connect itself fails."""
        before = self._debug_pods()
        out = self._run("-r", "application", "-o", "connect", "-a", application, "-p", profile,
                        "-n", self.kube.k10ns)
        m = re.search(r"Pod Name:\s*(debug-kopia-[a-z0-9]+)", out)
        if not m or '"StatusCode": "Error"' in out:
            for pod in self._debug_pods() - before:
                self.kube.run("-n", self.kube.k10ns, "delete", "pod", pod, "--wait=false", check=False)
            chain = self._connect_cause(out)
            if not chain:
                tail = [ANSI_RE.sub("", l).strip() for l in out.splitlines()]
                tail = [l for l in tail if l and not l.startswith(("Profile:", "Application Name:", "Operation:",
                                                                   "K10 Namespace:", "Repo type:"))]
                chain = tail[-2:]
            raise ConnectError(" -> ".join(chain))
        pod = m.group(1)
        self.created_pods.append(pod)
        self.kube.run("-n", self.kube.k10ns, "wait", "--for=condition=Ready", f"pod/{pod}",
                      "--timeout=180s", check=False)
        return pod

    def cleanup(self, wait=False):
        """Delete the debug pods this run created. Between namespaces the deletion is
        asynchronous; the final call (wait=True) waits for every pod deleted during the run
        to be gone, so "no debug pod left" holds the moment the script exits and a user
        checking right away does not see a Terminating pod."""
        if self.keep_pods:
            if self.created_pods:
                log(f"keeping debug pods: {', '.join(self.created_pods)}")
            return
        for pod in self.created_pods:
            self.kube.run("-n", self.kube.k10ns, "delete", "pod", pod, "--grace-period=5", "--wait=false", check=False)
            self.deleting_pods.append(pod)
        self.created_pods = []
        if wait and self.deleting_pods:
            self.kube.run("-n", self.kube.k10ns, "wait", "--for=delete", "--timeout=90s",
                          *[f"pod/{p}" for p in self.deleting_pods], check=False)
            self.deleting_pods = []


KOPIA_PREFIX = "export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config; "


def kopia_json(kube, pod, cmd, retries=1):
    """Run a kopia command with --json in the debug pod. kopia writes log-directory noise to
    stderr, so only stdout is parsed; the pod has no jq, parsing happens here. Once on a
    lab cluster a command returned something that was not clean JSON (a warning line ahead of
    the document) and the whole namespace was lost: tolerate leading/trailing noise and
    retry once before giving up, and keep the offending text in the error."""
    last = None
    for attempt in range(retries + 1):
        out, err, rc = kube.exec(kube.k10ns, pod, KOPIA_PREFIX + cmd + " --json 2>/dev/null\n")
        if not out.strip():
            last = RuntimeError(f"kopia {cmd}: no output (rc={rc}) {err.strip()[:200]}")
            continue
        try:
            return json.loads(out)
        except ValueError as e:
            # take the outermost JSON document if noise surrounds it
            starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
            ends = [i for i in (out.rfind("]"), out.rfind("}")) if i >= 0]
            if starts and ends:
                try:
                    return json.loads(out[min(starts):max(ends) + 1])
                except ValueError:
                    pass
            last = RuntimeError(f"kopia {cmd}: output is not JSON ({e}); starts with: {out.strip()[:160]!r}")
    raise last


def last_content_rewrite(maint):
    """Epoch of the last successful full-rewrite-contents that actually rewrote something.
    Maintenance re-stamps every rewritten content and pack blob with the maintenance time,
    so per-snapshot attribution by timestamp is impossible for anything older than that.
    Validated: a 3-day-old repository had all its contents dated at the maintenance run."""
    runs = ((maint.get("schedule") or {}).get("runs") or {}).get("full-rewrite-contents") or []
    last = None
    for r in runs:
        if not r.get("success"):
            continue
        n = 0
        for x in r.get("extra") or []:
            n = max(n, ((x.get("data") or {}).get("rewrittenContentCount") or 0))
        if n > 0:
            t = parse_rfc3339(r.get("start"))
            if t and (last is None or epoch(t) > last):
                last = epoch(t)
    return last


def assign_contents(contents, snaps, pad=1):
    """Give every live content block to exactly ONE snapshot: the one whose padded window
    contains its timestamp and whose midpoint is nearest. Content timestamps are whole
    seconds, and consecutive PVC snapshots of one export run within the same second, so
    naive per-window sums double count - the five mastodon snapshots summed to 2.3x the
    repository. Returns {snapshot id: {"physical", "logical", "count"}} plus
    {"_unassigned": ...} for contents matching no window."""
    wins = []
    for sn in snaps:
        st, en = parse_rfc3339(sn["startTime"]), parse_rfc3339(sn["endTime"])
        if st and en:
            wins.append((epoch(st) - pad, epoch(en) + pad, (epoch(st) + epoch(en)) / 2, sn.get("id")))
    out = {}
    for c in contents:
        if c.get("deleted"):
            continue
        t = c.get("time")
        if t is None:
            continue
        best, hits = None, 0
        for lo, hi, mid, sid in wins:
            if lo <= t <= hi:
                hits += 1
                d = abs(t - mid)
                if best is None or d < best[0]:
                    best = (d, sid)
        key = best[1] if best else "_unassigned"
        o = out.setdefault(key, {"physical": 0, "logical": 0, "count": 0, "ambiguous": 0})
        o["physical"] += c.get("length", 0)
        o["logical"] += c.get("originalLength", c.get("length", 0))
        o["count"] += 1
        if hits > 1:
            # several PVCs of the namespace were being exported at that moment (VM disks are
            # exported concurrently): the winner is the nearest window, which is a guess
            o["ambiguous"] += c.get("length", 0)
    return out


def written_in_window(items, tkey, lo, hi, is_pack=lambda x: True):
    """Count and bytes of items whose timestamp lies in [lo, hi]. Used for content blocks
    (physical ingest) and for pack blobs (objects that reached the object store)."""
    n = b = 0
    for it in items:
        if not is_pack(it):
            continue
        t = it.get(tkey)
        if isinstance(t, str):
            d = parse_rfc3339(t)
            t = epoch(d) if d else None
        if t is None or not (lo <= t <= hi):
            continue
        n += 1
        b += it.get("length", 0)
    return n, b


def snapshot_mode(sn):
    """K10 stores block-mode volumes (KubeVirt disks, and any volume exported in block
    mode) as a chunk tree: the root holds "meta:*" entries (BlockSzB, VolSnapID, ...) and
    a c/ directory of fixed-size chunks. Kopia's stats then report fileCount = number of
    chunks and totalFileSize = null; the real logical size is rootEntry.summ.fileSize.
    The description reads "volume:<pvc>:<csi-snapshot>" and the path is /volume/<pvc>."""
    desc = sn.get("description") or ""
    path = (sn.get("source") or {}).get("path") or ""
    return "block" if desc.startswith("volume:") or path.startswith("/volume/") else "filesystem"


def snapshot_size_files(sn):
    """(logical bytes, file or chunk count) of the snapshot TREE, from rootEntry.summ.
    stats.fileCount is not that: it counts the files Kopia processed in this run (a
    mastodon volume showed stats.fileCount 14 for a 167-file tree on its second snapshot),
    so it is exposed separately as filesHashed. summ.fileSize is also the only real size of
    a block-mode snapshot (summ.size and stats.totalFileSize are 0 / null there)."""
    stats = sn.get("stats") or {}
    summ = (sn.get("rootEntry") or {}).get("summ") or {}
    size = summ.get("fileSize")
    if size is None:
        size = stats.get("totalFileSize")
    files = summ.get("files")
    if files is None:
        files = stats.get("fileCount")
    return size, files


def snapshot_files_hashed(sn):
    """(hashed, unchanged): files Kopia had to read and hash in this run, and files it skipped
    as unchanged against the previous snapshot (stats.cachedFiles - 0 on a first snapshot).
    Validated on the reference cluster: second snapshot of a 167-file volume -> fileCount 14, cachedFiles 153.
    A per-PVC change indicator that survives maintenance. Meaningless in block mode, where
    cachedFiles holds the block size."""
    if snapshot_mode(sn) == "block":
        return None, None
    stats = sn.get("stats") or {}
    return stats.get("fileCount"), stats.get("cachedFiles")


def kopia_block_size(kube, pod, root):
    """BlockSzB from the meta entries of a block-mode root, hex, e.g. meta:BlockSzB:100000."""
    out, _, _ = kube.exec(kube.k10ns, pod, KOPIA_PREFIX + f"kopia ls {root} 2>/dev/null\n")
    m = re.search(r"meta:BlockSzB:([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def kopia_ls_sizes(kube, pod, root):
    """File sizes of a snapshot tree from `kopia ls -l -r`: mode size date time UTC objid path.
    Directories start with 'd'; only regular files count. The path is the tail and may
    contain spaces, so split only the first six fields."""
    out, _, _ = kube.exec(kube.k10ns, pod, KOPIA_PREFIX + f"kopia ls -l -r {root} 2>/dev/null\n")
    sizes = []
    for line in out.splitlines():
        if not line.startswith("-"):
            continue
        parts = line.split(None, 6)
        if len(parts) >= 2:
            try:
                sizes.append(int(parts[1]))
            except ValueError:
                pass
    return sizes


def histogram(sizes):
    h = {name: {"files": 0, "bytes": 0} for name, _ in HISTOGRAM_BUCKETS}
    for s in sizes:
        for name, upper in HISTOGRAM_BUCKETS:
            if upper is None or s < upper:
                h[name]["files"] += 1
                h[name]["bytes"] += s
                break
    srt = sorted(sizes)
    return {
        "buckets": h,
        "fileCount": len(sizes),
        "minBytes": srt[0] if srt else None,
        "medianBytes": srt[len(srt) // 2] if srt else None,
        "maxBytes": srt[-1] if srt else None,
    }


# --------------------------------------------------------------------------- scope from policies

NS_LITERAL = re.compile(r"^[A-Za-z0-9]+[A-Za-z0-9_-]*$")
NS_PREFIX = re.compile(r"^[A-Za-z0-9]+[A-Za-z0-9_-]*\*$")


def policy_targets(policies, namespaces, excluded):
    """(namespace, export profile) pairs selected by policies that have an export action -
    the scope rule (CLAUDE.md, lib/policies.sh). Selector values are literal names, a
    trailing glob, or bare '*', plus real namespace labels; NotIn subtracts; namespaces in
    excludedApps are invisible to K10. VM selectors are reported, not resolved.

    This does not depend on repo_checker, so discovery survives an inventory that aborts
    on a repository with a deleted profile. Whether a selected namespace actually holds
    exported data is decided later by reading its Kopia repository."""
    visible = {n["metadata"]["name"]: (n["metadata"].get("labels") or {}) for n in namespaces
               if n["metadata"]["name"] not in excluded}
    out = {}
    for name, pol in policies.items():
        spec = pol.get("spec") or {}
        exp = next((a for a in spec.get("actions") or [] if a.get("action") == "export"), None)
        if not exp:
            continue
        ep = exp.get("exportParameters") or {}
        if not (ep.get("exportData") or {}).get("enabled"):
            continue
        profile = (ep.get("profile") or {}).get("name")
        sel = spec.get("selector") or {}
        exprs = sel.get("matchExpressions") or []
        # VM policies (KubeVirt) select VMs, not namespaces, but a VM's disks are PVCs in the
        # VM's namespace and the export lands in that namespace's repository - so only the
        # namespace needs resolving and the Kopia read is identical:
        #   virtualMachineRef        In/NotIn  "namespace/vm-name"  (name may be a glob)
        #   virtualMachineNamespace  In/NotIn  namespace globs; matchLabels then apply to VM
        #                                      labels, which we do not evaluate - the
        #                                      repository decides whether anything was exported
        is_vm = any((e.get("key") or "").startswith("k10.kasten.io/virtualMachine") for e in exprs)
        include, exclude, label_terms = [], [], []
        if not is_vm:
            label_terms = list((sel.get("matchLabels") or {}).items())
        label_exprs = []
        for e in exprs:
            k, op, vals = e.get("key"), e.get("operator"), e.get("values") or []
            if k == "k10.kasten.io/appNamespace":
                (include if op == "In" else exclude if op == "NotIn" else []).extend(vals)
            elif k == "k10.kasten.io/virtualMachineRef":
                nss = [v.split("/", 1)[0] for v in vals if "/" in v]
                (include if op == "In" else exclude if op == "NotIn" else []).extend(nss)
            elif k == "k10.kasten.io/virtualMachineNamespace":
                (include if op == "In" else exclude if op == "NotIn" else []).extend(vals)
            elif k and not k.startswith("k10.kasten.io/"):
                label_exprs.append((k, op, vals))

        def expand(v):
            if v == "kasten-io-cluster":
                return set()
            if v == "*":
                return set(visible)
            if NS_PREFIX.match(v):
                return {n for n in visible if n.startswith(v[:-1])}
            if NS_LITERAL.match(v):
                return {v} if v in visible else set()
            return set()

        hits = set()
        for v in include:
            hits |= expand(v)
        if label_terms or label_exprs:
            for n, labels in visible.items():
                ok = all(labels.get(k) == v for k, v in label_terms)
                for k, op, vals in label_exprs:
                    if op == "In":
                        ok = ok and labels.get(k) in vals
                    elif op == "NotIn":
                        ok = ok and labels.get(k) not in vals
                    elif op == "Exists":
                        ok = ok and k in labels
                    elif op == "DoesNotExist":
                        ok = ok and k not in labels
                    else:
                        ok = False
                if ok:
                    hits.add(n)
        for v in exclude:
            hits -= expand(v)
        for n in hits:
            out.setdefault((n, profile), set()).add(name)
        POLICY_KIND[name] = "vm" if is_vm else "namespace"
    return out


POLICY_KIND = {}


# --------------------------------------------------------------------------- PVC name resolution

def resolve_pvc(host, ns_pvcs):
    """Source.host is <applicationID>.<workload>.<pvcName>; dots are legal in PVC names, so
    match the longest live PVC name the host ends with, else fall back to structure
    (lib/kopia.sh)."""
    for name in sorted(ns_pvcs, key=len, reverse=True):
        if host.endswith("." + name):
            rest = host[: -len(name) - 1]
            workload = rest.split(".", 1)[1] if "." in rest else ""
            return name, workload, "live-pvc"
    rest = host.split(".", 1)[1] if "." in host else host
    workload, _, pvc = rest.partition(".")
    return pvc or rest, workload, "structural"


# --------------------------------------------------------------------------- main assembly

def collect(args):
    kube = Kube(args.context, args.k10_namespace)

    # ---- cluster identity -------------------------------------------------------------
    ctx = args.context or kube.run("config", "current-context", check=False).stdout.strip()
    ver = kube.run("-n", kube.k10ns, "get", "cm", "k10-config", "-o", "jsonpath={.data.version}",
                   check=False).stdout.strip()
    if not ver:
        raise SystemExit(f"no k10-config in namespace {kube.k10ns}: wrong --k10-namespace or no K10")
    uid = kube.run("get", "ns", "default", "-o", "jsonpath={.metadata.uid}", check=False).stdout.strip()
    log(f"cluster {ctx}  K10 {ver}  uid {uid}")

    k10cfg = (kube.get("cm", "k10-config", ns=kube.k10ns) or {}).get("data", {})
    limiters = {k: v for k, v in sorted(k10cfg.items()) if LIMITER_KEY_REGEX.match(k)}
    features = (kube.get("cm", "k10-features", ns=kube.k10ns) or {}).get("data", {})

    # ---- K10 objects --------------------------------------------------------------------
    log("reading policies, profiles and PVCs ...")
    policies = {p["metadata"]["name"]: p for p in (kube.get("policies.config.kio.kasten.io", ns=kube.k10ns) or {"items": []})["items"]}
    profiles = {p["metadata"]["name"]: p for p in (kube.get("profiles.config.kio.kasten.io", ns=kube.k10ns) or {"items": []})["items"]}
    apspecs = {p["metadata"]["name"]: p for p in (kube.get("actionpodspecs.config.kio.kasten.io", ns=kube.k10ns) or {"items": []})["items"]}
    bindings = (kube.get("actionpodspecbindings.config.kio.kasten.io", all_ns=True) or {"items": []})["items"]
    # per-application exports live in the application namespace; the K10 namespace only
    # holds the policy run's metadata export
    export_actions = []  # listed after the repository reads (see below): an action that starts
    #                      during the run must not be missing while its snapshot is present
    all_pvcs = (kube.get("pvc", all_ns=True) or {"items": []})["items"]
    log(f"  {len(policies)} policies, {len(profiles)} profiles, {len(all_pvcs)} PVCs")
    with Step("node capacity and usage", indent=2) as stp:
        nodes_info = collect_nodes(kube)
        stp.note = f"{nodes_info['totals']['nodes']} nodes via {', '.join(nodes_info['usageSources']) or 'no usage source'}"
    pvc_by_ns = {}
    for p in all_pvcs:
        pvc_by_ns.setdefault(p["metadata"]["namespace"], {})[p["metadata"]["name"]] = p

    # ---- metrics ------------------------------------------------------------------------
    prom = None
    if not args.no_metrics:
        prom = Prom(kube, url=args.prom_url, token=args.prom_token, prom_ns=args.prom_namespace,
                    header=args.prom_header)
        prom.init()
        if prom.available:
            log(f"metrics: {prom.url} ({prom.flavour}), retention ~{(prom.retention_seconds or 0)//86400} d")

    # ---- scope, from the cluster APIs alone. repo_checker is NEVER consulted here: it is
    #      the one component that can abort, and a failed inventory must not shrink the
    #      report. Two complementary questions, and neither answer contains the other:
    #        what WILL be exported   export policies      (policy_targets)
    #        what HAS been exported  restorepointcontents (exported_history)
    #      A namespace selected but never yet exported appears only in the first; data whose
    #      policy or namespace has been deleted appears only in the second.
    # 1. the export policies (the scope rule; never depends on repo_checker)
    excluded = {x.strip() for x in (k10cfg.get("excludedApps") or "").split(",") if x.strip()}
    all_ns = (kube.get("namespaces") or {"items": []})["items"]
    targets = {}
    for (ns, prof), pols in policy_targets(policies, all_ns, excluded).items():
        if args.namespace and ns not in args.namespace:
            continue
        if args.policy:
            # focus on one export problem: keep only the requested policies, and only the
            # pairs they select
            pols = [x for x in pols if x in args.policy]
            if not pols:
                continue
        targets[(ns, prof)] = {"repo": None, "policies": set(pols), "inv_snaps": [], "source": "policy"}
    if args.policy:
        unknown = sorted(set(args.policy) - set(policies))
        if unknown:
            warn(f"--policy {', '.join(unknown)}: no such policy on the cluster")
    # A namespace with no RestorePoint at all was never backed up, so it cannot have been
    # exported; skip it before paying for a repository connect. VM policies selecting
    # "*" otherwise expand to every namespace on the cluster (47 on a lab cluster).
    with Step("restore points across namespaces", indent=2) as st:
        rps = (kube.get("restorepoints.apps.kio.kasten.io", all_ns=True) or {"items": []})["items"]
        st.note = f"{len(rps)}"
    ns_with_rp = {r["metadata"]["namespace"] for r in rps}
    skipped_no_rp = sorted({ns for (ns, _) in targets if ns not in ns_with_rp})
    targets = {k: v for k, v in targets.items() if k[0] in ns_with_rp}
    # the K10 namespace's own "export" is the disaster-recovery repository, a different kind
    dr_targets = [k for k in targets if k[0] == kube.k10ns]
    for k in dr_targets:
        targets.pop(k)
    log(f"scope from export policies: {len(targets)} namespace/profile pairs "
        f"({len(skipped_no_rp)} selected namespaces have no restore point and were skipped)")

    # 1b. what HAS been exported, from the cluster-scoped history. A policy or a namespace
    #     that has been deleted takes its pair out of policy_targets - and out of the
    #     namespaced RestorePoint filter above - while the Kopia repository and everything
    #     in it stay on the object store. Those are added here so the report still describes
    #     them; they are connected and read exactly like a live pair, because the profile
    #     (the credentials) is what a connect actually needs.
    with Step("exported restore points and storage repositories", indent=2) as st:
        hist_triples, hist_repos = exported_history(kube)
        st.note = f"{len(hist_triples)} namespace/policy/profile triples exported historically"
    orphan_pairs = {}
    for (ns, pol, prof), info in sorted(hist_triples.items()):
        if args.namespace and ns not in args.namespace:
            continue
        if args.policy and pol not in args.policy:
            continue
        if ns == kube.k10ns:
            continue
        gone = []
        if pol != "-" and pol not in policies:
            gone.append("policy")
        if ns not in {n["metadata"]["name"] for n in all_ns}:
            gone.append("namespace")
        if not gone:
            continue  # still live: policy_targets already has it, or it is simply not selected any more
        cr = hist_repos.get((ns, prof))
        # A frozen entry, not a live one: with the policy or the namespace gone nothing will
        # export here again, so the newest restore point is the date the data stopped moving.
        # Everything else in this namespace's section - sizes, file counts, change rates - is
        # history as of that date and will not change.
        info = dict(info, orphanedBy=gone, policy=pol, profileExistsOnCluster=prof in profiles,
                    storageRepository=cr, frozenSince=info.get("newest"),
                    note=("no longer exported: the " + " and ".join(gone) +
                          " no longer exists, so this namespace is frozen at its last restore "
                          "point and the figures below are history, not a current state. The "
                          "data still occupies the object store."))
        if (ns, prof) in targets:
            targets[(ns, prof)]["orphan"] = info
            continue
        orphan_pairs[(ns, prof)] = info
        targets[(ns, prof)] = {"repo": None, "policies": {pol} if pol != "-" else set(),
                               "inv_snaps": [], "source": "restorepointcontent", "orphan": info}
    # The StorageRepository CR names every repository and gives its location. Attach it to
    # every pair, live or orphaned: when repo_checker's inventory aborts, this is what keeps
    # the repository name in the report instead of a blank.
    for k, v in targets.items():
        v["cr"] = hist_repos.get(k)
    if orphan_pairs:
        for (ns, prof), info in sorted(orphan_pairs.items()):
            warn(f"{ns}/{prof}: exported {info['restorePoints']} restore points by policy "
                 f"{info['policy']} but the {' and '.join(info['orphanedBy'])} no longer exists"
                 + ("" if info["profileExistsOnCluster"] else
                    f"; profile {prof} is gone too, so the repository cannot be opened"))
        log(f"scope from exported history: {len(orphan_pairs)} further pairs whose policy or "
            f"namespace is gone - their data is still on the object store")

    # A pair whose PROFILE is gone cannot be opened at all - the profile holds the
    # credentials and the repository password - so do not spend a k10tools run on it.
    # Report it from history instead: the restore point count, the object-store location
    # from the StorageRepository CR, and (a later step) the PVCs and Kopia snapshot ids
    # from each restorepointcontents/<name>/details.
    # Two reasons a pair cannot be OPENED, both fatal to repo_checker and neither fatal to
    # the report: the namespace is gone (connect resolves the repository from the live
    # namespace UID - "Failed to get application namespace UID"), or the profile is gone
    # (no credentials, no repository password). Describe those from the restore point
    # details instead of paying for a connect that cannot succeed.
    unopenable = {}
    for k, v in list(targets.items()):
        o = v.get("orphan")
        if not o:
            continue
        why = []
        if "namespace" in o["orphanedBy"]:
            why.append("the namespace is gone, so repo_checker cannot resolve the repository "
                       "from its UID")
        if not o["profileExistsOnCluster"]:
            why.append(f"profile {k[1]} is gone, so the credentials and the repository "
                       "password are gone")
        if why:
            unopenable[k] = (targets.pop(k)["orphan"], why)
    if unopenable:
        with Step(f"restore point details for {len(unopenable)} unopenable pairs", indent=2) as st:
            for (ns, prof), (info, why) in sorted(unopenable.items()):
                pvcs, stats = restore_points_from_details(kube, info["names"])
                info.update(pvcs=pvcs, detailStats=stats, openable=False,
                            cannotOpenBecause=why,
                            describedFrom="restorepointcontents/<name>/details")
            st.note = f"{sum(len(i[0].get('pvcs') or []) for i in unopenable.values())} PVCs described"

    # ---- repo_checker: ENRICHMENT ONLY, after the scope is already settled ------------------
    # It contributes the repository name and the orphan/dangling/not-synced counts, which no
    # CR carries. Everything it returns is optional: if it aborts, the pairs above are still
    # read and reported.
    workdir = tempfile.mkdtemp(prefix="export-topology-")
    # Default to the registry K10 itself pulls from: on an enterprise cluster that one is
    # whitelisted or mirrored, gcr.io usually is not. gcr.io/kasten-images only when K10's
    # registry cannot be read or when asked for explicitly.
    detected = k10_image_registry(k10cfg)
    if args.image_registry in (None, "auto"):
        registry = detected or "gcr.io/kasten-images"
        origin = "the registry K10 pulls from" if detected else "repo_checker default, K10's registry not readable from k10-config"
    else:
        registry, origin = args.image_registry, "--image-registry"
    rc = RepoChecker(kube, args.repo_checker, ver, workdir, keep_pods=args.keep_pods, image_registry=registry, image_tag=args.image_tag or ver)
    rc.detected_registry = detected
    log(f"repo_checker images: {registry}/k10tools:{args.image_tag or ver}  ({origin})")

    inv, orphan_repos = {"repositories": []}, []
    if not args.no_inventory:
        log("repo_checker inventory (enrichment; scope is already known) ...")
        # the repositories of each profile, from the StorageRepository CRs, so that a profile
        # whose inventory aborts can still be listed one repository at a time
        repos_by_profile = {}
        for (ns_, prof_), cr_ in hist_repos.items():
            repos_by_profile.setdefault(prof_, []).append(cr_["repository"])
        try:
            focused = bool(args.namespace or args.policy)
            inv_profiles = sorted({prof for (_, prof) in targets} & set(profiles)) if focused else sorted(profiles)
            inv, orphan_repos = rc.inventory(inv_profiles, full=not focused,
                                             repos_by_profile=repos_by_profile,
                                             per_repository=args.inventory_per_repository)
        except ImagePullError:
            raise
        except SystemExit as e:
            warn(f"inventory unavailable: {e}; the report is built from the cluster APIs alone")
        if orphan_repos:
            enrich_orphans(kube, orphan_repos, profiles, uid)
    data_repos = [r for r in inv.get("repositories", []) if r.get("Type") == "Data"]
    log(f"  inventory: {len(inv.get('repositories', []))} repositories, {len(data_repos)} hold volume data")
    for r in data_repos:
        for s in r.get("Snapshots") or []:
            ns = s.get("RestorePointNamespace")
            if not ns or (args.namespace and ns not in args.namespace):
                continue
            key = (ns, r["ProfileName"])
            if args.policy and key not in targets:
                continue  # inventory snapshots carry no policy; a focused run stays with the policy's pairs
            t = targets.setdefault(key, {"repo": None, "policies": set(), "inv_snaps": [], "source": "inventory"})
            t["repo"] = r
            if s.get("PolicyName"):
                t["policies"].add(s["PolicyName"])
            t["inv_snaps"].append(s)

    if not targets and not unopenable:
        warn("no export policy selects any namespace and nothing has ever been exported "
             "according to the restore point history - nothing to report")

    ns_results = {}
    not_exported = []
    # the unopenable pairs join the results without a connect: same shape, history only
    for (ns, prof), (info, _why) in unopenable.items():
        ns_results[(ns, prof)] = {
            "repository": {"name": (info.get("storageRepository") or {}).get("repository"),
                           "nameFrom": "StorageRepository CR",
                           "profile": prof, "inventoried": False, "openable": False,
                           "location": {"type": (info.get("storageRepository") or {}).get("objectStoreType"),
                                        "bucket": (info.get("storageRepository") or {}).get("bucket"),
                                        "prefix": (info.get("storageRepository") or {}).get("path")},
                           "note": "described from restore point details; the repository was not opened"},
            "pvcs": info.get("pvcs") or [], "policies": {info["policy"]} if info["policy"] != "-" else set(),
            "orphan": info, "_blobs": [], "_contents": [], "_rewrite_ts": None,
            "_snapshot_times": [], "_snapshot_index": []}

    # ---- per (namespace, profile): connect and read Kopia -----------------------------------
    try:
        _read_repositories(args, kube, rc, prom, targets, pvc_by_ns, ns_results, not_exported)
    finally:
        rc.cleanup(wait=True)
    # ExportActions are listed only now: the repositories were read over the last minutes
    # and an export that started meanwhile has its Kopia snapshot in them - listing the
    # actions first left such a snapshot without its action (seen: 4 snapshots, 3 actions).
    export_actions = (kube.get("exportactions.actions.kio.kasten.io", all_ns=True) or {"items": []})["items"]
    log(f"  {len(export_actions)} export actions (listed after the repositories)")

    return _assemble(args, kube, ctx, ver, uid, limiters, features, policies, profiles, apspecs, bindings,
                     export_actions, prom, inv, orphan_repos, ns_results, not_exported, skipped_no_rp, workdir,
                     nodes_info, [{'namespace': k[0], 'profile': k[1], **{x: y for x, y in v[0].items() if x != 'names'}}
                                  for k, v in sorted(unopenable.items())])


def _read_repositories(args, kube, rc, prom, targets, pvc_by_ns, ns_results, not_exported):
    total = len(targets)
    for i, ((ns, prof), t) in enumerate(sorted(targets.items()), 1):
        log(f"[{i}/{total}] connecting to repository of {ns} on profile {prof} ...")
        try:
            pod = rc.connect(ns, prof)
        except ConnectError as e:
            cause = str(e)
            if "failed to connect to repository" in cause:
                log(f"  {ns}/{prof}: no repository on this profile ({cause.split(' -> ')[-1]}) - never exported there")
                not_exported.append({"namespace": ns, "profile": prof, "policies": sorted(t["policies"]),
                                     "reason": cause})
            else:
                warn(f"{ns}/{prof}: connect failed: {cause}")
            continue
        except Exception as e:  # noqa: BLE001
            warn(f"{ns}/{prof}: {e}")
            continue
        try:
            with Step("snapshot list") as st:
                snaps = kopia_json(kube, pod, "kopia snapshot list --all")
                st.note = f"{len(snaps)} snapshots, {len({s['source']['host'] for s in snaps})} sources"
            if not snaps:
                log(f"  {ns}/{prof}: selected by a policy but the repository holds no snapshots - out of scope")
                rc.cleanup()
                continue
            maint = kopia_json(kube, pod, "kopia maintenance info")
            if args.no_content_list:
                contents = []
            else:
                with Step("content list") as st:
                    contents = kopia_json(kube, pod, "kopia content list")
                    st.note = f"{len(contents)} contents"
            with Step("blob list") as st:
                blobs = kopia_json(kube, pod, "kopia blob list")
                st.note = f"{len(blobs)} blobs"
            rstatus = kopia_json(kube, pod, "kopia repository status")
        except Exception as e:  # noqa: BLE001
            warn(f"{ns}/{prof}: {e}")
            rc.cleanup()
            continue

        rewrite_ts = last_content_rewrite(maint)
        assigned = assign_contents(contents, snaps) if contents else {}
        if rewrite_ts:
            log(f"  {ns}: contents re-stamped by maintenance at {iso(dt.datetime.fromtimestamp(rewrite_ts, dt.timezone.utc))}; "
                f"physical ingest of earlier snapshots is unrecoverable")
        ns_pvc_names = set(pvc_by_ns.get(ns, {}).keys())
        # Kopia writes an incomplete manifest every 45 min while a long upload runs
        # (incomplete: "checkpoint", same startTime as the running snapshot). Not restore
        # points: keep them apart and report them as the export's live progress.
        by_source, checkpoints = {}, {}
        for s in snaps:
            if s.get("incomplete"):
                checkpoints.setdefault(s["source"]["host"], []).append(s)
            else:
                by_source.setdefault(s["source"]["host"], []).append(s)
        for host in checkpoints:
            by_source.setdefault(host, [])  # a first export still running: PVC with no complete snapshot yet

        pvcs = []
        for j, (host, slist) in enumerate(sorted(by_source.items()), 1):
            slist.sort(key=lambda x: x["startTime"])
            pvc_name, workload, how = resolve_pvc(host, ns_pvc_names)
            cps = sorted(checkpoints.get(host, []), key=lambda x: x["endTime"] or "")
            in_progress = None
            if cps:
                last_cp = cps[-1]
                cp_size, cp_files = snapshot_size_files(last_cp)
                st_, en_ = parse_rfc3339(last_cp["startTime"]), parse_rfc3339(last_cp["endTime"])
                elapsed = (epoch(en_) - epoch(st_)) if st_ and en_ else None
                in_progress = {"startTime": last_cp["startTime"], "checkpoints": len(cps),
                               "lastCheckpointTime": last_cp["endTime"], "filesSoFar": cp_files, "bytesSoFar": cp_size,
                               "elapsedSeconds": round(elapsed, 1) if elapsed else None,
                               "filesPerSecond": round(cp_files / elapsed, 1) if elapsed and cp_files else None,
                               "bytesPerSecond": round(cp_size / elapsed) if elapsed and cp_size else None,
                               "note": "Kopia checkpoints of a snapshot still being uploaded (one every 45 min); not restore points"}
            if not slist:
                log(f"    pvc {j}/{len(by_source)} {pvc_name}: no complete snapshot yet, export in progress "
                    f"({in_progress['checkpoints']} checkpoints, {in_progress['filesSoFar']} files so far)")
                pvcs.append({"name": pvc_name, "workload": workload, "resolvedBy": how, "sourceHost": host,
                             "existsOnCluster": pvc_name in ns_pvc_names, "snapshotCount": 0, "snapshots": [],
                             "mode": snapshot_mode(cps[-1]), "fileCount": None, "totalSizeBytes": None,
                             "sizeHistogram": None, "inProgress": in_progress})
                continue
            log(f"    pvc {j}/{len(by_source)} {pvc_name}: {len(slist)} snapshots ({snapshot_mode(slist[-1])} mode)"
                + (f", export in progress ({in_progress['checkpoints']} checkpoints)" if in_progress else ""))
            live = pvc_by_ns.get(ns, {}).get(pvc_name)
            entry = {
                "name": pvc_name,
                "workload": workload or None,
                "sourceHost": host,
                "resolvedBy": how,
                "storageClass": (live or {}).get("spec", {}).get("storageClassName") if live else None,
                "accessMode": ((live or {}).get("spec", {}).get("accessModes") or [None])[0] if live else None,
                "requestedStorage": ((live or {}).get("spec", {}).get("resources", {}).get("requests") or {}).get("storage") if live else None,
                "existsOnCluster": live is not None,
            }
            snap_entries = []
            prev = None
            mode = snapshot_mode(slist[-1])
            for s in slist:
                st, en = parse_rfc3339(s["startTime"]), parse_rfc3339(s["endTime"])
                stats = s.get("stats", {})
                size_b, files_n = snapshot_size_files(s)
                se = {
                    "id": s.get("id"),
                    "startTime": s["startTime"],
                    "endTime": s["endTime"],
                    "durationSeconds": round(epoch(en) - epoch(st), 3) if st and en else None,
                    "mode": snapshot_mode(s),
                    "fileCount": files_n,
                    "filesHashed": snapshot_files_hashed(s)[0],
                    "filesUnchanged": snapshot_files_hashed(s)[1],
                    "dirCount": stats.get("dirCount"),
                    "totalSizeBytes": size_b,
                    "errorCount": stats.get("errorCount"),
                    "rootObjectId": (s.get("rootEntry") or {}).get("obj"),
                }
                # change rate: logical net growth vs previous snapshot of the same source;
                # physical ingest = content blocks whose creation time falls in the window.
                # SizeBytes/totalFileSize is the logical size, not the increment (guide 06).
                if prev is not None:
                    psize, pfiles = snapshot_size_files(prev)
                    se["logicalDeltaBytes"] = (size_b or 0) - (psize or 0)
                    se["logicalDeltaFiles"] = (files_n or 0) - (pfiles or 0)
                    pst = parse_rfc3339(prev["startTime"])
                    se["intervalSinceLastSeconds"] = round(epoch(st) - epoch(pst), 1) if st and pst else None
                if contents and st and en:
                    if rewrite_ts and epoch(en) < rewrite_ts:
                        se["physicalIngestBytes"] = None
                        se["physicalIngestNote"] = ("unrecoverable: full-rewrite-contents maintenance on %s re-stamped every "
                                                    "content written before it" % iso(dt.datetime.fromtimestamp(rewrite_ts, dt.timezone.utc)))
                    else:
                        a = assigned.get(s.get("id"), {"physical": 0, "logical": 0, "count": 0, "ambiguous": 0})
                        phys, logi = a["physical"], a["logical"]
                        se["physicalIngestBytes"] = phys
                        se["logicalIngestBytes"] = logi
                        se["physicalIngestContents"] = a["count"]
                        if a.get("ambiguous"):
                            se["physicalIngestAmbiguousBytes"] = a["ambiguous"]
                            se["physicalIngestNote"] = ("%d of these bytes were written while another PVC of the namespace "
                                                        "was also being exported; attributed to the nearest window" % a["ambiguous"])
                        se["compressionDedupRatio"] = round(phys / logi, 3) if logi else None
                        if phys == 0 and (stats.get("totalFileSize") or 0) > 0 and prev is None:
                            se["physicalIngestNote"] = "0 bytes for a first snapshot is unexpected - contents may have been compacted"
                if st and en:
                    if prom and prom.retention_seconds and (time.time() - epoch(st)) > prom.retention_seconds:
                        se["datamover"] = {"note": "snapshot predates the metrics retention window"}
                    else:
                        se["datamover"] = datamover_metrics(prom, kube.k10ns, args.datamover_pod_regex, epoch(st), epoch(en), namespace=ns)
                snap_entries.append(se)
                prev = s

            last = slist[-1]
            lstats = last.get("stats", {})
            lsize, lfiles = snapshot_size_files(last)
            root = (last.get("rootEntry") or {}).get("obj")
            entry["mode"] = mode
            entry["dirCount"] = lstats.get("dirCount")
            entry["lastSnapshotTime"] = last["startTime"]
            if in_progress:
                entry["inProgress"] = in_progress
            entry["snapshotCount"] = len(slist)

            if mode == "block":
                # chunks, not files: the histogram would be N entries of size 0
                entry["fileCount"] = None
                entry["chunkCount"] = lfiles
                entry["totalSizeBytes"] = lsize
                entry["averageFileSizeBytes"] = None
                entry["blockSizeBytes"] = kopia_block_size(kube, pod, root) if root else None
                entry["sizeHistogram"] = {"skipped": "block-mode volume: the tree holds fixed-size chunks, not files"}
            else:
                entry["fileCount"] = lfiles
                entry["totalSizeBytes"] = lsize
                if args.no_histogram:
                    entry["sizeHistogram"] = None
                elif (lfiles or 0) > args.histogram_max_files:
                    entry["sizeHistogram"] = {"skipped": f"{lfiles} files exceeds --histogram-max-files {args.histogram_max_files}"}
                else:
                    try:
                        with Step("tree listing", indent=6) as st:
                            sizes = kopia_ls_sizes(kube, pod, root) if root else []
                            st.note = f"{len(sizes)} files"
                        entry["sizeHistogram"] = histogram(sizes)
                        # some filesystem snapshots carry stats.fileCount = 0 although the
                        # tree is populated (seen on CloudNativePG volumes); the listing wins
                        if not entry["fileCount"] and sizes:
                            entry["fileCount"] = len(sizes)
                            entry["fileCountSource"] = "tree listing"
                        if not entry["totalSizeBytes"] and sizes:
                            entry["totalSizeBytes"] = sum(sizes)
                    except Exception as e:  # noqa: BLE001
                        entry["sizeHistogram"] = {"error": str(e)}
                fc = entry.get("fileCount") or 0
                entry["averageFileSizeBytes"] = int((entry.get("totalSizeBytes") or 0) / fc) if fc else None

            # last change rate from the last two snapshots
            if len(snap_entries) >= 2:
                lse = snap_entries[-1]
                iv = lse.get("intervalSinceLastSeconds") or 0
                entry["lastChangeRate"] = {
                    "from": snap_entries[-2]["startTime"], "to": lse["startTime"],
                    "intervalSeconds": iv,
                    "logicalDeltaBytes": lse.get("logicalDeltaBytes"),
                    "logicalDeltaFiles": lse.get("logicalDeltaFiles"),
                    "physicalIngestBytes": lse.get("physicalIngestBytes"),
                    "physicalIngestBytesPerDay": round(lse["physicalIngestBytes"] * 86400 / iv) if lse.get("physicalIngestBytes") is not None and iv else None,
                    "note": "logicalDelta is net growth, not churn; physicalIngest is what reached the object store",
                }
            else:
                entry["lastChangeRate"] = {"note": "only one snapshot - a second export is needed before a rate exists"}
            entry["snapshots"] = snap_entries
            pvcs.append(entry)

        # maintenance: per repository, reported on every PVC as requested
        runs = ((maint.get("schedule") or {}).get("runs") or {})
        last_runs = []
        for task, rl in runs.items():
            if rl:
                r = rl[-1]
                last_runs.append({"task": task, "start": r.get("start"), "end": r.get("end"),
                                  "success": r.get("success"), "error": r.get("error")})
        last_runs.sort(key=lambda x: x.get("end") or "", reverse=True)
        maint_summary = {
            "owner": maint.get("owner"),
            "quick": maint.get("quick"), "full": maint.get("full"),
            "nextQuickMaintenance": (maint.get("schedule") or {}).get("nextQuickMaintenance"),
            "nextFullMaintenance": (maint.get("schedule") or {}).get("nextFullMaintenance"),
            "lastRun": last_runs[0] if last_runs else None,
            "lastRunPerTask": last_runs,
            "anyFailure": any(r.get("success") is False for r in last_runs),
        }
        for p in pvcs:
            p["lastMaintenance"] = {"end": maint_summary["lastRun"]["end"] if maint_summary["lastRun"] else None,
                                    "task": maint_summary["lastRun"]["task"] if maint_summary["lastRun"] else None,
                                    "success": maint_summary["lastRun"]["success"] if maint_summary["lastRun"] else None,
                                    "anyTaskFailed": maint_summary["anyFailure"]}

        repo = t["repo"] or {}
        cr = t.get("cr") or {}
        storage = (rstatus or {}).get("storage") or {}
        ns_results[(ns, prof)] = {
            "repository": {"name": repo.get("RepositoryName") or cr.get("repository"),
                           "nameFrom": "repo_checker inventory" if repo.get("RepositoryName")
                                       else ("StorageRepository CR" if cr.get("repository") else None),
                           "id": repo.get("RepositoryID"),
                           "kopiaUniqueId": (rstatus or {}).get("uniqueIDHex"),
                           "location": {"type": storage.get("type"),
                                        "bucket": (storage.get("config") or {}).get("bucket"),
                                        "prefix": (storage.get("config") or {}).get("prefix")},
                           "format": {k: ((rstatus or {}).get("contentFormat") or {}).get(k) for k in ("version", "maxPackSize", "indexVersion")},
                           "inventoried": bool(t["repo"]),
                           "profile": prof, "totalSnapshots": repo.get("TotalSnapshots", len(snaps)),
                           "orphanedCount": repo.get("OrphanedCount"), "danglingCount": repo.get("DanglingCount"),
                           "maintenance": maint_summary,
                           "objectCount": len(blobs),
                           "objectBytes": sum(b.get("length", 0) for b in blobs),
                           "packObjectCount": sum(1 for b in blobs if b.get("id", "")[:1] in ("p", "q")),
                           "contentsRestampedAt": iso(dt.datetime.fromtimestamp(rewrite_ts, dt.timezone.utc)) if rewrite_ts else None,
                           "contentBytesNotAttributableToASnapshot": (assigned.get("_unassigned") or {}).get("physical", 0) if contents else None,
                           "contentCount": len(contents) if contents else None,
                           "contentPhysicalBytes": sum(c["length"] for c in contents if not c.get("deleted")) if contents else None,
                           "contentLogicalBytes": sum(c.get("originalLength", c["length"]) for c in contents if not c.get("deleted")) if contents else None},
            "pvcs": pvcs,
            "policies": t["policies"],
            "orphan": t.get("orphan"),
            "_blobs": blobs, "_contents": contents, "_rewrite_ts": rewrite_ts,
            "_snapshot_times": [epoch(parse_rfc3339(s["startTime"])) for s in snaps if parse_rfc3339(s["startTime"])],
            # per snapshot: when, which PVC, how big the source was - the denominator of an
            # export's change rate
            "_snapshot_index": [{"t": epoch(parse_rfc3339(se["startTime"])), "pvc": p["name"],
                                 "logicalBytes": se.get("totalSizeBytes") or 0}
                                for p in pvcs for se in p["snapshots"] if parse_rfc3339(se["startTime"])],
        }
        rc.cleanup()



def _assemble(args, kube, ctx, ver, uid, limiters, features, policies, profiles, apspecs, bindings,
              export_actions, prom, inv, orphan_repos, ns_results, not_exported, skipped_no_rp, workdir,
              nodes_info=None, unopenable_pairs=None):
    # ---- export actions -----------------------------------------------------------------
    # One policy run (label runActionName) produces a metadata export in the K10 namespace
    # (isMetadataExport=true, no progress) and one export per application in that
    # application's namespace. Only the latter has bytes: status.progressDetails and the
    # per-volume operations sit behind the /details subresource, not on the object itself.
    in_scope_ns = {ns for (ns, _) in ns_results}
    exports_by_policy, policy_runs, to_detail = {}, {}, []
    for ea in export_actions:
        md, lab = ea["metadata"], ea["metadata"].get("labels") or {}
        pol = lab.get("k10.kasten.io/policyName")
        st, en = ea.get("status", {}).get("startTime"), ea.get("status", {}).get("endTime")
        s_, e_ = parse_rfc3339(st), parse_rfc3339(en)
        e = {"name": md["name"], "namespace": md["namespace"], "policy": pol,
             "runAction": lab.get("k10.kasten.io/runActionName"),
             "profile": lab.get("k10.kasten.io/exportProfile") or ((ea.get("spec") or {}).get("profile") or {}).get("name"),
             "state": ea.get("status", {}).get("state"), "startTime": st, "endTime": en,
             "durationSeconds": round(epoch(e_) - epoch(s_), 1) if s_ and e_ else None,
             "runNow": lab.get("k10.kasten.io/isRunNow") == "true"}
        if lab.get("k10.kasten.io/isMetadataExport") == "true" or md["namespace"] == kube.k10ns:
            policy_runs.setdefault(pol, []).append(e)
            continue
        exports_by_policy.setdefault(pol, []).append(e)
        if md["namespace"] in in_scope_ns and not (args.policy and pol not in args.policy):
            to_detail.append(e)
    for runs in policy_runs.values():
        runs.sort(key=lambda x: x.get("startTime") or "")

    # newest N per namespace/policy, unless asked for all
    to_detail.sort(key=lambda x: x.get("startTime") or "", reverse=True)
    if args.export_details_max:
        seen, kept = {}, []
        for e in to_detail:
            k = (e["namespace"], e["policy"])
            if seen.get(k, 0) < args.export_details_max:
                seen[k] = seen.get(k, 0) + 1
                kept.append(e)
        to_detail = kept
    if not args.no_export_details and to_detail:
        with Step(f"export details for {len(to_detail)} exports", indent=2) as stp:
            got = 0
            for e in to_detail:
                r = kube.run("get", "--raw", f"/apis/actions.kio.kasten.io/v1alpha1/namespaces/{e['namespace']}"
                             f"/exportactions/{e['name']}/details", check=False)
                if r.returncode != 0 or not r.stdout.strip():
                    e["detailsError"] = (r.stderr or "no output").strip()[:200]
                    continue
                try:
                    d = json.loads(r.stdout)
                except ValueError:
                    e["detailsError"] = "details is not JSON"
                    continue
                got += 1
                stt = d.get("status") or {}
                e["k10Progress"] = stt.get("progressDetails")
                vols = []
                for ph in ((stt.get("actionDetails") or {}).get("phases") or []):
                    for v in ph.get("volumeOperations") or []:
                        vols.append({"pvc": v.get("pvcName"), "operation": v.get("operation"),
                                     "dataFormat": v.get("dataFormat"), "exportDirective": v.get("exportDirective"),
                                     "storageClass": v.get("storageClass"), "storageType": v.get("storageType"),
                                     "snapshotId": v.get("snapshotId")})
                e["volumes"] = vols
                # what actually left the cluster for the object store, per K10 itself; unlike
                # Kopia timestamps this survives maintenance
                e["exportedBytes"] = (e["k10Progress"] or {}).get("transferredBytes")
            stp.note = f"{got} with progress"
    # datamover metrics per application export window
    for pol, lst in exports_by_policy.items():
        for e in lst:
            s_, e_ = parse_rfc3339(e.get("startTime")), parse_rfc3339(e.get("endTime"))
            if s_ and e_ and e["namespace"] in in_scope_ns and prom and prom.available and \
                    not (prom.retention_seconds and time.time() - epoch(s_) > prom.retention_seconds):
                e["datamover"] = datamover_metrics(prom, kube.k10ns, args.datamover_pod_regex, epoch(s_), epoch(e_), namespace=e["namespace"])

    # ---- assemble by policy -------------------------------------------------------------------
    def profile_summary(name):
        p = profiles.get(name)
        if not p:
            return {"name": name, "note": "profile not found on cluster"}
        loc = p.get("spec", {}).get("locationSpec", {})
        obj = loc.get("objectStore") or {}
        return {"name": name, "type": loc.get("type"), "objectStoreType": obj.get("objectStoreType"),
                "endpoint": obj.get("endpoint"), "bucket": obj.get("name"), "prefix": obj.get("path"),
                "region": obj.get("region"), "fileStore": loc.get("fileStore")}

    def aps_for_namespace(ns):
        out = []
        for b in bindings:
            if b["metadata"]["namespace"] != ns:
                continue
            ref = (b.get("spec") or {}).get("actionPodSpecRef") or {}
            spec = apspecs.get(ref.get("name"))
            out.append({"binding": b["metadata"]["name"], "actionPodSpec": ref.get("name"),
                        "spec": (spec or {}).get("spec")})
        return out

    def exports_for(res, ns, pol_name, prof):
        """This namespace's ExportActions for the policy (and profile), enriched with what the
        export wrote according to K10 (/details) and to the Kopia repository."""
        out = []
        for e in exports_by_policy.get(pol_name, []):
            if e["namespace"] != ns or (e.get("profile") and e["profile"] != prof):
                continue
            s_, e_ = parse_rfc3339(e.get("startTime")), parse_rfc3339(e.get("endTime"))
            ex = dict(e)
            if s_ and e_:
                lo, hi = epoch(s_) - 5, epoch(e_) + 5
                in_win = [x for x in res["_snapshot_index"] if lo <= x["t"] <= hi]
                src_bytes = sum(x["logicalBytes"] for x in in_win)
                ex["source"] = {"pvcs": sorted({x["pvc"] for x in in_win}), "logicalBytes": src_bytes,
                                "note": "logical size of every PVC snapshot taken by this export, from Kopia"}
                if res["_rewrite_ts"] and epoch(e_) < res["_rewrite_ts"]:
                    ex["written"] = {"note": "unrecoverable: repository contents re-stamped by maintenance since this export"}
                else:
                    is_pack = lambda b: b.get("id", "")[:1] in ("p", "q")
                    objs, obytes = written_in_window(res["_blobs"], "timestamp", lo, hi, is_pack)
                    cn, cb = written_in_window([c for c in res["_contents"] if not c.get("deleted")], "time", lo, hi)
                    ex["written"] = {"packObjects": objs, "packBytes": obytes, "contents": cn, "contentPhysicalBytes": cb,
                                     "note": "pack objects and bytes that reached the object store during this export, from Kopia timestamps"}
                # "change rate": bytes that left for the object store over the logical size of what
                # was exported. 1.0 = a first export with nothing deduplicated or compressed; dedup
                # against earlier snapshots and compression pull it down, encryption adds a little.
                # Numerator: K10's own transferredBytes when /details gave it, else Kopia's pack bytes.
                num, basis = ex.get("exportedBytes"), "k10 transferredBytes"
                if num is None and "packBytes" in (ex.get("written") or {}):
                    num, basis = ex["written"]["packBytes"], "kopia pack bytes written in the window"
                if num is not None and src_bytes:
                    ex["changeRate"], ex["changeRateBasis"] = round(num / src_bytes, 4), basis
                else:
                    ex["changeRate"] = None
                cap = (ex.get("k10Progress") or {}).get("totalBytes")
                if num is not None and cap:
                    ex["transferredOverCapacity"] = round(num / cap, 4)
            out.append(ex)
        out.sort(key=lambda x: x.get("startTime") or "")
        return out

    pol_out = {}
    for (ns, prof), res in ns_results.items():
        for pol_name in sorted(res["policies"]):
            pol = policies.get(pol_name)
            if pol_name not in pol_out:
                spec = (pol or {}).get("spec", {})
                exp = next((a for a in spec.get("actions", []) if a.get("action") == "export"), {})
                ep = exp.get("exportParameters", {})
                pol_out[pol_name] = {
                    "name": pol_name,
                    "existsOnCluster": pol is not None,
                    "profile": profile_summary(ep.get("profile", {}).get("name") or prof),
                    "frequency": spec.get("frequency"),
                    "subFrequency": spec.get("subFrequency"),
                    "exportFrequency": ep.get("frequency"),
                    "exportDataEnabled": (ep.get("exportData") or {}).get("enabled"),
                    "retention": spec.get("retention"),
                    "paused": spec.get("paused", False),
                    "validation": (pol or {}).get("status", {}).get("validation"),
                    "selectorKind": POLICY_KIND.get(pol_name, "namespace"),
                    "selector": spec.get("selector"),
                    "runs": policy_runs.get(pol_name, []),
                    "namespaces": [],
                }
            ns_exports = exports_for(res, ns, pol_name, prof)
            # every snapshot gets the ExportAction that produced it (window match); one
            # without is flagged instead of being left for the reader to guess about
            all_ns_exports = [e for e in exports_by_policy.get(pol_name, []) if e["namespace"] == ns]
            for pv in res["pvcs"]:
                for se in pv.get("snapshots") or []:
                    # a snapshot described from restore point details has no startTime,
                    # only the upload end time - match on whichever it has
                    _st = parse_rfc3339(se.get("startTime") or se.get("endTime"))
                    t = epoch(_st) if _st else None
                    hit = None
                    for e in all_ns_exports:
                        s_, e_ = parse_rfc3339(e.get("startTime")), parse_rfc3339(e.get("endTime") or e.get("startTime"))
                        if t is not None and s_ and e_ and epoch(s_) - 5 <= t <= epoch(e_) + 5:
                            hit = e["name"]
                            break
                    se["exportAction"] = hit
                    if hit is None and se.get("startTime") is None:
                        se["exportActionNote"] = ("described from restore point details: this "
                                                  "repository could not be opened, so there is no "
                                                  "Kopia snapshot window to match an action against")
                    elif hit is None:
                        se["exportActionNote"] = ("no ExportAction of this policy covers this time: written by another policy "
                                                  "or a run-now action into the same repository, or the action history was retired")
            pol_out[pol_name]["namespaces"].append({
                "name": ns,
                "orphan": res.get("orphan"),
                "actionPodSpecs": aps_for_namespace(ns),
                "repository": res["repository"],
                "exports": ns_exports,
                "pvcCount": len(res["pvcs"]),
                "totalSizeBytes": sum(p.get("totalSizeBytes") or 0 for p in res["pvcs"]),
                "totalFileCount": sum(p.get("fileCount") or 0 for p in res["pvcs"]),
                "pvcs": res["pvcs"],
            })

    pod_labels_exposed = ksm_pod_labels_exposed(prom, kube.k10ns) if (prom and prom.available) else None
    topo = {
        "generatedAt": iso(dt.datetime.now(dt.timezone.utc)),
        "cluster": {"context": ctx, "uid": uid, "k10Version": ver, "k10Namespace": kube.k10ns},
        "metrics": None if not prom else {
            "url": prom.url, "flavour": prom.flavour, "available": prom.available,
            "retentionSeconds": prom.retention_seconds,
            "datamoverPodRegex": args.datamover_pod_regex,
            "podLabelsExposed": pod_labels_exposed,
            "attribution": ("namespace and job via kube_pod_labels (KSM metric-labels-allowlist); not per PVC - "
                            "copy-vol-data pods mount an ephemeral clone, not the source PVC"
                            if pod_labels_exposed else
                            "NOT POSSIBLE: kube-state-metrics exposes no pod labels for the K10 namespace, so datamover "
                            "usage is per pod only. Add --metric-labels-allowlist=pods=[app-name,policy-name,k10.kasten.io/jobID] "
                            "to kube-state-metrics" if pod_labels_exposed is False else
                            "unknown: kube_pod_labels could not be queried"),
            "caveat": "pods living under one kubelet scrape interval (30s) leave no usable sample and are listed "
                      "in podsWithoutSamples; peak figures are a floor for short exports",
        },
        "nodes": nodes_info,
        "helmLimiters": limiters,
        "features": features,
        "actionPodSpecs": [{"name": n, "spec": s.get("spec")} for n, s in sorted(apspecs.items())],
        "policies": sorted(pol_out.values(), key=lambda p: p["name"]),
        "orphanedRepositories": orphan_repos,
        # exported data that cannot be opened at all - the namespace is gone (repo_checker
        # resolves the repository from its UID) or the profile is gone (no credentials).
        # Described from restorepointcontents/<name>/details instead.
        "unopenableRepositories": unopenable_pairs or [],
        "filter": ({"namespaces": args.namespace, "policies": args.policy} if (args.namespace or args.policy) else None),
        "notExported": not_exported,
        "scopeNotes": {"namespacesWithoutRestorePointSkipped": skipped_no_rp,
                       "k10NamespaceSkipped": "its export is the disaster-recovery repository, a different repository kind"},
        "repositoriesWithoutVolumeData": [
            {"name": r.get("RepositoryName"), "type": r.get("Type"), "profile": r.get("ProfileName"),
             "totalSnapshots": r.get("TotalSnapshots")} for r in inv.get("repositories", []) if r.get("Type") != "Data"],
        "warnings": WARNINGS,
    }
    shutil.rmtree(workdir, ignore_errors=True)
    return topo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", help="kube context (default: current)")
    ap.add_argument("--k10-namespace", default="kasten-io")
    ap.add_argument("-o", "--output", default="export-topology.json")
    ap.add_argument("--namespace", action="append", help="restrict to these application namespaces (repeatable)")
    ap.add_argument("--policy", action="append", help="restrict to these policies (repeatable); with --namespace, one pair")
    ap.add_argument("--repo-checker", help="path to k10_repo_checker.sh (default: download for the cluster's K10 version; "
                                           "required on an air-gapped cluster)")
    ap.add_argument("--image-registry", help="registry/prefix for the k10tools, datamover and kanister-tools images repo_checker "
                                             "runs (default: the one K10 itself pulls from, read from k10-config; "
                                             "gcr.io/kasten-images if that cannot be read)")
    ap.add_argument("--image-tag", help="image tag for those images (default: the cluster's K10 version)")
    ap.add_argument("--keep-pods", action="store_true", help="leave debug-kopia pods behind")
    ap.add_argument("--no-metrics", action="store_true", help="skip cAdvisor / kube-state-metrics")
    ap.add_argument("--prom-url", help="non-OpenShift Prometheus base URL")
    ap.add_argument("--prom-token", help="bearer token for --prom-url")
    ap.add_argument("--prom-header", help="extra header for --prom-url, e.g. 'X-Scope-OrgID: tenant'")
    ap.add_argument("--prom-namespace", default="openshift-monitoring")
    ap.add_argument("--datamover-pod-regex", default=DATAMOVER_POD_REGEX)
    ap.add_argument("--no-histogram", action="store_true", help="skip the file-size histogram (a full tree listing per PVC)")
    ap.add_argument("--histogram-max-files", type=int, default=2_000_000, help="skip the histogram above this many files")
    ap.add_argument("--no-content-list", action="store_true", help="skip kopia content list (no physical ingest figures)")
    ap.add_argument("--no-inventory", action="store_true", help="skip repo_checker inventory entirely; scope does not depend on it")
    ap.add_argument("--inventory-per-repository", action="store_true",
                    help="when a profile's inventory aborts, retry it one repository at a time "
                         "(-R). Recovers the orphan/dangling counts of the repositories that are "
                         "readable, at one full catalog re-scan each")
    ap.add_argument("--no-export-details", action="store_true", help="skip the ExportAction /details fetch (no K10 byte counters)")
    ap.add_argument("--export-details-max", type=int, default=20, help="newest exports per namespace/policy to fetch details for (0 = all)")
    ap.add_argument("-v", "--verbose", action="store_true", help="echo every repo_checker / k10tools progress line")
    args = ap.parse_args()
    global VERBOSE
    VERBOSE = args.verbose

    for tool in ("kubectl", "helm"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found in PATH ({'repo_checker requires helm' if tool == 'helm' else 'required'})")

    topo = collect(args)
    with open(args.output, "w") as f:
        json.dump(topo, f, indent=2, default=str)
    npol = len(topo["policies"])
    nns = sum(len(p["namespaces"]) for p in topo["policies"])
    npvc = sum(len(n["pvcs"]) for p in topo["policies"] for n in p["namespaces"])
    log(f"wrote {args.output}: {npol} policies, {nns} namespaces, {npvc} PVCs, {len(WARNINGS)} warnings")


if __name__ == "__main__":
    main()
