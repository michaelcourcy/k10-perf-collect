# check_auth.sh - the complete authorisation gate for the audit.  Sourced by init.sh.
#
# Every permission below is required by at least one guide. They are NOT optional and
# they are NOT staged: a missing one means some extract silently cannot be produced, so
# init.sh refuses to start the audit until all of them are granted.
#
#   check_auth     print the full table, return 1 if anything is denied
#   require_auth   same, but quiet when everything passes (called by init.sh)
#
# Each row is: <what it unlocks> | <kubectl auth can-i arguments>
# Arguments are passed through "$@", never word-split from a variable, because zsh does
# not split unquoted parameter expansions.

_auth_rows() {
  _k=${K10NS:-kasten-io}
  _p=${PROM_NS:-openshift-monitoring}
  cat <<ROWS
guide 07 08 : node inventory and capacity|get|nodes
guide 00 04 08 09 : kubelet stats and per-pod ephemeral storage|get|nodes|--subresource=proxy
guide 01 03 : namespace and volume inventory|get|namespaces
guide 03 : PersistentVolume inventory|get|persistentvolumes
guide 03 04 : StorageClass and snapshot capability|get|storageclasses
guide 03 : snapshot capability per driver|get|volumesnapshotclasses.snapshot.storage.k8s.io
guide 03 04 09 : pod and volume topology|get|pods|--all-namespaces
guide 13 : K10 service logs|get|pods/log|--all-namespaces
guide 03 04 05 : PVC inventory|get|persistentvolumeclaims|--all-namespaces
guide 13 : worker pod warnings|get|events|-n|$_k
guide 02 10 : k10-config and feature flags|get|configmaps|-n|$_k
guide 10 11 : Helm release values and object store credentials|get|secrets|-n|$_k
guide 10 : K10 service images and resources|get|deployments.apps|-n|$_k
guide 01 02 : policies|get|policies.config.kio.kasten.io|-n|$_k
guide 11 12 : Location Profiles|get|profiles.config.kio.kasten.io|-n|$_k
guide 10 : ActionPodSpec overrides|get|actionpodspecs.config.kio.kasten.io|-n|$_k
guide 10 : operator CR, if operator-installed|get|k10s.apik10.kasten.io|-n|$_k
guide 02 : policy runs and metadata exports|get|exportactions.actions.kio.kasten.io|-n|$_k
guide 01 06 13 : per-application exports|get|exportactions.actions.kio.kasten.io|--all-namespaces
guide 06 13 : export byte counters and per-volume detail|get|exportactions.actions.kio.kasten.io/details|--all-namespaces
guide 01 12 : restore points|get|restorepoints.apps.kio.kasten.io|--all-namespaces
guide 01 : applications K10 can see|get|applications.apps.kio.kasten.io|--all-namespaces
guide 12 : Kopia repository inventory|get|storagerepositories.repositories.kio.kasten.io|-n|$_k
guide 11 12 : repo-checker and object-count pods|create|pods|-n|$_k
guide 04 05 : PVC inspector pods in application namespaces|create|pods|--all-namespaces
guide 12 : kopia commands inside the debug pod|create|pods/exec|-n|$_k
guide 04 05 : file counts inside pods that mount PVCs|create|pods/exec|--all-namespaces
guide 02 03 09 11 13 : port-forward to K10 services|create|pods/portforward|-n|$_k
guide 11 12 : clean up the pods the audit creates|delete|pods|-n|$_k
guide 04 05 : clean up inspector pods|delete|pods|--all-namespaces
guide 09 13 : audit-scoped policies|create|policies.config.kio.kasten.io|-n|$_k
guide 09 13 : remove audit-scoped policies afterwards|delete|policies.config.kio.kasten.io|-n|$_k
guide 09 13 : trigger a policy run|create|runactions.actions.kio.kasten.io|-n|$_k
guide 00 and all metrics guides : mint a cluster monitoring token|create|serviceaccounts/token|-n|$_p
guide 00 : locate the thanos-querier route|get|routes.route.openshift.io|-n|$_p
guide 00 06 07 08 : Prometheus retention and replica uptime|get|prometheuses.monitoring.coreos.com|-n|$_p
guide 00 06 : Prometheus replica uptime|get|pods|-n|$_p
ROWS
}

# check_auth [-q]
# Returns 0 only if every permission is granted. Sets AUTH_DENIED to the count.
# Fed by "done < file" rather than a pipeline, so the loop runs in the current shell
# and AUTH_DENIED survives it (a piped while-loop runs in a subshell).
check_auth() {
  _quiet=0
  [ "${1:-}" = "-q" ] && _quiet=1

  _auth_tmp=$(mktemp)
  _auth_rows > "$_auth_tmp"
  AUTH_DENIED=0

  if [ "$_quiet" -eq 0 ]; then
    printf '%-60s %-5s %s\n' "PERMISSION" "OK" "NEEDED BY"
    printf '%s\n' "--------------------------------------------------------------------------------"
  fi

  while IFS='|' read -r _why _verb _res _f1 _f2; do
    [ -z "$_verb" ] && continue
    if [ -n "$_f2" ]; then
      _r=$(kubectl auth can-i "$_verb" "$_res" "$_f1" "$_f2" 2>/dev/null | tail -1)
      _label="$_verb $_res $_f1 $_f2"
    elif [ -n "$_f1" ]; then
      _r=$(kubectl auth can-i "$_verb" "$_res" "$_f1" 2>/dev/null | tail -1)
      _label="$_verb $_res $_f1"
    else
      _r=$(kubectl auth can-i "$_verb" "$_res" 2>/dev/null | tail -1)
      _label="$_verb $_res"
    fi
    [ -z "$_r" ] && _r="ERROR"

    if [ "$_r" = "yes" ]; then
      [ "$_quiet" -eq 0 ] && printf '%-60s %-5s %s\n' "$_label" "yes" "$_why"
    else
      AUTH_DENIED=$((AUTH_DENIED + 1))
      printf '%-60s %-5s %s\n' "$_label" "NO" "$_why" >&2
    fi
  done < "$_auth_tmp"

  rm -f "$_auth_tmp"
  [ "$AUTH_DENIED" -eq 0 ]
}

# require_auth   the hard gate used by init.sh
require_auth() {
  if check_auth -q; then
    return 0
  fi
  cat >&2 <<MSG

================================================================================
AUDIT NOT STARTED - $AUTH_DENIED required permission(s) missing (listed above).
================================================================================
Every permission this audit uses is mandatory. Without it an extract silently
cannot be produced, and a partial audit produces misleading recommendations.

Have the missing permissions granted, then source lib/init.sh again.
The full list, with the justification for each, is in
guides/00-prerequisites.md section 8.

To review the complete table, granted and denied:   check_auth
================================================================================
MSG
  return 1
}
