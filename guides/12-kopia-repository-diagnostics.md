# 12 — Kopia repository diagnostics

## What auditors need

For the repository of `$AUDIT_NS` on `$AUDIT_PROFILE`: its inventory entry, its
maintenance state, its blob and content statistics, and its orphan counts. This guide
also opens the connection that guides 04, 05, 06 and 11 read from — **run it before
those**.

## The tool

`k10_repo_checker.sh`, shipped per release:

```
https://docs.kasten.io/downloads/<VERSION>/tools/k10_repo_checker.sh
```

It needs `kubectl` and `helm` on the PATH, and it creates a `k10tools` pod in the K10
namespace using the `executor-svc` service account.

| Repository type | Holds | Invocation |
|------|-------|-----------|
| `application` | volume data for one namespace | `-r application -a <namespace> -p <profile>` |
| `collections` | metadata for one policy | `-r collections -l <policy> -p <profile>` |
| `disaster_recovery` | K10's own DR backup | `-r disaster_recovery -p <profile>` |
| `inventory` | lists all of the above | `-r inventory [-p <profile>]` |

### Pin the image, or it will pick the wrong one

`repo_checker` detects nothing about the cluster. Left alone it pulls
`gcr.io/kasten-images/k10tools` and takes the image **tag** from the newest `kasten/k10`
chart in your *local helm repository* — a 9.0.1 cluster once got `k10tools:9.0.5`.
Neither default is right on an enterprise cluster, where `gcr.io` is usually not
whitelisted.

```bash
. lib/init.sh
focus_dir 12-kopia

curl -sSLO "https://docs.kasten.io/downloads/${K10_VERSION}/tools/k10_repo_checker.sh"
chmod +x k10_repo_checker.sh

# the registry K10 itself pulls from - mirrored or whitelisted where gcr.io is not
RC_IMG=$(kubectl -n "$K10NS" get cm k10-config -o jsonpath='{.data.KanisterToolsImage}' \
         | sed 's|/[^/]*$||')
echo "repo_checker images: ${RC_IMG:-gcr.io/kasten-images}/k10tools:$K10_VERSION"
```

Validated: `registry.connect.redhat.com/kasten/k10tools:9.0.5`, pullable on an operator
install. Pass both on **every** invocation: `-i "$RC_IMG" -t "$K10_VERSION"`.

> **An unpullable image hangs the script forever.** Its pod wait is a bare `while true`
> until the pod is Running, so `ErrImagePull` never ends it. If a run sits silent for
> more than a minute:
>
> ```bash
> kubectl -n "$K10NS" get pods | grep -E 'k10tools|debug-kopia'
> kubectl -n "$K10NS" describe pod -l createdBy=Kasten-K10 | grep -A3 -i 'failed\|backoff'
> ```
>
> Then kill the script, delete the pod, and either mirror `k10tools` next to the K10
> images or pass `-i gcr.io/kasten-images` if that is reachable.

> **Run it from its own directory.** It writes `repo-checker.yaml` into the current
> directory and deletes it afterwards, so two concurrent runs in one directory race and
> one fails with `the path "repo-checker.yaml" does not exist`. `focus_dir` already
> gives you a per-pair directory.

## Method

### Step 1 — inventory, scoped to the profile

Read-only, and it tells you what else to run.

```bash
./k10_repo_checker.sh -r inventory -p "$AUDIT_PROFILE" -F json -n "$K10NS" \
    -i "$RC_IMG" -t "$K10_VERSION" 2>/dev/null \
  | sed -n '/^{/,/^}/p' > inventory.json

jq -r '(["TYPE","REPOSITORY","SNAPSHOTS","ORPHANED","NOT_SYNCED","DANGLING","NAMESPACE","POLICY"]|@tsv),
       (.repositories[] | . as $r
        | [ $r.Type, $r.RepositoryName, $r.TotalSnapshots,
            $r.OrphanedCount, $r.NotSyncedCount, $r.DanglingCount,
            ([$r.Snapshots[]?.RestorePointNamespace] | unique | join(",") | if .=="" then "-" else . end),
            ([$r.Snapshots[]?.PolicyName] | unique | join(",") | if .=="" then "-" else . end) ]
        | @tsv)' inventory.json | tee repositories.tsv | column -t
```

Validated:

