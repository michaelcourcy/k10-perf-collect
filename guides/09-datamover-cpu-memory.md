# 09 — Datamover CPU and RAM consumption

## What auditors need

What the datamover pods of `$AUDIT_NS` actually consumed in CPU, RSS and ephemeral
storage during its exports — and how much of the cluster's datamover load was
*somebody else's*.

## The attribution problem, and how far it can be solved

cAdvisor carries **no pod labels**. Confirmed: the full label set on a
`container_memory_working_set_bytes` series is

```
container, cpu, endpoint, id, image, instance, job, metrics_path, name,
namespace, node, pod, prometheus, service
```

So a series says "some pod in `kasten-io`", never "the export of `prod-test`".

**But `pod` is there, and that is enough.** kube-state-metrics publishes pod labels as
`label_*` on `kube_pod_labels`, and K10 labels its worker pods with the attribution:

| Pod | Labels it carries |
|---|---|
| `data-mover-svc-*` | `app-name` (the **application** namespace), `policy-name`, `k10.kasten.io/jobID` |
| `copy-vol-data-*` | `k10.kasten.io/jobID` only |

A `copy-vol-data` pod — the one actually reading the volume — inherits its namespace
from the `data-mover-svc` pod of the **same job**. The join chain is:

```
cAdvisor series (pod=...) → kube_pod_labels (label_app_name, label_k10_kasten_io_job_id) → namespace
```

This needs no capture loop and works on history. It replaces the live pod-watch that
earlier versions of this guide relied on — that loop is still in step 5, for a
controlled test run.

**Per-PVC attribution is not achievable.** A `copy-vol-data` pod handles one volume and
mounts an ephemeral `kanister-pvc-*` clone, not the source; the source PVC name is in
neither its labels nor its spec. Take per-PVC *work* from Kopia (guides 04–06) and
per-job *resources* from here.

## Method

```bash
. lib/init.sh
focus_dir 09-datamover
```

### Step 1 — can anything be attributed at all?

```bash
ksm_pod_labels_check | tee ksm-check.txt
```

Validated: `kube_pod_labels: pod labels ARE exposed (270 label_* keys) - attribution works`.

OpenShift runs kube-state-metrics with `--metric-labels-allowlist=pods=[*]`, so this
works out of the box. On another Prometheus stack it will report **NO pod labels**, and
then every figure below is per pod only. Say so in the report rather than attributing
by guesswork; the fix is
`--metric-labels-allowlist=pods=[app-name,policy-name,k10.kasten.io/jobID]` on
kube-state-metrics.

### Step 2 — usage for one of the pair's exports

```bash
export_actions | head -5 | column -t
datamover_for_export scheduled-gn9w65njrc | tee datamover-usage.txt
```

Validated:

```
window            : 2026-09-20T08:00:07Z .. 2026-09-20T08:05:30Z (323s, includes 45s padding each side)
namespace         : prod-test
pods              : 3
peak sum memory   : 742.96 MiB
cpu total         : 107.239 cpu-s  (avg 0.33 cores over the window)
concurrent        : kasten-io,large-test,test-calibrate
all datamovers    : 961.32 MiB peak - the load the cluster actually carried

POD                   APP_NS          POLICY            JOB_ID        SCOPE         PEAK_MEM_MiB  CPU_SECONDS
copy-vol-data-2vsvp   prod-test       -                 665c3024-...  own           250.14        39.864
copy-vol-data-d8vth   large-test      -                 6c99a162-...  concurrent    11.91         0
copy-vol-data-gjcm8   test-calibrate  -                 66608f11-...  concurrent    0             no-sample
create-repo-tgv7d     -               -                 -             unattributed  110.05        0
data-mover-svc-hxjxk  test-calibrate  -                 66608f11-...  concurrent    115.18        0.001
data-mover-svc-kl7dx  large-test      -                 6c99a162-...  concurrent    116.73        4.585
data-mover-svc-n6lfg  prod-test       -                 665c3024-...  own           492.81        67.375
data-mover-svc-wlzt4  kasten-io       calibrate-backup  5260458b-...  concurrent    129.62        1.501
```

Read it in three parts.

**`own` versus `concurrent`.** `calibrate-backup` selects three namespaces (guide 01
step 3), so one firing exports all three at once. Attributing all eight pods to
`prod-test` would triple its apparent cost; reporting only its own hides what the
cluster carried. Both numbers are printed: 743 MiB for this namespace, 961 MiB for
everything. Size the nodes against the second, charge the namespace the first.

**`peak sum memory` is not the sum of the peaks.** It is the maximum, over the window,
of memory summed across the pods at each step — 743 MiB, against 250 + 493 + 110 = 853
if you added the individual peaks of pods that never peaked together.

**CPU is CPU-seconds, not a rate.** 107 cpu-s over a 323 s window is 0.33 cores on
average. It comes from the cumulative counter `container_cpu_usage_seconds_total`, per
pod `max − min` inside the window. A `rate()` needs several samples per pod, and these
pods live seconds to minutes against a 15–30 s scrape interval, so a rate smooths a
short pod towards zero or misses it entirely. The counter difference keeps every second
that was sampled.

`no-sample` is honest reporting: that pod lived less than one scrape interval and left
nothing. **Every figure here is a floor for short exports.**

### Step 3 — across every export of the pair

```bash
export_actions | awk -F'\t' 'NR>1 {print $1}' | while read -r a; do
  printf '=== %s ===\n' "$a"
  datamover_for_export "$a" | head -8
done | tee datamover-all-exports.txt
```

The number to extract is peak memory **per million files**, or per GiB moved: that is
what sizes an `ActionPodSpec`. Cross-reference the window's `HASHED` count from guide
04 step 3.

