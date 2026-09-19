# 04 — Number of files per PVC

## What Global Engineering needs

The file count of every PVC in a protected namespace. For the file-level (generic)
backup path, K10's runtime is driven far more by file *count* than by total bytes: each
file is stat'd, hashed and indexed. A 10 GiB volume holding 5 million small files can take
longer — and more datamover memory — than a 500 GiB volume holding 50
large ones.

## The trap: kubelet inode metrics lie on network filesystems

The obvious answer is `kubelet_volume_stats_inodes_used`, one series per PVC, free from
Prometheus, no pod exec required. **It is correct for block volumes and wrong for
NFS/SMB volumes.**

For a network-mounted volume the kubelet runs `statfs` on the mount point, which
returns the statistics of the **entire remote export**, not of the subdirectory that
backs this particular PVC. Every PVC served by the same NFS server reports the same
number.

Measured on the validation cluster — 14 different `nfs-csi` PVCs across two namespaces:

```
cpd/cc-home-pvc                                 18366
cpd/conn-home-pvc                               18366
cpd/datastage-ibm-datastage-ds-storage-pvc      18366
cpd/elasticsearch-master-snapshot-repo          18366
cpd/file-api-claim                              18367
nfs-storage/nfs-pv-claim                        18366
basic-app/basic-app-pvc-2026-07-21-08-24-17     18366
```

Identical values, identical `usedBytes` (1.46 GiB) and identical `capacityBytes` (2.95
TiB) — the NFS server's filesystem, reported 14 times. The actual content of
`basic-app-pvc-2026-07-21-08-24-17` was **one** file.

`df -i` inside a pod has the same flaw, for the same reason: it is the same `statfs`
call. Verified in-pod on that volume: `df -i /data` reported 18 370 inodes used against
a real file count of 1.

**So: use inode metrics only for block volumes. For everything else, count files.**

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
mkdir -p "$AUDIT_DIR/04-files-per-pvc" && cd "$AUDIT_DIR/04-files-per-pvc"
```

### Step 0 — split PVCs into block-backed and network-backed

```bash
# StorageClasses whose provisioner is a network/shared filesystem.
# Note file.csi.azure.com (Azure Files) — matching on the string "azurefile" alone misses it.
kubectl get sc -o json \
  | jq -r '.items[] | select(.provisioner | test("nfs|smb|cifs|file\\.csi|azurefile|efs|filestore|gluster|cephfs"; "i"))
           | .metadata.name' | sort > sc-network.txt
cat sc-network.txt

