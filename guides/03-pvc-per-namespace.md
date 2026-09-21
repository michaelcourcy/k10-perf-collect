# 03 — PVCs in the namespace

## What auditors need

For `$AUDIT_NS`: how many PVCs, their provisioned size, and their breakdown by
StorageClass, access mode and volume mode. This is the number of datamover pods per
export cycle, and it tells you which volumes get a fast CSI snapshot and which fall
back to the slower generic path.

## Two sources, and they disagree on purpose

- **K10's own view**: `metering_pvc_count` / `metering_pvc_size`, one series per
  namespace. This is what K10 will act on, and the number to reconcile against the
  licence.
- **The API server's view**: `kubectl get pvc`. This is ground truth, and the only one
  that gives StorageClass, access mode and volume mode.

Collect both. A gap between them means K10's discovery is filtering something out.

Neither is the *used* size. That comes from the Kopia repository in guides 04 and 05 —
requested capacity and exported bytes routinely differ by an order of magnitude.

## Method

```bash
. lib/init.sh
focus_dir 03-pvc-per-namespace
```

### Step 1 — the namespace's PVCs

```bash
kubectl -n "$AUDIT_NS" get pvc -o json > pvc-raw.json

jq -r '(["PVC","STORAGECLASS","REQUESTED","PHASE","ACCESS_MODE","VOLUME_MODE","PV"]|@tsv),
       (.items[] | [
         .metadata.name,
         (.spec.storageClassName // "-"),
         (.spec.resources.requests.storage // "-"),
         .status.phase,
         (.spec.accessModes[0] // "-"),
         (.spec.volumeMode // "Filesystem"),
         (.spec.volumeName // "-")
       ] | @tsv)' pvc-raw.json | tee pvc-inventory.tsv | column -t
```

Validated:

```
PVC                   STORAGECLASS  REQUESTED  PHASE  ACCESS_MODE    VOLUME_MODE  PV
calibrate-100k-500kb  managed-csi   74Gi       Bound  ReadWriteOnce  Filesystem   pvc-5f7ab...
```

`VOLUME_MODE: Block` is the marker for a KubeVirt VM disk — guides 04 and 05 treat
those differently, because Kopia stores them as fixed-size chunks rather than files.

### Step 2 — the rollup, and K10's own figure

```bash
jq -r '
  [ .items[]
    | { sc: (.spec.storageClassName // "none"),
        mode: (.spec.volumeMode // "Filesystem"),
        bytes: (
          (.spec.resources.requests.storage // "0") | tostring
          | if   test("Ki$") then (rtrimstr("Ki")|tonumber)*1024
            elif test("Mi$") then (rtrimstr("Mi")|tonumber)*1048576
            elif test("Gi$") then (rtrimstr("Gi")|tonumber)*1073741824
            elif test("Ti$") then (rtrimstr("Ti")|tonumber)*1099511627776
            else tonumber end ) } ]
  | (["PVC_COUNT","PROVISIONED_GiB","STORAGECLASSES","VOLUME_MODES"]|@tsv),
    ([ length,
       ((map(.bytes)|add)/1073741824*100|round/100),
       (map(.sc)|unique|join(",")),
       (map(.mode)|unique|join(",")) ] | @tsv)' pvc-raw.json \
  | tee pvc-rollup.tsv | column -t
```

Validated: `1  74  managed-csi  Filesystem`.

K10's own accounting, for reconciliation:

```bash
k10prom_start
kq 'metering_pvc_count{namespace="'"$AUDIT_NS"'"}' \
  | jq -r '.data.result[] | [.metric.namespace, .value[1]] | @tsv' | tee k10-pvc-count.tsv
kq 'metering_pvc_size{namespace="'"$AUDIT_NS"'"}' \
  | jq -r '.data.result[] | [.metric.namespace, ((.value[1]|tonumber)/1073741824*100|round/100)] | @tsv' \
  | tee k10-pvc-size.tsv
k10prom_stop
```

A namespace with PVCs but no K10 `Application` object has no series here at all. A
count that matches with a size that does not is the interesting case — on an earlier
validation cluster `cpd` reported 37 PVCs / 796 GiB to K10 and 37 / 836 GiB to the API
server. Reconcile it; do not pick one.

### Step 3 — snapshot capability, for this namespace's StorageClasses

This determines whether each PVC gets a fast CSI snapshot or the slow file-level path,
and it is the single biggest driver of export duration.

