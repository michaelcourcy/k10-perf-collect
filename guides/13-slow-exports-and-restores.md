# 13 — Exports and restores with a performance or memory/CPU problem

## What auditors need

The concrete list of exports and restores that are slow, that fail, or that consume
abnormal memory or CPU — with enough surrounding data to explain *why*. This is the
output the recommendation is written against; everything in guides 01–12 is input.

## Definition first

"Slow" is meaningless without a baseline. Establish the thresholds **before**
collecting, and write them into the report:

| Signal | Suggested threshold | Rationale |
|--------|--------------------|-----------|
| Export duration | > 3× the median for the same namespace | relative, survives heterogeneous data sizes |
| Export duration | > the policy interval | the backup cannot keep up; this one is absolute |
| Throughput | < 20 MiB/s sustained for a > 10 GiB volume | points at small files or a storage bottleneck |
| Datamover peak RSS | > 1 GiB for a single volume | metadata-bound; likely a small-file volume |
| Any `OOMKilled` datamover | 1 occurrence | with no memory limit set, this means node pressure |
| Retries / restarts | > 0 | usually a timeout from guide 10 |
| Failure rate | > 5 % of runs for one policy | systemic, not transient |

> **This table is a starting point, not a verdict.** Confirm the thresholds with the
> audit owner before presenting anything as a finding — the right numbers depend on the
> customer's RPO commitments and on which namespaces they already consider problematic.

## Method

```bash
. lib/init.sh
focus_dir 13-slow-exports
```

### Step 1 — the pair's exports, with what they moved

The single most useful table in this guide, because it puts duration next to bytes:

```bash
export_table 20 | tee export-performance.tsv | column -t
```

Validated:

```
EXPORT                STATE     START                DUR_S  VOLUMES  TRANSFERRED  READ         CAPACITY     RATE_B_S    CHANGE_RATE
scheduled-gn9w65njrc  Complete  2026-09-20T08:00:52  233    1        10300000000  10200000000  79456894976  219504793   0.2012
scheduled-cdzgsbrfkz  Complete  2026-09-20T02:37:14  47     1        173          0            79456894976  1095333144  0
scheduled-5x9wblcnls  Complete  2026-09-19T22:01:10  236    1        10300000000  10200000000  79456894976  216795966   0.2012
scheduled-qhfwmmm2x4  Complete  2026-09-19T17:00:57  55     1        173          0            79456894976  918134538   0
scheduled-gjmzmxzwqp  Complete  2026-09-19T08:13:43  587    1        51200000000  51200000000  79456894976  87304917    1
```

Derive throughput and rank:

```bash
awk -F'\t' 'NR==1 {print $0"\tMiB_PER_S"; next}
     $4!="-" && $6!="-" && $4+0>0 {printf "%s\t%.1f\n", $0, $6/$4/1048576}' \
    export-performance.tsv | sort -t"$(printf '\t')" -k11 -n | column -t \
  | tee export-throughput.tsv
```

Validated: 42 MiB/s on the two incremental exports, 83 MiB/s on the full one. **The
full export is faster per byte than the incrementals** — the incrementals pay the same
100,003-file enumeration to move a fifth of the data. That is the file-count-bound
signature, and no datamover tuning fixes it.

> **The byte counters are on the `/details` subresource only.** On the ExportAction
> object `status.progressDetails` and `status.actionDetails` are both `null`, which is
> why they were long believed not to exist. `export_table` fetches
> `GET /apis/actions.kio.kasten.io/v1alpha1/namespaces/<ns>/exportactions/<name>/details`
> per action — about 0.2 s and 400 kB each, so bound it with the argument.

`CAPACITY` is the **volume capacity**, not the data size. `READ` 0 with `TRANSFERRED`
173 is a genuine no-op export, not a failure.

Per-volume detail for one export — the only place the export names its PVCs:

```bash
export_volumes scheduled-gn9w65njrc | tee export-volumes.tsv | column -t
```

```
PVC                   OPERATION  DATA_FORMAT  EXPORT_DIRECTIVE  STORAGE_CLASS  STORAGE_TYPE  SNAPSHOT_ID
calibrate-100k-500kb  Upload     Filesystem   FileSystemMode    managed-csi    CSI           k10-csi-snap-847jnt89b4zdw6wm
```

`dataFormat: Filesystem` versus `Block` decides whether guides 04 and 05 apply at all.

### Step 1b — queue time versus run time

An export that "took 4 hours" may have spent 3 h 50 m waiting for a limiter slot. The
job history separates the two:

