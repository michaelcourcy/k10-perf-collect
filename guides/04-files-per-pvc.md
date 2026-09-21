# 04 — Number of files per PVC

## What auditors need

The file count of every PVC in `$AUDIT_NS`. For the file-level path, K10's runtime is
driven far more by file *count* than by total bytes: each file is stat'd, hashed and
indexed. A 10 GiB volume holding 5 million small files can take longer — and more
datamover memory — than a 500 GiB volume holding 50 large ones.

## The right source: the Kopia repository

If the PVC has ever been exported, K10 already counted its files during the backup.
That count is exact, costs one repository read, needs no access to the application
namespace, and works identically for NFS, block-backed and VM-disk volumes.

Guide 12 step 4 leaves a `debug-kopia-*` pod connected read-only to the pair's
repository. **Run guide 12 first.**

### The field that is not the file count

`stats.fileCount` looks like the file count and is not. It is the number of files
Kopia **hashed in that run**, so it collapses as soon as the volume stops changing.
Measured on the validation cluster, one PVC, five consecutive snapshots:

```
START                stats.fileCount  summ.files  cachedFiles
2026-09-19T08:14:33  100003           100003      0
2026-09-19T17:01:48  0                100003      100003
2026-09-19T22:01:53  20000            100003      80003
2026-09-20T02:37:56  0                100003      100003
2026-09-20T08:01:37  20000            100003      80003
```

The volume holds 100,003 files throughout. `stats.fileCount` reports **0** on two of
the five snapshots. Reading it as a file count is wrong by a factor of five, or
infinite.

The tree's real count is `rootEntry.summ.files`, and its size is
`rootEntry.summ.fileSize`. `stats.fileCount` and `stats.cachedFiles` are still worth
having — as *hashed* and *unchanged*, they are a per-PVC change indicator (guide 06).
The helpers in [../lib/kopia.sh](../lib/kopia.sh) take all four from the right places
and label them accordingly; `generate-export-topology.py` does the same, which is why
the two agree.

## Method

```bash
. lib/init.sh
focus_dir 04-files-per-pvc
```

### Step 1 — pull the snapshot manifests once

```bash
kopia_exec 'kopia snapshot list --all --json' > kopia-snapshots.json
wc -c kopia-snapshots.json
```

`kopia_exec` runs the command inside the debug pod with `KOPIA_CONFIG_PATH` set, passes
it as an **argument** to `sh -c` and reads stdout only. Both details matter: feeding the
command on stdin (`sh -s`) intermittently truncates large output, and Kopia writes
log-directory noise to stderr that would corrupt the JSON.

### Step 2 — the headline table

```bash
kopia_pvcs | tee files-per-pvc.tsv | column -t
```

Validated:

```
PVC                   MODE        SNAPSHOTS  LAST_SNAPSHOT        FILES   SIZE_BYTES   AVG_FILE_BYTES  DIRS  ROOT_OBJECT_ID
calibrate-100k-500kb  filesystem  5          2026-09-20T08:01:37  100003  51200000058  511984          2     Ike67fd106c9ac17a355facae013021948
```

One row per PVC, from its most recent **complete** snapshot. `DIRS` next to `FILES` is
not padding: 100,003 files in **2** directories is the shape that makes an export
file-count-bound, because Kopia enumerates and `stat`s serially *within* a directory
(see guide 13).

`ROOT_OBJECT_ID` is the input to the size histogram in guide 05.

### Step 3 — per snapshot, with the change indicator

```bash
kopia_snapshots | tee snapshots-per-pvc.tsv | column -t
```

Validated:

```
PVC                   MODE        START                END                  DUR_S  FILES   SIZE_BYTES   HASHED  UNCHANGED  DIRS  D_FILES  D_BYTES
calibrate-100k-500kb  filesystem  2026-09-19T08:14:33  2026-09-19T08:23:24  531    100003  51200000058  100003  0          2     -        -
calibrate-100k-500kb  filesystem  2026-09-19T17:01:48  2026-09-19T17:01:49  1      100003  51200000058  0       100003     2     0        0
calibrate-100k-500kb  filesystem  2026-09-19T22:01:53  2026-09-19T22:05:00  187    100003  51200000058  20000   80003      2     0        0
calibrate-100k-500kb  filesystem  2026-09-20T02:37:56  2026-09-20T02:37:57  1      100003  51200000058  0       100003     2     0        0
calibrate-100k-500kb  filesystem  2026-09-20T08:01:37  2026-09-20T08:04:40  183    100003  51200000058  20000   80003      2     0        0
```

`FILES` is constant — the tree. `HASHED` / `UNCHANGED` is the work: 0/100003 on a run
with nothing to do (1 second), 20000/80003 on a run with 20 % churn (183 seconds).
`DURATION` tracks `HASHED`, not `FILES`. That relationship is the whole sizing argument.

`HASHED` is 0 on a first snapshot by construction, and it was seen wrongly 0 on an
`nfs-csi` volume, so corroborate it with the byte counters in guide 06 rather than
trusting it alone.

### Step 4 — exports still in flight

```bash
kopia_checkpoints | column -t
```

While a long export runs, Kopia writes an **incomplete** manifest every 45 minutes so
an interrupted upload can resume. `snapshot list --all` returns these with the same
`startTime` as the running snapshot and `incomplete: "checkpoint"`. They are not
restore points — counting them is how one export appears as "3 snapshots with the same
timestamp". `kopia_pvcs` and `kopia_snapshots` exclude them; this is where they show up,
with the files and bytes uploaded so far.

Validated: empty on this pair — no export was in flight.

### Step 5 — block-mode volumes

