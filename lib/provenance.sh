# provenance.sh - permission pre-flight and the audit provenance record.
# Sourced by init.sh.

# can <verb> <resource> [flags...]
# Arguments go through "$@" rather than word-splitting an unquoted variable, because
# zsh does not split unquoted parameter expansions.
can() {
  _can_r=$(kubectl auth can-i "$@" 2>/dev/null | tail -1)
  printf '%-58s %s\n' "$*" "${_can_r:-ERROR (cannot even ask)}"
}

# audit_preflight   check every permission the guides need
audit_preflight() {
  echo "--- cluster-scoped reads ---"
  can get nodes
  can get namespaces
  can get persistentvolumes
  can get storageclasses
  echo "--- node sub-resource: kubelet stats, guides 04 and 08 ---"
  can get nodes --subresource=proxy
  echo "--- namespaced reads ---"
  can get pods --all-namespaces
  can get persistentvolumeclaims --all-namespaces
  can get configmaps -n "${K10NS:-kasten-io}"
  can get secrets -n "${K10NS:-kasten-io}"
  echo "--- K10 aggregated APIs: guides 01, 02, 12, 13 ---"
  can get policies.config.kio.kasten.io -n "${K10NS:-kasten-io}"
  can get exportactions.actions.kio.kasten.io -n "${K10NS:-kasten-io}"
  can get restorepoints.apps.kio.kasten.io --all-namespaces
  echo "--- writes the guides need: 04, 11, 12, 13 ---"
  can create pods -n "${K10NS:-kasten-io}"
  can create pods/exec -n "${K10NS:-kasten-io}"
  can create pods/portforward -n "${K10NS:-kasten-io}"
  can create runactions.actions.kio.kasten.io -n "${K10NS:-kasten-io}"
  echo "--- metrics access ---"
  can create serviceaccounts/token -n "${PROM_NS:-openshift-monitoring}"
}

