# window.sh - derive the effective metrics lookback window.  Sourced by init.sh.
#
# Declared retention is NOT the window you have. If Prometheus has no persistent
# volume its TSDB lives in an emptyDir and dies with the pod, so the real window is
# bounded by how long the longest-running replica has been up.
#
# Exports AUDIT_WINDOW_DAYS, AUDIT_RANGE, AUDIT_START, AUDIT_END.

audit_window() {
  _w_ns=${PROM_NS:-openshift-monitoring}
  _w_cr=${PROM_CR:-k8s}

  _w_ret=$(kubectl -n "$_w_ns" get prometheus "$_w_cr" -o jsonpath='{.spec.retention}' 2>/dev/null)
  if [ -z "$_w_ret" ]; then
    _w_ret=$(kubectl -n "$_w_ns" get sts "prometheus-$_w_cr" -o json 2>/dev/null \
             | jq -r '.spec.template.spec.containers[]|select(.name=="prometheus")|.args[]' 2>/dev/null \
             | sed -n 's/^--storage.tsdb.retention.time=//p')
  fi
  [ -z "$_w_ret" ] && _w_ret=15d

  _w_days=$(printf '%s' "$_w_ret" | awk '
    /^[0-9.]+d$/ {sub(/d$/,""); print; exit}
    /^[0-9.]+h$/ {sub(/h$/,""); printf "%.4f\n", $0/24; exit}
    /^[0-9.]+w$/ {sub(/w$/,""); print $0*7; exit}
    {print 15}')

  if [ -n "$(kubectl -n "$_w_ns" get prometheus "$_w_cr" -o jsonpath='{.spec.storage}' 2>/dev/null)" ]; then
    AUDIT_WINDOW_PERSISTENT=yes
    _w_eff=$_w_days
    AUDIT_WINDOW_BASIS="declared retention, storage is persistent"
  else
    AUDIT_WINDOW_PERSISTENT=no
    _w_pods=$(kubectl -n "$_w_ns" get pods -l app.kubernetes.io/name=prometheus -o json 2>/dev/null)
    _w_up=$(printf '%s' "$_w_pods" | jq -r '
      def ts: sub("\\.[0-9]+(?=Z$)";"") | fromdateiso8601;
      [ .items[] | (now - (.status.startTime|ts))/86400 ] | max // 0' 2>/dev/null)
    [ -z "$_w_up" ] && _w_up=0
    AUDIT_WINDOW_UPTIME_DAYS=$(awk -v u="$_w_up" 'BEGIN{printf "%.2f", u}')
    _w_eff=$(awk -v r="$_w_days" -v u="$_w_up" 'BEGIN{print (u<r?u:r)}')
    AUDIT_WINDOW_BASIS="min of retention and longest replica uptime, storage is emptyDir"
  fi

  export AUDIT_WINDOW_DAYS
  AUDIT_WINDOW_DAYS=$(awk -v w="$_w_eff" 'BEGIN{printf "%d", (w>0?int(w):0)}')
  export AUDIT_RANGE="${AUDIT_WINDOW_DAYS}d"
  export AUDIT_END=$(date +%s)
  export AUDIT_START=$((AUDIT_END - AUDIT_WINDOW_DAYS * 86400))
  export AUDIT_WINDOW_RETENTION="$_w_ret"
  export AUDIT_WINDOW_PERSISTENT AUDIT_WINDOW_BASIS
  export AUDIT_WINDOW_UPTIME_DAYS="${AUDIT_WINDOW_UPTIME_DAYS:-}"

  if [ "$AUDIT_WINDOW_DAYS" -lt 7 ]; then
    echo "audit_window: WARNING - effective window is only ${AUDIT_WINDOW_DAYS} day(s)." >&2
    echo "audit_window: guides 06, 07, 08 and 13 cannot show a weekly trend. Report the" >&2
    echo "audit_window: window explicitly and do not extrapolate." >&2
  fi
  return 0
}

# audit_window_report   human-readable summary, written to $AUDIT_DIR/metrics-window.txt
audit_window_report() {
  {
    echo "retention_declared        : ${AUDIT_WINDOW_RETENTION:-?}"
    echo "persistent_storage        : ${AUDIT_WINDOW_PERSISTENT:-?}"
    echo "longest_replica_uptime_d  : ${AUDIT_WINDOW_UPTIME_DAYS:-n/a}"
    echo "window_basis              : ${AUDIT_WINDOW_BASIS:-?}"
    echo "AUDIT_WINDOW_DAYS         : ${AUDIT_WINDOW_DAYS:-?}"
    echo "AUDIT_RANGE               : ${AUDIT_RANGE:-?}"
    echo "AUDIT_START               : ${AUDIT_START:-?}"
    echo "AUDIT_END                 : ${AUDIT_END:-?}"
    echo
    kubectl -n "${PROM_NS:-openshift-monitoring}" get pods -l app.kubernetes.io/name=prometheus -o json 2>/dev/null \
      | jq -r 'def ts: sub("\\.[0-9]+(?=Z$)";"") | fromdateiso8601;
               (["POD","POD_START","UPTIME_D","RESTARTS"]|@tsv),
               (.items[] | [ .metadata.name, .status.startTime,
                             (((now - (.status.startTime|ts))/86400)*100|round/100),
                             ([.status.containerStatuses[]?.restartCount]|add // 0) ] | @tsv)'
  } > "${AUDIT_DIR:-.}/metrics-window.txt"
  cat "${AUDIT_DIR:-.}/metrics-window.txt"
}

# audit_window_verify   cross-check the derivation against the oldest real sample
audit_window_verify() {
  _v_oldest=$(tqr "container_memory_working_set_bytes{namespace=\"${K10NS:-kasten-io}\",container!=\"\"}" \
                  "$((AUDIT_END - 90*86400))" "$AUDIT_END" 6h \
              | jq -r '[.data.result[].values[0][0]] | min // empty')
  if [ -z "$_v_oldest" ]; then
    echo "audit_window_verify: no samples returned - cannot cross-check" >&2
    return 1
  fi
  awk -v o="$_v_oldest" -v n="$AUDIT_END" -v d="$AUDIT_WINDOW_DAYS" 'BEGIN{
    m = (n-o)/86400
    printf "measured=%.2fd derived=%sd\n", m, d
    if (m < d-1) print "  -> measured is SHORTER; override AUDIT_WINDOW_DAYS by hand and say so"
  }'
}
