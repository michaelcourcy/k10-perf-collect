# 01 — Namespaces in scope

## What Global Engineering needs

The set of namespaces this performance audit covers, with the export cadence and the
Location Profile for each. Every per-namespace figure in guides 03–06, 11 and 12 is
scoped off this list.

## The scope rule

**A namespace is in scope if a policy with an export action selects it.** That is the
whole rule.

Performance analysis is about where the datamover runs. An export is what creates a
Kopia repository, consumes object storage, moves bytes off-cluster and takes a
measurable amount of time. A backup-only policy produces local snapshots: no datamover
pod, no repository, no object count, no change rate, no export duration. There is
nothing to measure, so those namespaces are out of scope.

This is **not** coverage analysis. Whether a backup-only namespace is adequately
protected is a real and important question, but a different one — and one that needs
graded coverage levels and VM-selector handling that this guide deliberately does not
attempt.

## Method

```bash
mkdir -p "$AUDIT_DIR/01-scope" && cd "$AUDIT_DIR/01-scope"
```

### Step 1 — which policies export

```bash
export_policies | tee export-policies.tsv | column -t
```

```
POLICY            SNAP_FREQ  EXPORT_FREQ  PAUSED  PROFILE       EXPORTS_DATA
basic-app-backup  @onDemand  @onDemand    false   my-s3-bucket  true
clusters-backup   @hourly    @hourly      true    my-s3-bucket  true
```

`EXPORT_FREQ` is the number that matters, and it is frequently **not** `SNAP_FREQ`: a
policy can snapshot hourly and export on demand, in which case nothing leaves the
cluster on a schedule. `EXPORTS_DATA` distinguishes a real data export from a
metadata-only one.

### Step 2 — the namespaces those policies select

```bash
audit_scope            | tee audit-scope.tsv | column -t
audit_scope_namespaces | tee audit-scope-namespaces.txt
```

```
NAMESPACE  POLICY            SNAP_FREQ  EXPORT_FREQ  PAUSED  PROFILE
basic-app  basic-app-backup  @onDemand  @onDemand    false   my-s3-bucket
clusters   clusters-backup   @hourly    @hourly      true    my-s3-bucket
```

`audit-scope-namespaces.txt` is the flat list guides 03–06, 11 and 12 read. Selector
forms — literal names, globs, labels, exclusions — are resolved for you; the mechanics
are documented in [../lib/policies.sh](../lib/policies.sh) if you need to verify them.

### Step 3 — read the warnings

```bash
policy_selector_warnings | tee scope-warnings.tsv | column -t -s "$(printf '\t')"
```

Each one means "this policy will not give you performance data":

```
EXPORT IS @onDemand   basic-app-backup  exports only when triggered by hand
EXPORT POLICY PAUSED  clusters-backup   nothing runs, so no current performance data
```

If every export policy is paused or on-demand, there is no historical export activity to
analyse and guides 06, 09 and 13 will need a triggered run (guide 13 step 7).

### Step 4 — cross-check against exports that really happened

Step 2 is intent. This is evidence: a namespace in scope with no exported restore point
has never actually exported, so there is nothing to measure yet.

```bash
kubectl get restorepoints.apps.kio.kasten.io -A \
  -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,CREATED:.metadata.creationTimestamp' \
  > restorepoints.txt

comm -23 audit-scope-namespaces.txt \
     <(awk 'NR>1{print $1}' restorepoints.txt | sort -u) \
  | tee in-scope-never-exported.txt
```

Guide 12's repository inventory is the stronger check — a namespace with a Kopia
repository has definitely exported.

### Step 5 — record the exclusion list

```bash
k10_excluded_namespaces | tee excluded-namespaces.txt
```

## Caveats

- **An export policy selecting `*` puts the whole cluster in scope.** Check the row count
  before sizing anything from a per-namespace figure.
- **The list is a snapshot.** A selector is evaluated live, so a namespace created
  tomorrow can enter scope. `00-prerequisites.txt` records when the list was taken.
- **A paused export policy still resolves to namespaces.** They are in scope on paper
  and produce no data at all. Read the `PAUSED` column.
- **VM selectors are not resolved.** A policy selecting KubeVirt VMs by
  `virtualMachineRef` / `virtualMachineNamespace` is flagged
  `VM SELECTOR - RESOLVE BY HAND` rather than silently dropped. Resolve those namespaces
  manually and append them to `audit-scope-namespaces.txt`.
- **Multi-cluster**: policies may be pushed from a primary — check
  `kubectl -n "$K10NS" get distributions.dist.kio.kasten.io`. The local `Policy` objects
  are then copies and the primary is the source of truth.

## What to send back

| File | Contents |
|------|----------|
| `audit-scope.tsv` | namespace → policy → export cadence → profile (headline) |
| `audit-scope-namespaces.txt` | flat list, the input to guides 03–06, 11 and 12 |
| `export-policies.tsv` | every exporting policy with its cadence and profile |
| `scope-warnings.tsv` | policies that will yield no performance data |
| `in-scope-never-exported.txt` | in scope but no restore point yet |
| `excluded-namespaces.txt` | namespaces K10 cannot see at all |

## Validation status

Fully validated on K10 9.0.5. The scope rule reduced a 104-namespace cluster to **2**
in-scope namespaces — the two whose policies carry an export action. The other two
policies on that cluster are backup-only and were correctly excluded.

Selector resolution was validated by creating probe policies and throwaway namespaces,
then **running** the policies and checking where restore points appeared:

- A `basic-app*` policy produced restore points in `basic-app`, `basic-app1` and
  `basic-app2` — the glob matches the prefix itself as well as its extensions.
- A literal `cpd` selector matched `cpd` alone, leaving `cpd-operators` and `cpdbr` out.
- A `matchLabels` policy matched on **namespace** labels. K10 restricts these more than
  Kubernetes does: a label `matchExpression` may carry only one value, so
  `tier In (gold, silver)` is rejected with `label expressions may have only 1 value`.
- `excludedApps` beats everything: `kube-public`, labelled to match an active policy,
  still received no restore point and has no `Application` object at all.
- Accepted selector syntax was determined by submitting each form and reading
  `status.validation`; the rejection message carries K10's own regex, which
  `lib/policies.sh` uses verbatim.

Not exercised: VM-selector resolution (no VM policy existed, so the warning path is
reasoned rather than run) and `NotIn` subtraction on `appNamespace`.
