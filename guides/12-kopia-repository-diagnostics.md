# 12 — Kopia diagnostics on each repository

## What Global Engineering needs

For every Kopia repository K10 maintains: its inventory, its maintenance state, its blob
and content statistics, and its orphan/dangling counts. This guide also produces the
`kopia-snapshots.json` and `contents.json` that guides 04, 05 and 06 depend on — **run
it before those**, or at least before finalising them.

## The tool

`k10_repo_checker.sh`, shipped per release:

```
https://docs.kasten.io/downloads/<VERSION>/tools/k10_repo_checker.sh
```

Match the version to the cluster's K10 version from guide 00 §2:

```bash
mkdir -p "$AUDIT_DIR/12-kopia" && cd "$AUDIT_DIR/12-kopia"

K10_VERSION=$(kubectl -n "$K10NS" get cm k10-config -o jsonpath='{.data.version}')
echo "K10 version: $K10_VERSION"

curl -sSLO "https://docs.kasten.io/downloads/${K10_VERSION}/tools/k10_repo_checker.sh"
chmod +x k10_repo_checker.sh
./k10_repo_checker.sh 2>&1 | head -40   # prints usage
```

It needs `kubectl` and `helm` on the PATH, and it creates a `k10tools` pod in the K10
namespace using the `executor-svc` service account.

## Repository types

K10 keeps several kinds of repository, and each needs its own invocation:

| Type | Holds | Invocation |
|------|-------|-----------|
| `application` | volume data for one application | `-r application -a <namespace> -p <profile>` |
| `collections` | metadata for one policy | `-r collections -l <policy> -p <profile>` |
| `disaster_recovery` | K10's own DR backup | `-r disaster_recovery -p <profile>` |
| `inventory` | lists all of the above | `-r inventory [-p <profile>]` |

## Setup

```bash
. lib/init.sh
```

Sourcing `lib/init.sh` exports `K10NS`, `AUDIT_DIR`, `CLUSTER_UID`, the metrics window
(`AUDIT_WINDOW_DAYS`, `AUDIT_START`, `AUDIT_END`, `AUDIT_RANGE`) and the query helpers
(`tq`, `tqr`, `kq`, `pf_start`, `pf_stop`). It is idempotent — run it at the start of
every guide and in every new terminal. See [00-prerequisites.md](00-prerequisites.md).

## Method

### Step 1 — inventory first

Read-only, and it tells you what else to run.

```bash
./k10_repo_checker.sh -r inventory -F json -n "$K10NS" 2>/dev/null \
  | sed -n '/^{/,/^}/p' > inventory.json

jq -r '(["TYPE","REPOSITORY","PROFILE","SNAPSHOTS","ORPHANED","NOT_SYNCED","DANGLING"]|@tsv),
       (.repositories[] | [ .Type, .RepositoryName, .ProfileName,
          .TotalSnapshots, .OrphanedCount, .NotSyncedCount, .DanglingCount ] | @tsv)' inventory.json \
  | tee repositories.tsv | column -t
```

> **Parsing note**: the script writes coloured progress lines to stdout around the JSON.
> Piping straight into `jq` fails with `Invalid numeric literal`. The
> `sed -n '/^{/,/^}/p'` above extracts just the JSON document. This bites every time.

Validated output:

```
TYPE        REPOSITORY                              PROFILE       SNAPSHOTS  ORPHANED  NOT_SYNCED  DANGLING
Data        kopia-volumedata-repository-98v2rkz7sx  my-s3-bucket  2          0         0           0
Collection  kopia-metadata-repository-sv6qxrvbvd    my-s3-bucket  1          0         0           0
```

Also useful — the table form, and an orphan-only pass:

```bash
./k10_repo_checker.sh -r inventory -F table -n "$K10NS" 2>&1 | tee inventory-table.txt
./k10_repo_checker.sh -r inventory -O -F json -n "$K10NS" 2>/dev/null \
  | sed -n '/^{/,/^}/p' > inventory-orphaned.json
```