### Step 4 — K10's own worker-pod metric sidecar

Works without cluster monitoring, samples every 30 s, and survives where cAdvisor
misses a short pod. Check it is on:

```bash
kubectl -n "$K10NS" get cm k10-config -o json \
  | jq -r '.data | {WorkerPodMetricSidecarEnabled, WorkerPodMetricSidecarMetricLifetime,
                    WorkerPodPushgatewayMetricsInterval}'
```

Validated: `true`, `2m`, `30s`.

```bash
kq '{job="pushAggregator", __name__=~"process_resident_memory_bytes|process_cpu_seconds_total"}' \
  | jq -r '.data.result[] | [ .metric.__name__, .metric.podType, .value[1] ] | @tsv' \
  | tee worker-pod-metrics.tsv | column -t
```

`podType` is the useful dimension: `export-volume-to-repository` is the actual data
movement, `repository-operations` is maintenance, `create-repository` is one-off setup.

**Limitation**: these series carry `podType` and nothing else — no pod, no namespace,
no PVC — and the aggregator holds only the most recent push per `podType`, so
concurrent datamovers of the same type overwrite each other. With three namespaces
exporting at once, as here, that makes it useless for attribution. Use it for
per-`podType` profiling and step 2 for per-pod figures. Values also expire two minutes
after the pod stops pushing (`WorkerPodMetricSidecarMetricLifetime`), so scrape live or
rely on K10's Prometheus having captured them.

### Step 5 — ephemeral storage, which needs a live capture

Neither cAdvisor nor the sidecar covers `emptyDir`, and the datamover's Kopia cache is
an `emptyDir`. The kubelet has it, but only while the pod exists — so this one must run
*during* an export:

```bash
cat > capture-ephemeral.sh <<'SH'
#!/usr/bin/env sh
K10NS=${K10NS:-kasten-io}
DUR=${1:-900}
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

Peak per pod:

```bash
awk -F'\t' 'NR>1 { if ($4+0 > m[$3]) m[$3]=$4+0 } END { for (p in m) printf "%s\t%d\n", p, m[p] }' \
    datamover-ephemeral.tsv \
  | { printf 'POD\tPEAK_EPHEMERAL_MiB\n'; cat; } | column -t
```

Compare the peak against `k10DataStoreTotalCacheSizeLimitMB` (3000) and against the
node margin from guide 08 step 4.

### Step 6 — trigger an export, if the pair is `@onDemand` or you need a clean window

**This changes cluster state.** Agree a window first.

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
    name: $AUDIT_POLICY
    namespace: $K10NS
YAML
```

Start step 5's loop first, then trigger, then wait for the ExportAction to reach
`Complete` and run steps 2 and 3 against it. Note that triggering `$AUDIT_POLICY`
exports **every namespace it selects**, not just yours — three, here.

## Caveats

- **Datamover pods have no resource requests or limits.** Verified: `resources: {}` on
  every container of every worker pod. Consequences: QoS class BestEffort, so they are
  evicted first under node pressure; no OOM limit, so a runaway datamover takes node
  memory from everything else; and the scheduler places them blind. Record this — it is
  a finding, not a data point.
- **`workerPodResourcesCRDEnabled = false`** makes `ActionPodSpec` resource settings
  inert (guide 10). While it is off, any sizing recommendation needs that flag flipped
  first, and it requires a Helm upgrade — not a ConfigMap edit.
- **Short-lived pods fall between cAdvisor samples.** The kubelet scrapes every 30 s. A
  20-second datamover may produce one sample or none; `no-sample` rows say which. Peak
  RSS from a short run is a **floor**. For meaningful figures, measure an export that
  keeps the datamover alive for minutes.
- **Sum `container` and `metric-sidecar`** when reporting a pod's footprint; the
  sidecar was 18 MiB, not negligible at 10× concurrency. The queries here already sum
  across containers, excluding the pause container and the pod cgroup.
- **The window is padded by 45 s each side** (`DATAMOVER_PAD`). That slightly dilutes
  the average-core figure — 3.5 % on a 40-minute export, much more on a 30-second one —
  which is why `cpu-s` stays visible next to it.

## What to send back

| File | Contents |
|------|----------|
| `datamover-usage.txt` | peak memory and CPU for one export, own versus concurrent (headline) |
| `datamover-all-exports.txt` | the same across the pair's export history |
| `ksm-check.txt` | whether attribution was possible at all |
| `datamover-ephemeral.tsv` | ephemeral-storage timeline per datamover pod |
| `worker-pod-metrics.tsv` | per-`podType` RSS and CPU from K10's own sidecar |

## Validation status

Fully validated on K10 9.0.5 against `prod-test` / `calibrate-backup`, on real
scheduled exports — no test policy was needed, because the historical join works on
retained metrics.

Specifically confirmed: the cAdvisor label set contains `pod` but no pod labels;
`kube_pod_labels` exposes 270 `label_*` keys for the K10 namespace; `copy-vol-data`
pods carry only `k10.kasten.io/jobID` and were correctly resolved to their namespace
through the `data-mover-svc` pod of the same job; three namespaces
(`prod-test`, `large-test`, `test-calibrate`) plus a metadata mover in `kasten-io` were
alive in one 323-second window, giving 743 MiB own against 961 MiB total; one pod
produced no sample at all and is reported as such; every worker-pod container had
`resources: {}`.

Step 4's sidecar endpoint and `k10-config` values were verified. Steps 5 and 6 were
**not** re-run in this pass — the capture loop and the `RunAction` pattern are carried
over from an earlier validated run on this repository, where all five worker pods of a
real run were captured and the sidecar returned live per-`podType` figures that went
empty two minutes later. Step 5 in particular needs an export in flight.
