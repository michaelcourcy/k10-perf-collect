# init.sh - single entry point for the K10 performance audit guides.
#
#   SOURCE this file, do not execute it:      . lib/init.sh
#
# Safe to source repeatedly - run it at the start of every guide and at the start of
# every new terminal. It is idempotent and it re-mints the metrics token.
#
# After sourcing you have:
#
#   variables   K10NS  AUDIT_DIR  CLUSTER_UID  K10_VERSION
#               AUDIT_WINDOW_DAYS  AUDIT_RANGE  AUDIT_START  AUDIT_END
#               PROM_URL  PROM_TOKEN  (THANOS_HOST / THANOS_TOKEN as aliases)
#
#   metrics     tq  tqr  tq_scalar  prom_check  prom_token_refresh
#               k10prom_start  k10prom_stop  kq  kq_scalar  k10prom_check
#
#   pvc scan    pvc_scan_network  pvc_scan_one
#
#   kopia       kopia_inventory_pvcs  kopia_pvc_growth
#
#   scope       export_policies  audit_scope  audit_scope_namespaces
#               policy_selector_warnings  k10_excluded_namespaces
#
#   plumbing    check_auth  require_auth
#               pf_start  pf_stop  pf_stop_all
#               can  audit_preflight  audit_record  audit_redaction_check
#               audit_window  audit_window_report  audit_window_verify
#               audit_status
#
# Overrides, exported BEFORE sourcing:
#   K10NS=...        K10 namespace                     (default kasten-io)
#   AUDIT_DIR=...    output directory                  (default ./k10-audit-<ctx>-<date>)
#   PROM_URL=...     non-OpenShift Prometheus base URL (see lib/prometheus.sh)
#   PROM_NS=...      cluster monitoring namespace      (default openshift-monitoring)

# --- refuse to be executed ---------------------------------------------------
_ai_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
  case "${ZSH_EVAL_CONTEXT:-}" in *:file*) _ai_sourced=1 ;; esac
  AUDIT_SHELL="zsh $ZSH_VERSION"
  # zsh parses pasted '#' lines as commands unless this is set, which turns an
  # apostrophe in a comment into a hung quote> prompt. Fix it for this session.
  setopt interactive_comments 2>/dev/null
elif [ -n "${BASH_VERSION:-}" ]; then
  # shellcheck disable=SC2128
  [ "${BASH_SOURCE:-$0}" != "$0" ] && _ai_sourced=1
  AUDIT_SHELL="bash $BASH_VERSION"
else
  _ai_sourced=1
  AUDIT_SHELL="sh"
fi
if [ "$_ai_sourced" -ne 1 ]; then
  echo "init.sh must be SOURCED, not executed:   . lib/init.sh" >&2
  exit 1
fi
export AUDIT_SHELL

# --- locate ourselves --------------------------------------------------------
if [ -n "${ZSH_VERSION:-}" ]; then
  _ai_self=$(eval 'echo ${(%):-%x}')
elif [ -n "${BASH_VERSION:-}" ]; then
  _ai_self=$(eval 'echo ${BASH_SOURCE[0]}')
else
  _ai_self=""
fi
if [ -n "$_ai_self" ]; then
  K10_AUDIT_LIB=$(cd "$(dirname "$_ai_self")" && pwd)
else
  K10_AUDIT_LIB=${K10_AUDIT_LIB:-$PWD/lib}
fi
K10_AUDIT_ROOT=$(cd "$K10_AUDIT_LIB/.." && pwd)
export K10_AUDIT_LIB K10_AUDIT_ROOT

# Remember whether the CALLER configured a non-OpenShift Prometheus. A resumed
# session must not mistake a persisted PROM_URL for a deliberate override, or
# prom_init takes the custom branch and never mints a token.
_ai_prom_url_from_user=${PROM_URL:-}

# --- prerequisites -----------------------------------------------------------
_ai_missing=
for _t in kubectl jq curl awk sed; do
  command -v "$_t" >/dev/null 2>&1 || _ai_missing="$_ai_missing $_t"
done
if [ -n "$_ai_missing" ]; then
  echo "init.sh: missing required tool(s):$_ai_missing" >&2
  return 1
fi
command -v helm >/dev/null 2>&1 || echo "init.sh: note - helm not found, guides 10 and 12 need it" >&2

if ! kubectl version -o json >/dev/null 2>&1; then
  echo "init.sh: cannot reach the cluster - check your kubeconfig / login" >&2
  return 1
fi

