# 08 — Node disk and ephemeral storage

## What auditors need

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

## Method

```bash
. lib/init.sh
focus_dir 08-node-disk
```

### Step 1 — capacity and current usage per node

```bash
node_snapshot | tee node-snapshot.tsv | column -t
node_totals   | tee node-totals.txt
```

The `EPH_CAP_GiB` / `EPH_USED_GiB` / `EPH_PCT` columns are this guide's subject:
`node.fs` from the kubelet summary API, which is the filesystem the eviction manager
itself watches. Validated:

```
NODE      ROLES   EPH_CAP_GiB  EPH_USED_GiB  EPH_PCT  PRESSURE
worker-4  worker  511.25       424.95        83.1     -
worker-1  worker  511.25       167.44        32.8     -
worker-5  worker  511.25       135.46        26.5     -
worker-6  worker  511.25       104.03        20.3     -
worker-3  worker  511.25       91.69         17.9     -
worker-2  worker  511.25       114.23        22.3     -
```

**One worker at 83.1 % against a cluster median of 26 %.** With a default eviction
threshold of `nodefs.available < 10%`, that node has 35 GiB of margin — and ten
concurrent datamovers at 3 GB of Kopia cache each would eat most of it. That is the
finding this guide exists to produce; step 4 turns it into an exact number.

> **Unit trap.** `status.capacity["ephemeral-storage"]` is a `Ki` quantity
> (`"536083696Ki"`) while `status.allocatable["ephemeral-storage"]` is a bare byte
> count (`"492980991592"`). Treating them as the same unit gives a 1024× error.
> `node_snapshot` parses quantities; `EPH_CAP_GiB` prefers the kubelet's own
> `fs.capacityBytes` where it is available.

### Step 2 — inodes and the image filesystem

Inode exhaustion causes the same symptom as space exhaustion, and on most OpenShift
layouts `nodefs` and `imagefs` are the **same device** — image pulls and datamover
caches compete for one pool.

```bash
for n in $(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
  kubectl get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
    | jq -r --arg node "$n" '
        [ $node,
          (.node.fs.inodes // 0), (.node.fs.inodesUsed // 0), (.node.fs.inodesFree // 0),
          (if (.node.fs.inodes // 0) > 0
             then ((.node.fs.inodesUsed / .node.fs.inodes)*1000|round/10) else 0 end),
          ((.node.runtime.imageFs.usedBytes // 0)/1073741824*100|round/100),
          ((.node.runtime.imageFs.capacityBytes // 0)/1073741824*100|round/100)
        ] | @tsv'
done | { printf 'NODE\tINODES\tINODES_USED\tINODES_FREE\tINODES_PCT\tIMAGEFS_USED_GiB\tIMAGEFS_CAP_GiB\n'; cat; } \
  | tee node-inodes.tsv | column -t
```

If `IMAGEFS_CAP_GiB` equals `EPH_CAP_GiB` from step 1, they are the same filesystem —
do **not** add their usage together.

Inode exhaustion is not hypothetical here: the 5 M-file calibration volume in this
cluster hit 100 % inodes on a 74 GiB ext4 (4,849,664 inodes at the default 16 KiB
bytes-per-inode ratio) while blocks were only 77 % full, and the pod crash-looped
(guide 06 step 4).

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

Run this **during** an export and the datamover pods are at the top of the list.
Outside the window they do not exist at all — which is why guide 09 step 6 has a
capture loop rather than a single command. Filter to them:

```bash
awk -F'\t' -v ns="$K10NS" 'NR==1 || ($2==ns && $3 ~ /data-mover|copy-vol-data|create-repo|repository-server/)' \
    pod-ephemeral-usage.tsv | column -t
```

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

Defaults are `nodefs.available < 10%` and `imagefs.available < 15%`.

Turn that into the number that matters — margin to eviction, against what the
datamovers can ask for:

```bash
CACHE_MB=$(kubectl -n "$K10NS" get cm k10-config \
             -o jsonpath='{.data.k10DataStoreTotalCacheSizeLimitMB}')
LIMIT=$(kubectl -n "$K10NS" get cm k10-config \
          -o jsonpath='{.data.K10LimiterSnapshotExportsPerCluster}')
echo "per-pod cache ceiling: ${CACHE_MB} MB   concurrent exports: ${LIMIT}"

awk -F'\t' -v c="$CACHE_MB" -v l="$LIMIT" '
  NR==1 {print "NODE\tEPH_CAP_GiB\tEPH_USED_GiB\tMARGIN_TO_10PCT_GiB\tWORST_CASE_CACHE_GiB\tVERDICT"; next}
  $10=="-" || $11=="-" {next}
  { margin = $10*0.9 - $11; worst = c*l/1024
    printf "%s\t%s\t%s\t%.1f\t%.1f\t%s\n", $1, $10, $11, margin, worst,
           (margin < worst ? "AT RISK" : "ok") }' node-snapshot.tsv \
  | tee eviction-margin.tsv | column -t
```

Validated, with `k10DataStoreTotalCacheSizeLimitMB=3000` and
`K10LimiterSnapshotExportsPerCluster=10` — a 29.3 GiB worst case:

```
NODE      EPH_CAP_GiB  EPH_USED_GiB  MARGIN_TO_10PCT_GiB  WORST_CASE_CACHE_GiB  VERDICT
worker-4  511.25       421.84        38.3                 29.3                  ok
worker-1  511.25       165.18        294.9                29.3                  ok
master-1  462.94       386.35        30.3                 29.3                  ok
```

The busiest worker clears the worst case by 9 GiB and `master-1` by **1 GiB** — `ok`,
but only just, and the margin moves every time an image is pulled. On a node with a
200 GiB root disk rather than 511 GiB the same ten datamovers cross the threshold, and
because they are BestEffort they are also the first pods the kubelet evicts.

Two reasons this is a floor, not a ceiling: the cache limit is **per pod**, and the
buffer file gets `K10BackupBufferFileHeadroomFactor` (1.1) and
`K10EphemeralPVCOverhead` (0.1) on top. Masters are usually tainted, so read their rows
only if step 5 of guide 07 shows a matching toleration.

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

Steps 1–5 fully validated on K10 9.0.5 / OpenShift 4.18.

Step 1 returned `stats/summary` for all 9 nodes and surfaced a real imbalance: one
worker at **83.1 %** ephemeral-storage use against a cluster median of 26 %. Step 4's
margin calculation put it at 38.3 GiB of headroom against a 29.3 GiB worst case
(`k10DataStoreTotalCacheSizeLimitMB` 3000 × `K10LimiterSnapshotExportsPerCluster` 10),
and `master-1` at 30.3 GiB against the same 29.3 GiB — a 1 GiB margin.

Also confirmed here: the `Ki`-versus-bytes unit mismatch between `capacity` and
`allocatable`; `nodefs` and `imagefs` reporting identical figures, i.e. the same
device; `ephemeral_storage_pod_usage_bytes` absent from OpenShift monitoring while the
kubelet summary API returns per-pod `ephemeral-storage`; `/proxy/configz` returning the
effective thresholds (`nodefs.available: 10%`, `imagefs.available: 15%`,
`nodefs.inodesFree: 5%`, `imageGCHighThresholdPercent: 85`); and datamover pods created
with `resources: {}`.

Note that the `kubeletconfig` CRD lookup returns nothing on a cluster that has never
customised kubelet settings — `configz` is the reliable source, not the CRD.

Inode exhaustion (step 2) was observed for real on this cluster, on the 5 M-file
calibration volume: 100 % inodes at 77 % blocks, and a crash-looping pod. What was
**not** reproduced is an actual eviction: no node crossed its threshold during the
audit, so `evicted-pods.tsv` was empty and the eviction path itself is reasoned from
the thresholds rather than observed.
