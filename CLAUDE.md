# CLAUDE.md — authoring rules for this repo

Data-collection guides for a **Veeam Kasten K10 performance audit**. Read this before
editing anything in `guides/`.

## Layout

`lib/` holds the shared shell library; guides call its functions rather than redefining
setup inline. `lib/init.sh` is the only thing a reader sources. When adding a helper,
put it in `lib/` and reference it from the guide — do not paste a new function
definition into a guide.

Run after any change to `lib/`:

```bash
for sh in bash zsh sh; do $sh -n lib/*.sh || echo "$sh parse FAIL"; done
bash -c '. lib/init.sh' && zsh -c '. lib/init.sh'
```

## Audience

**Guides 00–13** are executed by the *customer's* platform team. They are procedures
under time pressure, not reference material.

Authorisation is not a separate document and is not staged: `lib/check_auth.sh` holds
the complete list, `lib/init.sh` refuses to start without all of it, and
`guides/00-prerequisites.md` §8 explains each entry. When a guide starts needing a new
permission, add it to `_auth_rows` in `lib/check_auth.sh` **and** to the §8 table.

Write lean. Command first, justification after it or in a clearly separable subsection.
Keep the hard-won gotchas — they are the value — but state them in a sentence or two.
One validated command beats two alternatives plus a comparison matrix.

## Shell portability — the recurring source of bugs

**Assume roughly half of readers use zsh.** Every snippet must work in bash *and* zsh.
Test both before committing; `zsh -c` and `bash -c`, and for anything meant to be pasted,
`zsh -i < file`.

Three defects have already shipped from ignoring this:

### 1. zsh has `INTERACTIVE_COMMENTS` off by default

A pasted `#` line is executed, not ignored. An apostrophe in a comment hangs on
`quote>`; `<`, `>`, `|`, `&`, `(`, `)`, or a bare `do`/`done` gives `zsh: parse error`.
bash has the option on, so this is invisible when testing in bash only.

Mitigations, in order of preference:

- Deliver any multi-line script as `cat > name.sh <<'SH' … SH` then `. ./name.sh`. A
  quoted heredoc body is literal text and is never parsed — immune regardless of shell
  options. This is why everything reusable lives in `lib/` as a sourced file.
- Keep comments inside pasted blocks free of `'`, `<`, `>`, `|`, `&`, `(`, `)` and shell
  keywords.
- Guide 00 §1b tells readers to `setopt interactive_comments`; do not rely on it alone.

Run the linter after editing any guide:

```bash
python3 lint-paste-safety.py
```

It classifies by severity. **HANG** (unbalanced quote — the paste appears to freeze at a
`quote>` prompt) and **EXEC** (backticks or `$(...)` in a comment — the comment's
contents are actually run) must be zero; the linter exits non-zero if not. **PARSE**
(a `(`, `<`, `>`, `|` or `;do` in comment prose) is neutralised by
`setopt interactive_comments` and is acceptable — purging every parenthesis from the
prose is not worth it. Currently: HANG=0, EXEC=0, PARSE=8.

### 2. zsh does not word-split unquoted *parameter* expansion

`for v in "get nodes"; do cmd $v; done` passes one argument in zsh, two in bash. Pass
argument lists through a function taking `"$@"`. Unquoted *command* substitution
(`$(cmd)`) does split in both, so `for n in $(kubectl get nodes -o name)` is fine.

### 3. Job specs are positional and fragile

Never `cmd & … kill %1`. Guide 00 leaves a port-forward running, so `%1` in a later
guide is the wrong job — and `kill: %1: no such job` means the process already died with
the reason unread in a log under `/tmp`. Use `pf_start` / `pf_stop` from guide 00 §4,
which capture `$!`, refuse an occupied port, and print the log on failure.

### Sourcing traps

`lib/init.sh` must be sourced, never executed (it detects and refuses) and never piped —
`. lib/init.sh | tee log` runs it in a subshell so nothing reaches the caller's shell,
and the only symptom is `command not found` later. Use a redirect.

### Stay in the POSIX subset

No `[[ ]]`, arrays, `local`, `${var,,}`, `read -a`, `function` keyword, or `/dev/tcp`
(zsh lacks it). Prefer `sh`-compatible syntax throughout.

## Validate before documenting

