# 10 — Helm and ActionPodSpec configuration around the datamovers

## What Global Engineering needs

The complete, effective configuration that governs datamover behaviour: the Helm values
in force, the derived `k10-config` tuning keys, the feature flags, and any
`ActionPodSpec` / `ActionPodSpecBinding` overrides. This is the input side of the
recommendation — every number produced by guides 03–09 gets compared against these
settings.

## Where the configuration actually lives

Four places, in increasing order of specificity:

1. **Helm values** (`helm get values k10`) — what the operator asked for.
2. **`k10-config` ConfigMap** — the rendered, effective tuning parameters. This is the
   authoritative source; a value absent from the Helm values is still present here with
   its default.
3. **`k10-features` ConfigMap** — feature flags, separate from tuning.
4. **`ActionPodSpec` + `ActionPodSpecBinding`** — per-action pod overrides (resources,
   node selector, tolerations, annotations).

On an operator-based install, `kubectl get k10s.apik10.kasten.io` replaces (1). A
cluster can have both: the validation cluster had a Helm release `k10-9.0.5` *and* a
`K10` CR, and the two agreed.

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
mkdir -p "$AUDIT_DIR/10-config" && cd "$AUDIT_DIR/10-config"
```

### Step 0 — set up redaction

The Helm values and the `K10` CR contain a licence/GVS token and, on basic-auth
installs, an htpasswd hash. Redact **before** the file lands on disk:

```bash
cat > redact.sed <<'SED'
s/\(token:\)[[:space:]]*.*/\1 <REDACTED>/
s/\(overridepubkey:\)[[:space:]]*.*/\1 <REDACTED>/
s/\(overridePublicKeyForGVS:\)[[:space:]]*.*/\1 <REDACTED>/
s/\(GVSActivationToken:\)[[:space:]]*.*/\1 <REDACTED>/
s/\(htpasswd:\)[[:space:]]*.*/\1 <REDACTED>/
s/\(secretAccessKey:\)[[:space:]]*.*/\1 <REDACTED>/
s/\(aws_secret_access_key:\)[[:space:]]*.*/\1 <REDACTED>/
s/\(receiveString:\)[[:space:]]*.*/\1 <REDACTED>/
SED
```

Verify afterwards with:

```bash
grep -niE 'token:|htpasswd:|secretaccesskey:|password' *.yaml *.json | grep -v REDACTED
```

That command should print nothing before you send anything.

### Step 1 — installation method and version

```bash
kubectl -n "$K10NS" get cm k10-config -o jsonpath='{.data.version}{"\n"}' | tee k10-version.txt

helm -n "$K10NS" list -o json 2>/dev/null | tee helm-releases.json | jq -r '.[] | "\(.name)\t\(.chart)\t\(.status)\t\(.updated)"'

kubectl -n "$K10NS" get k10s.apik10.kasten.io -o name 2>/dev/null | tee operator-cr.txt
```

Validated on the validation cluster: chart `k10-9.0.5`, status `deployed`, **and** a
`k10` CR present — an operator install whose Helm release is managed by the operator.
Report both; a customer who edits Helm values directly on an operator install will have
them reverted.

### Step 2 — Helm values (redacted)

```bash
helm -n "$K10NS" get values k10 2>/dev/null | sed -f redact.sed | tee helm-values-redacted.yaml
helm -n "$K10NS" get values k10 --all 2>/dev/null | sed -f redact.sed > helm-values-all-redacted.yaml
```

`--all` includes chart defaults and is the version to read when checking whether a knob
was ever set.

For an operator install:

```bash
kubectl -n "$K10NS" get k10s.apik10.kasten.io k10 -o yaml | sed -f redact.sed | tee k10-cr-redacted.yaml
```

### Step 3 — the datamover tuning keys

These are the ones that matter for performance. Dump all of them:

```bash
kubectl -n "$K10NS" get cm k10-config -o json \
  | jq -r '.data
      | with_entries(select(.key | test("Limiter|DataStore|Ephemeral|Buffer|Timeout|WorkerPod|Kanister|Catalog|csiSnapshot"; "i")))' \
  | sed -f redact.sed | tee datamover-tuning.json
