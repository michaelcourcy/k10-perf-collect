#!/usr/bin/env bash
# Standalone cluster inventory. Python's standard library handles JSON and timeouts.
set -euo pipefail
exec python3 - "$@" <<'PY'
import argparse
import json
import re
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

parser = argparse.ArgumentParser(prog="scan-all-pvc.sh", description="Scan all PVCs; JSON on stdout, progress on stderr.")
parser.add_argument("--namespace", "-n", help="Limit to one namespace (default: all)")
parser.add_argument("--context", help="kubectl context (default: current context)")
parser.add_argument("--timeout", type=int, default=180, help="Seconds per kubectl operation (default: 180)")
parser.add_argument("--image", default="busybox:1.37.0", help="Inspector image with sh, df, find, stat, du, awk, mktemp, timeout")
parser.add_argument("--no-inspector", action="store_true", help="Only use existing running containers")
parser.add_argument("--network-storage-class", action="append", default=[], metavar="NAME",
                    help="Force tree walking for a storage class; repeatable")
args = parser.parse_args()
if args.timeout <= 0:
    parser.error("--timeout must be positive")
if not shutil.which("kubectl"):
    parser.error("kubectl is required")
base = ["kubectl"] + (["--context", args.context] if args.context else [])


def kubectl(*words, payload=None, command_timeout=None):
    try:
        result = subprocess.run(base + list(words), input=payload, text=True,
                                capture_output=True, timeout=command_timeout or args.timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("kubectl timed out after %ss" % args.timeout) from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "kubectl failed")
    return result.stdout


def human(size):
    if size is None:
        return None
    for unit, factor in [("Ti", 2**40), ("Gi", 2**30), ("Mi", 2**20)]:
        if size >= factor or unit == "Mi":
            return "%.3f %s" % (size / factor, unit)