```
TYPE        REPOSITORY                              SNAPSHOTS  ORPHANED  NOT_SYNCED  DANGLING  NAMESPACE                            POLICY
Data        kopia-volumedata-repository-zwlcc2mpr8  5          0         0           0         test-calibrate                       calibrate-backup
Data        kopia-volumedata-repository-2vvf7fzhld  5          0         0           0         prod-test                            calibrate-backup
Data        kopia-volumedata-repository-ddxkpdh4bf  4          0         0           0         large-test                           calibrate-backup
Collection  kopia-metadata-repository-bwwfdkbd82    14         0         0           0         large-test,prod-test,test-calibrate  calibrate-backup
Collection  kopia-metadata-repository-8cclv6j629    3          0         0           0         large-test-block                     calibrate-backup-block
Data        kopia-volumedata-repository-bmqgkbzb6q  3          3         0           0         -                                    -
```

**One profile, six repositories.** One `Data` repository per namespace — which is why
this audit is scoped to a pair — plus one `Collection` per policy for metadata.
`kopia-volumedata-repository-2vvf7fzhld` is `$AUDIT_NS`'s.

> **Parsing note**: the script writes coloured progress lines around the JSON. Piping
> straight into `jq` fails with `Invalid numeric literal`. `sed -n '/^{/,/^}/p'`
> extracts the document. This bites every time.

> **A whole-cluster inventory aborts on the first broken repository.** Without `-p`,
> one repository whose Location Profile has been deleted kills the run with
> `failed to find a profile with given location information` — and k10tools has no
> continue-on-error. Always pass `-p "$AUDIT_PROFILE"`. Other causes seen: a repository
> written by *another* cluster (compare the UID in the prefix), and
> `unable to find migration token for repo location` for a migration repository whose
> receive-token secret is gone. Report the innermost cause verbatim; do not infer it
> from the path.

### Step 2 — resolve the orphans

The last row above has 3 snapshots, all orphaned, and **no namespace** — the inventory
cannot say whose it is. The `StorageRepository` CR can:

```bash
kubectl -n "$K10NS" get storagerepositories.repositories.kio.kasten.io -o json \
  | jq -r '(["REPOSITORY","NAMESPACE","PROFILE","CONTENT","BUCKET","PATH"]|@tsv),
           (.items[] | [ .metadata.name,
                         (.metadata.labels["k10.kasten.io/appName"] // "-"),
                         (.metadata.labels["k10.kasten.io/exportProfile"] // "-"),
                         (.status.contentType // "-"),
                         (.status.location.objectStore.name // "-"),
                         (.status.location.objectStore.path // "-") ] | @tsv)' \
  | tee storage-repositories.tsv | column -t
```

Validated — the orphan is resolved:

```
REPOSITORY                              NAMESPACE         PROFILE        CONTENT     BUCKET
kopia-volumedata-repository-bmqgkbzb6q  large-test-block  my-s3-profile  volumedata  my-s3-profile
kopia-volumedata-repository-2vvf7fzhld  prod-test         my-s3-profile  volumedata  my-s3-profile
```

`bmqgkbzb6q` belongs to `large-test-block`. Its snapshots are orphaned because their
restore points have been retired while the Kopia snapshots remain — storage that is
paid for and unreferenced. Report it; do **not** delete anything during an audit.

### Step 3 — diagnose the pair's repository

`diagnose` dumps Kopia's own debug data into a `.tar.gz` in the current directory.
Nothing is modified — it connects read-only.

```bash
./k10_repo_checker.sh -r application -o diagnose -a "$AUDIT_NS" -p "$AUDIT_PROFILE" \
    -n "$K10NS" -i "$RC_IMG" -t "$K10_VERSION" 2>&1 | tee diagnose.log

for t in kopia-diagnose-*.tar.gz; do
  d="${t%.tar.gz}"; mkdir -p "$d"
  tar -xzf "$t" -C "$d"
  chmod -R u+rwX "$d"          # otherwise: find: Permission denied
done
find . -name '*-stdout.txt' | sort
```

The bundle holds 12 files under `tmp/kopia-debug-logs/`. The four to read first:

```bash
cat */tmp/kopia-debug-logs/repository-status-stdout.txt
cat */tmp/kopia-debug-logs/maintenance-info-stdout.txt
cat */tmp/kopia-debug-logs/blob-stats-stdout.txt
cat */tmp/kopia-debug-logs/content-stats-stdout.txt
```

