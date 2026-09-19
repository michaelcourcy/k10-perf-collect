# policies.sh - determine which namespaces are in scope for a PERFORMANCE audit.
# Sourced by init.sh.  Used by guide 01.
#
# SCOPE RULE: a namespace is in scope if some policy that has an EXPORT action
# selects it. Nothing else matters here.
#
# Why only exports: performance analysis is about where the datamover runs. An export
# is what creates a Kopia repository, consumes object storage, moves bytes off-cluster
# and takes a measurable amount of time. A backup-only policy produces local snapshots -
# no datamover pod, no repository, no object count, no change rate, no export duration.
# There is nothing to measure, so those namespaces are out of scope.
#
# This is deliberately NOT coverage analysis. Whether a backup-only namespace is
# adequately protected is a real question, and an important one, but it is a different
# engagement - see kasten-mcp/kasten-bot/kasten_bot/policies.py, which grades coverage
# properly (exported / export_paused / backup_only / none) and handles VM selectors.
# Do not re-derive that here.
#
# Selector forms, per K10's own validation regex ^[*]$|^[A-Za-z0-9]+[A-Za-z0-9-_]*[*]?$
#   cpd          literal - matches cpd only, not cpd-operators
#   basic-app*   prefix  - matches basic-app AND basic-app1, basic-app2, ...
#   *            all namespaces K10 can see
# plus real namespace labels via matchLabels / matchExpressions on any other key.
# Namespaces in the k10-config excludedApps key are invisible to K10 and never match.
#
# The selector form is an implementation detail: it is resolved to get the namespace
# list and then not reported. Which form matched matters for a coverage conversation,
# not for a performance one.
#
#   export_policies            policies that export, with their cadence
#   audit_scope                namespace -> policy -> export cadence -> profile
#   audit_scope_namespaces     flat list, the input to guides 03-06
#   policy_selector_warnings   selectors that resolve to nothing, or need attention
#   k10_excluded_namespaces    namespaces K10 cannot see

k10_excluded_namespaces() {
  kubectl -n "${K10NS:-kasten-io}" get cm k10-config -o jsonpath='{.data.excludedApps}' 2>/dev/null \
    | tr ',' '\n' | awk 'NF{print}'
}

