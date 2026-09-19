# K10 Performance Audit — Data Collection Guides

This repository contains one guide per data point that Global Engineering needs in
order to produce Veeam Kasten (K10) configuration recommendations for a protected
cluster.

Each guide is self-contained: it states **what** is being collected, **why** it
matters for the recommendation, the **exact commands** to run, the **caveats** that
make the result wrong if ignored, and **what to send back**.

## Automated collection: `generate-export-topology.py`

For many clusters, namespaces and PVCs, the guides are too slow to run by hand. The
generator produces the whole export topology as one JSON document, built exclusively
from what is in the Kopia repositories — a namespace that has never been exported does
not appear:

```bash
./generate-export-topology.py --context <kube-context> -o export-topology.json
```

Requires `python3` (stdlib only), `kubectl` and `helm` in `PATH`. It downloads the
`k10_repo_checker.sh` matching the cluster's K10 version, runs the inventory, connects
read-only to each application repository, reads Kopia's snapshot, content, blob and
maintenance data, and joins in the K10 objects (policies, profiles, ActionPodSpecs,
ExportActions), the `k10-config` limiters and cAdvisor / kube-state-metrics for datamover
CPU and memory. Debug pods it creates are deleted; `--keep-pods` keeps them.

```
ExportTopology
  helmLimiters
  policies[]  profile, frequency, subFrequency, exportFrequency, retention
    namespaces[]  actionPodSpecs[], repository (objects, maintenance), exports[]
      pvcs[]  fileCount, totalSizeBytes, averageFileSizeBytes, sizeHistogram,
              lastChangeRate, lastMaintenance
        snapshots[]  files, size, start/end, logical delta, physical ingest,
                     datamover peak memory / CPU with namespace attribution
```

Render the JSON as one self-contained HTML page (no external assets, dark mode follows
the OS, every chart has a table-view twin):

```bash
./render-export-topology.py export-topology.json -o export-topology.html
```

Scope comes from two independent sources: the export policies (the scope rule — a
namespace is in scope iff a policy with an export action selects it) and, best-effort,
the `repo_checker` inventory, which also catches data whose policy was since deleted and
adds repository names and orphan counts. The inventory aborts entirely on a repository
whose Location Profile was deleted; the generator then falls back to one inventory per
profile and reports such repositories under `orphanedRepositories`. Discovery from the
policies never depends on it.

VM policies (KubeVirt, `virtualMachineRef` / `virtualMachineNamespace`) are included: a
VM's disks are PVCs in its namespace and the export lands in that namespace's repository,
so only the namespace is resolved from the selector. VM disks are stored in **block
mode** — the Kopia tree is fixed-size chunks, not files — and the JSON says so
(`mode: block`, `chunkCount`, `blockSizeBytes`, no histogram). A namespace selected by an
export policy but with no RestorePoint at all is skipped before any repository connect
(`scopeNotes.namespacesWithoutRestorePointSkipped`); one with restore points but no
repository on that profile is listed under `notExported`.

Focusing on one export problem: `--namespace NS --policy POLICY` (both repeatable)
collects a single namespace/policy pair in minutes instead of reading every repository,
and the JSON records the filter. Without re-collecting, the renderer cuts a full report
down the same way: `python3 render-export-topology.py topo.json --policy POLICY --namespace NS -o focus.html`
(a banner says what was hidden).

Useful flags: `--namespace NS` (repeatable) to restrict; `--no-inventory` to skip
`repo_checker` inventory entirely (policies only); `--no-histogram` to skip the
per-PVC tree listing; `--prom-url` / `--prom-token` for a non-OpenShift Prometheus;
`--no-metrics` to skip cAdvisor entirely. `./generate-export-topology.sh` is a thin
wrapper. `--help` lists everything.

A full run takes minutes to an hour (the `repo_checker` inventory re-scans the whole
catalog, and every filesystem PVC gets a full tree listing). Progress goes to stderr
with an elapsed-time prefix: one line per namespace/profile pair (`[3/18]`), one per
Kopia step with its duration (`snapshot list`, `content list`, `blob list`, per-PVC
`tree listing`), and a heartbeat every 30 s while `repo_checker` runs. `-v` echoes
every `repo_checker` line instead. Redirect stdout only (`2>&1` mixes them) if you want
the progress on screen while capturing the summary.

The report header also carries **the nodes at audit time** (`nodes`): per node the
roles, instance type, capacity/allocatable and a usage sample taken when the generator
ran — CPU cores, working-set memory and ephemeral storage (the root filesystem the
kubelet evicts on, where datamovers write their Kopia cache and cloned volume data),
plus any `*Pressure` condition. Usage comes from the kubelet stats summary
(`GET /api/v1/nodes/<node>/proxy/stats/summary`, needs `get` on `nodes/proxy`), falling
back to the metrics API (`metrics.k8s.io`, CPU and memory only). It is one sample, not
an average: run the generator while exports are in flight to see what they cost.

