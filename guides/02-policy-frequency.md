# 02 — Policy cadence and concurrency

## What auditors need

For `$AUDIT_POLICY`: how often it fires, what it does at each firing, how much it
keeps, how many namespaces it exports at once, and what it competes with. Cadence
multiplied by namespace size drives the concurrency limits and the datamover sizing
recommendation.

## Why frequency alone is not enough

A policy has up to **three independent cadences**:

1. `spec.frequency` — the snapshot cadence.
2. `spec.actions[].exportParameters.frequency` — the export cadence, which is
   frequently `@onDemand` even when the snapshot is hourly. Those namespaces have
   local snapshots but nothing off-cluster.
3. Retention, per granularity (`hourly`/`daily`/`weekly`/`monthly`/`yearly`), which
   determines how many restore points — and therefore how much repository maintenance
   work — accumulate.

Report all three or the recommendation will be wrong.

## Method

```bash
. lib/init.sh
focus_dir 02-policy-frequency
```

### Step 1 — the policy under audit

```bash
kubectl -n "$K10NS" get policies.config.kio.kasten.io "$AUDIT_POLICY" -o json \
  > policy-raw.json

jq -r '. as $p
  | ([$p.spec.actions[]? | select(.action=="export")] | first) as $e
  | (["POLICY","PAUSED","SNAP_FREQ","EXPORT_FREQ","ACTIONS","RETENTION","PROFILE","EXPORT_DATA","NAMESPACES"]|@tsv),
    ([ $p.metadata.name,
       ($p.spec.paused // false | tostring),
       ($p.spec.frequency // "@onDemand"),
       ($e.exportParameters.frequency // "-"),
       ([$p.spec.actions[]?.action] | join("+")),
       (($p.spec.retention // {}) | to_entries | map("\(.key)=\(.value)") | join(",")
        | if . == "" then "-" else . end),
       ($e.exportParameters.profile.name // "-"),
       ($e.exportParameters.exportData.enabled // false | tostring),
       ([$p.spec.selector.matchExpressions[]? | select(.key=="k10.kasten.io/appNamespace")
         | .values[]] | length | tostring)
     ] | @tsv)' policy-raw.json | tee policy-frequency.tsv | column -t
```

Validated:

```
POLICY            PAUSED  SNAP_FREQ  EXPORT_FREQ  ACTIONS        RETENTION                                      PROFILE        EXPORT_DATA  NAMESPACES
calibrate-backup  false   @hourly    @hourly      backup+export  daily=7,hourly=3,monthly=12,weekly=4,yearly=7  my-s3-profile  true         3
```

`NAMESPACES` is the fan-out: **3** means one firing starts three exports at once, into
three repositories, with their datamover pods sharing the cluster limiters. That number
is the multiplier for everything in guides 09 and 10.

### Step 2 — when it actually fires

K10 accepts crontab syntax and `@hourly` / `@daily` shorthands, plus a `subFrequency`
block that pins the exact minute. Two `@daily` policies with no `subFrequency` fire at
the same second and contend for the same limiter slots.

```bash
jq -r '{frequency: .spec.frequency, subFrequency: .spec.subFrequency}' policy-raw.json \
  | tee sub-frequency.json
```

Validated: `{"frequency": "@hourly", "subFrequency": null}` — no explicit offset, so
this policy fires at K10's default offset for the hour, as does every other `@hourly`
policy on the cluster. Flag that.

### Step 3 — observed cadence, not declared cadence

Declared frequency is intent. This is what really happened:

```bash
policy_runs | tee policy-runs.tsv | head -15 | column -t
```

```
RUN_ACTION      STATE    START                END                  DUR_S
run-tmvfjf2k9j  Skipped  2026-09-20T12:00:14  2026-09-20T12:00:14  0
run-5c25fr4dh6  Skipped  2026-09-20T11:00:15  2026-09-20T11:00:15  0
run-9vmgpx5x8f  Running  2026-09-20T08:00:18  -                    -
```

`policy_runs` ([../lib/exports.sh](../lib/exports.sh)) reads the policy's **metadata**
ExportActions, which live in the K10 namespace and carry no volume bytes — they are the
cadence evidence. The per-application exports, with the bytes, live in the application
namespace and are guide 13's subject.

Two states to read carefully:

- **`Skipped`** means the run fired on schedule and did nothing, normally because the
  previous run of the same policy had not finished. A wall of `Skipped` at the declared
  interval is the signature of a policy that cannot keep up with its own cadence — the
  most important finding this guide can produce.
- **`Running`** on an action hours old is either a genuinely long export or a stuck one.
  Cross-check the duration against guide 13.

Count the states — a wall of `Skipped` is the headline:

```bash
awk -F'\t' 'NR>1 {print $2}' policy-runs.tsv | sort | uniq -c | sort -rn \
  | tee run-states.txt
```