# --- namespace and K10 version ----------------------------------------------
export K10NS="${K10NS:-kasten-io}"
K10_VERSION=$(kubectl -n "$K10NS" get cm k10-config -o jsonpath='{.data.version}' 2>/dev/null)
if [ -z "$K10_VERSION" ]; then
  echo "init.sh: no k10-config in namespace $K10NS - set K10NS and source again" >&2
else
  export K10_VERSION
fi

# --- libraries ---------------------------------------------------------------
. "$K10_AUDIT_LIB/portforward.sh"
. "$K10_AUDIT_LIB/prometheus.sh"
. "$K10_AUDIT_LIB/window.sh"
. "$K10_AUDIT_LIB/provenance.sh"
. "$K10_AUDIT_LIB/check_auth.sh"
. "$K10_AUDIT_LIB/policies.sh"
. "$K10_AUDIT_LIB/pvcscan.sh"
. "$K10_AUDIT_LIB/kopia.sh"

# --- authorisation gate ------------------------------------------------------
# Every permission the guides use is mandatory. A partial audit produces misleading
# recommendations, so refuse to start rather than fail silently halfway through.
if ! require_auth; then
  AUDIT_READY=no
  export AUDIT_READY
  return 1
fi

# --- cluster uid: the UID of the default namespace, which cannot be deleted --
CLUSTER_UID=${CLUSTER_UID:-$(kubectl get ns default -o jsonpath='{.metadata.uid}' 2>/dev/null)}
export CLUSTER_UID

# --- output directory, reusing an existing audit if there is one -------------
if [ -z "${AUDIT_DIR:-}" ]; then
  _ai_existing=$(find "$K10_AUDIT_ROOT" -maxdepth 2 -name '00-audit-env.sh' 2>/dev/null | head -2)
  _ai_count=$(printf '%s\n' "$_ai_existing" | grep -c . )
  if [ "$_ai_count" -eq 1 ]; then
    AUDIT_DIR=$(dirname "$_ai_existing")
  elif [ "$_ai_count" -gt 1 ]; then
    echo "init.sh: several existing audits found - export AUDIT_DIR to pick one:" >&2
    find "$K10_AUDIT_ROOT" -maxdepth 2 -name '00-audit-env.sh' 2>/dev/null | sed 's|/00-audit-env.sh||; s|^|  |' >&2
    return 1
  else
    _ai_ctx=$(kubectl config current-context 2>/dev/null | tr '/:' '__')
    AUDIT_DIR="$K10_AUDIT_ROOT/k10-audit-${_ai_ctx}-$(date -u +%Y%m%d)"
  fi
fi
mkdir -p "$AUDIT_DIR" || return 1
export AUDIT_DIR

# --- resume a previous session, so the window stays comparable ---------------
# Sourced AFTER the libraries so the pinned AUDIT_* values win over fresh ones.
if [ -f "$AUDIT_DIR/00-audit-env.sh" ]; then
  . "$AUDIT_DIR/00-audit-env.sh"
  AUDIT_RESUMED=yes
else
  AUDIT_RESUMED=no
fi
# The caller's choice wins over anything the env file replayed.
PROM_URL=$_ai_prom_url_from_user
unset PROM_URL_IS_CUSTOM
[ -n "$PROM_URL" ] && export PROM_URL || unset PROM_URL

# --- metrics -----------------------------------------------------------------
if prom_init; then
  PROM_READY=yes
else
  PROM_READY=no
  echo "init.sh: cluster monitoring not configured - guides 04, 06, 07, 08, 09 are limited" >&2
fi

# Derive the window only if a resumed session did not already pin it.
if [ -z "${AUDIT_WINDOW_DAYS:-}" ] && [ "$PROM_READY" = yes ]; then
  audit_window
fi

# --- status ------------------------------------------------------------------
audit_status() {
  echo "K10 audit session"
  echo "  shell          : ${AUDIT_SHELL}"
  echo "  kube context   : $(kubectl config current-context 2>/dev/null)"
  echo "  K10            : ${K10_VERSION:-unknown} in ${K10NS}"
  echo "  cluster uid    : ${CLUSTER_UID:-UNRESOLVED}"
  echo "  audit dir      : ${AUDIT_DIR}"
  echo "  resumed        : ${AUDIT_RESUMED}"
  echo "  prometheus     : ${PROM_URL:-none} (${PROM_URL_IS_CUSTOM:+custom}${PROM_URL_IS_CUSTOM:-openshift})"
  echo "  window         : ${AUDIT_WINDOW_DAYS:-?}d  [${AUDIT_START:-?} .. ${AUDIT_END:-?}]"
  echo "  window basis   : ${AUDIT_WINDOW_BASIS:-?}"
}

AUDIT_READY=yes
export AUDIT_READY

audit_status
