# 05 — File size distribution per PVC

## What auditors need

The mean file size of each PVC in `$AUDIT_NS`, and — more importantly — the size
*distribution*. Combined with the file count from guide 04 this predicts the shape of
the Kopia workload:

- **Many small files** (mean < 64 KiB): metadata-bound. Runtime scales with file count,
  the Kopia index grows fast, and datamover memory is driven by the in-flight directory
  tree rather than by data volume.
- **Few large files** (mean > 16 MiB): throughput-bound. Runtime scales with bytes and
  network, and the tuning knobs are `k10DataStoreParallelUpload` and the pack size.

**The mean alone is a weak statistic.** A volume with one 14 GiB file and a million
1 KiB files has a reasonable mean and pathological behaviour. Get the histogram — and
since guide 12 leaves you connected to the repository, it costs one read.

## Method

```bash
. lib/init.sh
focus_dir 05-file-size
```

Reuses `kopia-snapshots.json` from guide 04 step 1.

### Step 1 — the mean, from the snapshot tree

```bash
cp "$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/04-files-per-pvc/kopia-snapshots.json" .
kopia_pvcs | tee mean-file-size.tsv | column -t
```

Validated:

```
PVC                   MODE        SNAPSHOTS  LAST_SNAPSHOT        FILES   SIZE_BYTES   AVG_FILE_BYTES  DIRS
calibrate-100k-500kb  filesystem  5          2026-09-20T08:01:37  100003  51200000058  511984          2
```

`AVG_FILE_BYTES` is `rootEntry.summ.fileSize / rootEntry.summ.files` — the tree, not
the run. Dividing by `stats.fileCount` instead is the trap guide 04 documents: on this
PVC it would give 2.56 MB on one snapshot and a division by zero on the next, for a
volume whose true mean never moves off 512 KB.

### Step 2 — the histogram, from the repository

Two commands. No access to the application namespace, no second walk of the volume.

```bash
ROOT=$(awk -F'\t' 'NR==2 {print $9}' "$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/04-files-per-pvc/files-per-pvc.tsv")
kopia_exec "kopia ls -l -r $ROOT" > tree-listing.txt
kopia_histogram tree-listing.txt | tee histogram.tsv | column -t
```

`kopia ls -l -r <rootObjectId>` enumerates exactly `summ.files` regular files, one per
line:

```
-rw-r--r--       512000 2026-09-20 04:07:44 UTC 3281cda877b9226da4bca1b84ac1be28   17390.v1.bin
```

Validated:

```
BUCKET        FILES   BYTES        PCT_FILES
<4KiB         3       58           0.0
4KiB-64KiB    0       0            0.0
64KiB-1MiB    100000  51200000000  100.0
1MiB-16MiB    0       0            0.0
16MiB-256MiB  0       0            0.0
>256MiB       0       0            0.0
TOTAL         100003  51200000058  100.0
min=0 max=512000 mean=511984
```

`TOTAL` must match `FILES` and `SIZE_BYTES` from step 1 — it does here, exactly. If it
does not, the root object id belongs to a different snapshot.

This distribution is unimodal at 512 KB, so the mean is honest for once. Read
`min=0 max=512000` next to it: that is the check on whether the mean means anything.

For a very large tree the listing is large (100,003 files ≈ 8 MB). The generator skips
the histogram above `--histogram-max-files` (2 M by default) for the same reason; do
the same by hand if `FILES` is in the millions.

### Step 3 — for several PVCs

```bash
awk -F'\t' 'NR>1 && $2!="block" {print $1"\t"$9}' \
    "$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/04-files-per-pvc/files-per-pvc.tsv" \
  | while IFS="$(printf '\t')" read -r pvc root; do
      kopia_exec "kopia ls -l -r $root" > "tree-$pvc.txt"
      printf '=== %s ===\n' "$pvc"
      kopia_histogram "tree-$pvc.txt" | column -t
    done | tee histograms-all.txt
```

The `$2!="block"` filter is deliberate — see step 4.

### Step 4 — block-mode volumes have no file sizes

A KubeVirt VM disk is stored as fixed-size chunks. Every chunk is the same size, so a
histogram over them is a single bar that tells you nothing about the guest filesystem
inside. Report the logical size and the block size instead, both from guide 04 step 5:

```
calibrate-5000k-10kb  block  71028 chunks  74478256128 bytes  BlockSzB 0x100000 = 1 MiB
```

