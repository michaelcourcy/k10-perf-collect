# datamover.sh - what the datamover pods of ONE export actually consumed.
# Sourced by init.sh. Used by guides 09 and 13.
#
# THE ATTRIBUTION PROBLEM. cAdvisor carries no pod labels - only namespace, pod,
# container and node - so a container_memory_working_set_bytes series says "some pod in
# kasten-io", never "the export of prod-test". The join that fixes it is on `pod`,
# against kube-state-metrics' kube_pod_labels, which exposes pod labels as label_* when
# they are in its allow-list. OpenShift runs KSM with --metric-labels-allowlist=pods=[*],
# so it works out of the box; on another stack add
#   --metric-labels-allowlist=pods=[app-name,policy-name,k10.kasten.io/jobID]
# Run ksm_pod_labels_check first - without it these functions report per pod only.
#
# K10 labels data-mover-svc-* pods with app-name (the APPLICATION namespace),
# policy-name and k10.kasten.io/jobID. copy-vol-data-* pods - the ones actually reading
# the volume clone - carry ONLY the job id, so their namespace is inherited from the
# data-mover-svc pod of the same job. Attribution is therefore per JOB, never per PVC:
# a copy-vol-data pod mounts an ephemeral kanister-pvc-* clone whose name does not
# reveal the source volume.
#
# WHY "OWN" AND "CONCURRENT" ARE SEPARATE. One policy run exports every namespace it
# selects at the same time. Measured on the reference cluster in a single 5-minute
# window: data-mover-svc pods for prod-test, large-test AND test-calibrate, plus a
# metadata mover in kasten-io - four jobs, twelve pods. Reporting all of them under
# prod-test would triple its apparent cost; reporting only its own hides the load the
# cluster actually carried. Both are printed.
#
#   ksm_pod_labels_check            can usage be attributed at all?
#   datamover_pods  <start> <end>   one row per pod, with attribution and usage
#   datamover_usage <start> <end>   the summary for AUDIT_NS, plus concurrent load
#   datamover_for_export <name>     both of the above for one ExportAction's window

DATAMOVER_POD_REGEX=${DATAMOVER_POD_REGEX:-'data-mover.*|copy-vol-data.*|create-repo.*|repository-server.*|restore-data.*'}
# cAdvisor is scraped every 30 s and a datamover can live for less than that, so the
# window is padded on both sides. Pods that still leave no sample are reported, not
# silently counted as 0.
DATAMOVER_PAD=${DATAMOVER_PAD:-45}

_dm_sel() {
  printf 'namespace="%s",pod=~"%s",container!="",container!="POD"' \
    "${K10NS:-kasten-io}" "$DATAMOVER_POD_REGEX"
}

# ksm_pod_labels_check   does kube-state-metrics expose pod labels for the K10 namespace?
ksm_pod_labels_check() {
  _dmk=$(tq "kube_pod_labels{namespace=\"${K10NS:-kasten-io}\"}" 2>/dev/null \
         | jq -r '[.data.result[]?.metric | keys[]] | map(select(startswith("label_"))) | length')
  if [ -z "$_dmk" ] || [ "$_dmk" = "null" ]; then
    echo "kube_pod_labels: query failed - cannot tell; check prom_check"
    return 2
  fi
  if [ "$_dmk" -gt 0 ]; then
    echo "kube_pod_labels: pod labels ARE exposed ($_dmk label_* keys) - attribution works"
    return 0
  fi
  echo "kube_pod_labels: NO pod labels exposed for ${K10NS:-kasten-io}."
  echo "  Datamover usage can only be reported per pod, not per namespace or policy."
  echo "  Fix: add --metric-labels-allowlist=pods=[app-name,policy-name,k10.kasten.io/jobID]"
  echo "  to kube-state-metrics. Say so in the report rather than attributing by guesswork."
  return 1
}

