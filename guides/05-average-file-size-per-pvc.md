# 05 — Average file size per PVC

## What Global Engineering needs

The mean file size of each PVC, and ideally the size *distribution*. Combined with the
file count from guide 04 this predicts the shape of the Kopia workload:

- **Many small files** (mean < 64 KiB): the backup is metadata-bound. Runtime scales
  with file count, the Kopia index grows fast, and datamover memory is driven by the
  in-flight directory tree rather than by data volume.
- **Few large files** (mean > 16 MiB): the backup is throughput-bound. Runtime scales
  with bytes and network, and the dominant tuning knobs are
  `k10DataStoreParallelUpload` and the pack size.

The mean alone is a weak statistic — a volume with one 14 GiB file and a million 1 KiB
files has a "reasonable" mean and pathological behaviour. Collect the histogram where
you can.

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
mkdir -p "$AUDIT_DIR/05-avg-file-size" && cd "$AUDIT_DIR/05-avg-file-size"
```

### Step A — preferred: derive it from the Kopia snapshot stats

Exact, zero impact on the application namespace, works on NFS and block alike. Reuses
`kopia-snapshots.json` from guide 04 step C.

```bash
jq -r '(["PVC","FILES","TOTAL_BYTES","AVG_FILE_BYTES","AVG_HUMAN"]|@tsv),
       (.[] | . as $s
        | ($s.stats.fileCount) as $n | ($s.stats.totalFileSize) as $b
        | [ ($s.source.host | split(".") | last),
            $n, $b,
            (if $n > 0 then ($b/$n|round) else 0 end),
            (if $n > 0 then
               ($b/$n) as $a
               | if   $a < 1024        then "\($a|round) B"
                 elif $a < 1048576     then "\(($a/1024*10|round)/10) KiB"
                 elif $a < 1073741824  then "\(($a/1048576*10|round)/10) MiB"
                 else "\(($a/1073741824*100|round)/100) GiB" end
             else "-" end) ] | @tsv)' \
   "$AUDIT_DIR/04-files-per-pvc/kopia-snapshots.json" \
  | tee avg-file-size-from-kopia.tsv \
  | column -t -s "$(printf '\t')"
```

`kopia snapshot list --all` returns **every** snapshot, so a PVC backed up 30 times
appears 30 times. Keep only the most recent per PVC:

```bash
jq -r 'group_by(.source.host)
       | map(sort_by(.startTime) | last)
       | (["PVC","SNAPSHOT_TIME","FILES","TOTAL_BYTES","AVG_FILE_BYTES"]|@tsv),
         (.[] | [ (.source.host|split(".")|last), .startTime,
                  .stats.fileCount, .stats.totalFileSize,
                  (if .stats.fileCount > 0 then (.stats.totalFileSize/.stats.fileCount|round) else 0 end) ]|@tsv)' \
   "$AUDIT_DIR/04-files-per-pvc/kopia-snapshots.json" \
  | tee avg-file-size-latest.tsv | column -t -s "$(printf '\t')"
```

Validated output from the validation cluster, after two backup cycles with 5 MiB of new
data written between them:

```
PVC                                FILES  TOTAL_BYTES  AVG_FILE_BYTES  AVG_HUMAN
basic-app-pvc                      1      2726         2726            2.7 KiB
basic-app-pvc                      1      2726         2726            2.7 KiB
basic-app-pvc-2026-07-21-08-24-17  1      14068509     14068509        13.4 MiB
basic-app-pvc-2026-07-21-08-24-17  3      19313245     6437748         6.1 MiB
```

Note how the mean for the second PVC *dropped* from 13.4 MiB to 6.1 MiB simply because
two smaller files were added. The mean is a moving target; that is the point of also
collecting the histogram.

### Step B — for PVCs with no restore point: the scan already has it

`pvc_scan_network` (guide 04 step B) computes the mean as it walks, so there is nothing
extra to run — `AVG_FILE_BYTES` is `USED_KIB * 1024 / FILES`:

```bash
awk -F'\t' 'NR==1 || $6 > 0 { print $1"/"$2"\t"$6"\t"$9"\t"$10 }' \
    "$AUDIT_DIR/04-files-per-pvc/network-pvc-scan.tsv" \
  | { printf 'PVC\tFILES\tUSED_KIB\tAVG_FILE_BYTES\n'; tail -n +2; } \
  | column -t -s "$(printf '\t')"