Per-snapshot detail, including `SizeBytes` and the restore point it belongs to:

> **`SizeBytes` is the logical size of the source at snapshot time** — the sum of the
> file sizes on that PVC — not the incremental bytes uploaded. Two consecutive snapshots
> of an unchanged volume report the *same* `SizeBytes`, and a snapshot of a volume that
> grew reports the new total, not the difference. Reading it as a change rate overstates
> object-store and network load by the dedup-and-compression factor (2.5× on the
> validation cluster). Guide 06 explains which of the three senses of "change rate" it
> can and cannot answer.
>
> `ProfileName` is on the **repository**, not the snapshot. Snapshots carry `PolicyName`
> and `RestorePointNamespace` and inherit the profile from their parent.

```bash
jq -r '(["REPOSITORY","SNAPSHOT_TIME","SOURCE_HOST","RESTORE_POINT","NAMESPACE","POLICY","SIZE_BYTES","ORPHAN"]|@tsv),
       (.repositories[] | .RepositoryName as $r | .Snapshots[]?
        | [ $r, .SnapshotTime, .Source.host, .RestorePointName,
            .RestorePointNamespace, .PolicyName, .SizeBytes, (.IsOrphaned|tostring) ] | @tsv)' \
   inventory.json | tee snapshots-inventory.tsv | column -t -s "$(printf '\t')"
```

### Step 2 — diagnose each repository

`diagnose` dumps Kopia's own debug data and copies a `.tar.gz` into the current
directory. Nothing is modified — it connects read-only.

```bash
# one per protected namespace that has an application repository
for ns in $(cat "$AUDIT_DIR/01-scope/audit-scope-namespaces.txt"); do
  echo "=== $ns ==="
  ./k10_repo_checker.sh -r application -o diagnose -a "$ns" -p <PROFILE> -n "$K10NS" \
    2>&1 | tee "diagnose-app-$ns.log"
done

# one per policy, for the collection (metadata) repository
for pol in $(kubectl -n "$K10NS" get policies.config.kio.kasten.io -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
  ./k10_repo_checker.sh -r collections -o diagnose -p <PROFILE> -l "$pol" -n "$K10NS" \
    2>&1 | tee "diagnose-collections-$pol.log"
done

# the DR repository belonging to K10 itself, if a DR policy exists
./k10_repo_checker.sh -r disaster_recovery -o diagnose -p <PROFILE> -n "$K10NS" \
  2>&1 | tee diagnose-dr.log
```

Unpack the bundles. They arrive owned by the pod's UID with restrictive modes:

```bash
for t in kopia-diagnose-*.tar.gz; do
  d="${t%.tar.gz}"; mkdir -p "$d"
  tar -xzf "$t" -C "$d"
  chmod -R u+rwX "$d"          # otherwise: find: Permission denied
done
find . -name '*-stdout.txt' | sort
```

### Step 3 — what is in a diagnose bundle

Validated contents of `tmp/kopia-debug-logs/` — 12 files:

| File | Use |
|------|-----|
| `blob-stats-stdout.txt` | object count, total bytes, size histogram → guide 11 |
| `content-stats-stdout.txt` | content count, compression ratio by method → guide 05 |
| `index-list-stdout.txt` | index blob count and timestamps; maintenance cost driver |
| `manifest-list-stdout.txt` | snapshot and policy manifests |
| `maintenance-info-stdout.txt` | maintenance schedule, owner, and recent run outcomes |
| `repository-status-stdout.txt` | format version, hash, splitter, epoch manager, pack size |
| `repository-status2-stdout.txt` | second capture, for comparison |
| `logs-list-stdout.txt` | Kopia's own log blobs |
| `logs-show-all-stdout.txt` | full Kopia debug log — per-operation latency |
| `restore-point-snapshotID-mapping.txt` | restore point → PVC → Kopia snapshot ID |
| `blob-list-stdout.txt` | every blob with id, length, timestamp |
| `kopia-binary-version-stdout.txt` | the Kopia build in use |