A live cluster is the validation target — the reference one used so far was OpenShift
4.18 / Kubernetes v1.31 with K10 9.0.5. Run commands against a real cluster first. Where a step could not be exercised,
**say so explicitly** rather than omitting it — guides 01–14 carry a
`## Validation status` section for this. Guide 00 does not; do not re-add one there.

Round-trip check for embedded scripts: extract the script back out of the markdown and
run *that*, so the document is correct as written, not merely as tested.

## Scope rule

**A namespace is in scope iff a policy with an export action selects it.** That single
rule defines what the audit covers, and `lib/policies.sh` implements it. An export is
what creates a datamover pod, a Kopia repository, object-storage consumption and a
measurable duration; a backup-only policy produces local snapshots with none of that, so
there is nothing to measure.

Do **not** reintroduce coverage grading or match-form reporting here. This is a
performance audit, not a coverage audit. Coverage is solved properly in
`kasten-mcp/kasten-bot/kasten_bot/policies.py` (graded levels, VM selectors, NotIn,
`kasten-io-cluster`); if a coverage question comes up, point at that rather than
re-deriving it.

VM policies (`virtualMachineRef`, `virtualMachineNamespace`) **are** in scope for the
generator: a VM's disks are PVCs in the VM's namespace and its export lands in that
namespace's repository, so only the namespace is resolved from the selector and the
Kopia read is the same. On a lab cluster 11 of 27 exporting policies are VM policies. The
guides (shell) still flag them for manual resolution.

## The generator

`generate-export-topology.py` is the automated counterpart of guides 01–13: one JSON
document per cluster, built only from what the Kopia repositories contain. Python 3
stdlib only, `kubectl` + `helm` via subprocess, no third-party modules — customers run
it where they cannot pip-install. It reuses the guides' findings; when a guide learns
something new about a data source, mirror it in the script, and vice versa.

Validate a change by running it against a live cluster and checking the debug pods are
gone afterwards (`kubectl -n kasten-io get pods | grep -E 'debug-kopia|k10tools'`).

It runs on large clusters that already have performance problems, so the user must be
able to tell "slow" from "hung". Progress goes to stderr through `log()` (elapsed-time
prefix); anything that can take more than a few seconds runs inside `with Step(...)`,
which announces the step, heartbeats every 60 s and prints the duration; `RepoChecker._run`
streams `repo_checker` and heartbeats every 30 s (`-v` echoes every line). Never add a
step that can be silent for minutes.

`render-export-topology.py` turns that JSON into one HTML file. It follows the dataviz
skill: colours are the documented palette roles as CSS custom properties (light + dark),
the file-size histogram is one series in slot-1 blue (a 6-step ordinal blue ramp cannot
pass the validator - no six documented steps have dL >= 0.06 above the 2:1 light-end
floor - and a value ramp on bars is an anti-pattern anyway), bars <= 24px with a rounded
data-end only, status colours always paired with a label, and every chart has a table
view. Screenshot it headless before shipping a layout change:
`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --screenshot=out.png file:///path.html`.

`repo_checker -r inventory` aborts the WHOLE inventory on the first repository whose
Location Profile was deleted ("failed to find a profile with given location
information"). k10tools has no continue-on-error; only `-p profile` and `-R repository`
filters, and every invocation re-scans the catalog (64k refs on a lab cluster, plus a
340k-entry "backstop scan" when a repository has orphaned snapshots), so per-repository
runs are expensive. The generator therefore discovers scope from the **export policies**
(`policy_targets`, a Python port of `lib/policies.sh`) and treats the inventory as
best-effort enrichment: full -> per profile, orphans reported under
`orphanedRepositories`. `storagerepositories.repositories.kio.kasten.io` (85 on a lab
cluster; empty on the reference cluster) carries per repository the owning namespace (label
`k10.kasten.io/appName`), the profile (label `k10.kasten.io/exportProfile`),
`status.contentType` (metadata/volumedata) and `status.location` (bucket, store type,
path, region) - the generator enriches orphans from it. The abort reason varies and is
only in k10tools' `Error: {json}` cause chain (`k10tools_cause_chain`; causes nest and
may be string-encoded JSON): "failed to find a profile with given location information"
(profile deleted, or a repository written by another cluster - compare the UID in the
path), "unable to find migration token for repo location" (a migration metadata
repository whose receive token secret is gone), ... Report the innermost cause verbatim
plus the CR facts; do not infer the reason from the path.