kubectl get pvc -A -o json \
  | jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name)\t\(.spec.storageClassName // "none")"' \
  > pvc-sc.tsv

awk -F'\t' 'NR==FNR{net[$1];next} ($2 in net){print $1}' sc-network.txt pvc-sc.tsv > pvc-network.txt
awk -F'\t' 'NR==FNR{net[$1];next} !($2 in net){print $1}' sc-network.txt pvc-sc.tsv > pvc-block.txt
wc -l pvc-network.txt pvc-block.txt
```

Check the provisioner list against your own backends before trusting the split — the
regex is a heuristic, and a vendor CSI driver with an opaque name will be
misclassified as block-backed. Confirm with `kubectl get sc -o custom-columns='NAME:.metadata.name,PROV:.provisioner'`.

### Step A — block-backed PVCs: free and accurate, from Prometheus

Requires the Thanos helpers from guide 00 §5. `max by (...)` collapses the duplicate
series that RWX volumes produce, one per mount.

```bash
# No header here: "sort" would scatter it into the data, and then "head -1" picks up a
# data row instead - which silently copies one network-backed PVC into the block file.
tq 'max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_inodes_used)' \
  | jq -r '.data.result[] | [.metric.namespace, .metric.persistentvolumeclaim, .value[1]] | @tsv' \
  | sort > inodes-all.tsv

# Keep only the block-backed ones, matching ns/pvc exactly, and add the header at the end.
{ printf 'NAMESPACE\tPVC\tINODES_USED\n'
  awk -F'\t' 'NR==FNR { keep[$1]; next } ($1"/"$2) in keep' pvc-block.txt inodes-all.tsv
} | tee inodes-block.tsv | column -t
```

`inodes_used` counts files **plus** directories plus symlinks. For sizing purposes that
is the right number — each one is an object the datamover must walk.

One row will look like an escapee and is not: the PVC that **backs** an in-cluster NFS
server is itself a block volume, so it stays in this file, and it legitimately reports
the inode count of the whole export tree. On the validation cluster
`nfs-storage/nfs-pv-claim` is `managed-csi` and reports 18 379 inodes — the same figure
the NFS *clients* alias to, because it is the same filesystem seen from the server side.
That one is the true count; the client-side copies are the lie.

Without cluster monitoring, read the same data straight from each kubelet:

```bash
for n in $(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
  kubectl get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
    | jq -r --arg node "$n" '.pods[]? | select(.volume) | .volume[]
             | select(.pvcRef) | [$node, .pvcRef.namespace, .pvcRef.name, .inodesUsed, .usedBytes, .capacityBytes] | @tsv'
done | sort -u | tee kubelet-summary-volumes.tsv | column -t
```

### Step B — network-backed PVCs: walk the tree

There is no shortcut: something has to traverse the filesystem. `pvc_scan_network`
does the whole set in one pass — it comes from [../lib/pvcscan.sh](../lib/pvcscan.sh).

```bash
export PVCSCAN_TIMEOUT=300          # seconds per PVC for the walk; raise for big volumes
pvc_scan_network > network-pvc-scan.tsv 2> scan.log
column -t -s "$(printf '\t')" network-pvc-scan.tsv
```

For a single volume, or to re-do one that timed out:

```bash
pvc_scan_one cpd cc-home-pvc
```

Validated output:

```
NAMESPACE  PVC                              METHOD     FILES  DIRS  USED_KIB  AVG_FILE_BYTES  STATFS_INODES
basic-app  basic-app-pvc                    inspector  1      1     8         8192            18380
cpd        cc-home-pvc                      inspector  574    164   15028     26810           18380
cpd        file-api-claim                   inspector  494    65    20872     43265           18380
cpd        volumes-datarefinerylibvol-pvc   inspector  14498  1589  1066728   75343           18380
cpd        ws-runtimes-libs-pvc             inspector  225    15    385632    1755054         18380
```

The `STATFS_INODES` column is deliberately kept beside the real count: every NFS PVC on
that cluster reports 18 380, while the true counts range from 1 to 14 498. It is the
evidence that the cheap method cannot be used here.

#### How it picks a method

Per PVC, in order:

| `METHOD` | When | Cost |
|----------|------|------|
| `pod-exec` | a Running pod already mounts the PVC **at its root** and has `find` and `du` | no new workload |
| `inspector` | nothing mounts it, or the only mounts are `subPath`, or the container lacks the tools | one short-lived read-only pod |
| `skipped` | `ReadWriteOnce` and still attached to a node | nothing to do |
| `failed` | the inspector could not be scheduled — reason recorded in the last column | — |

Three of those conditions exist to avoid a **silently wrong** answer, and all three were
hit on the validation cluster:

- **`subPath` mounts.** Every pod mounting `cpd/cc-home-pvc` does so with
  `subPath: _global_/config`, i.e. one subdirectory. Measuring that pod reported
  **0 files** for a volume that actually holds 574. Such a pod is rejected and an
  inspector mounts the PVC root instead.
- **Missing tools.** `find` is not installed in several application images. Without a
  probe, `find ... | wc -l` returns `0` and the row looks like an empty volume rather
  than a failure. `cpd/file-api-claim` read 0 files instead of 494 for this reason.
- **Leftover inspectors.** A pod from an interrupted run still mounts its PVC and would
  be selected as "a pod already mounts this". They are deleted before a scan starts.

#### Self-check

`FILES=0` with a non-trivial `USED_KIB` means the walk did not really run:

```bash
awk -F'\t' 'NR>1 && $6==0 && $9>64 {print "SUSPECT: "$1"/"$2" files=0 used_kib="$9}' \
    network-pvc-scan.tsv
```

That should print nothing. `USED_KIB` of 4 with 0 files is a genuinely empty volume — the
bare directory inode — not a failure.

#### Tunables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PVCSCAN_TIMEOUT` | 300 | seconds per PVC; a timeout marks the row a lower bound rather than failing it |
| `PVCSCAN_IMAGE` | UBI 9 minimal | inspector image — point it at your mirror on an air-gapped cluster |
| `PVCSCAN_HISTOGRAM` | 0 | also emit a file-size histogram, into `$PVCSCAN_HIST_FILE` |
| `PVCSCAN_KEEP_POD` | 0 | leave inspector pods for debugging |

Restrict the scan to the namespaces guide 01 put in scope:

```bash
pvc_scan_network $(cat "$AUDIT_DIR/01-scope/audit-scope-namespaces.txt") \
  > network-pvc-scan.tsv 2> scan.log
```

#### Impact to plan for

The walk generates real metadata IOPS against the storage backend and on NFS can take
minutes per volume. It is read-only, but it is not free — run it off-peak, and use
`PVCSCAN_TIMEOUT` to bound it. Inspector pods mount `readOnly: true`, run as non-root
with all capabilities dropped, and are compatible with OpenShift's `restricted-v2` SCC.

### Step C — the cheapest and most accurate source: ask Kopia

If the PVC has ever been exported, K10 already counted its files during the backup and
stored the result in the Kopia repository. This is exact, needs no access to the
application namespace at all, and works identically for NFS and block volumes.

See [12-kopia-repository-diagnostics.md](12-kopia-repository-diagnostics.md) for the
connection procedure, then:

```bash
kubectl -n "$K10NS" exec "$DEBUG_POD" -- sh -c \
  'export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config; kopia snapshot list --all --json' \
  > kopia-snapshots.json

jq -r '(["PVC","SNAPSHOT_TIME","FILES","DIRS","TOTAL_FILE_SIZE"]|@tsv),
       (.[] | [ (.source.host | split(".") | last),
                .startTime, .stats.fileCount, .stats.dirCount, .stats.totalFileSize ] | @tsv)' \
   kopia-snapshots.json | tee files-per-pvc-from-kopia.tsv | column -t
```

The Kopia snapshot source `host` field is
`<applicationID>.<workloadName>.<pvcName>`, so one repository holds many PVCs. Taking
the last dot-separated component is adequate here because `kopia snapshot list` is run
per application, but **dots are legal in a PVC name**, so `data.with.dots` would parse
as `dots`. For the inventory-based route, use `kopia_inventory_pvcs`
([../lib/kopia.sh](../lib/kopia.sh)) instead — it resolves the PVC against the live
namespace and falls back to a structural parse, reporting which it used. Validated output from the validation cluster:

```
PVC                                SNAPSHOT_TIME                  FILES  DIRS  TOTAL_FILE_SIZE
basic-app-pvc                      2026-09-15T13:18:39.838699054Z 1      1     2726
basic-app-pvc-2026-07-21-08-24-17  2026-09-15T13:18:40.475829529Z 3      1     19313245
```

Compare against the 18 366 that the kubelet reported for the same volume.

**Prefer step C wherever a restore point exists.** Fall back to step B only for PVCs
that have never been exported.

One trap in step C: a volume exported in **block mode** — every KubeVirt VM disk — is
stored as a tree of fixed-size chunks, not files. Its `stats.fileCount` is the chunk
count, `stats.totalFileSize` is null and the real logical size is
`rootEntry.summ.fileSize`. Recognise it by `description` starting with `volume:` (or
`source.path` under `/volume/`) and do not read its count as a file count. The
generator handles this automatically (`mode: block`).

## Caveats

- `find` on a multi-million-file volume is slow and generates real metadata IOPS
  against the storage backend. Run it off-peak and, on NFS, expect minutes to tens of
  minutes. It is read-only, but it is not free.
- `find | wc -l` under-reports if filenames contain newlines. Use
  `find /target -xdev -type f -exec printf '.' \; | wc -c` if you suspect that.
- **A restore point's file count is from backup time, not now.** Fine for sizing;
  note the snapshot timestamp alongside the count.
- On RWX volumes shared by several pods, any single pod's view is complete (they all
  see the same export), so B1 needs to run only once per PVC.
- `inodesUsed` from the kubelet includes directories; `stats.fileCount` from Kopia does
  not. Do not compare them without adding `dirCount`.

## What to send back

| File | Contents |
|------|----------|
| `files-per-pvc-from-kopia.tsv` | authoritative file/dir counts per PVC (preferred) |
| `inodes-block.tsv` | inode counts for block-backed PVCs |
| `pvc-network.txt`, `pvc-block.txt` | the classification, so the reader knows which number to trust |
| `network-pvc-scan.tsv` | per-PVC file count, size and mean for every network-backed volume |
| `scan.log` | which method was used per PVC, and why |
| `kubelet-summary-volumes.tsv` | raw kubelet data, if cluster monitoring was unavailable |

## Validation status

Fully validated on K10 9.0.5. Specifically confirmed on the validation cluster:
`kubelet_volume_stats_inodes_used` returned 62 series; the NFS aliasing described above
was reproduced across 14 PVCs and independently confirmed both via the kubelet summary
API and via `df -i` inside a pod; the `restricted-v2`-compatible inspector pod mounted
an unmounted PVC read-only and returned a correct count of 1 file where `df -i`
reported 18 371; and `kopia snapshot list --all --json` returned exact per-PVC
`fileCount` and `dirCount`.
