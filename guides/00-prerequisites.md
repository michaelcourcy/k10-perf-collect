# 00 — Prerequisites and shared setup

Run this once per shell session. Every other guide assumes the environment and
helper functions defined here.

## 0. Order of operations

Three phases, in this order. Getting it wrong is the one thing that makes this guide
confusing.

| | Phase | What |
|---|---|---|
| 1 | **Override** | export anything non-default — `K10NS`, `AUDIT_DIR`, `PROM_URL`, `PROM_NS` |
| 2 | **Source** | `. lib/init.sh` — does all the setup and prints `audit_status` |
| 3 | **Verify + record** | the remaining sections below are checks, not setup |

```bash
# 1. overrides, only if you need them
export K10NS=kasten-io            # default
# export PROM_URL=http://localhost:19091   # non-OpenShift Prometheus, see section 5

# 2. the one line that sets everything up. Do NOT pipe it - see the note below.
. lib/init.sh

# 3. verify and record
prom_check
audit_preflight
audit_window_report
audit_record
audit_redaction_check
```

Everything from §2 onwards is inspection: confirming what `init.sh` found and writing
it down. You do not need to re-export anything it already set.

Two ways to get this wrong:

- **Executing instead of sourcing.** `./lib/init.sh` or `sh lib/init.sh` sets everything
  in a child process that then exits. It detects this and refuses.
- **Piping the source command.** `. lib/init.sh | tee log` runs the whole thing in a
  subshell, so the variables and functions never reach your shell — you get
  `prom_check: command not found` a minute later with no other clue. Redirect instead:
  `. lib/init.sh > init.log 2>&1`.

## 1. Tooling

| Tool | Minimum | Used by |
|------|---------|---------|
| `kubectl` or `oc` | matching cluster minor version | all |
| `jq` | 1.6 | all |
| `bash` | 4.x (or zsh) | all |
| `helm` | 3.x | guides 10, 12 |
| `curl` | any | 04, 06, 07, 08, 09, 11 |

> **macOS note**: the stock BSD `awk` has no `strftime`. Every guide here does time
> bucketing in `jq` instead, so no GNU coreutils are required.

## 1b. Before you paste anything — zsh users

Run this **once per terminal**, before pasting any block from these guides:

```bash
[ -n "$ZSH_VERSION" ] && setopt interactive_comments
```

zsh ships with `INTERACTIVE_COMMENTS` **off**, so a pasted `#` line is executed as a
command rather than ignored. The failures are confusing and look like typos in the
guide:

| Pasted comment contains | zsh does this |
|---|---|
| an apostrophe — `# kubectl's own line` | hangs on a `quote>` prompt: `zsh: unmatched '` |
| `<` or `>` — `# Usage: pf_start <service>` | `zsh: parse error near '<'` |
| `\|`, `&`, `(`, `)` | runs a pipeline, backgrounds a job, or another parse error |

bash has the option on by default, which is why the guides were written without
noticing. The line above is harmless in bash and sh.

Blocks written as `cat > file <<'SH' ... SH` are immune either way — the heredoc body is
literal text, never parsed. That is also why everything reusable lives in `lib/` as a
sourced file rather than as a block you paste.

## 2. Identify the cluster and K10 build

`init.sh` already set `K10NS` and `K10_VERSION`. These commands confirm them:

```bash
kubectl config current-context
echo "$K10NS -> K10 $K10_VERSION"
kubectl get nodes -o wide
```

If K10 lives somewhere other than `kasten-io`, that is a phase-1 override:
`export K10NS=my-ns` **before** sourcing `init.sh`.

The `k10-config` `version` key is the authoritative K10 version. Do **not** rely on
the operator CR `.status.version` — it is empty on Helm-based installs.

Record the cluster UID too; it prefixes every object-storage path (guide 11) and labels
every series in K10's Prometheus. K10 derives it from the UID of the **`default`
namespace**, which makes it trivially stable — `default` cannot be deleted, so the UID
lives as long as the cluster does:

```bash
echo "$CLUSTER_UID"        # set by init.sh
```

## 3. Output directory

```bash
echo "$AUDIT_DIR"          # created by init.sh
```

`init.sh` names it `k10-audit-<context>-<date>`. If an audit directory already exists it
**reuses** it and replays `00-audit-env.sh`, so the metrics window stays pinned across
sessions. To place it elsewhere, `export AUDIT_DIR=...` in phase 1.

Each guide writes into `$AUDIT_DIR/<NN>-<name>/`.

## 4. K10's own Prometheus

K10 ships an embedded Prometheus. It only scrapes K10's own services — it has **no
cAdvisor and no kube-state-metrics** — so it answers "what did K10 do" but never
"what did the node do".