```bash
awk -F'\t' 'NR>1 {print $2}' pvc-inventory.tsv | sort -u > sc-in-use.txt

kubectl get sc -o custom-columns='NAME:.metadata.name,PROVISIONER:.provisioner,BINDING:.volumeBindingMode,EXPAND:.allowVolumeExpansion' \
  | tee storageclasses.txt

kubectl get volumesnapshotclass \
  -o custom-columns='NAME:.metadata.name,DRIVER:.driver,DELETION:.deletionPolicy' 2>/dev/null \
  | tee volumesnapshotclasses.txt

# StorageClasses this namespace uses whose provisioner has no VolumeSnapshotClass
# => generic file-level backup, the slow path
kubectl get sc -o json \
  | jq -r --slurpfile vsc <(kubectl get volumesnapshotclass -o json 2>/dev/null || echo '{"items":[]}') \
       --rawfile used sc-in-use.txt '
      ($used | split("\n") | map(select(length>0))) as $u
      | ([$vsc[0].items[]?.driver] | unique) as $drivers
      | .items[] | select(.metadata.name | IN($u[]))
      | select((.provisioner | IN($drivers[])) | not)
      | "\(.metadata.name)\t\(.provisioner)"' \
  | tee provisioners-without-snapshot-support.txt
```

Validated: empty — `managed-csi` (`disk.csi.azure.com`) has a VolumeSnapshotClass, so
this namespace is on the fast CSI path. An empty file is the good result, and the check
is still worth running: it is the fastest way to spot a StorageClass that will silently
fall back to the generic path.

### Step 4 — which PVCs a pod is holding

RWO volumes attached to a running pod cannot be mounted by an inspector pod, which
constrains the fallback path in guides 04 and 05:

```bash
kubectl -n "$AUDIT_NS" get pods -o json \
  | jq -r '.items[] | .metadata.name as $p
           | .spec.volumes[]? | select(.persistentVolumeClaim)
           | "\(.persistentVolumeClaim.claimName)\t\($p)"' | sort -u \
  | { printf 'PVC\tHELD_BY_POD\n'; cat; } | tee pvc-mounts.tsv | column -t
```

This matters less than it used to: guides 04 and 05 now read the file counts and sizes
out of the **Kopia repository**, which needs no access to the application namespace at
all. Step 4 is only for PVCs that have never been exported.

## Caveats

- **Requested size is not used size.** `spec.resources.requests.storage` is what was
  asked for. On the validation cluster a 74 GiB request held 47.7 GiB of data — and
  K10's `progressDetails.totalBytes` reports the *capacity*, not the data, which is why
  guide 13 keeps them in separate columns.
- **RWX PVCs are counted once here but mounted many times.** That matters in guide 04,
  where the kubelet reports one series per mount.
- **Mixed size notation.** The step 2 converter handles `Ki`/`Mi`/`Gi`/`Ti` and raw
  bytes; extend it if you see SI units (`M`, `G`, `T`, no `i`).
- **`VOLUME_MODE: Block` changes every later guide.** File counts, mean file size and
  histograms are meaningless on a block device; guides 04 and 05 report chunk counts
  and the block size instead.

## What to send back

| File | Contents |
|------|----------|
| `pvc-inventory.tsv` | every PVC in the namespace with class, size, access and volume mode |
| `pvc-rollup.tsv` | the one-line summary — PVC count and provisioned size |
| `k10-pvc-count.tsv`, `k10-pvc-size.tsv` | K10's own accounting, for reconciliation |
| `storageclasses.txt`, `volumesnapshotclasses.txt` | snapshot capability |
| `provisioners-without-snapshot-support.txt` | classes forced onto the generic path |
| `pvc-mounts.tsv` | which PVCs are held by a running pod |

## Validation status

Fully validated on K10 9.0.5 against `prod-test` / `calibrate-backup`: 1 PVC,
`managed-csi`, 74 GiB requested, `ReadWriteOnce`, `Filesystem`, bound and mounted by
the application pod. Step 3 found a VolumeSnapshotClass for `disk.csi.azure.com`, so
`provisioners-without-snapshot-support.txt` came out empty.

The K10-versus-API-server reconciliation in step 2 is quoted from an earlier validation
cluster (`cpd`, 37 PVCs, 40 GiB apart); on this single-PVC namespace the two agreed, so
the gap case itself was not reproduced here.
