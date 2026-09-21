# focus.sh - pin the audit to ONE namespace and ONE export policy. Sourced by init.sh.
#
# Guides 01-13 collect, by hand, what generate-export-topology.py collects for a whole
# cluster. By hand that is only tractable for one export problem at a time, so every
# guide reads the same pair from the environment:
#
#   AUDIT_NS       the application namespace
#   AUDIT_POLICY   the export policy that selects it
#   AUDIT_PROFILE  the Location Profile that policy exports to
#
# The same pair is what the generator takes as --namespace / --policy, so a focused
# run and a hand-run guide produce comparable figures.
#
# A pair, not a namespace: one namespace can be exported by several policies into
# several profiles, and each pair has its OWN Kopia repository. Mixing them is how a
# change rate ends up attributed to the wrong schedule.
#
#   audit_focus_candidates       every (namespace, policy) pair the scope rule yields
#   audit_focus <ns> <policy>    validate the pair and export the three variables
#   audit_focus_status           what is currently pinned
#   focus_dir <subdir>           mkdir -p and cd into this guide's output directory

# audit_focus_candidates   the pairs you may pick from, with the evidence to pick one.
#
# RESTORE_POINTS is the pre-filter: a namespace with none was never backed up, so it
# was never exported and there is no repository to connect to. A VM policy selecting
# "*" expands to every namespace on the cluster and this column is what cuts it back
# down to the ones worth a connect.
audit_focus_candidates() {
  _afc_rp=$(kubectl get restorepoints.apps.kio.kasten.io -A -o json 2>/dev/null \
            | jq -c '[.items[].metadata.namespace] | group_by(.)
                     | map({key: .[0], value: length}) | from_entries')
  [ -z "$_afc_rp" ] && _afc_rp='{}'
  _scope_json | jq -r --argjson rp "$_afc_rp" --arg k10ns "${K10NS:-kasten-io}" '
    (["NAMESPACE","POLICY","PROFILE","EXPORT_FREQ","PAUSED","RESTORE_POINTS"] | @tsv),
    ( map(select(.namespace != null and .exports_data and .namespace != $k10ns))
      | unique_by([.namespace, .policy])
      | sort_by(.namespace, .policy) | .[]
      | [ .namespace, .policy, .profile, .export_freq,
          (.paused | tostring), (($rp[.namespace] // 0) | tostring) ] | @tsv )'
}

# audit_focus <namespace> <policy>
audit_focus() {
  if [ $# -lt 2 ]; then
    echo "usage: audit_focus <namespace> <policy>" >&2
    echo >&2
    echo "candidate pairs on this cluster:" >&2
    audit_focus_candidates >&2
    return 1
  fi
  _af_ns=$1
  _af_pol=$2

  if ! kubectl get ns "$_af_ns" >/dev/null 2>&1; then
    echo "audit_focus: no namespace $_af_ns on this cluster" >&2
    return 1
  fi

  _af_pj=$(kubectl -n "${K10NS:-kasten-io}" get policies.config.kio.kasten.io "$_af_pol" \
             -o json 2>/dev/null)
  if [ -z "$_af_pj" ]; then
    echo "audit_focus: no policy $_af_pol in ${K10NS:-kasten-io}" >&2
    return 1
  fi

  # The scope rule: only an EXPORT action creates a repository, a datamover pod and a
  # measurable duration. A backup-only policy has nothing for this audit to measure.
  _af_prof=$(printf '%s' "$_af_pj" | jq -r '
    [.spec.actions[]? | select(.action == "export")] | first
    | if . == null then "" else (.exportParameters.profile.name // "") end')
  if [ -z "$_af_prof" ]; then
    echo "audit_focus: policy $_af_pol has no export action - out of scope for a" >&2
    echo "  performance audit. Pick one of:" >&2
    audit_focus_candidates >&2
    return 1
  fi

  _af_data=$(printf '%s' "$_af_pj" | jq -r '
    [.spec.actions[]? | select(.action == "export")] | first
    | .exportParameters.exportData.enabled // false')
  _af_paused=$(printf '%s' "$_af_pj" | jq -r '.spec.paused // false')

  AUDIT_NS=$_af_ns
  AUDIT_POLICY=$_af_pol
  AUDIT_PROFILE=$_af_prof
  export AUDIT_NS AUDIT_POLICY AUDIT_PROFILE

  # Warn, do not refuse. A VM policy is not resolved to namespaces by lib/policies.sh,
  # so a legitimate pair can be absent from the candidate list.
  if ! audit_focus_candidates | awk -F'\t' -v n="$_af_ns" -v p="$_af_pol" \
         'NR > 1 && $1 == n && $2 == p { found = 1 } END { exit !found }'; then
    echo "audit_focus: note - $_af_pol does not resolve to $_af_ns by namespace selector." >&2
    echo "  Expected for a VM policy (virtualMachineRef / virtualMachineNamespace), which" >&2
    echo "  lib/policies.sh does not resolve. Confirm by hand that the VMs live there." >&2
  fi

  _af_rp=$(kubectl -n "$_af_ns" get restorepoints.apps.kio.kasten.io \
             --no-headers 2>/dev/null | wc -l | tr -d ' ')
  [ -z "$_af_rp" ] && _af_rp=0
  if [ "$_af_rp" -eq 0 ]; then
    echo "audit_focus: warning - $_af_ns has no RestorePoint, so it has never been" >&2
    echo "  backed up and cannot have been exported. Guides 04-06, 11 and 12 will find" >&2
    echo "  no repository. Trigger a run first (guide 13 step 7) or pick another pair." >&2
  fi
  [ "$_af_data" = "true" ] || \
    echo "audit_focus: warning - exportData is disabled on $_af_pol: metadata only, no volume data moves" >&2
  [ "$_af_paused" = "false" ] || \
    echo "audit_focus: warning - $_af_pol is paused, so there is no current export activity" >&2

  audit_focus_status
}

audit_focus_status() {
  if [ -z "${AUDIT_NS:-}" ]; then
    echo "audit focus  : NOT SET - run 'audit_focus <namespace> <policy>'"
    return 1
  fi
  echo "audit focus"
  echo "  namespace    : $AUDIT_NS"
  echo "  policy       : $AUDIT_POLICY"
  echo "  profile      : $AUDIT_PROFILE"
  echo "  restore pts  : $(kubectl -n "$AUDIT_NS" get restorepoints.apps.kio.kasten.io \
                             --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  echo "  equivalent   : generate-export-topology.py --namespace $AUDIT_NS --policy $AUDIT_POLICY"
}

# focus_dir <subdir>   this guide's output directory, under the pinned pair.
# Output is nested per pair so that auditing a second pair into the same AUDIT_DIR
# cannot overwrite the first one's files.
focus_dir() {
  if [ -z "${AUDIT_NS:-}" ]; then
    echo "focus_dir: no focus pinned - run 'audit_focus <namespace> <policy>' first" >&2
    return 1
  fi
  if [ -z "${1:-}" ]; then
    echo "focus_dir: usage: focus_dir <subdir>" >&2
    return 1
  fi
  _fd_d="$AUDIT_DIR/$AUDIT_NS.$AUDIT_POLICY/$1"
  mkdir -p "$_fd_d" || return 1
  cd "$_fd_d" || return 1
  pwd
}