Four properties of the data worth knowing before reading the JSON:

- **File counts come from the snapshot tree** (`rootEntry.summ.files` / `summ.fileSize`).
  Kopia's `stats.fileCount` is the number of files it *hashed* in that run, exposed per
  snapshot as `filesHashed` with `filesUnchanged` (skipped as unchanged against the
  previous snapshot). Hashed ÷ files is a per-PVC change indicator that survives
  maintenance; it is 0 unchanged on a first snapshot and was seen wrongly 0 on an
  `nfs-csi` volume, so cross-check with `exportedBytes`.

- **The per-export `changeRate` is a proxy.** Numerator: `exportedBytes`, K10's own
  `transferredBytes` from the ExportAction `/details` subresource (`k10Progress`, with
  `readBytes`, `processedBytes`, `totalBytes` = volume capacity, `processingRate`, and
  per-volume `volumes[]` with data format and CSI snapshot id); Kopia pack-blob bytes
  written in the window are the fallback (`changeRateBasis` says which) and become a
  lower bound once maintenance has re-stamped part of the packs — seen on the reference cluster as 9 MB
  by Kopia timestamps vs 56 MB by K10 for the same export. Denominator:
  the logical size of the PVC snapshots that export took (`source.logicalBytes`). 100%
  means a first export with nothing deduplicated or compressed; dedup against earlier
  snapshots and compression pull it down, encryption adds a little. It is the best
  single figure available, not a measured change rate. Per-application ExportActions
  live in the **application namespace**; the ones in the K10 namespace are the policy
  run's metadata export and appear as `policies[].runs`. Details are fetched for the 20
  newest exports per namespace/policy (`--export-details-max`, 0 = all;
  `--no-export-details` skips them) and need `get` on `exportactions/details` in those
  namespaces.

- **Physical ingest per snapshot is only recoverable until the next full Kopia
  maintenance** (24 h by default). `full-rewrite-contents` re-stamps every content and
  pack blob with the maintenance time, after which the per-snapshot attribution is gone
  and the field is `null` with the reason. Run the generator within a day of the exports
  you care about.