```bash
pf_start jobs-svc 18081:8000 || exit 1
curl -s http://localhost:18081/v0/jobs > jobs-raw.json
pf_stop

jq -r '
  # jobs-svc emits fractional seconds; fromdateiso8601 rejects them, so strip them first.
  def ts: sub("\\.[0-9]+(?=Z$)";"") | fromdateiso8601;
  (["ID","STATUS","QUEUE_S","RUN_S","WAIT_COUNT","POLICY_ID"]|@tsv),
  ( .[]
    | (if .scheduledTime and .startedTime
         then ((.startedTime|ts) - (.scheduledTime|ts)) else null end) as $q
    | (if .startedTime and .completeTime
         then ((.completeTime|ts) - (.startedTime|ts)) else null end) as $r
    | [ .id, .status, ($q // "-"), ($r // "-"), (.waitCount // 0),
        (.originatingPolicies[0].id // "-") ] | @tsv )' jobs-raw.json \
  | tee job-durations.tsv | column -t

kubectl -n "$K10NS" get policies.config.kio.kasten.io "$AUDIT_POLICY" \
  -o jsonpath='{.metadata.uid}{"\n"}' | tee policy-uid.txt
```

Queue time dominating run time is the **limiter** signature (guide 02 step 4), not a
datamover problem. Cross-check against the `Skipped` count from guide 02 step 3: on
this cluster 12 of 16 runs were skipped because the previous one had not finished.

Per-phase detail for one slow job — this is what localises the problem:

```bash
JOB=<id from job-durations.tsv>
jq -r --arg id "$JOB" '.[] | select(.id==$id)
       | {id, status, errors, waitCount,
          phases: [.phases[] | {name, status, progress, weight}]}' jobs-raw.json \
  | tee "job-$JOB-phases.json"
```

### Step 2 — failures and errors

```bash
jq -r '(["ID","STATUS","ERRORS"]|@tsv),
       (.[] | select(.status != "succeeded")
        | [ .id, .status, ((.errors // []) | tostring) ] | @tsv)' jobs-raw.json \
  | tee failed-jobs.tsv | column -t

# aggregate counters from the Prometheus bundled with K10
for m in action_export_ended_overall action_backup_ended_overall \
         action_restore_ended_overall action_import_ended_overall; do
  echo "=== $m ==="
  kq "$m" | jq -r '.data.result[] | "\(.metric.state)\t\(.value[1])"'
done | tee action-outcomes.tsv

# mean duration per action type and outcome
for m in action_export_duration_seconds_sum_overall action_backup_duration_seconds_sum_overall \
         action_restore_duration_seconds_sum_overall; do
  echo "=== $m ==="
  kq "$m" | jq -r '.data.result[] | "\(.metric.state)\t\(.value[1])"'
done | tee action-duration-sums.tsv
```

Dividing `..._duration_seconds_sum_overall` by `..._ended_overall` for the same `state`
gives the mean duration per outcome. These counters are **cluster-wide and carry only a
`state` label** — no namespace, no policy. They give you the overall picture; step 1 is
where per-job attribution lives.

### Step 3 — the current state of actions

```bash
for r in exportactions backupactions restoreactions; do
  echo "=== $r in $AUDIT_NS ==="
  kubectl -n "$AUDIT_NS" get "$r.actions.kio.kasten.io" -o json \
    | jq -r '.items[] | [ .metadata.name, .status.state,
                          .status.startTime, .status.endTime, (.status.progress // "-"),
                          ((.status.error // .status.result) | tostring) ] | @tsv'
done | tee actions-current.tsv | column -t

echo "=== anything still running, cluster-wide ==="
kubectl get exportactions.actions.kio.kasten.io -A -o json \
  | jq -r '.items[] | select(.status.state == "Running")
           | [ .metadata.namespace, .metadata.name, .status.startTime ] | @tsv' \
  | tee running-now.tsv | column -t
```

The cluster-wide `Running` list is there on purpose: a stuck export in **another**
namespace holds limiter slots that `$AUDIT_NS` is waiting for.

### Step 4 — OOM kills and restarts of datamover pods

The single most valuable query in this guide, and it survives pod deletion:

```bash
# AUDIT_START / AUDIT_END are exported by lib/init.sh.
# This is the guide where a short window is most misleading: no OOM kills found over
# 2 days of history is not the same finding as none over 15.
echo "window: ${AUDIT_WINDOW_DAYS}d"

# containers terminated by the OOM killer
tqr 'max by (namespace, pod, container) (kube_pod_container_status_last_terminated_reason{reason="OOMKilled"})' \
    "$AUDIT_START" "$AUDIT_END" 5m \
  | jq -r '(["NAMESPACE","POD","CONTAINER"]|@tsv),
           (.data.result[] | select((.values|map(.[1]|tonumber)|max) > 0)
            | [.metric.namespace, .metric.pod, .metric.container] | @tsv)' \
  | tee oomkilled.tsv | column -t

# restart counts on K10 worker pods
tqr 'max by (namespace, pod, container) (kube_pod_container_status_restarts_total{namespace="'"$K10NS"'"})' \
    "$AUDIT_START" "$AUDIT_END" 5m \
  | jq -r '(["NAMESPACE","POD","CONTAINER","RESTARTS"]|@tsv),
           (.data.result[] | (.values|map(.[1]|tonumber)|max) as $m | select($m > 0)
            | [.metric.namespace, .metric.pod, .metric.container, $m] | @tsv)' \
  | tee restarts.tsv | column -t

# failed / evicted pods still in the API
kubectl -n "$K10NS" get pods --field-selector status.phase=Failed -o json \
  | jq -r '.items[] | [ .metadata.name, (.status.reason // "-"), (.status.message // "-") ] | @tsv' \
  | tee failed-pods.tsv
```

Also check for worker pods that failed to start at all — image pull, SCC denial,
scheduling failure:

```bash
kubectl -n "$K10NS" get events --sort-by=.lastTimestamp -o json \
  | jq -r '.items[] | select(.type=="Warning")
           | select(.involvedObject.name | test("data-mover|copy-vol-data|create-repo|repository-server|restore-data|k10tools"))
           | [ .lastTimestamp, .involvedObject.name, .reason, .message ] | @tsv' \
  | tee worker-pod-warnings.tsv | column -t
```

### Step 5 — resource consumption of the slow exports

No capture loop and no job-id arithmetic: guide 09's helper resolves the window from
the ExportAction itself.

```bash
awk -F'\t' 'NR>1 {print $1}' export-performance.tsv | while read -r a; do
  printf '=== %s ===\n' "$a"
  datamover_for_export "$a"
done | tee datamover-per-export.txt
```

Validated, for the slowest and the fastest export of the pair:

```
=== scheduled-gn9w65njrc ===   233 s, 10.3 GB
peak sum memory   : 742.96 MiB
cpu total         : 107.239 cpu-s  (avg 0.33 cores over the window)
concurrent        : kasten-io,large-test,test-calibrate
all datamovers    : 961.32 MiB peak - the load the cluster actually carried

=== scheduled-9gwfn7ktqw ===   150 s, no-op export
peak sum memory   : 365.06 MiB
cpu total         : 0.042 cpu-s
no sample         : copy-vol-data-lc7pj (alive for less than one scrape interval)
```

Two readings. **743 MiB for one PVC of 100,003 files** is the memory-per-file figure
that sizes an `ActionPodSpec` — compare against the 1 GiB threshold in the table above.
And `concurrent` naming three other namespaces means the node saw 961 MiB, not 743: if
you are diagnosing node pressure, that is the number.

A `no-sample` row means the pod lived less than one cAdvisor scrape interval, so the
figures are a **floor**. Absence of a memory series is no evidence at all.

### Step 6 — K10 logs for the slow jobs

```bash
# executor is where action orchestration is logged
# Two traps here, both silent:
#   1. kubectl logs --since takes Go durations only - s/m/h. 15d is rejected with
#      unknown unit d, so convert AUDIT_WINDOW_DAYS to hours.
#   2. logs deploy/NAME reads ONE pod - executor-svc runs 3 replicas and the
#      orchestration log is spread across all of them. Select by label instead and
#      raise --max-log-requests.
# Pod logs are also bounded by pod lifetime and node log rotation, not by
# AUDIT_WINDOW_DAYS — ask for the whole window and accept whatever exists.
SINCE_H="$((AUDIT_WINDOW_DAYS*24))h"
echo "log window: $SINCE_H"

for svc in executor controllermanager jobs; do
  kubectl -n "$K10NS" logs -l "component=$svc" --since="$SINCE_H" \
    --all-containers --prefix --max-log-requests 20 --tail=-1 \
    > "k10-$svc.log" 2>&1
  printf '%-22s %8s lines\n' "$svc" "$(wc -l < "k10-$svc.log")"
done

grep -iE 'error|timeout|throttl|retry|oom|deadline|limiter' k10-executor.log \
  | tail -200 | tee executor-errors.txt
```

