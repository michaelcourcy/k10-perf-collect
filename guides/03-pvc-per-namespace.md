# 03 — PVC count per namespace

## What Global Engineering needs

Per protected namespace: how many PVCs, their total provisioned size, and their
breakdown by StorageClass and access mode. This sets the number of datamover pods per
backup cycle and tells us which volumes can use CSI snapshots versus which fall back
to the slower generic (file-level) path.

## Two sources, and they disagree on purpose

- **K10's own view**: `metering_pvc_count` / `metering_pvc_size`, one series per
  namespace. This is what K10 will actually act on, and it is the number to reconcile
  against the licence.
- **The API server's view**: `kubectl get pvc`. This is the ground truth, and it is
  the only one that gives you StorageClass and access mode.

Collect both. A gap between them means K10's discovery is filtering something out.

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
mkdir -p "$AUDIT_DIR/03-pvc-per-namespace" && cd "$AUDIT_DIR/03-pvc-per-namespace"
```

### Step 1 — K10's view (needs `k10prom_start`)

```bash
k10prom_start

kq 'metering_pvc_count' \
  | jq -r '(["NAMESPACE","PVC_COUNT"]|@tsv), (.data.result[] | [.metric.namespace, .value[1]] | @tsv)' \
  | tee k10-pvc-count.tsv | column -t

kq 'metering_pvc_size' \
  | jq -r '(["NAMESPACE","PROVISIONED_GiB"]|@tsv),
           (.data.result[] | [.metric.namespace, ((.value[1]|tonumber)/1073741824*100|round/100)] | @tsv)' \
  | tee k10-pvc-size.tsv | column -t
```

### Step 2 — the API server's view, full detail

```bash
kubectl get pvc -A -o json > pvc-raw.json

