# 02 — Policy frequency

## What Global Engineering needs

For each policy: how often it fires, what it does at each firing, how much it keeps,
and where it exports to. Frequency multiplied by namespace size drives the
concurrency limits and the datamover sizing recommendation.

## Why frequency alone is not enough

A policy has up to **three independent cadences**:

1. `spec.frequency` — the snapshot cadence.
2. `spec.actions[].exportParameters.frequency` — the export cadence, which is
   frequently `@onDemand` even when the snapshot is hourly. Those namespaces have
   local snapshots but nothing off-cluster.
3. Retention, per granularity (`hourly`/`daily`/`weekly`/`monthly`/`yearly`), which
   determines how many restore points — and therefore how much repository
   maintenance work — accumulate.

Report all three or the recommendation will be wrong.

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
mkdir -p "$AUDIT_DIR/02-policy-frequency" && cd "$AUDIT_DIR/02-policy-frequency"
```

### Step 1 — the summary table

```bash
kubectl -n "$K10NS" get policies.config.kio.kasten.io -o json > policies-raw.json

jq -r '
  (["POLICY","PAUSED","SNAP_FREQ","EXPORT_FREQ","ACTIONS","RETENTION","EXPORT_PROFILE"]|@tsv),
  (.items[]
   | . as $p
   | ([$p.spec.actions[]? | select(.action=="export") | .exportParameters.frequency] | first // "-") as $ef
   | ([$p.spec.actions[]? | select(.action=="export") | .exportParameters.profile.name] | first // "-") as $ep
   | [ $p.metadata.name,
       ($p.spec.paused // false | tostring),
       ($p.spec.frequency // "@onDemand"),
       $ef,
       ([$p.spec.actions[]?.action] | join("+")),
       (($p.spec.retention // {}) | to_entries | map("\(.key)=\(.value)") | join(",") | if . == "" then "-" else . end),
       $ep ] | @tsv)' policies-raw.json | tee policy-frequency.tsv | column -t
```

### Step 2 — expand `@` shorthands into real times

K10 accepts both crontab syntax and `@hourly` / `@daily` / `@weekly` shorthands, plus
a `subFrequency` block that pins the exact minute/hour. Two `@daily` policies can fire
at the same second and contend for the same limiter slots — that is precisely the kind
of thing this audit is looking for.

```bash
jq -r '.items[] | select(.spec.subFrequency != null)
       | {policy: .metadata.name, frequency: .spec.frequency, subFrequency: .spec.subFrequency}' \
   policies-raw.json | tee sub-frequencies.json
```

If `sub-frequencies.json` is empty, every `@daily`/`@hourly` policy runs at K10's
default offset, i.e. **they all fire together**. Flag that.

### Step 3 — count the work per cycle

Combine the frequency with the namespace's PVC count from guide 03 to get the number
of datamover pods per cycle, and compare against the cluster limiters (guide 10):

```bash
kubectl -n "$K10NS" get cm k10-config -o json \
  | jq -r '.data | with_entries(select(.key|startswith("K10Limiter")))' \
  | tee limiters.json
```

The two that bite first are `K10LimiterSnapshotExportsPerCluster` (default 10) and
`K10LimiterGenericVolumeBackupsPerCluster` (default 10): with 37 PVCs in one namespace
and a limiter of 10, an export serialises into 4 waves.

### Step 4 — observed cadence, not declared cadence

Declared frequency is intent. Confirm what really happened:

```bash
# pf_start / pf_stop come from guide 00 section 4
pf_start jobs-svc 18081:8000 || exit 1
curl -s http://localhost:18081/v0/jobs > jobs-raw.json
pf_stop

jq -r '[.[] | {policy: (.originatingPolicies[0].id // "-"), start: .startedTime, end: .completeTime, status}]
       | sort_by(.start) | .[] | [.policy, .start, .end, .status] | @tsv' jobs-raw.json \
  | tee observed-runs.tsv | tail -40
```

Map the policy UID back to a name with:

```bash
jq -r '.items[] | "\(.metadata.uid)\t\(.metadata.name)"' policies-raw.json > policy-uid-map.tsv
```

## Caveats

- **`jobs-svc /v0/jobs` is an internal endpoint**, not a supported API. It is the only
  way to get job history from the CLI, so use it for the audit but do not build
  anything on it. It returned `[]` on a freshly reinstalled cluster even though
  policies existed.
- **Action history is garbage-collected.** On the validation cluster
  `K10GCActionsEnabled=false` and `K10GCKeepMaxActions=1000`. If GC is enabled on the
  audited cluster, step 4 only sees the last 1000 actions.
- `@onDemand` export inside an otherwise scheduled policy is easy to miss in the UI.
  Step 1 surfaces it in its own column — on the validation cluster, 2 of 2 policies
  with an export action had `@onDemand` export frequency.

## What to send back

| File | Contents |
|------|----------|
| `policy-frequency.tsv` | the summary table |
| `sub-frequencies.json` | explicit schedule offsets, or empty if all policies share the default |
| `limiters.json` | cluster-wide concurrency limits |
| `observed-runs.tsv` | actual run history |
| `policy-uid-map.tsv` | policy UID → name |

## Validation status

Steps 1–3 fully validated on K10 9.0.5. Step 4 validated mechanically — the endpoint
returned HTTP 200 and well-formed JSON with `startedTime`/`completeTime`/`status`
fields after a policy run, but the validation cluster had only minutes of history, so
the cadence analysis itself was not exercised on real data.