An **empty** VM disk is a real and confusing case: a blank DataVolume exports as a
block snapshot with 0 chunks and `summ.fileSize` 0 although the PVC requests
gigabytes. That is correct data — render it as "empty disk", not as `0 B` of a
74 GiB volume.

### Step 5 — Kopia's own content-block distribution

Not a file histogram, but free, and it answers a different question: how well the data
compresses, which is a direct input to the `k10DataStoreDisableCompression`
recommendation.

```bash
kopia_exec 'kopia content stats' | tee content-stats.txt
kopia_exec 'kopia blob stats'    | tee blob-stats.txt
```

Validated `blob stats`:

```
Count: 3452
Total: 71.7 GB
Average: 20.8 MB
Histogram:
        2 between 10 B and 100 B (total 48 B)
       11 between 100 B and 1 KB (total 3.3 KB)
       16 between 1 KB and 10 KB (total 53.6 KB)
        3 between 100 KB and 1 MB (total 1.9 MB)
        5 between 1 MB and 10 MB (total 16.1 MB)
     3415 between 10 MB and 100 MB (total 71.7 GB)
```

3,415 of 3,452 blobs sit in the 10–100 MB bucket against a 20 MB max pack size — packs
are being filled properly. The opposite shape (most blobs under 10 KB) means Kopia is
flushing before packs fill, which multiplies object count and maintenance cost;
guide 11 reads the same file for the object-count argument.

### Step 6 — fallback: PVCs never exported

`pvc_scan_network` (guide 04 step 6) computes the mean as it walks, and will produce a
histogram on request:

```bash
export PVCSCAN_HISTOGRAM=1
export PVCSCAN_HIST_FILE="$PWD/histograms-live.tsv"
: > "$PVCSCAN_HIST_FILE"
pvc_scan_network "$AUDIT_NS" > /dev/null 2> hist-scan.log
column -t -s "$(printf '\t')" "$PVCSCAN_HIST_FILE"
```

Same buckets as `kopia_histogram`, so the two are comparable. The walk uses
`find -exec stat -c '%s' {} +` rather than `find -printf`, because BusyBox — which many
application images use — does not implement `-printf`.

> **`du` measures allocation, `stat` measures size.** The live scan's mean is derived
> from `du`, so on a volume full of files smaller than the block size it is inflated: a
> million 100-byte files on a 4 KiB-block filesystem average out at 4,096, a 40× error
> in the direction that hides the problem. The Kopia-derived mean in step 1 has no such
> bias — another reason to prefer it.

## Caveats

- **The mean hides bimodality.** Always send the histogram if you have it, and say so
  explicitly when you only have the mean.
- **`summ.fileSize` excludes directories and symlinks.** Guide 04's `DIRS` covers those;
  do not add them into the size denominator.
- **Sparse files** (common in VM-image volumes) report a large `stat` size and a small
  `du` size. Kopia stores what it reads, so `summ.fileSize` is the honest number for
  backup sizing.
- **Compressed or deduplicated backends** (ZFS, VDO, some CSI drivers) make the live
  `du` report post-compression allocation. Note the backend alongside any step 6 figure.
- **The histogram is of the tree at the last snapshot**, not of the volume now.

## What to send back

| File | Contents |
|------|----------|
| `histogram.tsv` | size distribution per PVC, from the repository (headline) |
| `mean-file-size.tsv` | mean file size per PVC |
| `tree-listing.txt` | the raw `kopia ls -l -r` output the histogram is built from |
| `blob-stats.txt`, `content-stats.txt` | Kopia's block-size distribution and compression ratio |
| `histograms-live.tsv` | live-walk histogram, only for PVCs never exported |

## Validation status

Fully validated on K10 9.0.5 against `prod-test` / `calibrate-backup`. The histogram
from `kopia ls -l -r` totalled 100,003 files and 51,200,000,058 bytes — an exact match
with `rootEntry.summ` and with `generate-export-topology.py`'s own `sizeHistogram`
buckets for the same PVC, bucket for bucket.

Step 5's output is verbatim from that repository. Step 4's figures come from
`large-test-block` / `calibrate-backup-block`.

The `du`-versus-`stat` bias and the BusyBox `-printf` failure in step 6 are carried
over from an earlier validation cluster; step 6 itself was not re-run here, because
every PVC of this pair has been exported and step 2 is strictly better.