A volume exported in block mode — every KubeVirt VM disk — is a tree of fixed-size
chunks, not files. The helpers detect it (`description` starts `volume:`, or
`source.path` starts `/volume/`) and suppress the columns that would be nonsense:

```
PVC                   MODE   SNAPSHOTS  LAST_SNAPSHOT        FILES  SIZE_BYTES   AVG_FILE_BYTES  DIRS
calibrate-5000k-10kb  block  3          2026-09-20T11:01:39  71028  74478256128  -               43
```

`FILES` is the **chunk** count. `AVG_FILE_BYTES`, `HASHED` and `UNCHANGED` are `-`,
because in block mode `stats.totalFileSize` is null and `stats.cachedFiles` holds the
block size rather than a count. The real logical size is `rootEntry.summ.fileSize`.
The block size itself:

```bash
kopia_exec "kopia ls $(awk -F'\t' 'NR==2{print $9}' files-per-pvc.tsv)" | grep BlockSzB
```

Validated: `meta:BlockSzB:100000` — hexadecimal, so 1 MiB.

A file histogram over a block volume is meaningless; guide 05 says so and skips it.

### Step 6 — fallback: PVCs that have never been exported

Only for volumes with no snapshot in the repository. Everything above is cheaper and
exact; this walks the live filesystem and generates real metadata IOPS.

```bash
export PVCSCAN_TIMEOUT=300
pvc_scan_network "$AUDIT_NS" > network-pvc-scan.tsv 2> scan.log
column -t -s "$(printf '\t')" network-pvc-scan.tsv
```

`pvc_scan_network` ([../lib/pvcscan.sh](../lib/pvcscan.sh)) picks a method per PVC:
`pod-exec` when a running pod already mounts the PVC at its root and has `find` and
`du`; `inspector` when nothing mounts it, the only mounts are `subPath`, or the
container lacks the tools; `skipped` for an RWO volume still attached elsewhere.

Those conditions exist to avoid a **silently wrong** answer, and all three were hit on
an earlier validation cluster: a pod mounting `cc-home-pvc` with
`subPath: _global_/config` measured **0** files for a volume holding 574; `find` was
missing from several application images, so `find | wc -l` returned 0 and the row
looked like an empty volume.

Self-check — `FILES=0` with non-trivial `USED_KIB` means the walk did not run:

```bash
awk -F'\t' 'NR>1 && $6==0 && $9>64 {print "SUSPECT: "$1"/"$2" files=0 used_kib="$9}' \
    network-pvc-scan.tsv
```

### Why not the kubelet inode metric

`kubelet_volume_stats_inodes_used` is free and needs no pod, and it is **correct for
block-backed volumes and wrong for NFS/SMB**. For a network mount the kubelet runs
`statfs` on the mount point, which returns the statistics of the entire remote export.
Measured across 14 `nfs-csi` PVCs on an earlier validation cluster, every one reported
18,366 inodes and 2.95 TiB capacity — the NFS server's filesystem, reported 14 times.
The actual content of one of them was **one file**. `df -i` inside a pod has the same
flaw, for the same reason.

If you need it anyway, for block volumes only:

```bash
tq 'max by (persistentvolumeclaim) (kubelet_volume_stats_inodes_used{namespace="'"$AUDIT_NS"'"})' \
  | jq -r '.data.result[] | [.metric.persistentvolumeclaim, .value[1]] | @tsv' \
  | tee inodes.tsv | column -t
```

`inodes_used` counts files **plus** directories plus symlinks, so it is not comparable
with `FILES` from step 2 without adding `DIRS`.

## Caveats

- **A snapshot's file count is from backup time, not now.** Fine for sizing; the
  timestamp is in the table next to it.
- **`fileCount` 0 with a populated tree** happens on CloudNativePG-style volumes:
  `stats.fileCount` is 0 while `totalFileSize` is set and `kopia ls -r` lists real
  files. The tree listing wins — guide 05 step 1 recovers the count from it.
- **`find | wc -l` under-reports if filenames contain newlines.** Use
  `find /target -xdev -type f -exec printf '.' \; | wc -c` if you suspect that.
- **The step 6 walk is read-only but not free.** On NFS expect minutes to tens of
  minutes per volume. Run it off-peak and bound it with `PVCSCAN_TIMEOUT`.

## What to send back

| File | Contents |
|------|----------|
| `files-per-pvc.tsv` | file count, size, mean and directory count per PVC (headline) |
| `snapshots-per-pvc.tsv` | per snapshot: tree count, hashed, unchanged, duration |
| `kopia-snapshots.json` | the raw manifests — guides 05 and 06 read this file |
| `network-pvc-scan.tsv`, `scan.log` | live walk, only for PVCs never exported |
| `inodes.tsv` | kubelet inode counts, block volumes only |

## Validation status

Fully validated on K10 9.0.5 against `prod-test` / `calibrate-backup`, and cross-checked
field by field against `generate-export-topology.py` on the same pair: identical
`fileCount` (100,003), `totalSizeBytes` (51,200,000,058), `averageFileSizeBytes`
(511,984) and `dirCount` (2).

The `stats.fileCount` defect in the table above was reproduced directly: two of five
snapshots report 0 for a volume that never changed size. Block mode was validated
separately against `large-test-block` / `calibrate-backup-block` — 71,028 chunks,
`stats.totalFileSize` null, `summ.fileSize` 74,478,256,128,
`stats.cachedFiles` 1,048,576 (the block size, not a count) and
`meta:BlockSzB:100000`.

The NFS `statfs` aliasing and the step 6 method-selection traps are quoted from an
earlier validation cluster that had `nfs-csi` volumes and `subPath` mounts; this
cluster has neither, so those paths were not re-exercised here.
