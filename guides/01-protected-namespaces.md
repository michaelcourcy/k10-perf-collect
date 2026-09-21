# 01 — Scope: the namespace and policy under audit

## What auditors need

Which (namespace, policy) pair this audit covers, why that one, and what the cluster
looks like around it. Every figure in guides 02–13 describes **that pair**.

## The scope rule

**A namespace is in scope if a policy with an export action selects it.** That is the
whole rule.

An export is what creates a Kopia repository, consumes object storage, moves bytes
off-cluster and takes a measurable amount of time. A backup-only policy produces local
snapshots: no datamover pod, no repository, no object count, no change rate, no export
duration. There is nothing to measure.

This is **not** coverage analysis. Whether a backup-only namespace is adequately
protected is a real and important question, but a different one — and one that needs
graded coverage levels and VM-selector handling that this guide deliberately does not
attempt.

## Why one pair

One namespace can be exported by several policies into several profiles, and **each
(namespace, profile) pair has its own Kopia repository**. Guides 04–06, 11 and 12 read
that repository; pointing them at a namespace rather than a pair silently mixes two
schedules into one change rate.

For the whole cluster at once, use
[`generate-export-topology.py`](../generate-export-topology.py). These guides are the
manual, one-pair counterpart of `--namespace NS --policy POLICY`.

## Method

```bash
. lib/init.sh
focus_dir 01-scope
```

### Step 1 — the candidate pairs

```bash
audit_focus_candidates | tee candidates.tsv | column -t
```

```
NAMESPACE         POLICY                  PROFILE              EXPORT_FREQ  PAUSED  RESTORE_POINTS
basic-app         basic-app-backup        my-s3-bucket         @onDemand    false   4
clusters          clusters-backup         my-s3-bucket         @hourly      true    0
large-test        calibrate-backup        my-s3-profile        @hourly      false   9
large-test-block  calibrate-backup-block  my-s3-profile        @hourly      false   7
mastodon          mastodon-backup         azurefile-filestore  @daily       false   6
prod-test         calibrate-backup        my-s3-profile        @hourly      false   10
test-calibrate    calibrate-backup        my-s3-profile        @hourly      false   10
```

Read three columns before choosing:

- **`EXPORT_FREQ`** is the number that matters, and it is frequently **not** the
  snapshot frequency: a policy can snapshot hourly and export on demand, in which case
  nothing leaves the cluster on a schedule and there is no historical export to measure.
- **`PAUSED`** true means nothing runs at all.
- **`RESTORE_POINTS`** 0 means the namespace was never backed up, so it was never
  exported and no repository exists. Guides 04–06, 11 and 12 will come back empty.
  This column is also what cuts a VM policy selecting `*` back down: it otherwise
  resolves to every namespace on the cluster.

Note `calibrate-backup` above: one policy, three namespaces, one profile — three
separate repositories and three separate pairs.

### Step 2 — pin the pair

```bash
audit_focus prod-test calibrate-backup | tee focus.txt
```

```
audit focus
  namespace    : prod-test
  policy       : calibrate-backup
  profile      : my-s3-profile
  restore pts  : 10
  equivalent   : generate-export-topology.py --namespace prod-test --policy calibrate-backup
```

`audit_focus` warns rather than refuses on a pair that will produce thin data — no
restore point, paused policy, `exportData` disabled, or a namespace it cannot resolve
from the selector. Record the warnings; they predict which later guide comes back empty.

### Step 3 — the policy's own view of the selector

Evidence that the pair is what you think it is:

```bash
kubectl -n "$K10NS" get policies.config.kio.kasten.io "$AUDIT_POLICY" -o json \
  | jq '{selector: .spec.selector, validation: .status.validation,
         actions: [.spec.actions[] | {action,
                                      profile: .exportParameters.profile.name,
                                      frequency: .exportParameters.frequency,
                                      exportData: .exportParameters.exportData.enabled}]}' \
  | tee policy-selector.json
```

Validated:

```json
{
  "selector": {"matchExpressions": [{"key": "k10.kasten.io/appNamespace",
                                     "operator": "In",
                                     "values": ["large-test", "prod-test", "test-calibrate"]}]},
  "validation": "Success",
  "actions": [{"action": "backup", "profile": null, "frequency": null, "exportData": null},
              {"action": "export", "profile": "my-s3-profile",
               "frequency": "@hourly", "exportData": true}]
}
```

`validation: Success` is the one field to check — a rejected selector matches nothing
and the policy silently never runs. The nulls on the `backup` action are expected: only
an export action carries export parameters.

**This selector names three namespaces.** One policy run therefore exports `large-test`,
`prod-test` and `test-calibrate` at the same time, into three separate repositories, with
their datamover pods alive in the same window. That is why guide 09 separates "own" from
"concurrent" load, and why the pair — not the policy — is the unit of this audit.