For a complete bundle, the supported route is the K10 debug log download from the
dashboard (**Settings → Support → Download logs**), which packages every service. Use
that for anything sent to support.

The Kopia-side per-operation latency is in `logs-show-all-stdout.txt` in the guide 12
diagnose bundle — it records each `PutBlob`/`GetBlob` with its duration in
microseconds, which is how you distinguish a slow object store from a slow datamover:

```bash
jq -rs '[ .[] | select(.m=="PutBlob" and .duration) ]
        | (map(.duration)|add/length) as $mean
        | "PutBlob count=\(length) mean_us=\($mean|round) max_us=\(map(.duration)|max)"' \
   "$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/12-kopia/"*/tmp/kopia-debug-logs/logs-show-all-stdout.txt 2>/dev/null \
  | tee kopia-putblob-latency.txt
```

### Step 7 — targeted test policies (needs a maintenance window)

For namespaces still unexplained after steps 1–6, run a controlled policy and capture
everything. **This and guide 09 are the only steps that change cluster state.**

```bash
# a snapshot+export policy scoped to exactly one namespace
cat <<YAML | kubectl apply -f -
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: audit-<NAMESPACE>
  namespace: $K10NS
  labels: {app.kubernetes.io/managed-by: k10-performance-audit}
spec:
  frequency: '@onDemand'
  selector:
    matchExpressions:
    - key: k10.kasten.io/appNamespace
      operator: In
      values: ["<NAMESPACE>"]
  actions:
  - action: backup
  - action: export
    exportParameters:
      exportData: {enabled: true}
      frequency: '@onDemand'
      profile: {name: <PROFILE>, namespace: $K10NS}
YAML
```

Then, in order:

1. Start the guide 09 step 1 pod-label capture loop.
2. Start the guide 09 step 6 ephemeral-storage capture loop.
3. Record `RUN_START=$(date +%s)`.
4. Trigger with a `RunAction` (guide 09 step 2).
5. Wait for the action to reach `Complete` or `Failed`.
6. Record `RUN_END=$(date +%s)`, stop the loops.
7. Run guide 09 steps 3–4 and this guide's steps 1, 4 and 6.

Clean up afterwards:

```bash
kubectl -n "$K10NS" delete policy -l app.kubernetes.io/managed-by=k10-performance-audit
kubectl -n "$K10NS" get pods | grep -E 'data-mover|copy-vol-data|create-repo|debug-kopia|k10tools'
```

The restore points produced by the test policy will be retained by its retention
settings. Decide with the customer whether to keep or retire them; retiring is done from
the dashboard or by deleting the `RestorePoint`.

## Caveats

- **`jobs-svc /v0/jobs` is an internal endpoint.** It is the only CLI route to job
  history, and its schema is not contractual. Note the K10 version alongside any
  extract. `jobType`, `phaseName` and `createdTime` were `null` in the validated
  response even where `phases[]`, `startedTime` and `completeTime` were populated — do
  not build the analysis on the null fields.
- **History may be short, from two independent causes.** K10 side: with
  `K10GCActionsEnabled=true` only the last `K10GCKeepMaxActions` actions survive.
  Metrics side: `AUDIT_WINDOW_DAYS` from guide 00 §6 caps steps 4 and 6. On a cluster
  whose Prometheus runs on `emptyDir`, a single node drain resets that window to hours.
  Check both before promising any trend, and state the window next to every "no
  incidents found" claim — otherwise it reads as a clean bill of health when it is
  really an absence of evidence.
- **cAdvisor misses short-lived pods.** See guide 09 — a sub-minute datamover may
  produce no memory sample. Absence of an OOM record is weak evidence; absence of a
  memory series is no evidence at all.
- **`OOMKilled` on a pod with no memory limit means node memory exhaustion**, not a pod
  misconfiguration. Since datamover pods have `resources: {}` (guide 09), any OOM kill
  here points at the node, and other workloads on that node were affected too. Check
  `kube_pod_container_status_last_terminated_reason` for the whole node, not just
  `kasten-io`.
- **Queue time is not run time.** A job "taking 4 hours" that spent 3 h 50 m queued needs
  a limiter change, not a bigger datamover. Always report `QUEUE_S` and `RUN_S`
  separately.
- **Correlate with the storage backend.** A slow export against a saturated NFS server
  is not a K10 problem. Pull the backend's own metrics for the same window.
