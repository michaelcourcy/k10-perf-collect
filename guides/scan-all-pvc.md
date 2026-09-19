# Cluster-wide PVC JSON inventory

Run from the repository root with Python 3, kubectl, and your cluster credentials:

```bash
./scan-all-pvc.sh > pvc-report.json 2> pvc-scan.log
```

The script uses your current kubectl context. Options include `--context NAME`,
`--namespace NAME`, `--timeout 180`, `--image IMAGE`, and `--no-inspector`.
Use repeatable `--network-storage-class NAME` to force tree walking for custom shared storage.
It scans sequentially, tries running containers that mount the PVC root, and otherwise
creates a temporary read-only inspector pod. Inspectors are deleted after measurement
or failure. Exclusive PVCs referenced by another active pod are skipped if no usable
root mount exists. Stale storage attachments can still prevent an inspector from starting.

Permissions needed: list PVCs and StorageClasses, list pods in their namespaces, and exec into containers.
Inspector fallback additionally needs get namespace and create/get/watch/delete pods.
The default inspector image is `busybox:1.37.0`; use `--image` for a private registry.
Inspectors run as non-root; OpenShift supplies the namespace UID, while other clusters
use UID 65534. Permission failures are recorded, without changing volume permissions.
Inspectors mount a writable `emptyDir` at `/tmp` for tree-walk scratch files and set
`TMPDIR=/tmp`. The root filesystem and target PVC remain read-only. Scratch storage
is removed with the inspector pod.

The JSON object has `generated_at`, `measurement_notes`, and a `pvcs` array.
Each PVC entry includes namespace, PVC name, storage class, requested size, provisioned
size in Mi/Gi/Ti and bytes, status, method, error, and metrics:

StorageClass provisioners matching NFS, SMB, Azure Files, EFS, Filestore, Gluster,
or CephFS use a tree walk. Unknown or absent StorageClasses also use a tree walk.
Other provisioners use df; this classification is heuristic, so use the explicit
storage-class override for custom shared-storage drivers.

| Metric | Tree walk (network/unknown storage) | df (other storage) |
| --- | --- | --- |
| `file_count` | Regular-file paths counted with find/stat | null |
| `used`, `used_bytes` | Allocated space under the PVC root, from du | Filesystem used space |
| `logical_file_bytes` | Sum of regular-file logical lengths | null |
| `occupation_percent` | Allocated bytes / provisioned PVC bytes × 100 | df Use% |
| `inodes_used`, `inodes_total`, `inodes_free` | null: export-wide values are misleading | df inode statistics |
| `average_file_bytes` | Logical file bytes / regular-file count | null |
| `average_bytes_per_used_inode` | null | Used bytes / used inodes (estimate) |
| `average_size` | Human-readable mean logical file size | Human-readable inode-based estimate |
| `filesystem_capacity`, `filesystem_capacity_bytes`, `available_bytes`, `inode_occupation_percent` | null | df filesystem statistics |

`measurement` and `occupation_basis` identify the source and denominator. The top-level
PVC `size` and `size_bytes` always come from provisioned PVC capacity.

As explained in [guide 04](04-files-per-pvc.md), **df on NFS may describe the entire
export**. Network PVCs therefore never use df for their data size or file count.
Their occupation can exceed 100% if provisioned capacity is not an enforced quota;
it is not the remote filesystem's free-space percentage.

Tree walking requires `sh`, `find`, `stat`, `du`, `awk`, `mktemp`, and `timeout` in the
container, plus writable temporary space. Missing tools or access failures trigger
another existing mount or an inspector attempt. Failed or timed-out walks never
produce a successful zero count. A remote timeout bounds each walk; the local exec
allows an additional 15 seconds for termination and transport. Walks generate storage
metadata I/O, so increase `--timeout` for large PVCs and schedule accordingly.

The walk stays on one filesystem, excludes symlinks from regular-file counts, and
handles filenames containing newlines. Hard-linked file paths count separately.
The average uses logical lengths from stat, while du usage reflects allocation
(including directories); sparse files can therefore have a large average and small
allocated usage. Empty-file-set averages are null. Measurements are live rather
than a snapshot. For df measurements, used inodes include directories and symlinks.

Unbound PVCs, raw block volumes, and failed measurements remain in the report with
`status: "unavailable"`, an error, and `metrics: null`. Exit code 0 means all PVCs were
measured (or the inventory was empty); 2 means the report includes unavailable PVCs;
1 means the inventory could not be produced. Interruptions exit 130 without a final
report. Progress and cleanup errors go to stderr. An interrupted cleanup or unavailable
API can leave an inspector pod requiring manual deletion; its active deadline limits
its running time.
