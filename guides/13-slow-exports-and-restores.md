# 13 — Exports and restores with a performance or memory/CPU problem

## What Global Engineering needs

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
mkdir -p "$AUDIT_DIR/13-slow-jobs" && cd "$AUDIT_DIR/13-slow-jobs"
```

### Step 1 — job history with durations

```bash
# pf_start / pf_stop come from guide 00 section 4
pf_start jobs-svc 18081:8000 || exit 1
curl -s http://localhost:18081/v0/jobs > jobs-raw.json
pf_stop

jq -r '
  # jobs-svc emits fractional seconds; fromdateiso8601 rejects them, so strip them first.
  def ts: sub("\\.[0-9]+(?=Z$)";"") | fromdateiso8601;
  (["ID","STATUS","SCHEDULED","STARTED","COMPLETED","QUEUE_S","RUN_S","WAIT_COUNT","POLICY_ID"]|@tsv),
  ( .[]
    | (if .scheduledTime and .startedTime
         then ((.startedTime|ts) - (.scheduledTime|ts)) else null end) as $q
    | (if .startedTime and .completeTime
         then ((.completeTime|ts) - (.startedTime|ts)) else null end) as $r
    | [ .id, .status, .scheduledTime, .startedTime, .completeTime,
        ($q // "-"), ($r // "-"), (.waitCount // 0),
        (.originatingPolicies[0].id // "-") ] | @tsv )' jobs-raw.json \
  | tee job-durations.tsv | column -t
```

Validated output (IDs truncated), from two runs of the same policy:

```
ID        STATUS     QUEUE_S  RUN_S  WAIT_COUNT
5720c6d0  succeeded  2        58     7
58236bae  succeeded  2        38     5
69c5d60c  succeeded  31       19     0
d442aa4a  succeeded  8        69     8
f09cab38  succeeded  48       13     0
```

Note `f09cab38`: 48 s queued, 13 s running. On a cluster under real load that pattern —
queue time dominating run time — is the limiter signature, and no amount of datamover
tuning will help it.

`QUEUE_S` — the gap between scheduled and started — is as important as `RUN_S`. A large
queue time means the limiters from guide 10 are the bottleneck, not the datamover.

Rank the outliers:

```bash
tail -n +2 job-durations.tsv | sort -t"$(printf '\t')" -k7 -rn | head -25 \
  | { printf 'ID\tSTATUS\tSCHEDULED\tSTARTED\tCOMPLETED\tQUEUE_S\tRUN_S\tWAIT_COUNT\tPOLICY_ID\n'; cat; } \
  | tee slowest-jobs.tsv | column -t
```

Join policy IDs to names via `policy-uid-map.tsv` from guide 02.

Per-phase detail for a single slow job — this is what localises the problem:

```bash
JOB=<id from slowest-jobs.tsv>
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
for r in exportactions backupactions restoreactions importactions runactions; do
  echo "=== $r ==="
  kubectl get "$r.actions.kio.kasten.io" -A -o json \
    | jq -r '.items[] | [ .metadata.namespace, .metadata.name, .status.state,
                          .status.startTime, .status.endTime, (.status.progress // "-"),
                          ((.status.error // .status.result) | tostring) ] | @tsv'
done | tee actions-current.tsv | column -t
```

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

### Step 5 — resource consumption of the slow jobs

Cross-reference the slow jobs from step 1 with the datamover measurements from guide 09.
For a job identified as slow, use its `k10.kasten.io/jobID` to find its pods, then pull
their peak RSS and CPU over the job's window. Guide 09 steps 3–4 have the queries; the
join key is the `pod` label.

If the slow job is historical and its pods were never captured, you can still get the
pods' resource series from cAdvisor by name pattern over the job window:

```bash
S=<job start epoch>; E=<job end epoch>
tqr 'max by (pod, container) (container_memory_working_set_bytes{namespace="'"$K10NS"'",pod=~"data-mover.*|copy-vol-data.*",container!="",container!="POD"})' \
    "$S" "$E" 15s \
  | jq -r '.data.result[] | [ .metric.pod, .metric.container,
             ((.values|map(.[1]|tonumber)|max)/1048576*100|round/100) ] | @tsv' \
  | tee "job-window-memory.tsv" | column -t
```

You will get pod names without namespace attribution — acceptable when you already know
which job's window you queried.

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
   "$AUDIT_DIR/12-kopia/"*/tmp/kopia-debug-logs/logs-show-all-stdout.txt 2>/dev/null \
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
| `slowest-jobs.tsv` | the ranked outlier list (headline) |
| `job-durations.tsv` | every job with queue time and run time |
| `failed-jobs.tsv`, `failed-pods.tsv`, `worker-pod-warnings.tsv` | failures |
| `oomkilled.tsv`, `restarts.tsv` | memory and stability incidents |
| `actions-current.tsv` | present state of all action objects |
| `action-outcomes.tsv`, `action-duration-sums.tsv` | cluster-wide counters |
| `job-<id>-phases.json` | per-phase breakdown of each outlier |
| `kopia-putblob-latency.txt` | object-store latency, to separate backend from datamover |
| `executor-errors.txt`, `k10-*.log` | filtered and raw K10 log excerpts, all replicas |

## Validation status

Partially validated. What was confirmed on the validation cluster:

- `jobs-svc /v0/jobs` returns HTTP 200 and well-formed JSON; after a policy run it
  returned 7 job records with populated `scheduledTime`, `startedTime`, `completeTime`,
  `status`, `waitCount`, `phases[]` and `originatingPolicies[]`, and with `jobType`,
  `phaseName` and `createdTime` null. The duration arithmetic in step 1 was run against
  that real response — and the first version of it was wrong: `fromdateiso8601` rejects
  the fractional seconds K10 emits (`2026-09-15T13:07:01.466Z`), which is why the query
  above strips them. The corrected query produced the durations quoted in step 1.
- Step 3's action resources exist and return data (`ExportAction` with `state`,
  `startTime`, `endTime`, `progress`). Note that `status.actionDetails` was `null` — the
  actions do **not** carry transferred-byte counts, which is why guide 06 goes to Kopia
  instead.
- Steps 2, 4 and 6 queries all execute and return data.
- Step 7's policy-and-RunAction pattern was validated: an on-demand policy run was
  triggered twice and completed in ~50 s and ~61 s, producing the datamover pods
  analysed in guide 09.

- Steps 4 and 6 were re-validated using the derived window from guide 00 §6
  (`AUDIT_WINDOW_DAYS=15`); `kube_pod_container_status_restarts_total` returned 240
  series over that range in the `kasten-io` namespace. The step 6 log collection was
  corrected twice in the process: `--since=15d` is rejected by `kubectl`
  (`unknown unit "d"`), and `logs deploy/executor-svc` silently read 1 of 3 replicas.
  The label-selector form returned 998 / 29 / 94 lines for executor /
  controllermanager / jobs over 360 h.

What was **not** validated: the outlier analysis itself. The validation cluster had
minutes of job history, no failures, no OOM kills and no evicted pods, so
`slowest-jobs.tsv`, `oomkilled.tsv` and `restarts.tsv` were all empty. The queries are
correct; their output on a cluster with real problems has not been seen.

Note also that the validation cluster's Prometheus runs on `emptyDir` with 15 days of
retention and both replicas up for 78+ days, so `AUDIT_WINDOW_DAYS` came out at the
retention ceiling. The short-window branch of guide 00 §6 — where replica uptime rather
than retention is the binding constraint — was therefore validated arithmetically only,
not against a genuinely recently-restarted Prometheus. **This was reviewed and accepted
as sufficient; it is a closed question, not an outstanding gap.** If you do hit a
cluster where uptime is the binding constraint, the derived window is still worth
sanity-checking once against the measured-oldest-sample probe in guide 00 §6.