# _dm_attribution <start> <end>   TSV: pod, appNamespace, policy, jobId
# copy-vol-data pods inherit their namespace from the data-mover-svc pod of the same job.
_dm_attribution() {
  tqr "kube_pod_labels{namespace=\"${K10NS:-kasten-io}\",pod=~\"$DATAMOVER_POD_REGEX\"}" \
      "$1" "$2" 15s 2>/dev/null \
    | jq -r '
        [ .data.result[]?.metric ] as $m
        # job id -> application namespace, learned from the pods that carry app-name
        | ( reduce $m[] as $x ({};
              if ($x.label_k10_kasten_io_job_id and $x.label_app_name)
              then .[$x.label_k10_kasten_io_job_id] = $x.label_app_name else . end) ) as $job
        | [ $m[] | { pod: .pod,
                     ns: (.label_app_name // $job[.label_k10_kasten_io_job_id // ""] // "-"),
                     pol: (.label_policy_name // "-"),
                     job: (.label_k10_kasten_io_job_id // "-") } ]
        | unique_by(.pod) | .[]
        | [ .pod, .ns, .pol, .job ] | @tsv'
}

# datamover_pods <start> <end>   one row per datamover pod alive in the window.
#
# CPU is CPU-SECONDS, not a rate: the per-pod difference of the cumulative counter
# container_cpu_usage_seconds_total between the first and last sample in the window. A
# rate() needs several samples per pod, and these pods live seconds to minutes against a
# 15-30 s scrape interval, so a rate smooths a short pod towards zero or misses it.
datamover_pods() {
  if [ -z "${2:-}" ]; then
    echo "datamover_pods: usage: datamover_pods <start-epoch> <end-epoch>" >&2
    return 1
  fi
  _dm_s=$(( $1 - DATAMOVER_PAD ))
  _dm_e=$(( $2 + DATAMOVER_PAD ))
  _dm_a=$(_dm_attribution "$_dm_s" "$_dm_e" | jq -Rs 'split("\n") | map(select(length>0) | split("\t"))
            | map({key: .[0], value: {ns: .[1], pol: .[2], job: .[3]}}) | from_entries')
  [ -z "$_dm_a" ] && _dm_a='{}'

  _dm_mem=$(tqr "sum by (pod) (container_memory_working_set_bytes{$(_dm_sel)})" \
                "$_dm_s" "$_dm_e" 15s 2>/dev/null)
  _dm_cpu=$(tqr "sum by (pod) (container_cpu_usage_seconds_total{$(_dm_sel)})" \
                "$_dm_s" "$_dm_e" 15s 2>/dev/null)
  [ -z "$_dm_mem" ] && _dm_mem='{"data":{"result":[]}}'
  [ -z "$_dm_cpu" ] && _dm_cpu='{"data":{"result":[]}}'

  jq -rn --argjson mem "$_dm_mem" --argjson cpu "$_dm_cpu" --argjson att "$_dm_a" \
         --arg own "${AUDIT_NS:-}" '
    ( reduce $mem.data.result[]? as $r ({};
        .[$r.metric.pod] = ([ $r.values[] | .[1] | tonumber ] | max)) ) as $m
    | ( reduce $cpu.data.result[]? as $r ({};
        .[$r.metric.pod] = ( [ $r.values[] | .[1] | tonumber ]
                             | if length >= 2 then (max - min) else 0 end )) ) as $c
    | ( ($m | keys) + ($c | keys) + ($att | keys) | unique ) as $pods
    | (["POD","APP_NS","POLICY","JOB_ID","SCOPE","PEAK_MEM_MiB","CPU_SECONDS"] | @tsv),
      ( $pods[]
        | . as $p
        | ($att[$p] // {ns:"-", pol:"-", job:"-"}) as $a
        | [ $p, $a.ns, $a.pol, $a.job,
            (if $own == "" then "-"
             elif $a.ns == $own then "own"
             elif $a.ns == "-" then "unattributed"
             else "concurrent" end),
            (if $m[$p] == null then "no-sample" else ($m[$p] / 1048576 * 100 | round / 100) end),
            (if $c[$p] == null then "no-sample" else ($c[$p] * 1000 | round / 1000) end) ]
        | @tsv )'
}

# datamover_usage <start> <end>   the headline figures for AUDIT_NS over the window.
#
# peakSumMemory is the maximum, over the window, of memory SUMMED across the pods at
# each step - not the sum of each pod's individual peak, which would overstate it.
datamover_usage() {
  if [ -z "${2:-}" ]; then
    echo "datamover_usage: usage: datamover_usage <start-epoch> <end-epoch>" >&2
    return 1
  fi
  if [ -z "${AUDIT_NS:-}" ]; then
    echo "datamover_usage: no focus pinned - run 'audit_focus <namespace> <policy>' first" >&2
    return 1
  fi
  _dmu_s=$(( $1 - DATAMOVER_PAD ))
  _dmu_e=$(( $2 + DATAMOVER_PAD ))
  _dmu_w=$(( _dmu_e - _dmu_s ))
  _dmu_t=$(mktemp)
  datamover_pods "$1" "$2" > "$_dmu_t" || { rm -f "$_dmu_t"; return 1; }

  # Pods working for this namespace, plus any the labels could not attribute: leaving
  # an unattributed pod out would silently under-report.
  _dmu_own=$(awk -F'\t' 'NR>1 && ($5=="own" || $5=="unattributed") {print $1}' "$_dmu_t" \
             | paste -sd'|' -)
  _dmu_conc=$(awk -F'\t' 'NR>1 && $5=="concurrent" {print $2}' "$_dmu_t" | sort -u | paste -sd',' -)
  _dmu_nosample=$(awk -F'\t' 'NR>1 && $6=="no-sample" {print $1}' "$_dmu_t" | paste -sd',' -)

  echo "window            : $(date -u -r "$_dmu_s" +%Y-%m-%dT%H:%M:%SZ) .. $(date -u -r "$_dmu_e" +%Y-%m-%dT%H:%M:%SZ) (${_dmu_w}s, includes ${DATAMOVER_PAD}s padding each side)"
  echo "namespace         : $AUDIT_NS"

  if [ -z "$_dmu_own" ]; then
    echo "peak memory       : no datamover pod attributed to $AUDIT_NS in this window"
  else
    _dmu_peak=$(tqr "sum(container_memory_working_set_bytes{namespace=\"${K10NS:-kasten-io}\",pod=~\"$_dmu_own\",container!=\"\",container!=\"POD\"})" \
                    "$_dmu_s" "$_dmu_e" 15s 2>/dev/null \
                | jq -r '[.data.result[]?.values[]? | .[1] | tonumber] | if length==0 then "-" else (max/1048576*100|round/100) end')
    _dmu_cpu=$(awk -F'\t' 'NR>1 && ($5=="own" || $5=="unattributed") && $7!="no-sample" {s+=$7} END {printf "%.3f", s+0}' "$_dmu_t")
    echo "pods              : $(awk -F'\t' 'NR>1 && ($5=="own"||$5=="unattributed")' "$_dmu_t" | wc -l | tr -d ' ')"
    echo "peak sum memory   : ${_dmu_peak} MiB"
    echo "cpu total         : ${_dmu_cpu} cpu-s  (avg $(awk -v c="$_dmu_cpu" -v w="$_dmu_w" 'BEGIN{printf "%.2f", (w?c/w:0)}') cores over the window)"
  fi

  if [ -n "$_dmu_conc" ]; then
    _dmu_all=$(tqr "sum(container_memory_working_set_bytes{$(_dm_sel)})" "$_dmu_s" "$_dmu_e" 15s 2>/dev/null \
               | jq -r '[.data.result[]?.values[]? | .[1] | tonumber] | if length==0 then "-" else (max/1048576*100|round/100) end')
    echo "concurrent        : $_dmu_conc"
    echo "all datamovers    : ${_dmu_all} MiB peak - the load the cluster actually carried"
  else
    echo "concurrent        : none - this export had the datamovers to itself"
  fi
  [ -n "$_dmu_nosample" ] && \
    echo "no sample         : $_dmu_nosample (alive for less than one scrape interval; the figures above are a floor)"
  rm -f "$_dmu_t"
}

# datamover_for_export <exportaction-name>   resolve the window and report.
datamover_for_export() {
  if [ -z "${1:-}" ]; then
    echo "datamover_for_export: usage: datamover_for_export <exportaction-name>" >&2
    return 1
  fi
  if [ -z "${AUDIT_NS:-}" ]; then
    echo "datamover_for_export: no focus pinned - run 'audit_focus <ns> <policy>' first" >&2
    return 1
  fi
  _dmf=$(kubectl -n "$AUDIT_NS" get exportactions.actions.kio.kasten.io "$1" -o json 2>/dev/null \
         | jq -r 'def ts: sub("\\.[0-9]+(?=Z$)"; "") | fromdateiso8601;
                  if (.status.startTime == null or .status.endTime == null) then "-"
                  else "\(.status.startTime|ts) \(.status.endTime|ts)" end')
  if [ -z "$_dmf" ] || [ "$_dmf" = "-" ]; then
    echo "datamover_for_export: $1 has no start/end time (still running, or not found)" >&2
    return 1
  fi
  # shellcheck disable=SC2086
  set -- $_dmf
  echo "export            : $_dmf" >/dev/null
  datamover_usage "$1" "$2"
  echo
  datamover_pods "$1" "$2" | column -t
}