A focused run (`--namespace NS --policy P`) skips the full inventory and inventories
only the profiles of its pairs (`RepoChecker.inventory(..., full=False)`); the renderer
has the same `--policy/--namespace` filters for an existing JSON. Pin `--context` for any
run longer than a minute: the user switches kube contexts, and a run that starts on one
cluster and connects on another fails with "profile not found".

k10tools also has `repository snapshot-diff` (compare two restore points) - not yet
explored; a candidate for exact per-PVC change rate.

## Environment gotchas already discovered

Do not re-derive these; do not contradict them without re-testing.

| Area | Trap |
|------|------|
| NFS/SMB PVCs | `statfs` reports the whole export, so `kubelet_volume_stats_inodes_used` and `df -i` are wrong per-PVC. Use `find`/`du`, or Kopia snapshot stats. |
| Kopia per-snapshot stats | `stats.fileCount` / `nonCachedFiles` = files Kopia **hashed** in that run; `cachedFiles` = files skipped as unchanged against the previous snapshot (0 on a first snapshot - which is why it once looked "always 0"). The tree's file count and size are `rootEntry.summ.files` / `summ.fileSize`. Validated on the reference cluster: 167-file volume, second snapshot -> fileCount 14, cachedFiles 153, summ.files 167. Hashed/summ.files is a per-PVC change indicator that survives maintenance; `content list` timestamps give the bytes but not after a full maintenance. |
| cAdvisor | No pod labels — only `pod`, `container`, `namespace`, `node`. Capture pod labels live and join on `pod`. |
| Datamover pods | `resources: {}` — BestEffort, no limits. `workerPodResourcesCRDEnabled=false` makes ActionPodSpec resources inert. |
| BusyBox containers | No `find -printf`. Use `find … -exec stat -c '%s' {} +`. |
| macOS/BSD `awk` | No `strftime`. Do time bucketing in `jq`. |
| `kubectl logs --since` | Go durations only — `15d` is rejected, use `360h`. |
| `kubectl logs deploy/x` | Reads **one** pod. Use `-l component=… --max-log-requests N`. |
| `jq` + K10 timestamps | `fromdateiso8601` rejects fractional seconds; strip with `sub("\\.[0-9]+(?=Z$)";"")`. |
| K10 credential secrets | Keys are lowercase (`aws_access_key_id`); `envFrom` yields lowercase env vars and `mc`/`aws` then fail with `Access Denied`. Map explicitly. |
| K10 Prometheus | Served under `/k10/prometheus`, 404 at root. Scrapes only K10 services — no cAdvisor. |
| Cluster UID | It is the `default` namespace UID. Resolve once; an empty value makes `grep -v ""` exclude everything and silently report "no orphans". |
| `repo-checker` | Wraps JSON in log lines — extract with `sed -n '/^{/,/^}/p'`. Leaves `debug-kopia-*` pods behind. |
| Kopia timestamps vs maintenance | `full-rewrite-contents` re-stamps every rewritten content and pack blob with the maintenance time. Per-snapshot physical ingest from `content list` / `blob list` timestamps is only valid until the next full maintenance (24 h default). Report `null` + reason, never `0`. |
| `kopia maintenance info --json` | Runs live under `.schedule.runs`, not `.runs`. The debug pod has no `jq`; parse locally. `kopia` writes log-dir noise to stderr — read stdout only. |
| `kopia ls -l -r <root>` | Enumerates exactly `fileCount` regular files: mode size date time UTC objid path. The histogram can come from the repository without touching the PVC. |
| Datamover attribution | OpenShift KSM runs `--metric-labels-allowlist=pods=[*]`, so `kube_pod_labels{label_app_name,label_policy_name,label_k10_kasten_io_job_id}` joins on `pod` — no cAdvisor change needed. `copy-vol-data` pods only carry `job_id`; map it via the `data-mover-svc` pod of the same job. |
| Kopia `Source.host` | `<applicationID>.<workload>.<pvc>`; the ID is per application, not the RepositoryID. Dots are legal in PVC names — resolve against live PVCs, fall back to structure (`lib/kopia.sh`, `resolve_pvc`). |
| `SizeBytes` / `totalFileSize` | Logical size of the source at snapshot time, not the increment. Deltas give net growth only. |
| Block-mode volumes (VM disks) | Kopia tree is `meta:*` entries + a `c/` chunk directory; `stats.fileCount` = chunk count, `stats.totalFileSize` = null, real size is `rootEntry.summ.fileSize`, block size is `meta:BlockSzB:<hex>` (0x100000 = 1 MiB). Marker: `description` starts `volume:` / `source.path` starts `/volume/`. A file histogram is meaningless there. |
| CloudNativePG-style volumes | `stats.fileCount` = 0 while `totalFileSize` is set and `kopia ls -r` lists real files. Trust the listing for the count. |
| `repo_checker -o connect` failure | Leaves its `debug-kopia-*` pod Running. Cause is in the JSON status block `StatusMessage` (escaped error chain); "failed to connect to repository" = no repository for that namespace on that profile, i.e. never exported there. |
| `repo_checker` images | Defaults are hard-coded: repo `gcr.io/kasten-images` (`-i`) and tag = newest `kasten/k10` chart in the **local** helm repo (`-t`), not the cluster version - a 9.0.1 cluster got `k10tools:9.0.5`. The generator always passes `-t <cluster version>`; `--image-registry auto` derives `-i` from `k10-config` `KanisterToolsImage`. Air gap also needs `--repo-checker PATH` (no docs.kasten.io). |
| `repo_checker` cwd | It writes `repo-checker.yaml` to the current directory and deletes it afterwards; two runs in one directory race and one connect fails with `the path "repo-checker.yaml" does not exist`. The generator runs it with `cwd=workdir`. |
| Empty VM disks | A blank DataVolume disk exports as a block-mode snapshot with 0 chunks / `summ.fileSize` 0 although the PVC requests GiBs. Correct data - render it as "empty disk", not `0 B`. |
| Concurrent PVC exports | Disks of one VM are exported at the same time, so their snapshot windows overlap and a content block can fall in several. `assign_contents` picks the nearest window and reports the overlapping bytes as `physicalIngestAmbiguousBytes`; the renderer shows `~`. Namespace/export-level figures are unaffected. |
| ExportActions | Per-application exports live in the **application namespace** (`scheduled-*`, labels `policyName`, `exportProfile`, `runActionName`); the ones in the K10 namespace are the run's metadata export (`isMetadataExport=true`, no bytes). Byte counters (`status.progressDetails`: `totalBytes` = capacity, `readBytes`, `processedBytes`, `transferredBytes`, `processingRate`) and per-volume `phases[].volumeOperations` (pvcName, dataFormat, exportDirective, snapshotId) exist **only** on `GET …/exportactions/<name>/details`, ~0.8 s and ~700 KB each. |
| `kubectl exec -i` + stdin | Feeding the script on stdin (`sh -s`) intermittently truncates or garbles large stdout (a 100 KB `kopia … --json` broke mid-document). Pass the command as an argument: `kubectl exec pod -- sh -c '…'`. |
| Node usage | `GET /api/v1/nodes/<n>/proxy/stats/summary` returns CPU (`node.cpu.usageNanoCores`), memory (`node.memory.workingSetBytes/availableBytes`) and the root fs (`node.fs.usedBytes/capacityBytes`, = ephemeral storage) in one call; `metrics.k8s.io` gives CPU/memory only (10 s window). Node `status.capacity["ephemeral-storage"]` is in Ki, `allocatable` in plain bytes - parse quantities, do not compare strings. |
| Scope pre-filter | A namespace with no RestorePoint was never backed up, hence never exported — skip it before a connect. `virtualMachineNamespace In ["*"]` otherwise expands to every namespace (47 on a lab cluster; 18 after the filter). |

## Secrets

Never add a step that reads secrets from application namespaces. Helm values, the K10
CR and the `repo-checker` diagnose bundle all contain credentials — redact before any
extract leaves the cluster (guide 00 §7, guide 10 step 0). Provenance output must contain
no credentials; `THANOS_TOKEN` is deliberately excluded from `00-audit-env.sh`.

## Time windows

Never hardcode a period. Use `AUDIT_WINDOW_DAYS`, `AUDIT_START`, `AUDIT_END`,
`AUDIT_RANGE` from guide 00 §6, which derive `min(retention, longest Prometheus replica
uptime)` — Prometheus on `emptyDir` loses history with the pod. State the window next to
every trend figure; "no incidents found" over 2 days and over 15 days are different
claims.

Note that `$AUDIT_RANGE` will not expand inside single-quoted PromQL: write
`[' "$AUDIT_RANGE" ']` without the spaces.

## Open questions (the audit owner's call, not derivable from the cluster)

- Guide 13 thresholds for "slow".
- Which namespaces are already known to be problematic.
