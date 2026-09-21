# 11 — Number of objects on the object storages

## What auditors need

For `$AUDIT_PROFILE` and the repository of `$AUDIT_NS`: the number of objects and the
number of bytes. Object count drives list-operation cost, Kopia maintenance duration
and — on S3-compatible backends with per-request pricing — the actual bill. A repository
with millions of small blobs behaves very differently from one with the same bytes in
large packs.

## Four ways to get it, cheapest first

| Method | Cost | Granularity | Needs cluster access |
|--------|------|-------------|---------------------|
| Cloud-provider inventory report | free, already computed | bucket, daily | no |
| `kopia blob stats` per repository | one pod, seconds | per repository prefix | yes |
| `mc du` / `aws s3 ls` from a pod | full bucket walk | per prefix | yes |
| `catalog_storage_artifact_count` | free | K10 artifacts, not objects | yes |

**Use the provider's inventory if one exists.** A full `LIST` walk of a
hundred-million-object bucket is slow and, on S3, costs real money.

## Method

```bash
. lib/init.sh
focus_dir 11-object-store
```

Steps 1 and 2 need the `debug-kopia-*` pod from guide 12 step 4.

### Step 1 — the profile and the repository prefix

```bash
kubectl -n "$K10NS" get profiles "$AUDIT_PROFILE" -o json > profile-raw.json

jq -r '(["PROFILE","TYPE","OBJSTORE_TYPE","ENDPOINT","BUCKET","PREFIX","REGION","CRED_SECRET"]|@tsv),
       ([ .metadata.name,
          (.spec.locationSpec.type // "-"),
          (.spec.locationSpec.objectStore.objectStoreType // "-"),
          (.spec.locationSpec.objectStore.endpoint // "-"),
          (.spec.locationSpec.objectStore.name // "-"),
          (.spec.locationSpec.objectStore.path // "-"),
          (.spec.locationSpec.objectStore.region // "-"),
          (.spec.locationSpec.credential.secret.name // "-") ] | @tsv)' profile-raw.json \
  | tee profile.tsv | column -t
```

The repository's own view of where it lives — this is the **exact prefix** the pair
writes to, which the profile alone does not tell you:

```bash
kopia_exec 'kopia repository status --json' > repository-status.json
jq -r '{uniqueIDHex,
        storage: {type: .storage.type,
                  bucket: .storage.config.bucket,
                  prefix: .storage.config.prefix},
        format:  {version: .contentFormat.version,
                  maxPackSize: .contentFormat.maxPackSize,
                  indexVersion: .contentFormat.indexVersion}}' repository-status.json \
  | tee repository-location.json
```

Validated:

```json
{
  "uniqueIDHex": "3f1c9a0e5d2b47a8916c0fe3ab74d5c28e60b1937fa4d50cb8e2716340df9ac5",
  "storage": {"type": "s3", "bucket": "my-s3-profile",
              "prefix": "k10/0f7c6f0e-.../migration/repo/7b21c4de-9f03-4a61-8c5d-1e2f3a4b5c6d/"},
  "format": {"version": 3, "maxPackSize": 20971520, "indexVersion": 2}
}
```

Three things to read:

- The prefix embeds the **cluster UID** (the `default` namespace UID, guide 00 §2) and
  then a **per-repository UUID**. On a shared bucket that is what separates this
  cluster's data from a neighbour's, and this pair's from another pair's. Count per
  prefix, never per bucket.
- `format.version 3` with `indexVersion 2` is current. A repository still on v1/v2 with
  no epoch manager is a migration candidate and a known performance problem.
- `maxPackSize` is the denominator for the histogram in step 2.

A profile of type `FileStore` has no bucket at all — `mastodon-backup` on this cluster
exports to `azurefile-filestore`. Steps 2 and 3 do not apply; report the share and its
free space instead.

Foreign prefixes in the same bucket — data from a cluster that was rebuilt or
decommissioned, still costing storage:

```bash
: "${CLUSTER_UID:?empty - re-run guide 00 section 2}"
grep -c "$CLUSTER_UID" profile.tsv
```

The guard is not decoration: `grep -v ""` excludes **every** line, so an empty
`CLUSTER_UID` silently reports "no orphans".

### Step 2 — object count and size histogram, from Kopia

Exact, scoped to this repository's prefix, no bucket walk, no cost:

```bash
kopia_exec 'kopia blob stats' | tee blob-stats.txt
kopia_exec 'kopia blob list --json' > blob-list.json
jq -r '[.[] | select(.id|test("^[pq]"))] as $p
       | {objects: length, bytes: ([.[].length]|add),
          packObjects: ($p|length), packBytes: ([$p[].length]|add)}' blob-list.json \
  | tee blob-summary.json
```

Validated:

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

```json
{"objects": 3452, "bytes": 71716788616, "packObjects": 3428, "packBytes": 71707612383}
```

`Count` is the object count in this repository's prefix. **The histogram is the
diagnostic**: 3,415 of 3,452 blobs in the 10–100 MB bucket against a 20 MB
`maxPackSize` means packs are being filled properly. The opposite shape — most blobs
under 10 KB — means Kopia is flushing before packs fill, which multiplies object count,
list cost and maintenance time for the same bytes.

3,428 of 3,452 objects are pack blobs; the remaining 24 are indexes, logs and the
format blob.

Index and content counts, which drive maintenance cost:

```bash
kopia_exec 'kopia index list'    | wc -l | tee index-blob-count.txt
kopia_exec 'kopia content stats' | tee content-stats.txt
```

