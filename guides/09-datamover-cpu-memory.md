# 09 — Datamover CPU and RAM consumption

## What Global Engineering needs

What the datamover pods actually consume in CPU, RSS and ephemeral storage, and — as far
as possible — which PVC each figure belongs to.

## The cAdvisor attribution constraint, and how far it can be worked around

The starting position was: cAdvisor is enabled by default on OpenShift, but it does not
copy pod labels into its metrics, so only the pod name (`data-mover`) and the namespace
(`kasten-io`) look available for attribution.

That is correct. Confirmed on the validation cluster — the full label set on a cAdvisor
series is:

```
container, cpu, endpoint, id, image, instance, job, metrics_path, name,
namespace, node, pod, prometheus, service
```

No pod labels. **But `pod` is there, and that is enough**, because K10 puts the
attribution into the pod's *labels*, which can be captured while the pod still exists
and then joined on `pod` afterwards. Verified label sets:

```
data-mover-svc-b6kr7   app-name=basic-app  policy-name=basic-app-backup
                       k10.kasten.io/jobID=f09cab38-b107-11f1-bdad-0a580a8103a7
                       service=data-mover-svc  createdBy=kanister

copy-vol-data-259bj    k10.kasten.io/migrationOp=kopiaCopyVolumeData
                       k10.kasten.io/jobID=f09cab38-b107-11f1-bdad-0a580a8103a7
                       createdBy=Kasten-K10
```

So the join chain is:

```
cAdvisor series (pod=...)  →  captured pod labels (app-name, policy-name, jobID)  →  namespace + policy
```