- **A slow incremental is not a contradiction.** Kopia enumerates and `stat`s the whole
  tree every run, serially *within* a directory and concurrently across directories. On
  a volume with few huge directories that is roughly one random read per file, so a
  latency-bound disk (Azure Premium P10 at 3–4 ms) gives ~250–375 entries/s whatever
  the file size. Check `DIRS` next to `FILES` in guide 04 before blaming the network:
  100,003 files in 2 directories here. Block-mode export is the remedy, not tuning.
- **`/details` is not free.** About 0.2 s and 400 kB per action. Bound `export_table`
  rather than fetching a thousand-action history.

## Requires the audit owner's input

This guide is complete and runnable, but three decisions belong to the engagement, not
to whoever runs the collection:

1. **The thresholds** in the definition table above.
2. **Which namespaces are already known to be problematic** — steps 5 and 7 are
   expensive and should be pointed, not swept.
3. **Whether step 7 is in scope** for the test phase, and what window is acceptable.

## What to send back

| File | Contents |
|------|----------|
| `export-performance.tsv` | duration next to bytes for every export of the pair (headline) |
| `export-throughput.tsv` | the same, ranked by MiB/s |
| `export-volumes.tsv` | per-volume data format, storage class and CSI snapshot id |
| `datamover-per-export.txt` | peak memory and CPU per export, own versus concurrent |
| `job-durations.tsv` | every job with queue time and run time |
| `failed-jobs.tsv`, `failed-pods.tsv`, `worker-pod-warnings.tsv` | failures |
| `oomkilled.tsv`, `restarts.tsv` | memory and stability incidents |
| `actions-current.tsv`, `running-now.tsv` | present state, and anything blocking a limiter |
| `job-<id>-phases.json` | per-phase breakdown of each outlier |
| `kopia-putblob-latency.txt` | object-store latency, to separate backend from datamover |
| `executor-errors.txt`, `k10-*.log` | filtered and raw K10 log excerpts, all replicas |

## Validation status

Validated on K10 9.0.5 against `prod-test` / `calibrate-backup`.

> **Correction to an earlier version of this guide.** It stated that
> "`status.actionDetails` was `null` — the actions do **not** carry transferred-byte
> counts". That was a correct observation of the wrong object. The counters exist on
> the **`/details` subresource**, and step 1 now reads them: the same ExportAction
> that reports `progressDetails: null` on `kubectl get -o json` returns
> `transferredBytes: 10300000000`, `readBytes: 10200000000`,
> `totalBytes: 79456894976` and `processingRate: 219504793` from
> `GET .../exportactions/scheduled-gn9w65njrc/details`, plus per-volume operations with
> the PVC name, data format and CSI snapshot id. Guide 06 no longer has to reach for
> Kopia to answer "how much did this export move" — although it still does, as an
> independent check, and the two agreed to within 0.6 %.

Confirmed in this pass:

- `export_table` returned duration, transferred bytes and change rate for all five
  retained exports of the pair. Throughput came out at 42 MiB/s on the two incremental
  exports against 83 MiB/s on the full one — **the incrementals are slower per byte**,
  because they pay the same 100,003-file enumeration to move a fifth of the data.
  That is a file-count-bound export, and it is the finding the guide is for.
- `datamover_for_export` (step 5) resolved each export's window and reported 743 MiB
  peak for this namespace against 961 MiB for all datamovers alive at the time — three
  other namespaces of the same policy were exporting concurrently.
- Step 3's action resources return `state`, `startTime`, `endTime` and `progress`.
- Steps 2, 4 and 6 queries all execute and return data.

Carried over from earlier validated runs on this repository: `jobs-svc /v0/jobs`
returns HTTP 200 and well-formed JSON, with `jobType`, `phaseName` and `createdTime`
null even where `phases[]`, `startedTime` and `completeTime` are populated — the
duration arithmetic in step 1b strips the fractional seconds that `fromdateiso8601`
rejects (`2026-09-15T13:07:01.466Z`), which the first version of that query did not.
Step 6's log collection was corrected twice in the same way: `--since=15d` is rejected
by `kubectl` (`unknown unit "d"`), and `logs deploy/executor-svc` silently reads 1 of 3
replicas, so the label-selector form with `--max-log-requests` is the one here.

What was **not** validated: the outlier analysis itself. This cluster had no failed
exports, no OOM kills and no evicted pods, so `failed-jobs.tsv`, `oomkilled.tsv` and
`restarts.tsv` were empty. The queries are correct; their output on a cluster with real
failures has not been seen. Step 7's test-policy pattern was validated on an earlier
run but not re-exercised here — the pair exports hourly on its own.