| File | Use |
|------|-----|
| `blob-stats-stdout.txt` | object count, total bytes, size histogram → guide 11 |
| `content-stats-stdout.txt` | content count, compression ratio by method → guide 05 |
| `index-list-stdout.txt` | index blob count and timestamps; maintenance cost driver |
| `maintenance-info-stdout.txt` | schedule, owner, recent run outcomes |
| `repository-status-stdout.txt` | format version, hash, splitter, epoch manager, pack size |
| `restore-point-snapshotID-mapping.txt` | restore point → PVC → Kopia snapshot ID |
| `logs-show-all-stdout.txt` | full Kopia debug log — per-operation latency, → guide 13 |
| `blob-list-stdout.txt` | every blob with id, length, timestamp |

`repository-status` on this cluster, performance-relevant lines:

```
Hash:                BLAKE2B-256-128
Splitter:            DYNAMIC-4M-BUZHASH
Format version:      3
Content compression: true
Max pack length:     21 MB
Index Format:        v2
Epoch Manager:       enabled
```

Format version 3 with the epoch manager enabled is current and healthy; a repository
still on v1/v2 without it is a migration candidate and a known performance problem.

### Step 4 — connect, for guides 04, 05, 06 and 11

`connect` leaves a second pod running with the repository mounted read-only. **It does
not clean up after itself.**

```bash
./k10_repo_checker.sh -r application -o connect -a "$AUDIT_NS" -p "$AUDIT_PROFILE" \
    -n "$K10NS" -i "$RC_IMG" -t "$K10_VERSION" 2>&1 | tee connect.log

kopia_debug_pod          # finds the pod the script left behind
```

Validated: `Connection established` in about 40 s, leaving `debug-kopia-kw9ct`.

Everything downstream goes through `kopia_exec`
([../lib/kopia.sh](../lib/kopia.sh)), which sets `KOPIA_CONFIG_PATH`, passes the
command as an **argument** to `sh -c` and reads stdout only:

```bash
kopia_exec 'kopia repository status --json'  > repository-status.json
kopia_exec 'kopia maintenance info --json'   > kopia-maintenance-info.json
kopia_exec 'kopia snapshot list --all --json'> kopia-snapshots.json
kopia_exec 'kopia blob list --json'          > blob-list.json
kopia_exec 'kopia content list --json'       > contents.json
wc -c *.json
```

Validated sizes on this repository: snapshots 4.7 kB, blob list 411 kB, content list
**32.5 MB** (140,032 records). Stream `content list` to disk; never hold it in a shell
variable.

Two traps in one line: passing the command on **stdin** (`kubectl exec -i ... sh -s`)
intermittently truncates large stdout — a 100 kB `kopia ... --json` broke mid-document
— and Kopia writes log-directory noise to **stderr** that would corrupt the JSON if
merged. `kopia_exec` avoids both.

Copy what the other guides read:

```bash
P="$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY"
mkdir -p "$P/04-files-per-pvc" "$P/06-change-rate"
cp kopia-snapshots.json "$P/04-files-per-pvc/"
cp kopia-snapshots.json contents.json kopia-maintenance-info.json "$P/06-change-rate/"
```

Those guides also re-read them from here, so the copy is a convenience, not a
dependency.

### Step 5 — maintenance state

The repository's own account of whether it is keeping up:

```bash
jq -r '{owner, quick: .quick, full: .full,
        nextQuick: .schedule.nextQuickMaintenance,
        nextFull:  .schedule.nextFullMaintenance}' kopia-maintenance-info.json

jq -r '(["TASK","START","END","SUCCESS"]|@tsv),
       (.schedule.runs | to_entries[]
        | [ .key, (.value|last|.start), (.value|last|.end),
            (.value|last|.success|tostring) ] | @tsv)' kopia-maintenance-info.json \
  | tee maintenance-runs.tsv | column -t
```

Validated:

```
TASK                             START                           END                             SUCCESS
full-delete-blobs                2026-09-20T08:15:55.731108982Z  2026-09-20T08:15:58.575839341Z  true
full-drop-deleted-content        2026-09-20T08:15:52.677332930Z  2026-09-20T08:15:55.618923287Z  true
full-rewrite-contents            2026-09-19T08:14:41.044624954Z  2026-09-19T08:14:41.345885368Z  true
snapshot-gc                      2026-09-19T08:14:40.805758913Z  2026-09-19T08:14:40.806292595Z  true
```