The API is served under the `/k10/prometheus` prefix, not at the root:

```bash
k10prom_start   # port-forward to K10 Prometheus, sets $K10PROM
k10prom_check   # prints the Prometheus version, or explains what broke
kq '<promql>'    # instant query against it
k10prom_stop    # tear the forward down
```

`pf_start` / `pf_stop` / `pf_stop_all` in `lib/portforward.sh` are the generic form,
used by guides 02, 09 and 13. They capture the PID rather than relying on `%1`,
refuse to start on an occupied port, and print kubectl's log if the forward dies.

Stop it with `pf_stop` when done.

`pf_start` / `pf_stop` are reused by guides 02, 09 and 13 — keep them in the session, or
put them in a file and source it.

**Why not `... &` followed by `kill %1`?** Because `%1` is positional and silently
wrong as soon as anything else is backgrounded, and because a `kill %1` answered with
`no such job` means the forward had *already died* — with the reason sitting unread in a
log file under `/tmp`. `pf_start` refuses to start on an occupied port (otherwise the
readiness probe passes against the wrong process and every later query reads someone
else's service), waits for kubectl's own `Forwarding from` line, and prints the log if
it fails.

## 5. OpenShift cluster monitoring (Thanos Querier)

This is where cAdvisor, kubelet and kube-state-metrics data live. Needed for guides
04, 06, 07, 08, 09.

`init.sh` has already resolved the route, minted a token and defined the helpers:

```bash
prom_check                       # the one command that proves the whole path works
tq_scalar 'count(kube_node_info)'
tqr 'up' "$AUDIT_START" "$AUDIT_END" 1h | jq -r '.data.result | length'
```

`prom_token_refresh` re-mints the token if queries start returning `Unauthorized`; it
expires after 6 h.

### What `prom_check` actually asks, and why it is the right first query

`prom_check` runs `count(container_cpu_usage_seconds_total{namespace=$K10NS})`. It is a
connectivity test, not a measurement. One query proves three things at once, which is
exactly what you need before trusting anything in guides 04 and 06–09:

1. the Thanos route is reachable;
2. the bearer token is accepted;
3. **cAdvisor is actually scraping the K10 namespace** — not merely that a monitoring
   stack exists somewhere.

The token is minted from the `prometheus-k8s` service account in
`openshift-monitoring`, which already has cluster-wide metrics read access. It
expires after 6 h; re-run the `create token` line to refresh.

**If cluster monitoring is not available**, guides 04, 07 and 08 fall back to the
kubelet summary API (documented in each guide) and guide 09 falls back to K10's
worker-pod metric sidecar. Guide 06 does not depend on Prometheus at all.

### Non-OpenShift clusters

Replace the route lookup with a port-forward to whatever Prometheus scrapes cAdvisor,
and drop the bearer token:

Do not edit `lib/prometheus.sh`. Export `PROM_URL` in phase 1 and `init.sh` skips the
OpenShift path entirely:

```bash
kubectl -n monitoring port-forward svc/prometheus-operated 19091:9090 &
export PROM_URL=http://localhost:19091
. lib/init.sh
```

For a token or a tenant header, also export `PROM_TOKEN` / `PROM_HEADER`. The header of
[../lib/prometheus.sh](../lib/prometheus.sh) documents all three variants.

## 6. Effective metrics window — do this before anything else

Several guides look backwards in time. **Do not assume the declared retention is the
window you actually have.** Two things shorten it:

1. `--storage.tsdb.retention.time` (15 d by default on OpenShift) caps it from above.
2. If Prometheus has **no persistent volume**, its TSDB lives in an `emptyDir` and is
   destroyed with the pod. The real window is then bounded by how long the
   longest-running replica has been up.

### Inspect and confirm the window

`init.sh` already derived the window — `audit_status` printed it. These are the
inspection commands:

```bash
audit_window_verify   # cross-check the derivation against the oldest real sample
audit_window_report   # the summary, also written by audit_record in section 9
```

`audit_window` itself only needs calling by hand if you deliberately want to re-derive
the window mid-audit, which will move `AUDIT_END` and make earlier figures
non-comparable. The logic lives in [../lib/window.sh](../lib/window.sh).

Validated output on the validation cluster:

```
retention_declared        = 15d (15 d)
persistent_storage        = no
replica_restarts_total    = 0
longest_replica_uptime_d  = 95.20
window_basis              = min(retention, longest-running replica uptime) - storage is emptyDir

AUDIT_WINDOW_DAYS = 15
AUDIT_RANGE       = 15d

POD               POD_START             UPTIME_D  RESTARTS
prometheus-k8s-0  2026-06-12T12:27:43Z  95.2      0
prometheus-k8s-1  2026-06-29T09:46:36Z  78.31     0
```

### Cross-check against reality

The derivation is cheap; confirm it against the oldest sample that actually exists:

```bash
audit_window_verify
```

Validated: measured 15.25 d against a derived 15 d — consistent. If the measured value
is materially shorter, override `AUDIT_WINDOW_DAYS` by hand and say so in the report.

### K10 action history retention

Separate mechanism, separate limit — it governs guides 02 and 13:

```bash
kubectl -n "$K10NS" get cm k10-config -o json \
  | jq -r '.data | {K10GCActionsEnabled, K10GCKeepMaxActions, K10GCDaemonPeriod}'
```

Validated: `K10GCActionsEnabled=false`, `K10GCKeepMaxActions=1000`,
`K10GCDaemonPeriod=21600`. With GC disabled, all action history is retained.

### Record the window in the report

`AUDIT_WINDOW_DAYS` is not a detail — it is the confidence interval on every trend in
guides 06, 07, 08 and 13. Put `metrics-window.txt` in the deliverable and state the
window next to each trend figure.

## 7. Redaction — read before sending anything

Several extracts contain secrets. **Redact before sharing**:

| Source | What leaks |
|--------|-----------|
| `helm get values k10` | `genericStorageBackup.token`, `auth.basicAuth.htpasswd`, license tokens |
| `kubectl get k10s.apik10.kasten.io -o yaml` | same as above |
| `kubectl get profile -o yaml` | object-store endpoint, bucket, region (credentials are in a separate Secret) |
| `repo-checker` diagnose bundle | `repository-status-stdout.txt` contains the bucket, prefix and **access key ID** (the secret key is masked) |
| Any `kubectl get secret` | everything |

Guide 10 includes a ready-made redaction filter. Never run
`kubectl get secret -o yaml` as part of this collection — no guide needs it.

## 8. Authorisation — all of it, up front

`lib/init.sh` **refuses to start the audit** unless every permission below is granted.
There is no partial mode: each one is required by at least one guide, and a missing one
means an extract silently cannot be produced. A partial audit produces misleading
recommendations, which is worse than no audit.

The gate runs automatically when you source `init.sh`. To see the full table at any
time, granted and denied:

```bash
check_auth
```

Denials are printed with the guides they block:

```
get nodes --subresource=proxy    NO    guide 00 04 08 09 : kubelet stats and per-pod ephemeral storage
```

and `init.sh` aborts with `AUDIT NOT STARTED - <n> required permission(s) missing`.
`AUDIT_READY` stays `no` and the helpers (`tq`, `pf_start`, `audit_record`, …) refuse to
run, so a script cannot blunder past the gate either.

### What is required, and why

Grouped by what the guides do with it. The authoritative list is the `_auth_rows`
function in [../lib/check_auth.sh](../lib/check_auth.sh) — it is data, not prose, so it
cannot drift from what is actually checked.

| Capability | Resources | Why it cannot be dropped |
|---|---|---|
| **Read the cluster** | `nodes`, `namespaces`, `persistentvolumes`, `persistentvolumeclaims`, `pods`, `storageclasses`, `volumesnapshotclasses` | the inventory the whole audit is built on |
| **Read kubelet stats** | `nodes/proxy` | the *only* source of per-pod ephemeral-storage and per-volume inode data. Not in most read-only roles, and guides 04 and 08 cannot be produced without it |
| **Read the K10 API** | `policies`, `profiles`, `actionpodspecs`, `k10s`, `exportactions`, `restorepoints`, `applications`, `storagerepositories` | policies, schedules, restore points and repositories. These are aggregated APIs, not CRDs, so a generic CRD read role does not cover them |
| **Read K10 config** | `configmaps`, `deployments`, `secrets` **in the K10 namespace only** | `k10-config` holds the effective tuning; the Helm release lives in a Secret, so `helm get values` needs Secret read; the object-store credential is consumed by the object-count pod |
| **Read K10 logs** | `pods/log`, `events` | guide 13 reads orchestration logs and worker-pod warnings |
| **Port-forward** | `pods/portforward` in the K10 namespace | K10's own Prometheus, `metering-svc` and `jobs-svc` have no route |
| **Create and delete pods** | `pods`, `pods/exec` cluster-wide | `repo-checker` and the object-count pod in the K10 namespace; read-only PVC inspector pods in application namespaces. `delete` matters as much as `create` — it is how the audit cleans up |
| **Create and delete policies** | `policies`, `runactions` in the K10 namespace | guides 09 and 13 must watch a real datamover run; the pods exist only while a job runs |
| **Mint a metrics token** | `serviceaccounts/token`, `routes`, `prometheuses`, `pods` in the monitoring namespace | cluster monitoring access, plus Prometheus retention and replica uptime for the window derivation in §6 |

Secret read is scoped to the K10 namespace. **No guide reads secrets from application
namespaces**, and none reads application data — the audit counts and measures files, it
does not open them.

### What the audit creates, and how to verify it is gone

Only guides 04, 05, 09, 11, 12 and 13 create anything. Everything is labelled:

```bash
kubectl get pods -A -l app.kubernetes.io/name=k10-audit-inspector
kubectl -n "$K10NS" get pods -l app.kubernetes.io/name=k10-audit-objcount
kubectl -n "$K10NS" get policies -l app.kubernetes.io/managed-by=k10-performance-audit
kubectl -n "$K10NS" get pods | grep -E 'k10tools|debug-kopia'
```

All four must return nothing at the end of a session. Note that `repo-checker` does
**not** always clean up its `debug-kopia-*` pod — guide 12 covers this explicitly.

Inspector pods mount their PVC `readOnly: true`, run as non-root with all capabilities
dropped, and are compatible with OpenShift's `restricted-v2` SCC — no privileged SCC is
needed.

### Data handling

- Extracts containing the object-store endpoint, bucket, prefix or access key ID are
  redacted before leaving the cluster (§7, and guide 10 step 0).
- The provenance record (§9) contains no credentials, and `audit_redaction_check`
  verifies that.
- The metrics token lives in a mode-0600 `curl` config file, never in a command line, so
  it does not appear in `ps` output.

### Two constraints worth agreeing before you start

- **RWO volumes already attached elsewhere** cannot be mounted by an inspector pod.
  Unattached RWO volumes are fine, which is the set guides 04 and 05 care about.
- **Guides 09 and 13 run backup policies.** Agree a window, and agree whether the
  restore points they produce are kept or retired afterwards.

## 9. Record the provenance — the deliverable for this guide

Every other guide produces files. This one records *under what conditions everything
else was collected*, which is what makes the rest of the audit readable weeks later.

```bash
audit_record            # writes 00-prerequisites.txt and 00-audit-env.sh
audit_redaction_check   # confirms no credentials landed in the deliverable
```

Both functions come from `lib/provenance.sh`, sourced by `lib/init.sh`.

Sample of the result:

```
generated_utc        : 2026-09-15T20:25:51Z
collected_by         : <your user>
cluster_uid          : 0f7c6f0e-1a2b-4c3d-9e8f-0123456789ab
kubernetes_version   : v1.31.6
openshift_version    : 4.18.6
node_count           : 9
pvc_count            : 70
k10_version          : 9.0.5
install_method       : helm=k10-9.0.5 operator_cr=k10
AUDIT_WINDOW_DAYS    : 15
kubectl_client       : v1.34.2
get nodes --subresource=proxy                  : yes
```

### Resuming a multi-day audit

The script also writes `00-audit-env.sh`. Source it at the start of every later session:

```bash
. "$AUDIT_DIR/00-audit-env.sh"
```

This is a correctness matter, not convenience. Re-deriving the window on day 3
produces a new `AUDIT_END`, so guides 06–08 and 13 would measure a different window than
the ones run on day 1 and the figures would stop being comparable. Sourcing the saved
env pins the window. `THANOS_TOKEN` is deliberately not saved — re-mint it per §5.

### No credentials in either file

Both are safe to ship as-is. Verify with:

```bash
audit_redaction_check
```

It scans the whole of `$AUDIT_DIR`, not just these two files, and is the **only**
implementation of this check — do not hand-roll a `grep` beside it. It matches assigned
*values* (`token: eyJ...`, `Bearer sha256~...`, `aws_secret_access_key = ...`) rather
than the mere appearance of the word, because a permission line such as
`create serviceaccounts/token -n openshift-monitoring   yes` is not a credential. The
obvious word-matching grep flags that line and the explanatory comment in
`00-audit-env.sh`, giving two false positives on a clean deliverable — which is how a
check like this ends up being ignored. The patterns live in
[../lib/provenance.sh](../lib/provenance.sh) and are tested against JWTs, OpenShift
`sha256~` tokens, AWS secret keys, htpasswd hashes and bare passwords.

## What to send back

| File | Contents |
|------|----------|
| `00-prerequisites.txt` | cluster, K10 version and install method, metrics window, tooling, permissions |
| `00-audit-env.sh` | re-sourceable environment pinning the audit window across sessions |
| `metrics-window.txt` | the §6 window derivation, including replica uptimes |