```

Validated output:

```
PVC                                 FILES  USED_KIB  AVG_FILE_BYTES
basic-app/basic-app-pvc             1      8         8192
cpd/cc-home-pvc                     574    15028     26810
cpd/file-api-claim                  494    20872     43265
cpd/volumes-datarefinerylibvol-pvc  14498  1066728   75343
cpd/ws-runtimes-libs-pvc            225    385632    1755054
```

The spread is the point: 26 KiB on one volume and 1.75 MiB on another, in the same
namespace. `ws-runtimes-libs-pvc` is throughput-bound and
`volumes-datarefinerylibvol-pvc`, with 14 498 files, is metadata-bound — they need
opposite tuning.

> **`du` measures allocation, `stat` measures size.** `AVG_FILE_BYTES` is derived from
> `du`, so on a volume full of files smaller than the block size it is inflated — a
> million 100-byte files on a 4 KiB-block filesystem average out at 4 096, a 40× error
> in the direction that hides the problem. When the mean lands suspiciously close to the
> block size, get the histogram instead (step C).

### Step C — the size histogram

Much more informative than the mean, and the scanner will produce it on request:

```bash
export PVCSCAN_HISTOGRAM=1
export PVCSCAN_HIST_FILE="$AUDIT_DIR/05-avg-file-size/histograms.tsv"
: > "$PVCSCAN_HIST_FILE"

pvc_scan_network > /dev/null 2> hist-scan.log
column -t -s "$(printf '\t')" "$PVCSCAN_HIST_FILE"
```

Buckets are `<4KiB`, `4KiB-64KiB`, `64KiB-1MiB`, `1MiB-16MiB`, `16MiB-256MiB`,
`>256MiB`. The walk is done with `find -exec stat -c '%s' {} +` rather than
`find -printf`, because BusyBox — which many application images use — does not implement
`-printf`.

This is a second full walk, so run it only for the volumes that matter:

```bash
pvc_scan_one cpd volumes-datarefinerylibvol-pvc > /dev/null
```

### Step D — repository-wide size distribution, for free

Kopia already computes a histogram of the *content blocks* it stores. It is not a file
histogram, but it is a good proxy for whether the workload is small-file or large-file
shaped, and it costs nothing. From the guide 12 diagnose bundle:

```bash
cat "$AUDIT_DIR/12-kopia/"*/kopia-debug-logs/content-stats-stdout.txt
cat "$AUDIT_DIR/12-kopia/"*/kopia-debug-logs/blob-stats-stdout.txt
```

Validated output:

```
Count: 12
Total Bytes: 14.1 MB
Total Packed: 2.8 MB (compression 80.0%)
By Method:
  (uncompressed)         count: 5 size: 2.6 KB
  s2-default             count: 4 size: 14.1 MB packed: 2.8 MB compression: 80.0%
  zstd-fastest           count: 3 size: 888 B packed: 712 B compression: 19.8%
Average: 1.2 MB
```

The compression ratio in that output is itself a recommendation input — 80 % on this
dataset means `k10DataStoreDisableCompression` should stay at its default.

## Caveats

- **`du` measures allocation, `stat` measures size.** On a volume with a 4 KiB block
  size and a million 100-byte files, `du`-derived mean is 4 096 B and the true mean is
  100 B — a 40× error in the direction that hides the problem. When the mean from step
  B lands suspiciously close to the filesystem block size, switch to step C.
- **The mean hides bimodality.** Always report the histogram if you have it, and say
  so explicitly when you only have the mean.
- **Sparse files** (common in VM-image volumes) report a large `stat` size and small
  `du` size. For KubeVirt/CNV volumes, `du` is the honest number for backup sizing.
- **Kopia's `totalFileSize` excludes directories and symlinks.** Guide 04's
  `dirCount` covers those; do not add them into the size denominator.
- **Block-mode volumes have no file sizes.** VM disks are stored as fixed-size chunks
  (guide 04 step C); a mean or histogram over them is meaningless — report the logical
  size from `rootEntry.summ.fileSize` and the block size, nothing else.
- Compressed or deduplicated backends (ZFS, VDO, some CSI drivers) make `du` report
  post-compression allocation. Note the backend alongside the figure.

## What to send back

| File | Contents |
|------|----------|
| `avg-file-size-from-kopia.tsv` | mean file size per PVC, derived from backup stats (preferred) |
| `histogram-<namespace>-<pvc>.txt` | size distribution per PVC, from step C |
| `du-means.tsv` | step B output for PVCs with no restore point |
| `content-stats-stdout.txt`, `blob-stats-stdout.txt` | Kopia's own block-size distribution and compression ratio |

## Validation status

Fully validated on K10 9.0.5. The Kopia-derived mean (step A) was cross-checked against
an in-pod `stat` walk of the same volume and matched. The BusyBox histogram in step C
was executed successfully inside an application container whose only shell was
`/bin/busybox`; note that `find -printf` failed there, which is why the guide uses
`-exec stat -c` instead. Step D output is verbatim from the validation cluster's
`content-stats-stdout.txt`.
