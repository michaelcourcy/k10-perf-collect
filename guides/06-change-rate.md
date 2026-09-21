# 06 — Change rate

## What auditors need

How much data actually changes between two export cycles for `$AUDIT_NS`. This
determines incremental export duration, repository growth, object-storage cost and how
much maintenance work Kopia has to do. It is the hardest figure on the list to obtain
honestly, and the one most often replaced by a guess.

## Four quantities, all called "change rate"

They differ by an order of magnitude on real data. Collect the first three; the fourth
is the one to distrust.

| Quantity | Definition | Source |
|---|---|---|
| **Physical ingest** | bytes that actually reached the object store, after dedup and compression | Kopia content timestamps, per snapshot |
| **Files changed** | files Kopia had to hash, against files it skipped | `stats.fileCount` / `cachedFiles`, per snapshot |
| **Transferred** | what K10 itself says it moved | ExportAction `/details`, per export |
| **Logical growth** | net change in total file bytes on the volume | `summ.fileSize` delta, per snapshot |

Logical growth is last for a reason: it is **net**, so a volume where 10 GiB is
rewritten in place every night reports **zero**. Databases and log-structured stores
behave exactly that way. When logical growth says nothing changed and physical ingest
says 10 GiB, you have found an in-place-rewrite workload — say so, it changes the
retention recommendation.

## Method

```bash
. lib/init.sh
focus_dir 06-change-rate
cp "$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/04-files-per-pvc/kopia-snapshots.json" .
```

Needs the `debug-kopia-*` pod from guide 12 step 4.

### Step 1 — files changed per snapshot

Free — it is already in the manifests:

```bash
kopia_snapshots | tee files-changed.tsv | column -t
```

```
PVC                   START                DUR_S  FILES   HASHED  UNCHANGED  D_FILES  D_BYTES
calibrate-100k-500kb  2026-09-19T08:14:33  531    100003  100003  0          -        -
calibrate-100k-500kb  2026-09-19T17:01:48  1      100003  0       100003     0        0
calibrate-100k-500kb  2026-09-19T22:01:53  187    100003  20000   80003      0        0
calibrate-100k-500kb  2026-09-20T02:37:56  1      100003  0       100003     0        0
calibrate-100k-500kb  2026-09-20T08:01:37  183    100003  20000   80003      0        0
```

`HASHED / FILES` is the change indicator: 20000/100003 = **20 %** on the two long runs,
0 % on the two short ones. It survives maintenance, unlike the timestamp-based method
below, and costs nothing.

`D_BYTES` is 0 throughout while 20 % of files were rewritten — the in-place-rewrite
signature above, in one table.

Two limits: `HASHED` is 0 on a first snapshot by construction, and it was seen wrongly
0 on an `nfs-csi` volume. Corroborate with step 2 or step 3.

### Step 2 — physical ingest per snapshot

The authoritative measurement. Every content block Kopia writes carries its creation
time, its logical size (`originalLength`) and its stored size (`length`). Blocks whose
timestamp falls inside a snapshot's window are that snapshot's ingest — after dedup and
compression, which is exactly what the object store and the network see.

```bash
kopia_exec 'kopia content list --json' > contents.json
kopia_exec 'kopia maintenance info --json' > kopia-maintenance-info.json
wc -c contents.json

kopia_ingest | tee physical-ingest.tsv | column -t -s "$(printf '\t')"
```

Validated:

```
PVC                   SNAPSHOT_START       PHYSICAL_BYTES  LOGICAL_BYTES  CONTENTS  RATIO  AMBIGUOUS_BYTES  NOTE
calibrate-100k-500kb  2026-09-19T08:14:33  51205587472     51217835489    100011    1      0                -
calibrate-100k-500kb  2026-09-19T17:01:48  589             561            1         1.05   0                -
calibrate-100k-500kb  2026-09-19T22:01:53  10243350085     10257835205    20006     0.999  0                -
calibrate-100k-500kb  2026-09-20T02:37:56  594             566            1         1.049  0                -
calibrate-100k-500kb  2026-09-20T08:01:37  10243352516     10257835217    20006     0.999  0                -
(unattributed)        -                    3151            2955           7         -      0                written outside every snapshot window
```

