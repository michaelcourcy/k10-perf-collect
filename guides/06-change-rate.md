# 06 — Change rate

## What Global Engineering needs

How much data actually changes between two backup cycles. This is what determines
incremental export duration, repository growth, object-storage cost, and how much
maintenance work Kopia has to do. It is the hardest figure on the list to obtain
honestly, and the one most often replaced by a guess.

## What "change rate" has to mean here

Three different quantities get called "change rate", and they differ by an order of
magnitude on real data:

| Quantity | Definition | Where to get it |
|----------|-----------|-----------------|
| **Logical growth** | net change in total file bytes on the volume between two snapshots | Kopia snapshot stats, per PVC |
| **Churn** | bytes rewritten, whether or not the total changed | Kopia content creation timestamps, per repository |
| **Physical ingest** | bytes actually uploaded after dedup and compression | Kopia content `length`, per repository |

The recommendation needs **physical ingest** for storage and network sizing, and
**logical growth** per PVC to spot which namespace is responsible. Collect both.

## The method that is not reliable

The intuitive approach is Kopia's `cachedFiles` / `nonCachedFiles` per snapshot: files
skipped as unchanged against the previous snapshot, and files Kopia had to hash. The
mechanism does exist under K10 - it compares against the previous snapshot manifest,
not a local cache - but it is not dependable across volumes:

```
data-mastodon-elasticsearch-data-0  (managed-csi)  2nd snapshot  files=167 cached=153 nonCached=14   correct
basic-app-pvc-2026-07-21-08-24-17   (nfs-csi)    2nd snapshot  files=3   cached=0   nonCached=3    wrong: 1 of 3 was unchanged
```

On a first snapshot `cachedFiles` is always 0. Where it works it is a good per-PVC
change indicator (`generate-export-topology.py` reports it as `filesHashed` /
`filesUnchanged`); where it reports 0 on a later snapshot, trust the methods below
instead. Note also that `stats.fileCount` is the number of files *hashed*, not the files
in the snapshot - that is `rootEntry.summ.files`.

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
mkdir -p "$AUDIT_DIR/06-change-rate" && cd "$AUDIT_DIR/06-change-rate"
```

### Step A — physical ingest per day, from Kopia content timestamps

This is the authoritative measurement. Every content block Kopia writes carries its
creation time, its logical size (`originalLength`) and its stored size (`length`).
Bucketing by day gives the repository's true ingest rate — after deduplication and
compression, which is exactly what the object store and the network see.

Connect to the repository as described in
[12-kopia-repository-diagnostics.md](12-kopia-repository-diagnostics.md), then:

```bash
DEBUG_POD=debug-kopia-xxxxx   # printed by repo-checker -o connect

kubectl -n "$K10NS" exec "$DEBUG_POD" -- sh -c \
  'export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config; kopia content list --json' \
  > contents.json
```

Aggregate. Time bucketing is done in `jq`, not `awk`, because BSD/macOS `awk` has no
`strftime`:

```bash
jq -r '
 [ .[] | select(.deleted == false)
   | {day: (.time | strftime("%Y-%m-%d")), logical: .originalLength, physical: .length} ]
 | group_by(.day)
 | map({day: .[0].day,
        contents: length,
        logical_MiB:  ((map(.logical) |add)/1048576*100|round/100),
        physical_MiB: ((map(.physical)|add)/1048576*100|round/100)})
 | (["DAY","CONTENTS","LOGICAL_MiB","PHYSICAL_MiB"]|@tsv),
   (.[] | [.day,.contents,.logical_MiB,.physical_MiB]|@tsv)' contents.json \
  | tee change-rate-daily.tsv | column -t