def quantity(value):
    if value is None:
        return None
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[KMGTPE]i|[numkKMGTPE]?)", value)
    if not match:
        return None
    number, suffix = match.groups()
    if suffix.endswith("i"):
        factor = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
    elif suffix.startswith(("e", "E")) and len(suffix) > 1:
        factor = Decimal(10) ** int(suffix[1:])
    else:
        factor = Decimal(10) ** {"": 0, "n": -9, "u": -6, "m": -3, "k": 3, "K": 3,
                                  "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}[suffix]
    return int(Decimal(number) * factor)


def parse_df(output):
    # -P forces one data line; split at most five times to preserve mount paths with spaces.
    fields = output.strip().splitlines()[-1].split(None, 5)
    if len(fields) != 6:
        raise ValueError("Unrecognized df output: " + output.strip())
    values = [int(x) if x.isdigit() else None for x in fields[1:4]]
    percent = fields[4].rstrip("%")
    values.append(float(percent) if re.fullmatch(r"\d+(?:\.\d+)?", percent) else None)
    return values


def measure(ns, pod, container, path):
    out = kubectl("-n", ns, "exec", pod, "-c", container, "--", "sh", "-c",
                  'df -Pk "$1" || exit; printf "\\n__INODES__\\n"; df -Pi "$1"', "scan", path)
    blocks, inodes = out.split("\n__INODES__\n", 1)
    capacity, used, available, percent = parse_df(blocks)
    total_inodes, used_inodes, free_inodes, inode_percent = parse_df(inodes)
    if capacity is None or used is None or available is None:
        raise ValueError("df did not return numeric filesystem usage")
    used *= 1024
    average = round(used / used_inodes, 2) if used_inodes else None
    return dict(filesystem_capacity_bytes=capacity * 1024, filesystem_capacity=human(capacity * 1024),
                used_bytes=used, used=human(used), available_bytes=available * 1024,
                occupation_percent=percent, inodes_total=total_inodes, inodes_used=used_inodes,
                inodes_free=free_inodes, inode_occupation_percent=inode_percent,
                average_bytes_per_used_inode=average, average_size=human(average))


# No pipeline around find/du: preserve failures instead of reporting partial counts.
# stat emits one size per file, so filenames containing newlines are safe.
WALK_SCRIPT = r'''set -eu
export LC_ALL=C
for tool in find stat du awk mktemp; do
    command -v "$tool" >/dev/null || exit 1
done
tmp=$(mktemp -d "${TMPDIR:-/tmp}/pvc-scan.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
find "$1" -xdev -type f -exec sh -c '
    stat -c "%s" "$@" || { echo ERROR; exit 1; }
' scan {} + > "$tmp/files"
du -skx "$1" > "$tmp/du"
awk '
    /^[0-9]+$/ { n++; bytes += $1; next }
    { bad=1 }
    END { if (bad) exit 1; printf "%.0f %.0f\n", n, bytes }
' "$tmp/files"
awk 'NR == 1 { print $1 }' "$tmp/du"
'''


def measure_network(ns, pod, container, path, capacity):
    # A remote timeout also stops the walk if the local kubectl connection closes.
    out = kubectl("-n", ns, "exec", pod, "-c", container, "--",
                  "timeout", "-s", "TERM", "-k", "5", str(args.timeout),
                  "sh", "-c", WALK_SCRIPT, "scan", path,
                  command_timeout=args.timeout + 15)
    if not re.fullmatch(r"\s*\d+ \d+\n\d+\s*", out):
        raise ValueError("Invalid tree-walk output: " + out.strip())
    files, logical_bytes, used_kib = map(int, out.split())
    used = used_kib * 1024
    average = round(logical_bytes / files, 2) if files else None
    return dict(measurement="tree-walk", file_count=files, logical_file_bytes=logical_bytes,
                used_bytes=used, used=human(used),
                occupation_percent=round(100 * used / capacity, 2) if capacity else None,
                occupation_basis="du allocated bytes / provisioned PVC capacity",
                filesystem_capacity_bytes=None, filesystem_capacity=None, available_bytes=None,
                inodes_total=None, inodes_used=None, inodes_free=None, inode_occupation_percent=None,
                average_bytes_per_used_inode=None, average_file_bytes=average, average_size=human(average))


def measure_pvc(ns, pod, container, path, network, capacity):
    if network:
        return measure_network(ns, pod, container, path, capacity)
    metrics = measure(ns, pod, container, path)
    metrics.update(measurement="df", file_count=None, logical_file_bytes=None,
                   average_file_bytes=None, occupation_basis="df Use%")
    return metrics


def mounts(pods, pvc):
    for pod in pods:
        if pod.get("status", {}).get("phase") != "Running" or pod["metadata"].get("deletionTimestamp"):
            continue
        volumes = {v["name"] for v in pod["spec"].get("volumes", [])
                   if v.get("persistentVolumeClaim", {}).get("claimName") == pvc}
        running = {c["name"] for c in pod.get("status", {}).get("containerStatuses", [])
                   if "running" in c.get("state", {})}
        for container in pod["spec"].get("containers", []):
            if container["name"] not in running:
                continue
            for mount in container.get("volumeMounts", []):
                if mount["name"] in volumes and not ("subPath" in mount or "subPathExpr" in mount):
                    yield pod["metadata"]["name"], container["name"], mount["mountPath"]


def inspect(ns, pvc, network, capacity):
    name = "pvc-scan-" + uuid.uuid4().hex[:16]
    manifest = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns},
                "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": args.timeout * 3,
                         "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [{"name": "inspector", "image": args.image,
                             "env": [{"name": "TMPDIR", "value": "/tmp"}],
                             "command": ["sh", "-c", "sleep %d" % (args.timeout * 3)],
                             "securityContext": {"allowPrivilegeEscalation": False,
                                 "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True},
                             "volumeMounts": [{"name": "target", "mountPath": "/target", "readOnly": True},
                                              {"name": "scratch", "mountPath": "/tmp"}]}],
                         "volumes": [{"name": "target", "persistentVolumeClaim": {"claimName": pvc, "readOnly": True}},
                                     {"name": "scratch", "emptyDir": {}}]}}
    # OpenShift supplies its namespace UID; ordinary Kubernetes needs an explicit non-root UID.
    namespace = json.loads(kubectl("get", "namespace", ns, "-o", "json"))
    if not namespace["metadata"].get("annotations", {}).get("openshift.io/sa.scc.uid-range"):
        manifest["spec"]["securityContext"]["runAsUser"] = 65534
    try:
        kubectl("create", "-f", "-", payload=json.dumps(manifest))
        kubectl("-n", ns, "wait", "--for=condition=Ready", "pod/" + name, "--timeout=%ss" % args.timeout)
        return measure_pvc(ns, name, "inspector", "/target", network, capacity)
    finally:
        try:
            kubectl("-n", ns, "delete", "pod", name, "--ignore-not-found", "--wait=false")
        except RuntimeError as exc:
            print("Inspector cleanup failed for %s/%s: %s" % (ns, name, exc), file=sys.stderr)