- **Datamover CPU/memory is a sum over a time window, not a per-PVC figure.** The same
  rule applies at export level (window = the ExportAction's start/end) and at PVC level
  (window = that PVC's Kopia snapshot start/end): the window is padded by 45 s on each
  side, and cAdvisor is queried at a 15 s step for every pod in the K10 namespace whose
  name matches the datamover regex (`data-mover.*|copy-vol-data.*|create-repo.*|`
  `repository-server.*|restore-data.*`, `--datamover-pod-regex`), excluding the pause
  container and the pod-level cgroup:

  ```promql
  sum by (pod) (container_memory_working_set_bytes{namespace="kasten-io",pod=~"<regex>",container!="",container!="POD"})
  sum by (pod) (container_cpu_usage_seconds_total{namespace="kasten-io",pod=~"<regex>",container!="",container!="POD"})
  kube_pod_labels{namespace="kasten-io",pod=~"<regex>"}
  ```

  `peakSumMemoryBytes` is the maximum, over the window, of memory summed across those
  pods at each step; `cpuSecondsTotal` is per pod `max − min` of the cumulative counter
  (no `rate()`, so short-lived pods are not smoothed away), summed — **CPU-seconds**, the
  total CPU time the datamovers consumed; `avgCpuCores` divides it by the window length
  (716 cpu-s over 14.5 min ≈ 0.82 cores on average), which is what the HTML shows first. Any datamover alive
  in the window is counted — two policies exporting in the same minute see the same
  pods, and the disks of one VM export concurrently, so their PVC rows show the same
  figures. Pods that lived less than one scrape interval are listed in
  `podsWithoutSamples` rather than reported as a peak of 0.

  Attribution to namespace and policy is not a cAdvisor feature: cAdvisor only knows
  `namespace`/`pod`/`container`/`node`. It is a join, on `pod`, with kube-state-metrics'
  `kube_pod_labels`, which exposes pod labels as `label_*` when they are in its
  allow-list. K10 labels `data-mover-svc-*` pods with `app-name` (the application
  namespace), `policy-name` and `k10.kasten.io/jobID`; `copy-vol-data-*` pods (the ones
  reading the PVC clone) carry only the job id and inherit namespace/policy from the
  `data-mover-svc` pod of the same job. Attribution is therefore per job, never per PVC
  (the clone's name does not reveal the source). OpenShift runs kube-state-metrics with
  `--metric-labels-allowlist=pods=[*]`, so this works out of the box; on another
  Prometheus stack add `--metric-labels-allowlist=pods=[app-name,policy-name,k10.kasten.io/jobID]`
  to kube-state-metrics. The generator probes this once per run:
  `metrics.podLabelsExposed` is `true`/`false`, and when false `metrics.attribution`
  says so and usage is reported per pod only.

## Other scripts

Two standalone helpers at the repository root complement the generator. Both are bash
wrappers around embedded Python (standard library only, `kubectl` for cluster access),
take `--context` and `--namespace`/`-n`, print progress on stderr and JSON on stdout.

### `exported_namespace.sh` — which namespaces have been exported, by which policy

```sh
./exported_namespace.sh [--context CTX] [-n NAMESPACE] [--timeout 180] > exported.json
```

Reads every retained `ExportAction`, groups them by application namespace (the
`k10.kasten.io/appNamespace` label, else the subject namespace) and lists per namespace
the export-action count and states, the policies that produced them (from the action
labels, or as `current_selector_candidate` when a live policy's selector matches the
namespace — `appNamespace` literals and trailing globs, label `In`/`NotIn`/`Exists`/
`DoesNotExist`), each policy's snapshot and export frequency, profile and paused flag.
VM or otherwise unsupported selectors are listed under `unresolved_selector_policies`
rather than guessed. Actions whose namespace cannot be determined go to
`unresolved_export_actions`. It answers the scope question of guide 01 in one command,
and it is faster than the generator because it never touches a repository — but it only
sees actions still retained on the cluster, and a retained action does not mean a
successful export.

### `scan-all-pvc.sh` — live occupation of every PVC

```sh
./scan-all-pvc.sh [--context CTX] [-n NAMESPACE] [--timeout 180] [--image busybox:1.37.0] \
                  [--no-inspector] [--network-storage-class NAME ...] > pvc-scan.json
```

Measures every Bound filesystem PVC (block-mode PVCs and unbound claims are reported as
unavailable). Where a running container already mounts the PVC without `subPath`, it
measures there; otherwise it starts a read-only inspector pod (restricted security
context, `busybox` by default, non-root UID on non-OpenShift clusters) and deletes it
afterwards — `--no-inspector` disables that, and an RWO PVC held by another pod is
skipped rather than double-mounted. Local or block storage is measured with `df`
(capacity, used, inodes); network storage — detected from the provisioner
(`nfs|smb|cifs|file.csi|azurefile|efs|filestore|gluster|cephfs`, or forced with
`--network-storage-class`) — is walked with `find`/`stat`/`du` instead, because `df` on
a shared export describes the whole export (guide 04). Per PVC: capacity, used and
available bytes, occupation %, inode counts, `file_count`, `logical_file_bytes` and the
average file size, plus the method used. Exit code 2 if any PVC could not be measured.
This is the live-PVC counterpart of the generator's repository view: it also covers
PVCs that were never exported, at the cost of reading each volume.

## How to use this repository

```bash
. lib/init.sh          # source it; do not execute it, and do not pipe it
audit_status           # what it configured
```

**`lib/init.sh` will not start the audit unless every required permission is granted.**
It runs the full authorisation check first and aborts with the list of what is missing
and which guides each gap blocks. There is no partial mode — see
[guides/00-prerequisites.md](guides/00-prerequisites.md) §8 for the complete list with
the justification for each.

`lib/init.sh` is the single entry point. It works in bash and zsh, is idempotent, and
exports everything the guides need: `K10NS`, `AUDIT_DIR`, `CLUSTER_UID`, the metrics
window (`AUDIT_WINDOW_DAYS`, `AUDIT_START`, `AUDIT_END`, `AUDIT_RANGE`) and the query
helpers (`tq`, `tqr`, `kq`, `pf_start`). It defaults to OpenShift cluster monitoring;
for any other Prometheus, export `PROM_URL` first — see the header of
[lib/prometheus.sh](lib/prometheus.sh).

| File | Role |
|------|------|
| [lib/init.sh](lib/init.sh) | entry point: detects the cluster, sets up everything below |
| [lib/prometheus.sh](lib/prometheus.sh) | cluster monitoring (`tq`/`tqr`) and K10's own Prometheus (`kq`) |
| [lib/window.sh](lib/window.sh) | derives the effective metrics lookback window |
| [lib/portforward.sh](lib/portforward.sh) | `pf_start`/`pf_stop` — port-forwards that fail loudly |
| [lib/check_auth.sh](lib/check_auth.sh) | `check_auth`/`require_auth` — the mandatory authorisation gate |
| [lib/provenance.sh](lib/provenance.sh) | `audit_record`, `audit_redaction_check` |
| [lint-paste-safety.py](lint-paste-safety.py) | checks guide code blocks survive a paste into zsh |
| [generate-export-topology.py](generate-export-topology.py) | the automated sweep: whole export topology as JSON |
| [render-export-topology.py](render-export-topology.py) | that JSON as a single self-contained HTML page |

Sourcing `init.sh` is what performs the setup: it defines the helpers, creates
`$AUDIT_DIR`, and derives `AUDIT_WINDOW_DAYS` — the effective metrics lookback window
that guides 06, 07, 08 and 13 depend on. You do not have to run anything from guide 00
to get them.

Then:

1. Read [guides/00-prerequisites.md](guides/00-prerequisites.md) first. It does not set
   anything up — it explains what `init.sh` just did, documents every permission the
   audit requires (§8), and produces this guide's deliverable: `audit_record` writes the
   provenance record so every later figure can be read against the cluster state, K10
   version and metrics window it was taken from.
2. Work through guides 01–13 in order.
3. Guides 04, 05, 06, 11 and 12 create short-lived pods; everything they do is
   read-only with respect to your data.
4. Guides 09 and 13 run backup policies and need a maintenance window.

## Guide index

| # | Guide | Data point | Impact on cluster |
|---|-------|-----------|-------------------|
| 00 | [Prerequisites](guides/00-prerequisites.md) | Tooling, access, effective metrics window, redaction | none |
| 01 | [Namespaces in scope](guides/01-protected-namespaces.md) | Which namespaces have an export action, and its cadence | read-only |
| 02 | [Policy frequency](guides/02-policy-frequency.md) | Schedule, retention, actions, export targets | read-only |
| 03 | [PVCs per namespace](guides/03-pvc-per-namespace.md) | PVC count and provisioned size per namespace | read-only |
| 04 | [Files per PVC](guides/04-files-per-pvc.md) | File count per PVC | read-only |
| 05 | [Average file size per PVC](guides/05-average-file-size-per-pvc.md) | Mean file size per PVC | read-only |
| 06 | [Change rate](guides/06-change-rate.md) | Bytes ingested per backup cycle | read-only |
| 07 | [Node CPU and RAM](guides/07-node-cpu-memory.md) | Node capacity, allocatable, real headroom | read-only |
| 08 | [Node disk and ephemeral storage](guides/08-node-disk-ephemeral-storage.md) | Free disk and ephemeral-storage pressure per node | read-only |
| 09 | [Datamover CPU and RAM](guides/09-datamover-cpu-memory.md) | Actual datamover consumption, with PVC attribution | runs a policy; needs a window |
| 10 | [Datamover configuration](guides/10-datamover-helm-actionpodspec.md) | Helm values, `k10-config`, ActionPodSpec | read-only |
| 11 | [Object store object count](guides/11-object-store-object-count.md) | Objects and bytes per repository prefix | read-only |
| 12 | [Kopia repository diagnostics](guides/12-kopia-repository-diagnostics.md) | `repo-checker` inventory and diagnose bundles | read-only, spawns pods |
| 13 | [Slow exports and restores](guides/13-slow-exports-and-restores.md) | Job durations, outliers, OOM-killed datamovers | reads history; test policies need a window |

## Validation status

Every command in guides 00–13 was executed against a live OpenShift 4.18
(Kubernetes v1.31) cluster running **K10 9.0.5**, installed via the Helm chart
`k10-9.0.5` in namespace `kasten-io`, with:

- storage backends: Azure Disk CSI (`managed-csi`), Azure File CSI, NFS CSI
- object storage: in-cluster MinIO via an S3 `Location Profile`
- OpenShift cluster monitoring (Thanos Querier + cAdvisor) enabled

Where a command could not be fully exercised on that cluster, the guide says so
explicitly under **Validation status**.

## Conventions

- `$K10NS` is the K10 namespace, `kasten-io` by default.
- `$AUDIT_DIR` is the output root, `./k10-audit-<cluster>-<date>/`, one subdirectory per
  guide.
- `$AUDIT_WINDOW_DAYS`, `$AUDIT_START`, `$AUDIT_END` and `$AUDIT_RANGE` are exported by
  `lib/init.sh` via `lib/window.sh`. Every time-bounded query uses them instead of a
  hardcoded period.
- Commands assume `bash`, `kubectl` (or `oc`), and `jq`. `helm` is needed for guide 10.

## A note on time windows

Declared Prometheus retention is not the window you have. If Prometheus has no
persistent volume — its TSDB in an `emptyDir` — history dies with the pod, and the real
window is bounded by the longest-running replica's uptime.
[lib/window.sh](lib/window.sh) derives `min(retention, longest replica uptime)` and every
period-dependent query in guides 06, 07, 08 and 13 reads it from there. The derivation is
written to `$AUDIT_DIR/metrics-window.txt` by `audit_record`, and should be quoted next
to every trend figure in the final report: "no OOM kills found" over 2 days and over 15 days are different
statements.