```

Validated output (one hour of activity, two backup cycles, ~5 MiB of new data):

```
DAY         CONTENTS  LOGICAL_MiB  PHYSICAL_MiB
2026-09-15  20        20.3         8.06
```

Swap `%Y-%m-%d` for `%Y-%m-%dT%H:00Z` to get hourly buckets and see the backup window
itself:

```bash
jq -r '
 [ .[] | select(.deleted == false)
   | {h: (.time | strftime("%Y-%m-%dT%H:00Z")), l: .originalLength, p: .length} ]
 | group_by(.h)
 | map({h: .[0].h, n: length,
        logical_MiB:  ((map(.l)|add)/1048576*100|round/100),
        physical_MiB: ((map(.p)|add)/1048576*100|round/100)})
 | (["HOUR","CONTENTS","LOGICAL_MiB","PHYSICAL_MiB"]|@tsv),
   (.[] | [.h,.n,.logical_MiB,.physical_MiB]|@tsv)' contents.json \
  | tee change-rate-hourly.tsv | column -t
```

The ratio `physical / logical` is the effective dedup+compression factor. On the
validation cluster it was 8.06 / 20.3 = **0.40**, i.e. 60 % reduction.

### Step B — logical growth per PVC, from consecutive snapshots

Step A is repository-wide. This attributes growth to individual PVCs, which is what
identifies the namespace to act on.

Net logical growth per PVC comes straight from guide 12's `inventory.json` — no
repository connection needed:

```bash
kopia_pvc_growth | tee logical-growth-per-pvc.tsv | column -t -s "$(printf '\t')"
```

```
NAMESPACE  PVC                                FROM                  TO                    DELTA_BYTES  DELTA_MiB
basic-app  basic-app-pvc                      2026-09-15T13:07:46Z  2026-09-15T13:18:39Z  0            0.00
basic-app  basic-app-pvc-2026-07-21-08-24-17  2026-09-15T13:07:49Z  2026-09-15T13:18:40Z  5244736      5.00
```

5 244 736 B is exactly the 3 MiB + 2 MiB written between those two cycles.

`kopia_inventory_pvcs` gives the underlying per-snapshot rows, with the workload and a
`RESOLVED` column — see [../lib/kopia.sh](../lib/kopia.sh). One Kopia repository holds
many PVCs; they are distinguished by `Source.host`, which the library parses for you.

If you are already connected to the repository (guide 12 step 4), the same figures plus
the file-count delta come from `kopia-snapshots.json`:

```bash
# kopia-snapshots.json comes from guide 04 step C
jq -r '
 group_by(.source.host)
 | map( sort_by(.startTime)
        | [ range(1; length) as $i
            | { pvc:   (.[$i].source.host | split(".") | last),
                from:  .[$i-1].startTime,
                to:    .[$i].startTime,
                d_files: (.[$i].stats.fileCount     - .[$i-1].stats.fileCount),
                d_bytes: (.[$i].stats.totalFileSize - .[$i-1].stats.totalFileSize) } ] )
 | flatten
 | (["PVC","FROM","TO","DELTA_FILES","DELTA_BYTES","DELTA_MiB"]|@tsv),
   (.[] | [.pvc,.from,.to,.d_files,.d_bytes,(.d_bytes/1048576*100|round/100)]|@tsv)' \
   "$AUDIT_DIR/04-files-per-pvc/kopia-snapshots.json" \
  | tee logical-growth-per-pvc.tsv | column -t
```

Validated output:

```
PVC                                DELTA_FILES  DELTA_BYTES  DELTA_MiB
basic-app-pvc                      0            0            0
basic-app-pvc-2026-07-21-08-24-17  2            5244736      5
```

5 244 736 B is exactly the 3 MiB + 2 MiB written between the two cycles. The
measurement is sound.

This is **net** growth. A volume where 10 GiB is rewritten in place every night shows
`d_bytes = 0` here while contributing 10 GiB to step A. When step B says zero and step
A says a lot, you have found an in-place-rewrite workload — databases and log-structured
stores behave this way. Say so in the report; it changes the retention recommendation.

### Step C — repository growth over time, as a sanity check

Independent of Kopia's own accounting. Run it twice, a week apart, and difference it.

```bash
# see guide 11 for the mc pod; then, per repository prefix:
kubectl -n "$K10NS" exec objcount -- sh -c \
  'mc alias set t "$S3_ENDPOINT" "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" >/dev/null
   mc du --depth 6 "t/$BUCKET"' | tee "repo-du-$(date -u +%Y%m%dT%H%MZ).txt"