def main():
    scope = ["-n", args.namespace] if args.namespace else ["-A"]
    pvcs = json.loads(kubectl("get", "pvc", *scope, "-o", "json"))["items"]
    classes = json.loads(kubectl("get", "storageclass", "-o", "json"))["items"]
    provisioners = {sc["metadata"]["name"]: sc.get("provisioner", "") for sc in classes}
    network_pattern = re.compile(r"nfs|smb|cifs|file\.csi|azurefile|efs|filestore|gluster|cephfs", re.I)
    pod_cache = {}
    results = []
    for pvc in pvcs:
        ns, name = pvc["metadata"]["namespace"], pvc["metadata"]["name"]
        spec, status = pvc["spec"], pvc.get("status", {})
        size = status.get("capacity", {}).get("storage")
        storage_class = spec.get("storageClassName")
        provisioner = provisioners.get(storage_class)
        # Unknown/static classes are walked conservatively rather than trusting export-wide df.
        network = (not provisioner or bool(network_pattern.search(provisioner)) or
                   storage_class in args.network_storage_class)
        row = dict(provisioner=provisioner, measurement_strategy="tree-walk" if network else "df", namespace=ns, pvc=name, storage_class=spec.get("storageClassName"),
                   phase=status.get("phase"), requested_size=spec.get("resources", {}).get("requests", {}).get("storage"),
                   size=human(quantity(size)), size_bytes=quantity(size),
                   status="unavailable", method=None, error=None, metrics=None)
        results.append(row)
        print("Scanning %s/%s" % (ns, name), file=sys.stderr)
        try:
            if status.get("phase") != "Bound":
                raise RuntimeError("PVC is not Bound")
            if spec.get("volumeMode", "Filesystem") == "Block":
                raise RuntimeError("Raw block PVC has no filesystem for df")
            if ns not in pod_cache:
                pod_cache[ns] = json.loads(kubectl("-n", ns, "get", "pods", "-o", "json"))["items"]
            pods = pod_cache[ns]
            failures = []
            for pod, container, path in mounts(pods, name):
                try:
                    row["metrics"] = measure_pvc(ns, pod, container, path, network, quantity(size))
                    row["method"] = "pod-exec"
                    break
                except (RuntimeError, ValueError) as exc:
                    failures.append(str(exc))
            if row["metrics"] is None:
                if args.no_inspector:
                    raise RuntimeError("No usable existing mount. " + "; ".join(failures))
                exclusive = bool(set(spec.get("accessModes", [])) & {"ReadWriteOnce", "ReadWriteOncePod"})
                in_use = any(p.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
                             any(v.get("persistentVolumeClaim", {}).get("claimName") == name
                                 for v in p["spec"].get("volumes", [])) for p in pods)
                if exclusive and in_use:
                    raise RuntimeError("Exclusive PVC referenced by another pod; no usable root mount. " + "; ".join(failures))
                row["metrics"] = inspect(ns, name, network, quantity(size))
                row["method"] = "inspector"
            row["status"] = "ok"
        except (RuntimeError, ValueError, KeyError) as exc:
            row["error"] = str(exc)
    json.dump(dict(generated_at=datetime.now(timezone.utc).isoformat(),
                   measurement_notes=["Network and unknown storage use find/stat for regular-file counts and logical sizes, and du for allocated usage.",
                                      "Network inodes_used is null: df inode counts can describe the entire shared export.",
                                      "Tree-walk occupation is allocated bytes / provisioned PVC capacity; it can exceed 100% on unquotaed exports.",
                                      "Tree-walk average_file_bytes is logical regular-file bytes / file_count; hard-linked paths count separately.",
                                      "Other storage uses df: inodes_used includes directories and symlinks; its average is an estimate.",
                                      "Provisioner detection is heuristic; use --network-storage-class for unrecognized shared storage.",
                                      "Measurements are live, not an atomic snapshot."],
                   pvcs=results), sys.stdout, indent=2)
    print()
    return 2 if any(row["status"] != "ok" for row in results) else 0


def interrupted(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, interrupted)
try:
    sys.exit(main())
except (RuntimeError, ValueError, KeyError) as exc:
    print("scan-all-pvc: " + str(exc), file=sys.stderr)
    sys.exit(1)
except KeyboardInterrupt:
    print("scan-all-pvc: interrupted", file=sys.stderr)
    sys.exit(130)
PY