### Step 4 — what the policy competes with

The limiters are cluster-wide, so the pair's exports queue behind every other policy:

```bash
kubectl -n "$K10NS" get cm k10-config -o json \
  | jq -r '.data | with_entries(select(.key|startswith("K10Limiter")))' \
  | tee limiters.json
```

Validated:

```json
{
  "K10LimiterCsiSnapshotRestoresPerAction": "3",
  "K10LimiterCsiSnapshotsPerCluster": "10",
  "K10LimiterDirectSnapshotsPerCluster": "10",
  "K10LimiterExecutorThreads": "8",
  "K10LimiterGenericVolumeBackupsPerCluster": "10",
  "K10LimiterImageCopiesPerCluster": "10",
  "K10LimiterSnapshotExportsPerAction": "3",
  "K10LimiterSnapshotExportsPerCluster": "10",
  "K10LimiterVMBackupsPerCluster": "3",
  "K10LimiterVMSnapshotsPerCluster": "1",
  "K10LimiterVolumeRestoresPerAction": "3",
  "K10LimiterVolumeRestoresPerCluster": "10",
  "K10LimiterVolumeRetiresPerCluster": "10",
  "K10LimiterWorkloadRestoresPerAction": "3",
  "K10LimiterWorkloadSnapshotsPerAction": "5"
}
```

The two that bite first are `K10LimiterSnapshotExportsPerCluster` (10) and
`K10LimiterSnapshotExportsPerAction` (3). With 37 PVCs in one namespace and a
cluster limiter of 10, an export serialises into 4 waves — and the per-action limit
of 3 caps each namespace regardless of what the cluster limit allows. For KubeVirt,
note `K10LimiterVMSnapshotsPerCluster` at **1**: VM snapshots are serialised
cluster-wide whatever the export limits say.

How many exports the cluster starts per cycle, across all policies:

```bash
audit_focus_candidates | awk -F'\t' 'NR>1 && $5=="false" {print}' \
  | tee other-active-pairs.tsv | column -t
```

Every row here is an export competing for those 10 slots at the same instant if the
cadences coincide. Validated: 6 active pairs, 4 of them `@hourly` with no
`subFrequency` — so four exports start in the same second, every hour.

### Step 5 — how much history exists

Governs what guides 02 and 13 can see at all:

```bash
kubectl -n "$K10NS" get cm k10-config -o json \
  | jq -r '.data | {K10GCActionsEnabled, K10GCKeepMaxActions, K10GCDaemonPeriod}' \
  | tee action-retention.json
```

Validated: `K10GCActionsEnabled=false`, `K10GCKeepMaxActions=1000`. With GC disabled
all action history is retained. If it is enabled on the audited cluster, step 3 sees
only the last `K10GCKeepMaxActions` actions — say so next to any cadence claim.

## Caveats

- **`@onDemand` export inside an otherwise scheduled policy is easy to miss in the UI.**
  Step 1 gives it its own column.
- **Retention is per granularity and compounds.** `hourly=3, daily=7, weekly=4,
  monthly=12, yearly=7` is up to 33 restore points per namespace, each one work for
  Kopia maintenance (guide 12) and objects in the bucket (guide 11). Multiply by the
  `NAMESPACES` fan-out from step 1.
- **A `Skipped` run is not a failure** and does not appear in a failure count. It is
  still the clearest evidence that the cadence is too aggressive for the data.
- **Action history is bounded twice**: by `K10GCKeepMaxActions` (step 5) and, for
  anything metric-based, by `AUDIT_WINDOW_DAYS` (guide 00 §6). State both.

## What to send back

| File | Contents |
|------|----------|
| `policy-frequency.tsv` | the policy's three cadences, retention, profile and fan-out |
| `policy-runs.tsv` | actual run history with states — the `Skipped` evidence |
| `sub-frequency.json` | explicit schedule offset, or null if it shares the default |
| `limiters.json` | cluster-wide concurrency limits |
| `other-active-pairs.tsv` | what else exports, and therefore what this policy queues behind |
| `action-retention.json` | how far back step 3 can see |

## Validation status

Fully validated on K10 9.0.5 against `calibrate-backup` / `prod-test`. Confirmed there:
the policy selects **three** namespaces and exports all three per firing;
`subFrequency` is null, so it shares the default hourly offset with the other `@hourly`
policies; `policy_runs` returned **12 `Skipped` against 4 `Complete`** — three
runs in four fired and did nothing because the previous one was still going. The
cadence is faster than the data can be exported, which is exactly the finding this
guide exists to surface. Limiter values in step 4 are the
literal contents of that cluster's `k10-config`.

The step 3 `Skipped` interpretation is drawn from the observed run history and the
corresponding ExportAction timestamps in guide 13; it was not reproduced by deliberately
slowing an export.