# audit_record   write $AUDIT_DIR/00-prerequisites.txt and 00-audit-env.sh
# No credentials are recorded, by design.
audit_record() {
  _audit_ready audit_record || return 1
  if [ -z "${AUDIT_DIR:-}" ]; then
    echo "audit_record: AUDIT_DIR is not set - source lib/init.sh first" >&2
    return 1
  fi
  _r_out="$AUDIT_DIR/00-prerequisites.txt"
  _r_env="$AUDIT_DIR/00-audit-env.sh"

  # 00-prerequisites.txt points at metrics-window.txt, so produce it here rather than
  # relying on the reader having run audit_window_report by hand.
  if command -v audit_window_report >/dev/null 2>&1; then
    audit_window_report >/dev/null 2>&1
  fi

  {
    echo "K10 PERFORMANCE AUDIT - PROVENANCE RECORD"
    echo "========================================="
    echo "generated_utc        : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "collected_by         : $(kubectl auth whoami -o jsonpath='{.status.userInfo.username}' 2>/dev/null || echo unknown)"
    echo "kube_context         : $(kubectl config current-context 2>/dev/null)"
    echo
    echo "-- cluster --------------------------------------------------------"
    echo "cluster_uid          : ${CLUSTER_UID:-UNRESOLVED}"
    echo "kubernetes_version   : $(kubectl version -o json 2>/dev/null | jq -r '.serverVersion.gitVersion')"
    echo "openshift_version    : $(kubectl get clusterversion version -o jsonpath='{.status.desired.version}' 2>/dev/null || echo 'n/a')"
    echo "node_count           : $(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo "namespace_count      : $(kubectl get ns --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo "pvc_count            : $(kubectl get pvc -A --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo
    echo "-- K10 ------------------------------------------------------------"
    echo "k10_namespace        : ${K10NS:-kasten-io}"
    echo "k10_version          : ${K10_VERSION:-$(kubectl -n "${K10NS:-kasten-io}" get cm k10-config -o jsonpath='{.data.version}' 2>/dev/null)}"
    echo "install_method       : helm=$(helm -n "${K10NS:-kasten-io}" list -o json 2>/dev/null | jq -r '.[0].chart // "none"') operator_cr=$(kubectl -n "${K10NS:-kasten-io}" get k10s.apik10.kasten.io -o name 2>/dev/null | head -1 | sed 's|.*/||')"
    echo "policy_count         : $(kubectl -n "${K10NS:-kasten-io}" get policies.config.kio.kasten.io --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo "profile_count        : $(kubectl -n "${K10NS:-kasten-io}" get profiles --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo
    echo "-- audit focus ----------------------------------------------------"
    echo "AUDIT_NS             : ${AUDIT_NS:-NOT SET}"
    echo "AUDIT_POLICY         : ${AUDIT_POLICY:-NOT SET}"
    echo "AUDIT_PROFILE        : ${AUDIT_PROFILE:-NOT SET}"
    echo "(guides 01-13 collect this pair only; see guide 00 section 10)"
    echo
    echo "-- metrics window -------------------------------------------------"
    echo "AUDIT_WINDOW_DAYS    : ${AUDIT_WINDOW_DAYS:-NOT SET}"
    echo "AUDIT_RANGE          : ${AUDIT_RANGE:-NOT SET}"
    echo "AUDIT_START          : ${AUDIT_START:-NOT SET}"
    echo "AUDIT_END            : ${AUDIT_END:-NOT SET}"
    echo "window_basis         : ${AUDIT_WINDOW_BASIS:-NOT SET}"
    echo "prometheus_url       : ${PROM_URL:-not configured}"
    echo "prometheus_flavour   : ${PROM_URL_IS_CUSTOM:+custom}${PROM_URL_IS_CUSTOM:-openshift-thanos}"
    echo "(window detail: metrics-window.txt)"
    echo
    echo "-- tooling --------------------------------------------------------"
    echo "kubectl_client       : $(kubectl version -o json 2>/dev/null | jq -r '.clientVersion.gitVersion')"
    echo "jq                   : $(jq --version 2>/dev/null)"
    echo "helm                 : $(helm version --short 2>/dev/null || echo 'not installed')"
    echo "shell                : ${AUDIT_SHELL:-unknown}"
    echo "platform             : $(uname -sr)"
    echo
    echo "-- permissions ----------------------------------------------------"
    audit_preflight
    echo
    echo "NOTE: no credentials are recorded in this file by design."
  } > "$_r_out"

  cat > "$_r_env" <<ENVEOF
# Source this to resume the audit with the ORIGINAL window:
#   . lib/init.sh          (init.sh picks this up automatically)
export K10NS="${K10NS:-kasten-io}"
export AUDIT_DIR="$AUDIT_DIR"
export CLUSTER_UID="${CLUSTER_UID:-}"
export AUDIT_NS="${AUDIT_NS:-}"
export AUDIT_POLICY="${AUDIT_POLICY:-}"
export AUDIT_PROFILE="${AUDIT_PROFILE:-}"
export AUDIT_WINDOW_DAYS="${AUDIT_WINDOW_DAYS:-}"
export AUDIT_RANGE="${AUDIT_RANGE:-}"
export AUDIT_START="${AUDIT_START:-}"
export AUDIT_END="${AUDIT_END:-}"
export AUDIT_WINDOW_BASIS="${AUDIT_WINDOW_BASIS:-}"
export AUDIT_WINDOW_RETENTION="${AUDIT_WINDOW_RETENTION:-}"
export AUDIT_WINDOW_PERSISTENT="${AUDIT_WINDOW_PERSISTENT:-}"
export AUDIT_WINDOW_UPTIME_DAYS="${AUDIT_WINDOW_UPTIME_DAYS:-}"
export PROM_NS="${PROM_NS:-openshift-monitoring}"
${PROM_URL_IS_CUSTOM:+export PROM_URL="$PROM_URL"}
# PROM_URL is only persisted for a non-OpenShift Prometheus. On OpenShift it is
# re-derived from the thanos-querier route, because a replayed PROM_URL would make
# prom_init skip minting a token.
# PROM_TOKEN / THANOS_TOKEN are intentionally omitted - init.sh re-mints them.
ENVEOF

  # Anything empty here breaks a downstream guide silently, so say so loudly.
  _r_missing=
  for _v in CLUSTER_UID AUDIT_WINDOW_DAYS AUDIT_RANGE AUDIT_START AUDIT_END; do
    eval "_r_val=\$$_v"
    [ -n "$_r_val" ] || _r_missing="$_r_missing $_v"
  done
  case "${CLUSTER_UID:-}" in
    ????????-????-????-????-????????????) ;;
    *) _r_missing="$_r_missing CLUSTER_UID-not-a-uuid" ;;
  esac

  echo "wrote $_r_out"
  echo "wrote $_r_env"
  if [ -n "$_r_missing" ]; then
    echo
    echo "WARNING: empty or malformed:$_r_missing" >&2
    echo "  Do NOT proceed to guides 06-08, 11 or 13 until these are set." >&2
    return 1
  fi
  return 0
}

# audit_redaction_check   confirm nothing sensitive landed in the deliverable
audit_redaction_check() {
  # Match assigned VALUES, not the mere mention of the word: 'serviceaccounts/token'
  # in a permission line is not a credential, whereas 'token: eyJ...' is.
  _rc=$(grep -rniE \
          -e '(token|password|passwd|secret[_-]?key|htpasswd|access[_-]?key)[[:space:]]*[:=][[:space:]]*[^[:space:]]' \
          -e 'Bearer[[:space:]]+[A-Za-z0-9._~/+=-]{16,}' \
          -e 'eyJ[A-Za-z0-9_-]{10,}' \
          "${AUDIT_DIR:-.}" 2>/dev/null \
        | grep -viE 'intentionally omitted|no credentials are recorded|re-mints them|<redacted>|(NOT SET|none)$')
  if [ -n "$_rc" ]; then
    echo "audit_redaction_check: POSSIBLE CREDENTIALS in the deliverable:" >&2
    printf '%s\n' "$_rc" >&2
    return 1
  fi
  echo "audit_redaction_check: OK - nothing credential-shaped in $AUDIT_DIR"
  return 0
}