Selector forms — literal names, prefix globs, labels, exclusions — are resolved by
[../lib/policies.sh](../lib/policies.sh); the mechanics are documented in its header.

### Step 4 — evidence that the pair really exports

Step 2 is intent. This is evidence:

```bash
kubectl -n "$AUDIT_NS" get restorepoints.apps.kio.kasten.io \
  -o custom-columns='NAME:.metadata.name,CREATED:.metadata.creationTimestamp' \
  | tee restorepoints.txt

export_actions | tee export-actions.tsv | column -t
```

`export_actions` ([../lib/exports.sh](../lib/exports.sh)) lists the pair's
ExportActions, newest first. An empty list with restore points present means the
namespace is backed up but has never exported — or that the action history has been
retired (guide 02 step 3).

```
EXPORT                STATE     START                END                  DUR_S  RUN_ACTION        PROFILE        RUN_NOW
scheduled-gn9w65njrc  Complete  2026-09-20T08:00:52  2026-09-20T08:04:45  233    run-9vmgpx5x8f    my-s3-profile  false
scheduled-cdzgsbrfkz  Complete  2026-09-20T02:37:14  2026-09-20T02:38:01  47     run-ppj86pdgft    my-s3-profile  false
scheduled-5x9wblcnls  Complete  2026-09-19T22:01:10  2026-09-19T22:05:06  236    run-shvt66w5dp    my-s3-profile  false
```

The 233 s / 47 s / 236 s spread on an unchanged volume size is the first hint of what
guide 06 will explain: the short runs had nothing to upload.

### Step 5 — the surrounding cluster, for context

Two facts about the rest of the cluster change how the pair's figures should be read:

```bash
audit_focus_candidates | awk -F'\t' 'NR>1 {print $2}' | sort -u | wc -l \
  | tee other-exporting-policies.txt
k10_excluded_namespaces | tee excluded-namespaces.txt
```

Other exporting policies matter because they compete for the same cluster-wide
limiters (guide 02 step 4) and the same datamover pods (guide 09). `excludedApps`
namespaces are invisible to K10 and can never match any selector.

## Caveats

- **The list is a snapshot.** A selector is evaluated live, so a namespace created
  tomorrow can enter scope. `00-prerequisites.txt` records when the pair was chosen.
- **VM selectors are not resolved.** A policy selecting KubeVirt VMs by
  `virtualMachineRef` / `virtualMachineNamespace` does not appear in the candidate
  list, and `audit_focus` says so instead of failing. A VM's disks are PVCs in the VM's
  namespace and its export lands in that namespace's repository, so the pair is still
  valid — resolve the namespace by hand and pin it anyway. The generator resolves these
  automatically.
- **A paused or `@onDemand` export policy still resolves to namespaces.** It is in scope
  on paper and produces no current data. Guides 09 and 13 will need a triggered run
  (guide 13 step 7).
- **Multi-cluster**: policies may be pushed from a primary — check
  `kubectl -n "$K10NS" get distributions.dist.kio.kasten.io`. The local `Policy` objects
  are then copies and the primary is the source of truth.

## What to send back

| File | Contents |
|------|----------|
| `focus.txt` | the pair under audit — the header for every other deliverable |
| `candidates.tsv` | every exporting (namespace, policy) pair, with the evidence for the choice |
| `policy-selector.json` | the policy's selector, validation and export parameters |
| `export-actions.tsv` | the pair's export history |
| `restorepoints.txt` | proof the namespace has been backed up |
| `excluded-namespaces.txt` | namespaces K10 cannot see at all |

## Validation status

Fully validated on K10 9.0.5 (OpenShift 4.18) against the pair
`prod-test` / `calibrate-backup`. `audit_focus_candidates` reproduced the generator's
own scope exactly: the same seven pairs, the same profiles — including
`mastodon-backup → azurefile-filestore`, a FileStore rather than an object store.

Selector resolution was validated separately by creating probe policies and throwaway
namespaces, then **running** the policies and checking where restore points appeared:

- a `basic-app*` policy produced restore points in `basic-app`, `basic-app1` and
  `basic-app2` — the glob matches the prefix itself as well as its extensions;
- a literal `cpd` selector matched `cpd` alone, leaving `cpd-operators` and `cpdbr` out;
- a `matchLabels` policy matched on **namespace** labels, and K10 restricts these more
  than Kubernetes does: a label `matchExpression` may carry only one value, so
  `tier In (gold, silver)` is rejected with `label expressions may have only 1 value`;
- `excludedApps` beats everything: `kube-public`, labelled to match an active policy,
  still received no restore point and has no `Application` object at all.

Not exercised: VM-selector resolution (the warning path is reasoned, not run) and
`NotIn` subtraction on `appNamespace`.