10.24 GB of 51.2 GB is **20.0 %** — the same answer step 1 gave from a completely
independent field. 20,006 contents against 20,000 hashed files is the same agreement
again.

`RATIO` is physical ÷ logical: the effective dedup-and-compression factor. **1.0 here
because the data is random** and does not compress; encryption overhead pushes the
two near-empty snapshots slightly above 1.0. A ratio of 0.4 would mean 60 % reduction.

Three things this function does that a naive per-window sum does not:

- **Each block is attributed to exactly one snapshot**, the nearest window by midpoint.
  The PVCs of a namespace are exported concurrently, so their windows overlap; summing
  per window double-counts. Blocks that fell in more than one window are still counted
  once and their bytes reported as `AMBIGUOUS_BYTES`, so the guess is visible.
- **Snapshots older than the last full maintenance report `UNRECOVERABLE`, never 0.**
  See the warning below.
- **Blocks matching no window are reported** as `(unattributed)` rather than dropped —
  maintenance rewrites, or an export whose snapshot has been retired.

> **Physical ingest has a shelf life of one maintenance cycle.**
> `full-rewrite-contents` re-stamps every rewritten content and pack blob with the
> maintenance time, after which per-snapshot attribution is gone for good. The default
> full cycle is 24 h. Run this guide within a day of the exports you care about, and
> check when the last full run was:
>
> ```bash
> jq -r '.schedule.runs["full-rewrite-contents"] | last | {start,end,success}' \
>    kopia-maintenance-info.json
> ```
>
> Validated: `2026-09-19T08:14:41Z`, success. Everything before that timestamp would
> read `UNRECOVERABLE`.

### Step 3 — what K10 says it transferred

Independent of Kopia, and it **survives maintenance** — the one measurement that does.

```bash
export_table 10 | tee export-change-rate.tsv | column -t
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

**`CHANGE_RATE` 0.2012 against a workload rewriting exactly 20,000 of 100,003 files.**
Three independent measurements — hashed files, Kopia content bytes, K10's own
`transferredBytes` — agree to three significant figures. And the first export reads
1.0: everything is new.

`CHANGE_RATE` is `transferredBytes` divided by the logical size of the PVC snapshots
the export took. It can exceed 1.0 on incompressible small files, because
`transferredBytes` includes Kopia's directory entries and per-block encryption
overhead — 105 % was observed on a 5 M-file volume of random data. It is the best
single figure available, not a measured change rate.

`CAPACITY` is `progressDetails.totalBytes`, which is the **volume capacity**
(79.5 GB = the 74 GiB PVC), not the data size. Do not read it as a denominator.

> **These counters are only on the `/details` subresource.** On the ExportAction object
> itself `status.progressDetails` and `status.actionDetails` are both `null`, which is
> why they were long believed not to exist. Verified on this cluster: the object
> reported `null` while
> `GET .../exportactions/scheduled-gn9w65njrc/details` returned
> `transferredBytes: 10300000000` — within 0.6 % of Kopia's 10,243,352,516 for the same
> window. Each fetch costs about 0.2 s and 400 kB, so fetch the exports you are
> analysing, not the whole history.

### Step 4 — when the tool and the workload disagree, suspect the workload

A change rate near 100 % on every export usually is not a measurement error.

On this cluster the 5 M-file calibration pod crash-looped on **inode exhaustion** —
74 GiB ext4 gives 4,849,664 inodes at the default 16 KiB ratio, so `df -i` hit 100 %
while blocks were at 77 %. The pod died before writing its `touch initial` marker and
regenerated every file from scratch on each restart, so each export really was ~100 %
new data. K10's `readBytes` / `processedBytes` / `transferredBytes` and Kopia's
hashed-versus-unchanged all agreed with each other, and all of them were right.

```bash
kubectl -n "$AUDIT_NS" get pods -o wide | tee pods.txt
kubectl -n "$AUDIT_NS" get events --sort-by=.lastTimestamp | tail -30 | tee events.txt
```

Also: Kopia's incremental base can be a **checkpoint of a failed export**, so
`UNCHANGED` may exceed the previous complete snapshot's file count.

### Step 5 — repository growth, as an independent sanity check

```bash
kopia_exec 'kopia blob stats' | tee "blob-stats-$(date -u +%Y%m%dT%H%MZ).txt"
```

Run it twice a week apart and difference the totals. Independent of Kopia's own
per-snapshot accounting and of K10's counters.

### Step 6 — when no repository exists yet

Only for a pair that has never exported. Bound from volume-level growth:

```bash
tqr 'max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes{namespace="'"$AUDIT_NS"'"})' \
    "$AUDIT_START" "$AUDIT_END" 1h \
  | jq -r '(["PVC","FIRST_GiB","LAST_GiB","DELTA_GiB"]|@tsv),
           (.data.result[]
            | (.values | map(.[1]|tonumber)) as $v
            | [ .metric.persistentvolumeclaim,
                (($v|first)/1073741824*100|round/100),
                (($v|last)/1073741824*100|round/100),
                ((($v|last)-($v|first))/1073741824*100|round/100) ] | @tsv)' \
  | tee "used-bytes-${AUDIT_WINDOW_DAYS}d-delta.tsv" | column -t
