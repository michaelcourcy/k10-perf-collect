# 11 — Number of objects on the object storages

## What Global Engineering needs

Per Location Profile / bucket / repository prefix: the number of objects and the number
of bytes. Object count drives list-operation cost, Kopia maintenance duration, and — on
S3-compatible backends with per-request pricing — the actual bill. A repository with
millions of small blobs behaves very differently from one with the same bytes in large
packs.

## Four ways to get it, cheapest first

| Method | Cost | Granularity | Needs cluster access |
|--------|------|-------------|---------------------|
| Cloud-provider inventory report | free, already computed | bucket, daily | no |
| `kopia blob stats` per repository | one pod, seconds | per repository prefix | yes |
| `mc du` / `aws s3 ls` from a pod | full bucket walk | per prefix | yes |
| `catalog_storage_artifact_count` | free | K10 artifacts, not objects | yes |

**Use the provider's inventory if one exists.** A full `LIST` walk of a
hundred-million-object bucket is slow and, on S3, costs real money.

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
mkdir -p "$AUDIT_DIR/11-object-store" && cd "$AUDIT_DIR/11-object-store"
```

### Step 1 — enumerate the profiles and their prefixes

```bash
kubectl -n "$K10NS" get profiles -o json > profiles-raw.json

jq -r '(["PROFILE","TYPE","OBJSTORE_TYPE","ENDPOINT","BUCKET","PREFIX","REGION","CRED_SECRET"]|@tsv),
       (.items[]
        | . as $p | ($p.spec.locationSpec // {}) as $l | ($l.objectStore // {}) as $o
        | [ $p.metadata.name, ($l.type // "-"),
            ($o.objectStoreType // "-"), ($o.endpoint // "-"),
            ($o.name // "-"), ($o.path // "-"), ($o.region // "-"),
            (($l.credential.secret.name) // "-") ] | @tsv)' profiles-raw.json \
  | tee profiles.tsv | column -t
```

Validated output:

```
PROFILE              TYPE         OBJSTORE_TYPE  ENDPOINT                                  BUCKET  PREFIX                                                   CRED_SECRET
azurefile-filestore  FileStore    -              -                                         -       -                                                        -
my-s3-bucket         ObjectStore  S3             http://minio.minio.svc.cluster.local:9000 mod     k10/0f7c6f0e-1a2b-4c3d-9e8f-0123456789ab/migration                               k10-minio-creds
```

Note that the prefix embeds the **cluster UID**. On a bucket shared between clusters,
this is what separates them — count per prefix, not per bucket, or you will attribute
another cluster's objects to this one.

The cluster UID is the UID of the `default` namespace (guide 00 §2), so you can resolve
a prefix back to a cluster — or confirm which prefix belongs to the cluster in front of
you — without touching K10:

```bash
export CLUSTER_UID=$(kubectl get ns default -o jsonpath='{.metadata.uid}')
# Guard: grep -v "" excludes EVERY line, so an empty CLUSTER_UID silently reports
# "no foreign prefixes" below. Never let it run empty.
: "${CLUSTER_UID:?empty - re-run guide 00 section 2}"

grep -c "$CLUSTER_UID" profiles.tsv     # profiles belonging to THIS cluster
```

That also means any `k10/<uuid>/...` prefix in the bucket whose UUID is not the
`default` namespace UID of a live cluster is a strong orphan candidate — data from a
cluster that has been rebuilt or decommissioned. Worth listing separately:

```bash
kubectl -n "$K10NS" exec objcount -- sh -c "
  mc alias set t '$ENDPOINT' \"\$AWS_ACCESS_KEY_ID\" \"\$AWS_SECRET_ACCESS_KEY\" >/dev/null
  mc ls t/$BUCKET/k10/" | tee cluster-prefixes-in-bucket.txt

: "${CLUSTER_UID:?empty - re-run guide 00 section 2}"
grep -v "$CLUSTER_UID" cluster-prefixes-in-bucket.txt | tee foreign-cluster-prefixes.txt
```

Validated output — one prefix, matching this cluster, no orphans:

```
[2026-09-15 19:53:43 UTC]     0B 0f7c6f0e-1a2b-4c3d-9e8f-0123456789ab/
```

An empty `foreign-cluster-prefixes.txt` is the clean result. Anything listed there is
data K10 on this cluster will never touch, never retire and never count — it just
accrues storage cost. Report it, but do **not** delete it during an audit: the owning
cluster may still exist, and on a versioned or object-locked bucket deletion may not
even be reversible.

### Step 2 — preferred: `kopia blob stats`, per repository

Exact, scoped to the repository prefix, and it comes with a size histogram. Already
produced by the guide 12 diagnose bundle — no extra work:

```bash
grep -H -A12 '^Count:' "$AUDIT_DIR/12-kopia/"*/kopia-debug-logs/blob-stats-stdout.txt
```

Or live, from a connected debug pod (guide 12):

```bash
kubectl -n "$K10NS" exec "$DEBUG_POD" -- sh -c \
  'export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config; kopia blob stats' \
  | tee blob-stats.txt
```

Validated output:

```
Count: 19
Total: 2.9 MB
Average: 150.2 KB
Histogram:
        0 between 0 B and 10 B (total 0 B)
        1 between 10 B and 100 B (total 30 B)
        7 between 100 B and 1 KB (total 3 KB)
       10 between 1 KB and 10 KB (total 32.9 KB)
        0 between 10 KB and 100 KB (total 0 B)
        0 between 100 KB and 1 MB (total 0 B)
        1 between 1 MB and 10 MB (total 2.8 MB)
```

`Count` is the object count in that repository's prefix. The histogram is the diagnostic
that matters: 17 of 19 blobs under 10 KB means Kopia is not filling its packs, which
points at either a very small dataset or an over-frequent flush.

Add the index and content counts, which drive maintenance cost:

```bash
kubectl -n "$K10NS" exec "$DEBUG_POD" -- sh -c \
  'export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config
   echo "--- index blobs ---";  kopia index list | wc -l
   echo "--- contents ---";     kopia content stats' | tee index-content-stats.txt
```

### Step 3 — full bucket walk with `mc`

Use when you need the bucket total, or when there is no Kopia repository yet.

The credential secret uses **lowercase** keys (`aws_access_key_id`,
`aws_secret_access_key`). `envFrom.secretRef` therefore creates lowercase environment
variables, and `mc`/`aws` will not see `AWS_ACCESS_KEY_ID`. Map them explicitly — this
is the single most common way this step fails silently with `Access Denied`:

```bash
kubectl -n "$K10NS" get secret k10-minio-creds -o json | jq -r '.data | keys'
# => ["aws_access_key_id","aws_secret_access_key"]

cat <<'YAML' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: objcount
  namespace: kasten-io
  labels: {app.kubernetes.io/name: k10-audit-objcount}
spec:
  restartPolicy: Never
  containers:
  - name: mc
    image: quay.io/minio/mc:latest
    command: ["sleep","3600"]
    env:
    - name: AWS_ACCESS_KEY_ID
      valueFrom: {secretKeyRef: {name: k10-minio-creds, key: aws_access_key_id}}
    - name: AWS_SECRET_ACCESS_KEY
      valueFrom: {secretKeyRef: {name: k10-minio-creds, key: aws_secret_access_key}}
YAML

kubectl -n "$K10NS" wait --for=condition=Ready pod/objcount --timeout=300s

ENDPOINT=http://minio.minio.svc.cluster.local:9000   # from profiles.tsv
BUCKET=mod

kubectl -n "$K10NS" exec objcount -- sh -c "
  mc alias set t '$ENDPOINT' \"\$AWS_ACCESS_KEY_ID\" \"\$AWS_SECRET_ACCESS_KEY\" >/dev/null
  echo '=== objects and bytes per prefix ==='
  mc du --depth 6 t/$BUCKET
  echo '=== total objects ==='
  mc ls --recursive t/$BUCKET | wc -l
" | tee objcount-mc.txt

kubectl -n "$K10NS" delete pod objcount
```

Validated output after two backup cycles:

```
=== objects and bytes per prefix ===
2.9MiB	35 objects	mod/k10/0f7c6f0e-1a2b-4c3d-9e8f-0123456789ab/migration
2.9MiB	35 objects	mod/k10/0f7c6f0e-1a2b-4c3d-9e8f-0123456789ab
2.9MiB	35 objects	mod/k10
2.9MiB	35 objects	mod
=== total objects ===
35
```

`mc du` output is cumulative up the tree — the deepest line is the one to read, and the
shallower ones are roll-ups, not additional objects.

If listing the bucket root returns `Access Denied` but the prefix works, the credential
is scoped to the bucket — expected and fine. Start from `t/$BUCKET`, never `t`.

### Step 4 — provider-native counts, no cluster involvement

**AWS S3** — use Storage Lens or an S3 Inventory report if configured; otherwise:

```bash
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX" \
  --query 'length(Contents)' --output text

# or, free and already computed, from CloudWatch
aws cloudwatch get-metric-statistics --namespace AWS/S3 --metric-name NumberOfObjects \
  --dimensions Name=BucketName,Value="$BUCKET" Name=StorageType,Value=AllStorageTypes \
  --start-time "$(date -u -v-2d +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -d '2 days ago' +%Y-%m-%dT00:00:00Z)" \
  --end-time "$(date -u +%Y-%m-%dT00:00:00Z)" --period 86400 --statistics Average
```

**Azure Blob** — the Blob Inventory report, or:

```bash
az storage blob list --container-name "$CONTAINER" --prefix "$PREFIX" \
  --account-name "$ACCOUNT" --num-results '*' --query 'length(@)' -o tsv
```

**GCS**:

```bash
gcloud storage ls -r "gs://$BUCKET/$PREFIX**" | wc -l
```

**Ceph RGW** — by far the cheapest, from the storage admin:

```bash
radosgw-admin bucket stats --bucket="$BUCKET" | jq '.usage["rgw.main"]'
```

**MinIO** as administrator:

```bash
mc admin info --json t
mc ls --summarize --recursive "t/$BUCKET"
```

### Step 5 — K10's artifact accounting, for reconciliation

Not an object count, but it tells you how many restore-point artifacts K10 believes it
has. A large gap against step 2 suggests orphaned data.

```bash
kq 'catalog_storage_artifact_count' \
  | jq -r '(["APP_TYPE","CATEGORY","RETIREMENT","COUNT"]|@tsv),
           (.data.result[] | [ (.metric.app_type // "-"), .metric.category,
                               .metric.retirement, .value[1] ] | @tsv)' \
  | tee artifact-count.tsv | column -t

kq 'catalog_repository_version_count' \
  | jq -r '.data.result[] | [.metric.version, .value[1]] | @tsv' | tee repo-version-count.tsv
```

Cross-check against the orphan count from `repo-checker -r inventory` (guide 12), which
reports `OrphanedCount`, `NotSyncedCount` and `DanglingCount` per repository.

## Caveats

- **Listing costs money and time.** On S3, `LIST` is billed per 1 000 keys. Ask for the
  inventory report before walking a large bucket, and say in the report which method was
  used — the numbers are not equivalent (inventory reports are up to 24 h stale).
- **Count per prefix, not per bucket**, on any shared bucket. The K10 prefix contains
  the cluster UID, which is the `default` namespace UID (guide 00 §2) — use it to tell
  this cluster's data from a neighbour's, and to spot prefixes left behind by rebuilt
  clusters.
- **`mc ls --recursive | wc -l` counts current versions only.** On a versioned bucket
  with a lifecycle policy, noncurrent versions and delete markers are additional
  objects that still cost storage. Use `mc ls --versions --recursive` or
  `aws s3api list-object-versions` to include them. This is a frequent source of
  "the bucket is bigger than K10 says".
- **Object-lock / immutability** multiplies object count and blocks Kopia's maintenance
  from deleting anything. Check `extendObjectLocks` in `maintenance-info-stdout.txt`
  (guide 12) and the bucket's retention configuration.
- `catalog_storage_artifact_count` counts K10 artifacts, not objects. A single artifact
  maps to many Kopia blobs. Never present it as an object count.
- **One profile can hold several repositories.** On the validation cluster the single
  `my-s3-bucket` profile held two (`kopia-volumedata-*` and `kopia-metadata-*`), each
  with its own prefix and its own `blob stats`. Sum them for the profile total.

## What to send back

| File | Contents |
|------|----------|
| `profiles.tsv` | every profile with its bucket, prefix and cluster UID |
| `foreign-cluster-prefixes.txt` | `k10/<uuid>/` prefixes in the bucket that do not belong to this cluster |
| `blob-stats.txt` | per-repository object count, total bytes and size histogram (headline) |
| `objcount-mc.txt` | full bucket/prefix walk, if performed |
| `index-content-stats.txt` | index blob count and content statistics |
| `artifact-count.tsv`, `repo-version-count.tsv` | K10's own accounting for reconciliation |
| provider inventory export | if available, preferred over any walk |

## Validation status

Steps 1, 2, 3 and 5 fully validated on K10 9.0.5 against an in-cluster MinIO S3 profile.
Confirmed there: the lowercase-secret-key trap — an `envFrom.secretRef` pod produced
`Access Denied` on every operation until the env vars were mapped explicitly; `mc du`
reporting cumulative roll-ups up the prefix tree; bucket-root listing denied while
prefix listing succeeded; 35 objects / 2.9 MiB after two backup cycles, consistent with
`kopia blob stats` reporting 19 blobs for one of the two repositories in that prefix.

The cluster-prefix / orphan-detection snippet was validated: the `default` namespace UID
matched the single `k10/<uuid>/` prefix present in the bucket, and
`foreign-cluster-prefixes.txt` came out empty as expected.

Step 4's provider commands are documented from the vendor CLIs and were **not** executed
— the validation cluster had no cloud object store attached. Verify the exact flags
against your CLI version before relying on them.
