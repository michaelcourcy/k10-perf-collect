# 08 — Node disk and ephemeral storage

## What Global Engineering needs

Per node: free space on the kubelet root filesystem and the image filesystem, the
declared `ephemeral-storage` capacity and allocatable, and how much of it pods are
actually consuming.

## Why this is the most under-monitored failure mode in K10

Datamover pods write to **`emptyDir` volumes**, which live on the node's ephemeral
storage:

| Volume | Purpose |
|--------|---------|
| `kopia-cache-volume` | Kopia content and metadata cache |
| `tmp-volume` | scratch space, buffer files |
| `home-dir-volume` | Kopia config and logs |

Two K10 settings govern how large this gets:

```
k10DataStoreTotalCacheSizeLimitMB = 3000
K10BackupBufferFileHeadroomFactor = 1.1
K10EphemeralPVCOverhead           = 0.1
```

`k10DataStoreTotalCacheSizeLimitMB` is **per datamover pod**. With
`K10LimiterSnapshotExportsPerCluster = 10`, ten concurrent datamovers can request up to
~30 GB of node-local cache, plus buffer files.

And — verified on the validation cluster — datamover pods are created with
**`resources: {}`**: no `ephemeral-storage` request and no limit. There is nothing
stopping them from filling the node filesystem, and nothing telling the scheduler to
avoid a node that is nearly full. When the kubelet crosses its eviction threshold it
evicts pods, and BestEffort pods go first — which is exactly what the datamovers are.

So this guide is not bookkeeping. It is the check for the most common cause of
"the backup failed and took some application pods with it".

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
mkdir -p "$AUDIT_DIR/08-node-disk" && cd "$AUDIT_DIR/08-node-disk"
```

### Step 1 — declared ephemeral-storage capacity and allocatable

```bash
kubectl get nodes -o json > nodes-raw.json

jq -r '(["NODE","EPH_CAP_GiB","EPH_ALLOC_GiB","RESERVED_GiB"]|@tsv),
       (.items[]
        | ((.status.capacity["ephemeral-storage"]    | rtrimstr("Ki") | tonumber) * 1024) as $cap
        | (.status.allocatable["ephemeral-storage"] | tonumber)                        as $alloc
        | [ .metadata.name,
            ($cap/1073741824*100|round/100),
            ($alloc/1073741824*100|round/100),
            (($cap-$alloc)/1073741824*100|round/100) ] | @tsv)' nodes-raw.json \
  | tee ephemeral-capacity.tsv | column -t