Four questions to ask of this table:

1. Is the full cycle **completing**, or timing out? Durations growing run over run?
2. Is `owner` a pod that no longer exists — a stuck maintenance lock?
3. When did `full-rewrite-contents` last run? Everything before it has **unrecoverable**
   per-snapshot physical ingest (guide 06 step 2). Here: `2026-09-19T08:14:41Z`.
4. Is any task `success: false`?

Note the JSON shape: live runs are under `.schedule.runs`, **not** `.runs`. The debug
pod has no `jq`, so parse locally.

### Step 6 — clean up. Do not skip this.

```bash
kubectl -n "$K10NS" delete pod "$(kopia_debug_pod)"

kubectl -n "$K10NS" get pods | grep -E 'debug-kopia|k10tools' \
  || echo "clean: no diagnostic pods left"
```

`diagnose` leaves a `debug-kopia-*` pod behind too, even when it reports success and
deletes its own `k10tools` pod. Check after every invocation, not just after `connect`.

### Step 7 — format upgrade readiness

`repo_checker` exposes `upgrade_begin` / `upgrade_rollback`. **Do not run either during
an audit** — they mutate the repository. Report the current format version from step 3
and whether an upgrade is available; the upgrade itself is a change request, not a data
collection.

## Caveats

- **An empty inventory is a real answer.** Before any export has run, `repo_checker`
  reports `Found 0 storage repositories`. That means nothing has been exported
  off-cluster yet — not that the tool failed.
- **The diagnose bundle contains the bucket, prefix and access key ID** in
  `repository-status-stdout.txt`. The secret key is masked. Redact before sharing
  externally (guide 00 §7).
- **Every inventory invocation re-scans the whole catalog** — 64 k references on a lab
  cluster, plus a 340 k-entry backstop scan when a repository has orphaned snapshots.
  Per-repository runs are expensive; `-p` and `-R` are the only filters.
- **Air-gapped clusters** also need the script itself: there is no `docs.kasten.io`.
  Download `k10_repo_checker.sh` once from a connected host and mirror `k10tools`,
  `datamover` and `kanister-tools` at the cluster's version.
- The script requires `helm` even on operator-based installs.

## What to send back

| File | Contents |
|------|----------|
| `repositories.tsv` | every repository on the profile, with snapshot and orphan counts (headline) |
| `storage-repositories.tsv` | repository → namespace → bucket → path, which resolves orphans |
| `maintenance-runs.tsv` | last run per maintenance task, and whether it succeeded |
| `kopia-diagnose-*.tar.gz` | the raw bundle, redacted |
| `repository-status.json`, `kopia-maintenance-info.json` | the live equivalents |
| `connect.log`, `diagnose.log` | what the tool did, including the image it used |

Also confirm these exist, since other guides consume them:

- `$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/04-files-per-pvc/kopia-snapshots.json`
- `$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/06-change-rate/contents.json`

## Validation status

Fully validated on K10 9.0.5 against `prod-test` / `calibrate-backup` on the
`my-s3-profile` profile.

Exercised end to end: `-r inventory -p <profile> -F json`, returning six repositories
on that one profile — three `Data` (one per namespace of the policy) and two
`Collection`, plus one with **3 of 3 snapshots orphaned and no namespace**, which the
`StorageRepository` CR resolved to `large-test-block`; `-r application -o connect`
followed by `repository status`, `maintenance info`, `snapshot list --all`,
`blob list` and `content list` through `kopia_exec`; and the cleanup in step 6, after
which no `debug-kopia-*` or `k10tools-*` pod remained.

The image pinning was validated directly: `KanisterToolsImage` resolved to
`registry.connect.redhat.com/kasten`, and `-i registry.connect.redhat.com/kasten
-t 9.0.5` pulled successfully on this operator install.

Confirmed failure modes: the JSON-wrapped-in-logs parse error, and the leftover
`debug-kopia-*` pod after `connect`.

Not exercised in this pass: `-r collections -o diagnose`,
`-r disaster_recovery -o diagnose`, and the whole-cluster inventory abort — this
profile has no repository with a deleted Location Profile, so the abort message is
quoted from an earlier occurrence rather than reproduced here. `upgrade_begin` /
`upgrade_rollback` were deliberately not run.