```

Validated values on the validation cluster, K10 9.0.5 defaults throughout:

| Key | Value | What it controls |
|-----|-------|------------------|
| `K10LimiterSnapshotExportsPerCluster` | 10 | concurrent exports cluster-wide |
| `K10LimiterSnapshotExportsPerAction` | 3 | concurrent exports within one action |
| `K10LimiterGenericVolumeBackupsPerCluster` | 10 | concurrent file-level backups |
| `K10LimiterCsiSnapshotsPerCluster` | 10 | concurrent CSI snapshots |
| `K10LimiterVolumeRestoresPerCluster` / `PerAction` | 10 / 3 | restore concurrency |
| `K10LimiterExecutorThreads` | 8 | executor worker threads |
| `k10DataStoreParallelUpload` | 8 | Kopia parallel upload streams |
| `k10DataStoreParallelDownload` | 8 | Kopia parallel download streams |
| `k10DataStoreParallelBlockUpload` / `BlockDownload` | 8 / 8 | block-mode parallelism |
| `k10DataStoreTotalCacheSizeLimitMB` | 3000 | **per-pod** Kopia cache ceiling |
| `k10DataStoreGeneralContentCacheSizeMB` | 500 | content cache for backup |
| `k10DataStoreGeneralMetadataCacheSizeMB` | 500 | metadata cache for backup |
| `k10DataStoreRestoreContentCacheSizeMB` | 500 | content cache for restore |
| `k10DataStoreRestoreMetadataCacheSizeMB` | 500 | metadata cache for restore |
| `k10DataStoreEstimationType` | `adaptive` | how K10 sizes the buffer PVC |
| `k10DataStoreAdaptiveEstimationThreshold` | 300000 | switch-over point for adaptive sizing |
| `k10DataStoreDisableCompression` | false | Kopia compression on/off |
| `K10EphemeralPVCOverhead` | 0.1 | +10 % on the ephemeral buffer PVC |
| `K10BackupBufferFileHeadroomFactor` | 1.1 | +10 % on the buffer file |
| `workerPodResourcesCRDEnabled` | **false** | whether `ActionPodSpec` resources apply |
| `WorkerPodMetricSidecarEnabled` | true | metric sidecar in worker pods |
| `WorkerPodMetricSidecarMetricLifetime` | 2m | how long pushed metrics survive |
| `WorkerPodPushgatewayMetricsInterval` | 30s | sidecar push interval |
| `K10TimeoutWorkerPodReady` | 15 (minutes) | how long to wait for a datamover to start |
| `K10TimeoutBlueprintBackup` / `Restore` / `Hooks` | 45 / 600 / 20 (minutes) | Kanister action timeouts |
| `csiSnapshotCreationTimeout` | 10m | CSI snapshot create timeout |
| `csiSnapshotReadyTimeout` | 30m | CSI snapshot ready timeout |

Capture the full set rather than only these, since defaults move between releases:

```bash
kubectl -n "$K10NS" get cm k10-config -o json | jq -r '.data' | sed -f redact.sed > k10-config-full.json
kubectl -n "$K10NS" get cm k10-config -o json | jq -r '.data | keys | .[]' > k10-config-keys.txt
```

### Step 4 — feature flags

```bash
kubectl -n "$K10NS" get cm k10-features -o json | jq -r '.data' | tee k10-features.json
```

Validated content, with the performance-relevant entries called out:

```json
{
  "backgroundMaintenanceRun": "true",
  "repositoryServerDispatcher": "false",
  "repositoryServerDispatcherCapacity": "5",
  "repositoryServerPodAffinity": "false",
  "bmdVolumefsEnableMetadataPaging": "false",
  "catalogUpdateBatcher": "true",
  "jobsUpdateBatcher": "true",
  "ephemeralPodsReadOnlyRootFilesystem": "true",
  "persistentPodsReadOnlyRootFilesystem": "true"
}
```

- `repositoryServerDispatcher: false` means a fresh repository-server pod per action
  instead of a pooled one — significant per-action startup cost at high policy counts.
- `bmdVolumefsEnableMetadataPaging: false` matters on small-file-heavy volumes; it is
  the switch for paging file metadata rather than holding it in memory.
- `backgroundMaintenanceRun: true` means Kopia maintenance runs outside the backup
  window, which is what you want.

### Step 5 — ActionPodSpec and bindings

```bash
kubectl -n "$K10NS" get actionpodspecs -o yaml | tee actionpodspecs.yaml
kubectl -n "$K10NS" get actionpodspecbindings -o yaml | tee actionpodspecbindings.yaml
```

On the validation cluster **both were empty** (`items: []`). That means datamover pods
get no resource requests, no limits, no node selector and no tolerations — confirmed
independently in guide 09, where every worker-pod container had `resources: {}`.

Also capture the related override CRDs, which affect the same pods:

```bash
kubectl -n "$K10NS" get transformsets -o yaml            > transformsets.yaml
kubectl -n "$K10NS" get storagesecuritycontexts -o yaml  > storagesecuritycontexts.yaml
kubectl -n "$K10NS" get storagesecuritycontextbindings -o yaml > storagesecuritycontextbindings.yaml
kubectl -n "$K10NS" get blueprintbindings -o yaml        > blueprintbindings.yaml
kubectl -n "$K10NS" get blueprints -o yaml 2>/dev/null   > blueprints.yaml
```

### Step 6 — what the datamover images actually are

Air-gapped or mirrored registries change pull time, which shows up as datamover startup
latency:

```bash
kubectl -n "$K10NS" get deploy -o json \
  | jq -r '.items[] | .metadata.name as $d | .spec.template.spec.containers[] | "\($d)\t\(.name)\t\(.image)"' \
  | tee k10-images.tsv