```

> **Unit trap**: on OpenShift 4.18, `capacity["ephemeral-storage"]` is expressed in
> `Ki` (`"536083696Ki"`) while `allocatable["ephemeral-storage"]` is a bare byte count
> (`"492980991592"`). The `jq` above handles both. If you write your own, check the
> suffix — treating them as the same unit gives a 1024× error.

### Step 2 — real filesystem usage, straight from each kubelet

The kubelet summary API is the authoritative source: it is what the eviction manager
itself reads.

```bash
for n in $(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
  kubectl get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
    | jq -r --arg node "$n" '
        [ $node,
          ((.node.fs.capacityBytes  // 0)/1073741824*100|round/100),
          ((.node.fs.usedBytes      // 0)/1073741824*100|round/100),
          ((.node.fs.availableBytes // 0)/1073741824*100|round/100),
          (if (.node.fs.capacityBytes // 0) > 0
             then ((.node.fs.usedBytes / .node.fs.capacityBytes)*1000|round/10) else 0 end),
          (.node.fs.inodes // 0), (.node.fs.inodesUsed // 0), (.node.fs.inodesFree // 0),
          ((.node.runtime.imageFs.usedBytes // 0)/1073741824*100|round/100)
        ] | @tsv'
done | { printf 'NODE\tFS_CAP_GiB\tFS_USED_GiB\tFS_AVAIL_GiB\tFS_USED_PCT\tINODES\tINODES_USED\tINODES_FREE\tIMAGEFS_USED_GiB\n'; cat; } \
  | tee node-fs.tsv | column -t
```

Validated sample from one worker:

```
FS_CAP_GiB   511.25
FS_USED_GiB  158.93
FS_AVAIL_GiB 352.32
FS_USED_PCT  31.1
INODES       268172736
INODES_USED  2427698
```

Note that on this cluster `nodefs` and `imagefs` are the **same filesystem**
(`/dev/sdd4`, identical capacity and inode figures). That is the common OpenShift
layout and it means image pulls and datamover caches compete for the same space.

### Step 3 — per-pod ephemeral-storage consumption

There is no `ephemeral_storage_pod_usage_bytes` metric in OpenShift monitoring — it was
absent on the validation cluster. The kubelet summary API has it:

```bash
for n in $(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
  kubectl get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
    | jq -r --arg node "$n" '.pods[]?
        | [ $node, .podRef.namespace, .podRef.name,
            ((.["ephemeral-storage"].usedBytes // 0)/1048576|round) ] | @tsv'
done | sort -t"$(printf '\t')" -k4 -rn \
  | { printf 'NODE\tNAMESPACE\tPOD\tEPHEMERAL_MiB\n'; cat; } \
  | tee pod-ephemeral-usage.tsv | head -30 | column -t
```

Run this **during** a backup window and the datamover pods will be at the top of the
list. Outside the window they do not exist.

From cluster monitoring, the closest equivalent covers the container writable layer
only (not `emptyDir`), so prefer the kubelet API above:

```bash
tq 'topk(20, sum by (namespace, pod) (container_fs_usage_bytes{container!=""}))' \
  | jq -r '.data.result[] | [.metric.namespace, .metric.pod, ((.value[1]|tonumber)/1048576|round)] | @tsv' \
  | tee container-fs-usage.tsv | column -t
```

### Step 4 — eviction thresholds

The number that actually triggers the failure.

```bash
# OpenShift: kubelet config is rendered per machine config pool
kubectl get kubeletconfig -o yaml 2>/dev/null \
  | grep -A15 -E "evictionHard|evictionSoft|imageGC" | tee eviction-config.txt

# effective values as the kubelet sees them
for n in $(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
  echo "=== $n ==="
  kubectl get --raw "/api/v1/nodes/$n/proxy/configz" 2>/dev/null \
    | jq -r '.kubeletconfig | {evictionHard, evictionSoft, imageGCHighThresholdPercent, imageGCLowThresholdPercent}'
done | tee kubelet-eviction-effective.txt
```

Defaults are `nodefs.available < 10%` and `imagefs.available < 15%`. Compare against
`FS_AVAIL_GiB` from step 2 and compute the margin in GiB, then compare that margin
against `k10DataStoreTotalCacheSizeLimitMB × concurrent exports`.

### Step 5 — historical disk pressure and past evictions

```bash
# AUDIT_START / AUDIT_END / AUDIT_RANGE are exported by lib/init.sh. On a cluster whose
# Prometheus has no PV these may be far less than 14 days.
echo "window: ${AUDIT_WINDOW_DAYS}d"

tqr 'min by (instance) (node_filesystem_avail_bytes{mountpoint="/",fstype!="tmpfs"})' \
    "$AUDIT_START" "$AUDIT_END" 1h \
  | jq -r --arg w "$AUDIT_WINDOW_DAYS" '(["INSTANCE","MIN_AVAIL_GiB_"+$w+"d"]|@tsv),
           (.data.result[] | [ .metric.instance,
                ((.values|map(.[1]|tonumber)|min)/1073741824*100|round/100) ] | @tsv)' \
  | tee "min-avail-${AUDIT_WINDOW_DAYS}d.tsv" | column -t

# nodes that reported disk pressure at any point in the window.
# NB: $AUDIT_RANGE does not expand inside single quotes — break the quoting as shown.
tq 'max_over_time(kube_node_status_condition{condition="DiskPressure",status="true"}['"$AUDIT_RANGE"'])' \
  | jq -r '.data.result[] | select((.value[1]|tonumber) > 0) | .metric.node' \
  | tee nodes-with-disk-pressure.txt

# evicted pods still recorded in the API
kubectl get pods -A --field-selector status.phase=Failed -o json \
  | jq -r '.items[] | select(.status.reason=="Evicted")
           | [.metadata.namespace, .metadata.name, .status.message] | @tsv' \
  | tee evicted-pods.tsv
```

## Caveats

- **`kubectl get --raw .../proxy/stats/summary` needs `nodes/proxy` RBAC.** It is in
  `cluster-admin` but not in most read-only roles. This is one of the reasons the test
  audit requires `nodes/proxy` up front (guide 00 §8).
- **`emptyDir` usage is invisible to `container_fs_usage_bytes`.** That metric covers
  the container writable layer. The datamover's cache is an `emptyDir` and only shows up
  in the kubelet summary API's `ephemeral-storage` field. Using the Prometheus metric
  alone will report near-zero and hide the problem entirely.
- **Datamover pods are short-lived.** Steps 3 and 5 only see them if sampled during the
  window. Schedule step 3 to run every 15 s for the duration of a test policy run
  (guide 09 has a ready-made capture loop).
- **`nodefs` and `imagefs` are often the same device.** Do not add their usage together.
- **A clean history may just be a short history.** An empty
  `nodes-with-disk-pressure.txt` over a 2-day `AUDIT_WINDOW_DAYS` says almost nothing.
  Report the window alongside the result.
- Inode exhaustion on the node filesystem causes the same symptom as space exhaustion.
  Step 2 collects both; on the validation cluster inode usage was 0.9 %, not a concern
  there, but small-file-heavy datamover caches can change that.

## What to send back

| File | Contents |
|------|----------|
| `node-fs.tsv` | per-node filesystem capacity, used, available, inodes (headline) |
| `ephemeral-capacity.tsv` | declared ephemeral-storage capacity vs allocatable |
| `pod-ephemeral-usage.tsv` | top ephemeral-storage consumers, ideally captured during a backup |
| `kubelet-eviction-effective.txt`, `eviction-config.txt` | the thresholds |
| `min-avail-<N>d.tsv`, `nodes-with-disk-pressure.txt`, `evicted-pods.tsv` | history over `AUDIT_WINDOW_DAYS` |

## Validation status

Steps 1–3 and 5 fully validated on the validation cluster. Confirmed there: the
`Ki`-vs-bytes unit mismatch between `capacity` and `allocatable`
(`536083696Ki` vs `492980991592`); `nodefs` and `imagefs` reporting identical figures;
`ephemeral_storage_pod_usage_bytes` absent from OpenShift monitoring while the kubelet
summary API returned per-pod `ephemeral-storage` for every pod;
`node_filesystem_avail_bytes` present with 119 series; and datamover pods created with
`resources: {}` — no ephemeral-storage request or limit.

Step 5 was re-validated using the derived window from guide 00 §6
(`AUDIT_WINDOW_DAYS=15`): the range query returned per-node minima (45.7 GiB on the
masters) and the `['"$AUDIT_RANGE"']` interpolation returned 9 `DiskPressure` series.

Step 4 validated: `/proxy/configz` returned the effective thresholds
(`nodefs.available: 10%`, `imagefs.available: 15%`, `nodefs.inodesFree: 5%`,
`imageGCHighThresholdPercent: 85`). Note that the `kubeletconfig` CRD lookup returns
nothing on a cluster that has never customised kubelet settings — `configz` is the
reliable source, not the CRD.

On that worker the margin to eviction was 352 GiB against a 10 % (51 GiB) threshold, so
there was ample room. On a node with a smaller root disk, ten concurrent datamovers at
3 GB cache each is a realistic way to cross it.
