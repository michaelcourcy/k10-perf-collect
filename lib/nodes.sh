# nodes.sh - node capacity and a usage sample, the way the generator takes it.
# Sourced by init.sh. Used by guides 07 and 08.
#
# ONE CALL, THREE ANSWERS. GET /api/v1/nodes/<node>/proxy/stats/summary returns, per
# node, CPU (node.cpu.usageNanoCores), memory (node.memory.workingSetBytes /
# availableBytes) AND the root filesystem (node.fs.usedBytes / capacityBytes) - which is
# the ephemeral storage the kubelet evicts on, where every datamover writes its Kopia
# cache and its cloned volume data. metrics.k8s.io gives CPU and memory only, so it is
# the fallback, not the first choice. Needs `get` on nodes/proxy.
#
# UNIT TRAP. Node status.capacity["ephemeral-storage"] is a Ki quantity ("536083696Ki")
# while status.allocatable["ephemeral-storage"] is a bare byte count ("492980991592").
# Comparing the strings, or treating both as the same unit, gives a 1024x error. Parse
# quantities; never compare them as text.
#
# This is ONE SAMPLE, not an average: it says what the cluster looked like when you ran
# it. Take it while an export is in flight and it tells you what the export costs; take
# it at midday and it tells you nothing about the backup window. Guides 07 and 08 pair
# it with the range queries over AUDIT_WINDOW_DAYS for the trend.
#
#   node_snapshot    capacity, allocatable and current usage per node
#   node_totals      the same, summed - the cluster's headroom in one line

# Kubernetes quantity -> number. Suffix order matters: "100Mi" must not match "M$".
_NODES_QTY='
  def qty: if . == null then null
    elif (type == "number") then .
    else (tostring
      | if   test("Ki$") then (rtrimstr("Ki") | tonumber) * 1024
        elif test("Mi$") then (rtrimstr("Mi") | tonumber) * 1048576
        elif test("Gi$") then (rtrimstr("Gi") | tonumber) * 1073741824
        elif test("Ti$") then (rtrimstr("Ti") | tonumber) * 1099511627776
        elif test("Pi$") then (rtrimstr("Pi") | tonumber) * 1125899906842624
        elif test("m$")  then (rtrimstr("m")  | tonumber) / 1000
        elif test("k$")  then (rtrimstr("k")  | tonumber) * 1000
        elif test("M$")  then (rtrimstr("M")  | tonumber) * 1000000
        elif test("G$")  then (rtrimstr("G")  | tonumber) * 1000000000
        elif test("T$")  then (rtrimstr("T")  | tonumber) * 1000000000000
        else tonumber end)
    end;
'