kubectl -n "$K10NS" get cm k10-config -o jsonpath='{.data.KanisterToolsImage}{"\n"}' | tee kanister-tools-image.txt
```

Validated: all images came from `registry.connect.redhat.com/kasten/*` by digest, and
`datamover` resolved to
`registry.connect.redhat.com/kasten/datamover@sha256:189cb04a...`.

### Step 7 — K10's own persistence and sizing

The catalog and jobs PVCs are on the critical path of every action:

```bash
kubectl -n "$K10NS" get pvc -o custom-columns='NAME:.metadata.name,SC:.spec.storageClassName,SIZE:.spec.resources.requests.storage,PHASE:.status.phase' \
  | tee k10-internal-pvcs.txt

kq 'catalog_persistent_volume_free_space_percent' \
  | jq -r '.data.result[] | [.metric.service, .value[1]] | @tsv'
kq 'jobs_persistent_volume_free_space_percent' \
  | jq -r '.data.result[] | [.metric.service, .value[1]] | @tsv'
kq 'logging_persistent_volume_free_space_percent' \
  | jq -r '.data.result[] | [.metric.service, .value[1]] | @tsv'
```

A catalog PVC nearing full degrades every job; include the free-space percentages in the
report. Validated output on the validation cluster: `catalog=99`, `jobs=99`,
`logging=99` — i.e. 99 % free, healthy.

Note the sum of the cache settings against `k10DataStoreTotalCacheSizeLimitMB`: four
500 MB caches with a 3000 MB per-pod ceiling means the ceiling is not the binding
constraint on this cluster — the individual cache sizes are.

## Caveats

- **`k10-config` is the effective configuration, Helm values are the intent.** Always
  send both. A customer "sure" they set a limiter will be contradicted by
  `k10-config`.
- **`k10DataStoreTotalCacheSizeLimitMB` is per pod, not per cluster.** Multiply by the
  relevant limiter before comparing against node disk (guide 08).
- **`workerPodResourcesCRDEnabled: false` makes ActionPodSpec resources inert.** If the
  recommendation is going to set datamover resources, that flag has to be flipped first,
  and it requires a Helm upgrade — not a ConfigMap edit.
- **Do not hand-edit `k10-config`.** It is rendered from the chart and reverts on
  upgrade. Every recommendation must be expressed as Helm values or as a CR change.
- On an operator install, direct `helm upgrade` is reverted by the operator's reconcile
  loop. Check step 1 before recommending a change path.
- Defaults shift between K10 releases. Always pair the values with the version from
  step 1.

## What to send back

| File | Contents |
|------|----------|
| `k10-version.txt` | the version every other number must be read against |
| `helm-values-redacted.yaml`, `helm-values-all-redacted.yaml` | intent, with defaults |
| `k10-cr-redacted.yaml` | operator CR, if operator-installed |
| `datamover-tuning.json` | the performance-relevant subset of `k10-config` |
| `k10-config-full.json` | the whole effective configuration |
| `k10-features.json` | feature flags |
| `actionpodspecs.yaml`, `actionpodspecbindings.yaml` | per-action overrides, or proof there are none |
| `k10-images.tsv` | image sources and digests |
| `k10-internal-pvcs.txt` | catalog/jobs/logging PVC sizing and free space |

## Validation status

Fully validated on K10 9.0.5. Every command ran successfully and the values quoted in
step 3 are the literal contents of that cluster's `k10-config` (85 keys total). Step 5
returned empty `ActionPodSpec` and `ActionPodSpecBinding` lists, consistent with the
`resources: {}` observed on live datamover pods in guide 09. The redaction filter was
tested against the real Helm values, which did contain a GVS token and public key.