The four to read first:

```bash
cat */tmp/kopia-debug-logs/repository-status-stdout.txt
cat */tmp/kopia-debug-logs/maintenance-info-stdout.txt
cat */tmp/kopia-debug-logs/blob-stats-stdout.txt
cat */tmp/kopia-debug-logs/content-stats-stdout.txt
```

`repository-status` on the validation cluster, with the performance-relevant lines:

```
Hash:                BLAKE2B-256-128
Encryption:          AES256-GCM-HMAC-SHA256
Splitter:            DYNAMIC-4M-BUZHASH
Format version:      3
Content compression: true
Max pack length:     21 MB
Index Format:        v2
Epoch Manager:       enabled
Current Epoch: 0
Epoch refresh frequency: 20m0s
Epoch advance on:        20 blobs or 10.5 MB, minimum 24h0m0s
Epoch range-compaction every: 7 epochs
```

`Max pack length: 21 MB` against a `blob stats` histogram where most blobs are under
10 KB tells you packs are not being filled. `Epoch Manager: enabled` with format
version 3 is current and healthy — a repository still on format v1/v2 with no epoch
manager is a migration candidate and a known performance problem.

`maintenance-info` is where slow repositories give themselves away:

```
Owner: k10-admin@maintenance-owner
Quick Cycle:  scheduled: true  interval: 1h0m0s   next run: ...
Full Cycle:   scheduled: true  interval: 24h0m0s  next run: ...
Log Retention: max count: 10000  max age: 720h0m0s  max total size: 1.1 GB
Object Lock Extension: disabled
Recent Maintenance Runs:
  full-rewrite-contents:
    ... (0s) SUCCESS: Found 0(0 B) contents to rewrite and rewrote 0(0 B). Retained 12(2.8 MB)
  generate-epoch-range-index:
    ... (0s) SUCCESS: Generated a range checkpoint from epoch 0 to 0 inclusive
```

Check: is the full cycle actually completing, or timing out? Are the durations growing
run over run? Is `Owner` a pod that no longer exists (a stuck maintenance lock)?

### Step 4 — connect, for the data guides 04–06 need

`connect` leaves a second pod running with the repository mounted read-only. **It does
not clean up after itself** — you must delete the pod.

```bash
./k10_repo_checker.sh -r application -o connect -a <NAMESPACE> -p <PROFILE> -n "$K10NS" \
  2>&1 | tee connect.log

DEBUG_POD=$(kubectl -n "$K10NS" get pods -o name | grep debug-kopia | tail -1 | xargs basename)
echo "DEBUG_POD=$DEBUG_POD"

# per-PVC file counts, sizes and history  → guides 04, 05, 06 step B
kubectl -n "$K10NS" exec "$DEBUG_POD" -- sh -c \
  'export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config; kopia snapshot list --all --json' \
  > "$AUDIT_DIR/04-files-per-pvc/kopia-snapshots.json"

# content index with timestamps  → guide 06 step A (change rate)
kubectl -n "$K10NS" exec "$DEBUG_POD" -- sh -c \
  'export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config; kopia content list --json' \
  > "$AUDIT_DIR/06-change-rate/contents.json"

# extra diagnostics
kubectl -n "$K10NS" exec "$DEBUG_POD" -- sh -c '
  export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config
  echo "=== repository status ==="; kopia repository status
  echo "=== blob stats ===";        kopia blob stats
  echo "=== content stats ===";     kopia content stats
  echo "=== index list ===";        kopia index list
  echo "=== maintenance info ===";  kopia maintenance info
  echo "=== snapshot sources ===";  kopia snapshot list --all
' | tee kopia-live-diagnostics.txt
```

Clean up — do not skip this:

```bash
kubectl -n "$K10NS" delete pod "$DEBUG_POD"

# catch any left from earlier runs, including failed ones
kubectl -n "$K10NS" get pods | grep -E 'debug-kopia|k10tools'
kubectl -n "$K10NS" delete pod -l createdBy=Kasten-K10 --ignore-not-found
```