# _node_usage   TSV: node, source, usedCores, workingSetBytes, fsUsedBytes, fsCapacityBytes,
#                    inodesUsed, imageFsUsedBytes
_node_usage() {
  _nu_fallback=""
  for _nu_n in $(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
    _nu_j=$(kubectl get --raw "/api/v1/nodes/$_nu_n/proxy/stats/summary" 2>/dev/null)
    if [ -n "$_nu_j" ]; then
      printf '%s' "$_nu_j" | jq -r --arg n "$_nu_n" '.node
        | [ $n, "stats/summary",
            ((.cpu.usageNanoCores // 0) / 1000000000),
            (.memory.workingSetBytes // "-"),
            (.fs.usedBytes // "-"), (.fs.capacityBytes // "-"),
            (.fs.inodesUsed // "-"),
            (.runtime.imageFs.usedBytes // "-") ] | @tsv'
    else
      _nu_fallback="yes"
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$_nu_n" "PENDING" "-" "-" "-" "-" "-" "-"
    fi
  done
  if [ -n "$_nu_fallback" ]; then
    echo "_node_usage: nodes/proxy unavailable for some nodes - falling back to metrics.k8s.io (CPU and memory only, no ephemeral storage)" >&2
  fi
}

# node_snapshot   per node: roles, size, what is allocatable, and what is in use now.
node_snapshot() {
  _ns_u=$(mktemp)
  _node_usage > "$_ns_u" 2>/dev/null
  # metrics.k8s.io for any node stats/summary did not answer for
  if grep -q '	PENDING	' "$_ns_u" 2>/dev/null; then
    _ns_m=$(kubectl get --raw /apis/metrics.k8s.io/v1beta1/nodes 2>/dev/null)
    if [ -n "$_ns_m" ]; then
      _ns_t=$(mktemp)
      printf '%s' "$_ns_m" | jq -r "$_NODES_QTY"'
        .items[]? | [ .metadata.name, "metrics.k8s.io",
                      (.usage.cpu | qty), (.usage.memory | qty),
                      "-", "-", "-", "-" ] | @tsv' > "$_ns_t"
      awk -F'\t' 'NR==FNR { m[$1]=$0; next }
                  $2 == "PENDING" && ($1 in m) { print m[$1]; next } { print }' \
          "$_ns_t" "$_ns_u" > "$_ns_u.new" && mv "$_ns_u.new" "$_ns_u"
      rm -f "$_ns_t"
    fi
  fi

  kubectl get nodes -o json 2>/dev/null | jq -r --rawfile usage "$_ns_u" "$_NODES_QTY"'
    ( $usage | split("\n") | map(select(length > 0) | split("\t"))
      | map({key: .[0], value: {src: .[1], cpu: .[2], mem: .[3],
                                fsu: .[4], fsc: .[5], ino: .[6], img: .[7]}})
      | from_entries ) as $u
    | (["NODE","ROLES","SOURCE","CPU_ALLOC","CPU_USED","CPU_PCT","MEM_ALLOC_GiB","MEM_USED_GiB","MEM_PCT","EPH_CAP_GiB","EPH_USED_GiB","EPH_PCT","PRESSURE"] | @tsv),
      ( [ .items[] ]
        | sort_by([ (if ([.metadata.labels | keys[] | select(startswith("node-role.kubernetes.io/worker"))] | length) > 0 then 0 else 1 end), .metadata.name ])
        | .[]
        | . as $n
        | ($u[$n.metadata.name] // {src:"-", cpu:"-", mem:"-", fsu:"-", fsc:"-", ino:"-", img:"-"}) as $s
        | ($n.status.allocatable.cpu | qty) as $ca
        | ($n.status.allocatable.memory | qty) as $ma
        # capacity is Ki here, allocatable is bytes: parse, never compare as text
        | (($s.fsc | if . == "-" then null else tonumber end)
           // ($n.status.capacity["ephemeral-storage"] | qty)) as $ec
        | ($s.cpu | if . == "-" then null else tonumber end) as $cu
        | ($s.mem | if . == "-" then null else tonumber end) as $mu
        | ($s.fsu | if . == "-" then null else tonumber end) as $eu
        | [ $n.metadata.name,
            ([ $n.metadata.labels | keys[] | select(startswith("node-role.kubernetes.io/"))
               | sub("node-role.kubernetes.io/"; "") ] | join(",") | if . == "" then "-" else . end),
            $s.src,
            ($ca // "-"),
            (if $cu == null then "-" else ($cu * 100 | round / 100) end),
            (if ($cu != null and $ca) then ($cu / $ca * 1000 | round / 10) else "-" end),
            (if $ma then ($ma / 1073741824 * 100 | round / 100) else "-" end),
            (if $mu == null then "-" else ($mu / 1073741824 * 100 | round / 100) end),
            (if ($mu != null and $ma) then ($mu / $ma * 1000 | round / 10) else "-" end),
            (if $ec then ($ec / 1073741824 * 100 | round / 100) else "-" end),
            (if $eu == null then "-" else ($eu / 1073741824 * 100 | round / 100) end),
            (if ($eu != null and $ec) then ($eu / $ec * 1000 | round / 10) else "-" end),
            ([ $n.status.conditions[]? | select((.type | endswith("Pressure")) and .status == "True") | .type ]
             | join(",") | if . == "" then "-" else . end)
          ] | @tsv )'
  rm -f "$_ns_u"
}

# node_totals   cluster headroom in one line, workers only by default.
# Masters are normally tainted NoSchedule, so counting their cores as datamover capacity
# overstates headroom. Pass "all" to include every node.
node_totals() {
  _nt_all=${1:-workers}
  node_snapshot | awk -F'\t' -v mode="$_nt_all" '
    NR == 1 { next }
    {
      if (mode != "all" && $2 !~ /worker/) next
      n++
      if ($4 != "-") ca += $4
      if ($5 != "-") cu += $5
      if ($7 != "-") ma += $7
      if ($8 != "-") mu += $8
      if ($10 != "-") ec += $10
      if ($11 != "-") eu += $11
    }
    END {
      printf "nodes counted     : %d (%s)\n", n, mode
      printf "cpu               : %.2f of %.2f cores allocatable in use (%.1f%%)\n", cu, ca, (ca ? cu/ca*100 : 0)
      printf "memory            : %.1f of %.1f GiB allocatable in use (%.1f%%)\n", mu, ma, (ma ? mu/ma*100 : 0)
      printf "ephemeral storage : %.1f of %.1f GiB in use (%.1f%%)\n", eu, ec, (ec ? eu/ec*100 : 0)
      printf "headroom          : %.2f cores, %.1f GiB RAM, %.1f GiB ephemeral\n", ca-cu, ma-mu, ec-eu
    }'
}