```

Also available from K10's own Prometheus, if the profile is used for exports:

```bash
kq 'catalog_storage_artifact_count' \
  | jq -r '.data.result[] | "\(.metric.category)/\(.metric.retirement)\t\(.value[1])"' \
  | tee artifact-count.tsv
```

### Step D — the indirect estimate, when no repository exists yet

Export metrics can be used to bound the change rate indirectly. Where no Kopia
repository is available, estimate it from volume-level growth:

```bash
# AUDIT_START / AUDIT_END / AUDIT_WINDOW_DAYS are exported by lib/init.sh.
tqr 'max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes)' \
    "$AUDIT_START" "$AUDIT_END" 1h \
  | jq -r '(["NAMESPACE","PVC","FIRST_GiB","LAST_GiB","DELTA_GiB"]|@tsv),
           (.data.result[]
            | (.values | map(.[1]|tonumber)) as $v
            | [ .metric.namespace, .metric.persistentvolumeclaim,
                (($v|first)/1073741824*100|round/100),
                (($v|last)/1073741824*100|round/100),
                ((($v|last)-($v|first))/1073741824*100|round/100) ] | @tsv)' \
  | tee "used-bytes-${AUDIT_WINDOW_DAYS}d-delta.tsv" | column -t
```

The delta covers `AUDIT_WINDOW_DAYS`, not a week. Divide by
`$AUDIT_WINDOW_DAYS` for a daily rate and label the output with the window.

**Treat this as a lower bound only.** It measures net used-space change, misses all
in-place rewrites, and — per guide 04 — is meaningless for NFS-backed PVCs, where every
PVC on the same export reports the same number. Restrict it to `pvc-block.txt`.

## Caveats

- **`kopia content list` enumerates the whole content index.** On a large repository
  that is millions of JSON records — tens of seconds of CPU in the debug pod and a
  large file locally. Stream it to disk, do not hold it in a shell variable.
- **Maintenance rewrites content.** After a full maintenance cycle, compacted content
  gets a *new* timestamp, which inflates the bucket for that day. Cross-check against
  `maintenance-info-stdout.txt` from guide 12 and exclude days where a full cycle ran
  (`full-rewrite-contents`).
- **One repository serves many PVCs.** Step A cannot attribute ingest to a PVC. Use
  step B for attribution and accept that the two answer different questions.
- **A short observation window is worthless.** Change rate needs at least two full
  backup cycles, preferably a week, to include weekly batch jobs. If the repository was
  recently recreated or migrated, say so instead of extrapolating. Note that step A and
  step B read the Kopia repository, which is **not** bounded by
  `AUDIT_WINDOW_DAYS` — Kopia history goes back as far as retention allows, so prefer
  them whenever the Prometheus window is short.
- `originalLength` is pre-compression but post-chunking; it is not identical to the sum
  of file sizes.

## What to send back

| File | Contents |
|------|----------|
| `change-rate-daily.tsv` | physical and logical ingest per day (headline figure) |
| `change-rate-hourly.tsv` | same, hourly — shows the backup window |
| `logical-growth-per-pvc.tsv` | net growth attributed per PVC |
| `repo-du-*.txt` | repository size snapshots for week-over-week differencing |
| `used-bytes-<N>d-delta.tsv` | lower-bound estimate for block PVCs, if used; `<N>` = `AUDIT_WINDOW_DAYS` |
| `contents.json` | raw content index, if size permits |

## Validation status

Steps A and B fully validated on K10 9.0.5, end to end: two backup cycles were run 11
minutes apart with 5 MiB of new data written in between, and both the repository-wide
ingest (20.3 MiB logical / 8.06 MiB physical) and the per-PVC delta (+2 files,
+5 244 736 B) were reproduced exactly. The `cachedFiles` behaviour described above was
observed directly on both volumes, not inferred.

Step C validated mechanically (`mc du` and `catalog_storage_artifact_count` both
return), but week-over-week differencing was not exercised — the validation cluster had
one hour of history. Step D validated as a query; its accuracy claim is not testable on
a cluster with no multi-day PVC growth.