Per-**PVC** attribution is one step harder and only partly achievable — see
[Per-PVC attribution](#per-pvc-attribution) below.

## Three independent sources, in order of usefulness

| Source | Granularity | Attribution | Survives pod deletion |
|--------|------------|-------------|----------------------|
| cAdvisor via Thanos | per container, 30 s samples | via captured labels | yes, in TSDB |
| K10 worker-pod metric sidecar | per `podType` | `podType` only | in K10's Prometheus |
| kubelet summary API | per pod, including `emptyDir` | pod name | no — poll live |

## Setup

```bash
. lib/init.sh
```

Sourcing `lib/init.sh` exports `K10NS`, `AUDIT_DIR`, `CLUSTER_UID`, the metrics window
(`AUDIT_WINDOW_DAYS`, `AUDIT_START`, `AUDIT_END`, `AUDIT_RANGE`) and the query helpers
(`tq`, `tqr`, `kq`, `pf_start`, `pf_stop`). It is idempotent — run it at the start of
every guide and in every new terminal. See [00-prerequisites.md](00-prerequisites.md).

## Method

```bash
mkdir -p "$AUDIT_DIR/09-datamover" && cd "$AUDIT_DIR/09-datamover"
```

### Step 1 — start the label capture loop *before* triggering the policy

This is the part that makes the rest work. Datamover pods on the validation cluster
lived **under 60 seconds**. If you start capturing after the run, there is nothing left
to capture.

```bash
cat > capture-datamover-pods.sh <<'SH'
#!/usr/bin/env bash
# Poll kasten-io for worker pods and persist their spec + labels before they vanish.
K10NS=${K10NS:-kasten-io}
OUT=${1:-./dm-specs}
DUR=${2:-1800}          # seconds to keep watching
mkdir -p "$OUT"
end=$(( $(date +%s) + DUR ))
while [ "$(date +%s)" -lt "$end" ]; do
  for p in $(kubectl -n "$K10NS" get pods -o name 2>/dev/null \
             | grep -E 'data-mover|copy-vol-data|create-repo|repository-server|restore-data|check-repo'); do
    n=$(basename "$p")
    [ -f "$OUT/$n.json" ] || {
      kubectl -n "$K10NS" get "$p" -o json > "$OUT/$n.json" 2>/dev/null && echo "captured $n"
    }
  done
  sleep 2
done
SH
chmod +x capture-datamover-pods.sh
./capture-datamover-pods.sh ./dm-specs 1800 &
CAPTURE_PID=$!
```

A `kubectl get pods -w` watch is lighter but loses pods that appear and disappear
between events under load; the 2-second poll above captured all 5 worker pods of a real
run.

### Step 2 — trigger the workload

```bash
cat <<YAML | kubectl create -f -
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: audit-run-
  namespace: $K10NS
spec:
  subject:
    kind: Policy
    name: <POLICY_NAME>
    namespace: $K10NS
YAML

# note the window — you need it for the range queries
RUN_START=$(date +%s)
```

Wait for completion, then:

```bash
kubectl -n "$K10NS" get exportactions,backupactions -A \
  -o custom-columns='KIND:.kind,NAME:.metadata.name,STATE:.status.state,START:.status.startTime,END:.status.endTime' \
  | tee action-windows.tsv

RUN_END=$(date +%s)
kill $CAPTURE_PID 2>/dev/null
```

### Step 3 — build the pod → attribution map

```bash
for f in dm-specs/*.json; do
  jq -r '[ .metadata.name,
           (.metadata.labels["app-name"]            // "-"),
           (.metadata.labels["policy-name"]          // "-"),
           (.metadata.labels["k10.kasten.io/jobID"]  // "-"),
           (.metadata.labels["k10.kasten.io/migrationOp"] // "-"),
           .spec.nodeName,
           ([.spec.volumes[]? | select(.persistentVolumeClaim) | .persistentVolumeClaim.claimName] | join(",") ),
           ([.spec.containers[].name] | join(",")),
           ([.spec.containers[] | (.resources | tostring)] | join(" | "))
         ] | @tsv' "$f"
done | { printf 'POD\tAPP_NS\tPOLICY\tJOB_ID\tMIGRATION_OP\tNODE\tMOUNTED_PVC\tCONTAINERS\tRESOURCES\n'; cat; } \
  | tee datamover-attribution.tsv | column -t -s "$(printf '\t')"
```

### Step 4 — cAdvisor: peak RSS and CPU seconds per datamover pod

```bash
# widen the window slightly — cAdvisor samples every 30 s
S=$((RUN_START - 120)); E=$((RUN_END + 120))

tqr 'max by (pod, container) (container_memory_working_set_bytes{namespace="'"$K10NS"'",pod=~"data-mover.*|copy-vol-data.*|create-repo.*|repository-server.*|restore-data.*",container!="",container!="POD"})' "$S" "$E" 15s \
  | jq -r '(["POD","CONTAINER","PEAK_WS_MiB"]|@tsv),
           (.data.result[] | [ .metric.pod, .metric.container,
                ((.values|map(.[1]|tonumber)|max)/1048576*100|round/100) ] | @tsv)' \
  | tee datamover-peak-memory.tsv | column -t

tqr 'max by (pod, container) (container_cpu_usage_seconds_total{namespace="'"$K10NS"'",pod=~"data-mover.*|copy-vol-data.*|create-repo.*|repository-server.*|restore-data.*",container!="",container!="POD"})' "$S" "$E" 15s \
  | jq -r '(["POD","CONTAINER","CPU_SECONDS"]|@tsv),
           (.data.result[] | (.values|map(.[1]|tonumber)) as $v
            | [ .metric.pod, .metric.container, (($v|max)-($v|min)|.*1000|round/1000) ] | @tsv)' \
  | tee datamover-cpu-seconds.tsv | column -t
```

Then join with step 3 to get the namespace and policy:

```bash
join -t"$(printf '\t')" -1 1 -2 1 \
  <(tail -n +2 datamover-peak-memory.tsv | sort -k1,1) \
  <(tail -n +2 datamover-attribution.tsv | cut -f1,2,3,6 | sort -k1,1) \
  | { printf 'POD\tCONTAINER\tPEAK_WS_MiB\tAPP_NS\tPOLICY\tNODE\n'; cat; } \
  | tee datamover-usage-attributed.tsv | column -t -s "$(printf '\t')"
```

Container names to expect: `container` is the Kopia/kanister-tools process — the one
that matters — and `metric-sidecar` is K10's metrics pusher. `POD` is the pause
container and is always ~0.

### Step 5 — K10's own worker-pod metric sidecar

K10 runs a `metric-sidecar` container in every worker pod which pushes process metrics
to `metering-svc`, where K10's Prometheus scrapes them. This works even without cluster
monitoring, and it samples every 30 s rather than depending on cAdvisor's cadence.

Check it is enabled:

```bash
kubectl -n "$K10NS" get cm k10-config -o json \
  | jq -r '.data | {WorkerPodMetricSidecarEnabled, WorkerPodMetricSidecarMetricLifetime, WorkerPodPushgatewayMetricsInterval}'
```

Validated values: `true`, `2m`, `30s`.

Read it live during a run:

```bash
# pf_start / pf_stop come from guide 00 section 4
pf_start metering-svc 18000:8000 || exit 1
curl -s http://localhost:18000/v0/push-metric-agg/metrics | tee worker-pod-metrics-raw.txt
pf_stop
```

Validated output, captured during a real export:

```
process_resident_memory_bytes{podType="create-repository"}          6.3787008e+07
process_resident_memory_bytes{podType="repository-server"}          1.21389056e+08
process_resident_memory_bytes{podType="repository-operations"}      2.47058432e+08
process_resident_memory_bytes{podType="export-volume-to-repository"} 1.25087744e+08

process_cpu_seconds_total{podType="create-repository"}              0.04
process_cpu_seconds_total{podType="repository-server"}              0.08
process_cpu_seconds_total{podType="repository-operations"}          0.19
process_cpu_seconds_total{podType="export-volume-to-repository"}    0.11
```

Or from K10's Prometheus, which retains it:

```bash
kq '{job="pushAggregator", __name__=~"process_resident_memory_bytes|process_cpu_seconds_total"}' \
  | jq -r '.data.result[] | [ .metric.__name__, .metric.podType, .value[1] ] | @tsv' \
  | tee worker-pod-metrics.tsv | column -t
```

`podType` is the useful dimension here: `export-volume-to-repository` is the actual data
movement, `repository-operations` is maintenance, `create-repository` is one-off setup.
On the validation cluster `repository-operations` was the heaviest at 247 MiB RSS.

**Limitation**: these series carry `podType` and nothing else — no pod name, no
namespace, no PVC. With several concurrent datamovers of the same `podType`, the
aggregator holds only the most recent push per `podType`, so concurrent values overwrite
each other. Use this source for *per-podType* profiling and step 4 for *per-pod*
figures.

Also note `WorkerPodMetricSidecarMetricLifetime = 2m`: values expire from the
aggregator endpoint two minutes after the pod stops pushing. Scrape live, or rely on
K10's Prometheus having captured them.

### Step 6 — ephemeral storage per datamover pod

Neither cAdvisor nor the sidecar covers `emptyDir`. Poll the kubelet during the run:

```bash
cat > capture-ephemeral.sh <<'SH'
#!/usr/bin/env bash
K10NS=${K10NS:-kasten-io}; DUR=${1:-900}
end=$(( $(date +%s) + DUR ))
while [ "$(date +%s)" -lt "$end" ]; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  for n in $(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
    kubectl get --raw "/api/v1/nodes/$n/proxy/stats/summary" 2>/dev/null \
      | jq -r --arg ts "$ts" --arg node "$n" --arg ns "$K10NS" '.pods[]?
          | select(.podRef.namespace == $ns)
          | select(.podRef.name | test("data-mover|copy-vol-data|create-repo|repository-server|restore-data"))
          | [ $ts, $node, .podRef.name,
              ((.["ephemeral-storage"].usedBytes // 0)/1048576|round),
              ((.["ephemeral-storage"].inodesUsed // 0)) ] | @tsv'
  done
  sleep 15
done
SH
chmod +x capture-ephemeral.sh
./capture-ephemeral.sh 900 \
  | { printf 'TIME\tNODE\tPOD\tEPHEMERAL_MiB\tINODES\n'; cat; } \
  | tee datamover-ephemeral.tsv
```

Then take the max per pod:

```bash
tail -n +2 datamover-ephemeral.tsv \
  | awk -F'\t' '{ if ($4+0 > m[$3]) m[$3]=$4+0 } END { for (p in m) printf "%s\t%d\n", p, m[p] }' \
  | { printf 'POD\tPEAK_EPHEMERAL_MiB\n'; cat; } | column -t
```

## Per-PVC attribution

How far each source gets you:

| Level | Achievable | How |
|-------|-----------|-----|
| namespace | **yes** | `app-name` label on `data-mover-svc-*` pods, joined on `pod` |
| policy | **yes** | `policy-name` label |
| job / action | **yes** | `k10.kasten.io/jobID` label, shared by all pods of one run |
| PVC | **partially** | see below |

A `copy-vol-data-*` pod handles exactly one volume, and it does mount a PVC — but it
mounts an **ephemeral clone**, not the source. Verified: the pod mounted
`kanister-pvc-tgg7j`, a temporary PVC created from the source volume's snapshot and
deleted when the job ends. The source PVC name is not in the datamover pod's labels or
spec.

To close that last gap you must capture the clone PVCs while they exist:

```bash
# run alongside step 1
while true; do
  kubectl get pvc -A -o json \
    | jq -r '.items[] | select(.metadata.name | startswith("kanister-pvc"))
             | [ (now|strftime("%Y-%m-%dT%H:%M:%SZ")), .metadata.namespace, .metadata.name,
                 (.spec.dataSource.name // "-"), (.spec.dataSource.kind // "-"),
                 (.metadata.annotations | tostring) ] | @tsv'
  sleep 3
done | tee clone-pvc-map.tsv
```

The clone's `spec.dataSource` points at the VolumeSnapshot, whose
`spec.source.persistentVolumeClaimName` is the source PVC. Capture VolumeSnapshots in
the same loop.

**If that is too invasive, do not force it.** Take per-PVC *work* from Kopia instead
(guides 04–06: exact file counts, bytes and deltas per PVC) and per-pod *resource
consumption* from step 4. Attribute resources at namespace/job level and correlate with
Kopia's per-PVC workload. That combination answers the sizing question without needing a
live clone-PVC watcher.

## Caveats

- **Short-lived pods fall between cAdvisor samples.** The kubelet scrapes cAdvisor every
  30 s by default. A 20-second datamover may produce one sample or none — on the
  validation cluster only 1 of 4 worker pods yielded a usable memory series (84 MiB
  peak, 18 MiB for its sidecar); the others read 0. **Peak RSS from a short run is a
  floor, not a peak.** For meaningful figures, run the test policy against a namespace
  big enough to keep the datamover alive for several minutes.
- **Datamover pods have no resource requests or limits.** Verified: `resources: {}` on
  every container of every worker pod. Consequences: QoS class BestEffort, so they are
  evicted first under node pressure; no OOM limit, so a runaway datamover takes node
  memory from everything else; and the scheduler places them blind. Record this — it is
  a finding, not just a data point.
- **`workerPodResourcesCRDEnabled = false`** on the validation cluster. That is the
  switch which lets an `ActionPodSpec` set datamover resources (guide 10). While it is
  off, ActionPodSpec resource settings are ignored.
- Sum `container` and `metric-sidecar` when reporting a pod's total footprint; the
  sidecar was 18 MiB, not negligible at 10× concurrency.
- The `container_memory_working_set_bytes` peak excludes page cache reclaim; for
  OOM-risk assessment compare against `container_memory_max_usage_bytes` where
  available.

## What to send back

| File | Contents |
|------|----------|
| `datamover-usage-attributed.tsv` | peak RSS and CPU per pod, joined to namespace and policy (headline) |
| `datamover-attribution.tsv` | pod → labels → node → mounted clone PVC → resources |
| `datamover-peak-memory.tsv`, `datamover-cpu-seconds.tsv` | raw cAdvisor extracts |
| `worker-pod-metrics.tsv` | per-`podType` RSS and CPU from K10's own sidecar |
| `datamover-ephemeral.tsv` | ephemeral-storage timeline per datamover pod |
| `dm-specs/*.json` | full pod specs, the evidence for the `resources: {}` finding |
| `action-windows.tsv` | start/end of each action, for range queries |

## Validation status

Steps 1–6 fully validated on K10 9.0.5 by running a real policy end to end and
capturing everything. Specifically confirmed:

- the cAdvisor label set contains `pod` but no pod labels — the join in step 4 is
  necessary and sufficient for namespace-level attribution;
- all 5 worker pods of a real run (`data-mover-svc` ×2, `copy-vol-data` ×2,
  `create-repo` ×1) were captured by the step 1 loop, with their labels;
- every worker-pod container had `resources: {}`;
- the metric sidecar endpoint returned live per-`podType` RSS and CPU during the run and
  went empty two minutes after;
- `copy-vol-data` pods mount an ephemeral `kanister-pvc-*` clone, not the source PVC.

Not validated: the clone-PVC watcher in [Per-PVC attribution](#per-pvc-attribution) was
reasoned from captured pod specs, not executed — the clones were already gone by the
time it was written. Treat that snippet as untested.