```

**A lower bound only.** It measures net used-space change, misses every in-place
rewrite, and per guide 04 is meaningless for NFS-backed PVCs. The delta covers
`AUDIT_WINDOW_DAYS`, not a week — divide and label it.

## Caveats

- **`kopia content list` enumerates the whole content index.** 140,032 records / 32 MB
  on this repository, and millions on a large one. Stream it to disk; never hold it in
  a shell variable.
- **A short observation window is worthless.** Change rate needs at least two full
  export cycles, preferably a week, to include weekly batch jobs. Note that steps 1–3
  read the repository and the action history, which are **not** bounded by
  `AUDIT_WINDOW_DAYS` — prefer them whenever the Prometheus window is short.
- **`originalLength` is pre-compression but post-chunking**; it is not identical to the
  sum of file sizes.
- **Block-mode volumes** have no `HASHED`/`UNCHANGED` (step 1 prints `-`), because
  `cachedFiles` holds the block size there. Steps 2 and 3 work normally.

## What to send back

| File | Contents |
|------|----------|
| `physical-ingest.tsv` | bytes that reached the object store, per snapshot (headline) |
| `export-change-rate.tsv` | K10's own transferred bytes and change rate, per export |
| `files-changed.tsv` | hashed versus unchanged files, per snapshot |
| `kopia-maintenance-info.json` | when the last full maintenance re-stamped the contents |
| `blob-stats-*.txt` | repository size snapshots for week-over-week differencing |
| `contents.json` | the raw content index, if size permits |

## Validation status

Fully validated on K10 9.0.5 against `prod-test` / `calibrate-backup`, a volume of
100,003 files of 512 KB each with a known 20,000-file rewrite between cycles. **Three
independent measurements agreed**: 20,000/100,003 files hashed (step 1),
10,243,352,516 of 51,200,000,058 bytes physically ingested (step 2, 20.0 %) and a
`CHANGE_RATE` of 0.2012 from K10's own `transferredBytes` (step 3). The first export
read 1.0 on all three.

Step 2's figures match `generate-export-topology.py` byte for byte on the same
snapshots (51,205,587,472 / 589 / 10,243,350,085 / 594), including the nearest-window
attribution and the 1-second window padding.

Step 3's `/details` behaviour was verified directly: `progressDetails` null on the
object, populated on the subresource.

Step 5's week-over-week differencing was not exercised — it needs two runs a week
apart. Step 6 was validated as a query; its accuracy claim is not testable on a pair
that has a repository.