### Step 3 — full prefix walk with `mc`, when you need the bucket's own answer

Use when reconciling against the provider's bill, or when there is no repository yet.

The credential secret uses **lowercase** keys (`aws_access_key_id`,
`aws_secret_access_key`). An `envFrom.secretRef` therefore creates lowercase
environment variables and `mc`/`aws` will not see `AWS_ACCESS_KEY_ID`. Map them
explicitly — this is the single most common way this step fails silently with
`Access Denied`:

```bash
SECRET=$(jq -r '.spec.locationSpec.credential.secret.name' profile-raw.json)
BUCKET=$(jq -r '.spec.locationSpec.objectStore.name' profile-raw.json)
ENDPOINT=$(jq -r '.spec.locationSpec.objectStore.endpoint' profile-raw.json)
PREFIX=$(jq -r '.storage.config.prefix' repository-status.json)
kubectl -n "$K10NS" get secret "$SECRET" -o json | jq -r '.data | keys'

cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: objcount
  namespace: $K10NS
  labels: {app.kubernetes.io/name: k10-audit-objcount}
spec:
  restartPolicy: Never
  containers:
  - name: mc
    image: quay.io/minio/mc:latest
    command: ["sleep","3600"]
    env:
    - name: AWS_ACCESS_KEY_ID
      valueFrom: {secretKeyRef: {name: $SECRET, key: aws_access_key_id}}
    - name: AWS_SECRET_ACCESS_KEY
      valueFrom: {secretKeyRef: {name: $SECRET, key: aws_secret_access_key}}
YAML

kubectl -n "$K10NS" wait --for=condition=Ready pod/objcount --timeout=300s

kubectl -n "$K10NS" exec objcount -- sh -c "
  mc alias set t '${ENDPOINT:-https://s3.amazonaws.com}' \"\$AWS_ACCESS_KEY_ID\" \"\$AWS_SECRET_ACCESS_KEY\" >/dev/null
  echo '=== this repository prefix ==='
  mc du 't/$BUCKET/$PREFIX'
  echo '=== whole K10 prefix in the bucket ==='
  mc du --depth 4 't/$BUCKET/k10'
" | tee objcount-mc.txt

kubectl -n "$K10NS" delete pod objcount
```

`mc du` output is cumulative up the tree — the deepest line is the one to read, and the
shallower ones are roll-ups, not additional objects. If listing the bucket root returns
`Access Denied` but the prefix works, the credential is scoped to the bucket: expected
and fine. Start from `t/$BUCKET`, never `t`.

Compare the prefix total against step 2's `Count` and `Total`. A gap means either
noncurrent versions (see the caveats) or objects Kopia no longer references.

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

Not an object count, but it says how many restore-point artifacts K10 believes it has.
A large gap against step 2 suggests orphaned data.

```bash
k10prom_start
kq 'catalog_storage_artifact_count' \
  | jq -r '(["APP_TYPE","CATEGORY","RETIREMENT","COUNT"]|@tsv),
           (.data.result[] | [ (.metric.app_type // "-"), .metric.category,
                               .metric.retirement, .value[1] ] | @tsv)' \
  | tee artifact-count.tsv | column -t
k10prom_stop
```

Cross-check against the orphan counts from `repo_checker -r inventory` (guide 12
step 1), which reports `OrphanedCount`, `NotSyncedCount` and `DanglingCount` per
repository.

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
- **One profile holds many repositories** — one per namespace for volume data, plus
  metadata and migration repositories. Step 2 counts **this pair's** repository only.
  For the profile total, sum across the repositories listed by guide 12 step 1; do not
  present a per-pair figure as the bucket's size.

## What to send back

| File | Contents |
|------|----------|
| `blob-stats.txt` | object count, total bytes and size histogram for this repository (headline) |
| `blob-summary.json` | objects, bytes, pack objects and pack bytes as numbers |
| `repository-location.json` | the exact prefix, format version and max pack size |
| `profile.tsv` | the profile with its bucket, endpoint and credential secret |
| `objcount-mc.txt` | prefix walk, if performed |
| `index-blob-count.txt`, `content-stats.txt` | index and content counts, maintenance cost drivers |
| `artifact-count.tsv` | K10's own accounting, for reconciliation |
| provider inventory export | if available, preferred over any walk |

## Validation status

Steps 1, 2 and 5 fully validated on K10 9.0.5 against `prod-test` /
`calibrate-backup` on the `my-s3-profile` S3 profile: 3,452 objects, 71.7 GB,
3,428 of them pack blobs, format version 3, `maxPackSize` 20 MB, and a histogram with
3,415 blobs in the 10–100 MB bucket — a properly packed repository.

`kopia repository status --json` resolved the per-repository prefix
(`k10/<cluster-uid>/migration/repo/<repo-uuid>/`), confirming that one profile holds a
separate prefix per namespace: the three namespaces of `calibrate-backup` have three
different repository UUIDs under the same bucket.

The `FileStore` case in step 1 was confirmed by inspection of the
`azurefile-filestore` profile used by `mastodon-backup` — it has no `objectStore`
block at all.

Step 3's lowercase-secret-key trap and `mc du` roll-up behaviour are carried over from
an earlier validated run against an in-cluster MinIO profile; step 3 was **not** re-run
here, because step 2 answers the same question for this pair without a bucket walk.
Step 4's provider commands are documented from the vendor CLIs and were not executed.