# export_policies   which policies actually move data off-cluster
export_policies() {
  kubectl -n "${K10NS:-kasten-io}" get policies.config.kio.kasten.io -o json 2>/dev/null | jq -r '
    (["POLICY","SNAP_FREQ","EXPORT_FREQ","PAUSED","PROFILE","EXPORTS_DATA"] | @tsv),
    ( .items[]
      | . as $p
      | ([$p.spec.actions[]? | select(.action == "export")] | first) as $e
      | select($e != null)
      | [ $p.metadata.name,
          ($p.spec.frequency // "@onDemand"),
          ($e.exportParameters.frequency // "@onDemand"),
          ($p.spec.paused // false | tostring),
          ($e.exportParameters.profile.name // "-"),
          ($e.exportParameters.exportData.enabled // false | tostring)
        ] | @tsv )'
}

# Resolve label-based selectors against live namespace labels. Emits {"policy":[ns,...]}
_scope_label_hits() {
  _slh_out="{}"
  _slh_tmp=$(mktemp)
  kubectl -n "${K10NS:-kasten-io}" get policies.config.kio.kasten.io -o json 2>/dev/null | jq -r '
    .items[]
    | . as $p | ($p.spec.selector // {}) as $sel
    | [ ( ($sel.matchLabels // {}) | to_entries[] | "\(.key)=\(.value)" ),
        ( ($sel.matchExpressions // [])[]
          | select(.key | startswith("k10.kasten.io/") | not)
          | if   .operator == "In"           then "\(.key)=\(.values[0])"
            elif .operator == "NotIn"        then "\(.key)!=\(.values[0])"
            elif .operator == "Exists"       then .key
            elif .operator == "DoesNotExist" then "!\(.key)"
            else "UNSUPPORTED" end )
      ] as $terms
    | select(($terms | length) > 0)
    | [$p.metadata.name, ($terms | join(","))] | @tsv' > "$_slh_tmp"
  while IFS='	' read -r _slh_pol _slh_sel; do
    [ -z "$_slh_pol" ] && continue
    case "$_slh_sel" in *UNSUPPORTED*) continue ;; esac
    _slh_ns=$(kubectl get ns -l "$_slh_sel" -o json 2>/dev/null | jq -c '[.items[].metadata.name]')
    [ -z "$_slh_ns" ] && _slh_ns='[]'
    _slh_out=$(printf '%s' "$_slh_out" | jq -c --arg p "$_slh_pol" --argjson n "$_slh_ns" '. + {($p): $n}')
  done < "$_slh_tmp"
  rm -f "$_slh_tmp"
  printf '%s' "$_slh_out"
}

# One row per (namespace, export policy, selector value).
_scope_json() {
  _sj_ns=$(kubectl get ns -o json 2>/dev/null)
  _sj_pol=$(kubectl -n "${K10NS:-kasten-io}" get policies.config.kio.kasten.io -o json 2>/dev/null)
  _sj_exc=$(kubectl -n "${K10NS:-kasten-io}" get cm k10-config -o jsonpath='{.data.excludedApps}' 2>/dev/null)
  [ -n "$_sj_ns" ]  || _sj_ns='{"items":[]}'
  [ -n "$_sj_pol" ] || _sj_pol='{"items":[]}'
  _sj_lab=$(_scope_label_hits)

  jq -n --argjson ns "$_sj_ns" --argjson pol "$_sj_pol" \
        --argjson lab "${_sj_lab:-{\}}" --arg exc "${_sj_exc:-}" '
    ($exc | split(",") | map(select(length > 0))) as $excluded
    | ( $ns.items | map(.metadata.name) | map(select(IN($excluded[]) | not)) ) as $visible
    | [ $pol.items[]
        | . as $p
        | ($p.spec.selector // {}) as $sel
        | ([$p.spec.actions[]? | select(.action == "export")] | first) as $e
        # SCOPE RULE: no export action, not our problem
        | select($e != null)
        | {
            policy:      $p.metadata.name,
            frequency:   ($p.spec.frequency // "@onDemand"),
            export_freq: ($e.exportParameters.frequency // "@onDemand"),
            exports_data:($e.exportParameters.exportData.enabled // false),
            paused:      ($p.spec.paused // false),
            profile:     ($e.exportParameters.profile.name // "-"),
            validation:  ($p.status.validation // "unknown"),
            # k10.kasten.io/* keys other than appNamespace mean a VM selector,
            # which this guide does not resolve - it warns instead.
            vm_selector: ( [ ($sel.matchExpressions // [])[]
                             | select(.key | startswith("k10.kasten.io/virtualMachine")) ] | length > 0 ),
            includes: [ ($sel.matchExpressions // [])[]
                        | select(.key == "k10.kasten.io/appNamespace" and .operator == "In")
                        | .values[] ],
            excludes: [ ($sel.matchExpressions // [])[]
                        | select(.key == "k10.kasten.io/appNamespace" and .operator == "NotIn")
                        | .values[] ]
          }
        | . as $b
        # resolve one selector value to the namespaces it matches
        | ( def hits($v):
              if   ($v | test("^[*]$")) then $visible
              elif ($v | test("^[A-Za-z0-9]+[A-Za-z0-9_-]*[*]$"))
                then ($v | rtrimstr("*")) as $pfx | ($visible | map(select(startswith($pfx))))
              elif ($v | test("^[A-Za-z0-9]+[A-Za-z0-9_-]*$"))
                then ($visible | map(select(. == $v)))
              else [] end;
            def kind($v):
              if   ($v | test("^[*]$")) then "all"
              elif ($v | test("^[A-Za-z0-9]+[A-Za-z0-9_-]*[*]$")) then "prefix"
              elif ($v | test("^[A-Za-z0-9]+[A-Za-z0-9_-]*$")) then "literal"
              else "invalid" end;
            # kasten-io-cluster is a sentinel for cluster-scoped resources, not a namespace
            ( [ $b.excludes[] | select(. != "kasten-io-cluster") ] | map(hits(.)) | flatten | unique ) as $denied
            | ( [ $lab[$b.policy] // [] | .[] | select(IN($excluded[]) | not) ] ) as $labelhits
            | ( [ $b.includes[]
                  | select(. != "kasten-io-cluster")
                  | . as $v
                  | (hits($v) | map(select(IN($denied[]) | not))) as $h
                  | if ($h | length) == 0
                    then { selector: $v, match: (kind($v) + "-NOMATCH"), namespace: null }
                    else ($h[] | { selector: $v, match: kind($v), namespace: . })
                    end ] ) as $namerows
            | ( [ $labelhits[] | select(IN($denied[]) | not)
                  | { selector: "<labels>", match: "label", namespace: . } ] ) as $labelrows
            | ( if ($namerows + $labelrows | length) > 0 then ($namerows + $labelrows)
                elif $b.vm_selector then [ { selector: "<vm>", match: "vm-UNRESOLVED", namespace: null } ]
                else [ { selector: null, match: "none", namespace: null } ] end )
          ) as $rows
        | [ $rows[] | $b + . ]
      ] | flatten'
}

# audit_scope   the headline table: what the performance audit covers
audit_scope() {
  _scope_json | jq -r '
    (["NAMESPACE","POLICY","SNAP_FREQ","EXPORT_FREQ","PAUSED","PROFILE"] | @tsv),
    ( map(select(.namespace != null and .exports_data))
      | sort_by(.namespace, .policy) | .[]
      | [ .namespace, .policy, .frequency, .export_freq,
          (.paused|tostring), .profile ] | @tsv )'
}

# audit_scope_namespaces   flat, de-duplicated - the input to guides 03-06
audit_scope_namespaces() {
  _scope_json | jq -r 'map(select(.namespace != null and .exports_data) | .namespace) | unique | .[]'
}

policy_selector_warnings() {
  _scope_json | jq -r '
    ( [ .[] | select(.match == "invalid-NOMATCH")
        | "SELECTOR INVALID - REJECTED BY K10\t\(.policy)\tleading or embedded wildcards are not allowed" ]
      + [ .[] | select((.match | endswith("NOMATCH")) and .match != "invalid-NOMATCH")
          | "SELECTOR MATCHES NO NAMESPACE\t\(.policy)\tthe policy exports nothing" ]
      + [ .[] | select(.match == "vm-UNRESOLVED")
          | "VM SELECTOR - RESOLVE BY HAND\t\(.policy)\tthis resolves namespace selectors only" ]
      + [ .[] | select(.validation != "Success")
          | "POLICY NOT VALID\t\(.policy)\tvalidation=\(.validation)" ]
      + [ .[] | select(.exports_data | not)
          | "exportData DISABLED\t\(.policy)\tmetadata only - no volume data moves, so nothing to measure" ]
      + [ .[] | select(.paused and .exports_data)
          | "EXPORT POLICY PAUSED\t\(.policy)\tnothing runs, so no current performance data" ]
      + [ .[] | select(.exports_data and (.export_freq == "@onDemand"))
          | "EXPORT IS @onDemand\t\(.policy)\texports only when triggered by hand" ]
    ) | unique | .[]'
}