jq -r '(["NAMESPACE","PVC","STORAGECLASS","REQUESTED","PHASE","ACCESS_MODE","VOLUME_MODE","PV"]|@tsv),
       (.items[] | [
         .metadata.namespace, .metadata.name,
         (.spec.storageClassName // "-"),
         (.spec.resources.requests.storage // "-"),
         .status.phase,
         (.spec.accessModes[0] // "-"),
         (.spec.volumeMode // "Filesystem"),
         (.spec.volumeName // "-")
       ] | @tsv)' pvc-raw.json | tee pvc-inventory.tsv | column -t
```

### Step 3 — per-namespace rollup, restricted to protected namespaces

```bash
# uses audit-scope-namespaces.txt from guide 01
jq -r '
  [ .items[]
    | { ns: .metadata.namespace,
        sc: (.spec.storageClassName // "none"),
        bytes: (
          (.spec.resources.requests.storage // "0")
          | tostring
          | if test("Gi$") then (rtrimstr("Gi")|tonumber)*1073741824
            elif test("Mi$") then (rtrimstr("Mi")|tonumber)*1048576
            elif test("Ti$") then (rtrimstr("Ti")|tonumber)*1099511627776
            else tonumber end ) } ]
  | group_by(.ns) | map({
      namespace: .[0].ns,
      pvc_count: length,
      provisioned_GiB: ((map(.bytes)|add)/1073741824*100|round/100),
      storageclasses: (map(.sc)|unique|join(","))
    })
  | (["NAMESPACE","PVC_COUNT","PROVISIONED_GiB","STORAGECLASSES"]|@tsv),
    (.[] | [.namespace,.pvc_count,.provisioned_GiB,.storageclasses]|@tsv)' pvc-raw.json \
  > pvc-by-namespace.tsv

# Filter to the in-scope set from guide 01. Match on column 1 exactly - a plain
# "grep -F -f" matches substrings, so "clusters" would also pull in
# "clusters-guest1"; and "grep -Fx -f" matches whole LINES, which never match a
# "namespace<TAB>count" row at all.
awk -F'\t' 'NR==FNR { keep[$1]; next } FNR == 1 || ($1 in keep)' \
    "$AUDIT_DIR/01-scope/audit-scope-namespaces.txt" pvc-by-namespace.tsv \
  | tee pvc-in-scope.tsv | column -t
```

### Step 4 — snapshot capability per StorageClass

This determines whether a PVC gets a fast CSI snapshot or the slow file-level path.
It is the single biggest driver of export duration.

```bash
kubectl get sc -o custom-columns='NAME:.metadata.name,PROVISIONER:.provisioner,RECLAIM:.reclaimPolicy,BINDING:.volumeBindingMode,EXPAND:.allowVolumeExpansion' \
  | tee storageclasses.txt

kubectl get volumesnapshotclass \
  -o custom-columns='NAME:.metadata.name,DRIVER:.driver,DELETION:.deletionPolicy' 2>/dev/null \
  | tee volumesnapshotclasses.txt

# StorageClasses with no matching VolumeSnapshotClass => generic file-level backup
comm -23 <(kubectl get sc -o jsonpath='{range .items[*]}{.provisioner}{"\n"}{end}' | sort -u) \
         <(kubectl get volumesnapshotclass -o jsonpath='{range .items[*]}{.driver}{"\n"}{end}' 2>/dev/null | sort -u) \
  | tee provisioners-without-snapshot-support.txt
```

### Step 5 — unmounted PVCs

PVCs not attached to any pod need special handling in guides 04 and 05, and are
candidates for the inspector-pod measurement in guides 04 and 05.

```bash
jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name)"' pvc-raw.json | sort > all-pvc.txt

kubectl get pods -A -o json \
  | jq -r '.items[] | .metadata.namespace as $ns
           | .spec.volumes[]? | select(.persistentVolumeClaim)
           | "\($ns)/\(.persistentVolumeClaim.claimName)"' | sort -u > mounted-pvc.txt

comm -23 all-pvc.txt mounted-pvc.txt | tee unmounted-pvc.txt
printf 'total=%s mounted=%s unmounted=%s\n' \
  "$(wc -l < all-pvc.txt)" "$(wc -l < mounted-pvc.txt)" "$(wc -l < unmounted-pvc.txt)"
```

## Caveats

- **Requested size is not used size.** `spec.resources.requests.storage` is what was
  asked for. Guide 04 gets what is actually consumed. A 3 Ti request on a 1.5 Gi
  dataset is common and skews any sizing done from this table alone.
- **RWX PVCs are counted once here but mounted many times.** That matters in guide 04,
  where the kubelet reports one series per mount.
- **Mixed size notation**: on the validation cluster some PVCs requested `107374182400`
  (raw bytes) and others `100Gi`. The step 3 converter handles `Mi`/`Gi`/`Ti` and raw
  bytes; extend it if you see `Ki`, `M`, `G` or `T` (SI, no `i`).
- `metering_pvc_count` only has series for namespaces K10 has discovered — a namespace
  with PVCs but no K10 `Application` object will be missing from step 1 and present in
  step 2.

## What to send back

| File | Contents |
|------|----------|
| `pvc-in-scope.tsv` | the headline table, limited to the namespaces guide 01 put in scope |
| `pvc-inventory.tsv` | every PVC with StorageClass, size, access mode |
| `k10-pvc-count.tsv`, `k10-pvc-size.tsv` | K10's own accounting, for reconciliation |
| `storageclasses.txt`, `volumesnapshotclasses.txt` | snapshot capability |
| `provisioners-without-snapshot-support.txt` | provisioners forced onto the generic path |
| `unmounted-pvc.txt` | PVCs needing an inspector pod in guides 04/05 |

## Validation status

Fully validated on K10 9.0.5. On the validation cluster: 70 PVCs total, 47 mounted, 23
unmounted. Step 4 found a VolumeSnapshotClass for all three CSI drivers in use
(`disk.csi.azure.com`, `file.csi.azure.com`, `nfs.csi.k8s.io`), so
`provisioners-without-snapshot-support.txt` was empty — the check is still worth
running, since it is the fastest way to spot a StorageClass that will silently fall
back to the generic path.

Step 1 and step 3 disagreed on namespace `cpd`: K10 metering reported 37 PVCs / 796
GiB while the API server reported 37 PVCs / 836 GiB. Same count, 40 GiB apart. That is
exactly the kind of gap this guide exists to surface — reconcile it rather than
picking one number.