### Step 5 — repository format upgrade readiness

`repo-checker` exposes `upgrade_begin` / `upgrade_rollback`. **Do not run either during
an audit.** They mutate the repository. Only report the current format version from
step 3 and whether an upgrade is available; the upgrade itself is a change request, not
a data collection.

## Caveats

- **`diagnose` leaves a `debug-kopia-*` pod behind.** Observed on the validation
  cluster: a `diagnose` run left `debug-kopia-q9lrb` running after the script reported
  success and deleted its own `k10tools` pod. Always run the cleanup in step 4.
- **The JSON is wrapped in log output.** Use `sed -n '/^{/,/^}/p'`.
- **The k10tools image tag can differ from the K10 version.** On the validation cluster,
  K10 was 9.0.5 and the script pulled k10tools 9.0.4. That is normal; do not "fix" it.
- **The diagnose bundle contains the bucket, prefix and access key ID** in
  `repository-status-stdout.txt`. The secret key is masked. Redact before sharing
  externally (guide 00 §7).
- **`kopia content list` on a large repository is very large.** Redirect to a file; do
  not let it through a pipe into `jq -s` in memory.
- **Air-gapped clusters**: the script pulls `k10tools` and `datamover` images. On a
  mirrored registry, pass `--image-registry`; check `./k10_repo_checker.sh` usage for
  the exact flag on your version.
- The script requires `helm` even on operator-based installs.
- **An empty inventory is a real answer.** Before any export had run, the validation
  cluster reported `Found 0 storage repositories`. That means no data has been exported
  off-cluster yet — not that the tool failed.

## What to send back

| File | Contents |
|------|----------|
| `repositories.tsv` | all repositories with snapshot and orphan counts (headline) |
| `snapshots-inventory.tsv` | per-snapshot detail with size, namespace and policy |
| `kopia-diagnose-*.tar.gz` | the raw bundles, redacted |
| `kopia-live-diagnostics.txt` | consolidated live output per repository |
| `inventory-orphaned.json` | orphan-only pass |
| `*-stdout.txt` extracts | at minimum `repository-status`, `maintenance-info`, `blob-stats`, `content-stats` |

Also confirm you have written these, since other guides consume them:

- `$AUDIT_DIR/04-files-per-pvc/kopia-snapshots.json`
- `$AUDIT_DIR/06-change-rate/contents.json`

## Validation status

Fully validated on K10 9.0.5 with `k10_repo_checker.sh` downloaded from the 9.0.5 path.
Exercised end to end: `-r inventory` in both `table` and `json` form (before and after
an export, i.e. with 0 and with 2 repositories); `-r application -o diagnose`, producing
a 6 976-byte bundle with all 12 files listed in step 3; and `-r application -o connect`
followed by `kopia repository status`, `blob stats`, `content stats`, `index list`,
`maintenance info`, `snapshot list --all --json` and `content list --json` inside the
debug pod.

Confirmed failure modes: the JSON-wrapped-in-logs parse error; the leftover
`debug-kopia-*` pod after `diagnose`; the `Permission denied` on the extracted bundle
before `chmod -R u+rwX`; and the k10tools 9.0.4 / K10 9.0.5 version skew.

The `snapshots-inventory.tsv` extraction was validated and correctly resolved the
`Source.host` encoding for both repository kinds — `<repoUUID>.<workload>.<pvc>` for the
Data repository and `migration.<k10ns>.<policy>` for the Collection repository.

Not exercised: `-r collections -o diagnose` and `-r disaster_recovery -o diagnose`. A
Collection repository did exist (`kopia-metadata-repository-sv6qxrvbvd`, 1 snapshot) but
only the `application` diagnose path was run; no DR policy was configured at all. The
`-r collections` and `-r disaster_recovery` invocations shown above come from the
script's own usage output, so verify the flags interactively before a long unattended
loop. `upgrade_begin` / `upgrade_rollback` were deliberately not run.
