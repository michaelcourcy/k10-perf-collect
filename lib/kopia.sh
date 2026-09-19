# kopia.sh - read guide 12's inventory.json into per-PVC rows.  Sourced by init.sh.
#
# One Kopia Data repository holds MANY PVCs. They are told apart by Source.host:
#
#   <applicationID>.<workloadName>.<pvcName>
#
#   626027b6-....mastodon-postgresql.data-mastodon-postgresql-0
#   f7c34364-....basic-app-deployment.basic-app-pvc-2026-07-21-08-24-17
#   f7c34364-.....basic-app-pvc            <- empty workload, hence the double dot
#
# applicationID is constant per namespace and is NOT the RepositoryID.
#
# Splitting on "." and taking the last field is what the obvious parse does, and it is
# wrong: dots are legal in a PVC name (RFC 1123 subdomain), so "data.with.dots" would
# come back as "dots". Snapshots carry RestorePointNamespace, so the PVC is resolved by
# matching the host against the real PVC names in that namespace - longest match wins,
# which also disambiguates a PVC whose name is a suffix of another.
#
#   kopia_inventory_pvcs [inventory.json]   TSV: namespace, workload, pvc, snapshot, bytes
#   kopia_pvc_growth     [inventory.json]   TSV: per-PVC logical growth between snapshots

_kopia_inventory_file() {
  if [ -n "${1:-}" ]; then printf '%s' "$1"; return 0; fi
  printf '%s' "${AUDIT_DIR:-.}/12-kopia/inventory.json"
}

kopia_inventory_pvcs() {
  _ki_f=$(_kopia_inventory_file "${1:-}")
  if [ ! -f "$_ki_f" ]; then
    echo "kopia_inventory_pvcs: $_ki_f not found - run guide 12 step 1 first" >&2
    return 1
  fi
  _ki_pvc=$(kubectl get pvc -A -o json 2>/dev/null \
            | jq -c '[.items[] | {ns: .metadata.namespace, name: .metadata.name}]')
  [ -z "$_ki_pvc" ] && _ki_pvc='[]'

  jq -r --argjson pvcs "$_ki_pvc" '
    # PVC names in a namespace, longest first so the longest suffix wins. That matters
    # when one PVC name is a suffix of another.
    def names($ns): [ $pvcs[] | select(.ns == $ns) | .name ] | sort_by(-(. | length));

    # The PVC is the longest known name the host ends with, after a ".".
    # $n must be bound BEFORE the pipe: inside "$host | endswith(...)" the "." refers to
    # $host, not to the name being tested.
    def resolve($host; $ns):
      ( [ names($ns)[] as $n | select($host | endswith("." + $n)) | $n ] | first ) // null;

    (["NAMESPACE","WORKLOAD","PVC","SNAPSHOT_TIME","SIZE_BYTES","RESOLVED","SOURCE_HOST"] | @tsv),
    ( .repositories[]
      | select(.Type == "Data")
      | .Snapshots[]?
      | . as $s
      | ($s.RestorePointNamespace // "") as $ns
      | ($s.Source.host) as $h
      | (resolve($h; $ns)) as $pvc
      | ( if $pvc != null then
            # verified against a live PVC of that name
            { pvc: $pvc,
              workload: ( $h | rtrimstr("." + $pvc) | sub("^[^.]*\\.?"; "") ),
              how: "live-pvc" }
          else
            # No live PVC matches - deleted since the snapshot, or a different cluster.
            # Fall back to structure: drop the leading application ID, then the first
            # remaining field is the workload and everything after it is the PVC.
            ( $h | sub("^[^.]*\\."; "") ) as $rest
            | { pvc: ( $rest | sub("^[^.]*\\."; "") ),
                workload: ( $rest | split(".") | .[0] ),
                how: "structural" }
          end ) as $p
      | [ $ns, ($p.workload | if . == "" then "-" else . end), $p.pvc,
          $s.SnapshotTime, $s.SizeBytes, $p.how, $h ] | @tsv )'  "$_ki_f"
}

# kopia_pvc_growth   net logical growth per PVC between consecutive snapshots.
# SizeBytes is the logical size of the source at snapshot time, so the delta is NET
# growth - not churn, and not physical ingest. See guide 06.
kopia_pvc_growth() {
  # Inventory order is newest-first, so sort by (namespace, pvc, time) ascending before
  # differencing - otherwise every delta comes out negated.
  kopia_inventory_pvcs "${1:-}" \
    | awk -F'\t' 'NR > 1 { print $1 "\t" $3 "\t" $4 "\t" $5 }' \
    | sort -t'	' -k1,1 -k2,2 -k3,3 \
    | awk -F'\t' '
        { key = $1 "\t" $2
          if (key in last_t) {
            d = $4 - last_b[key]
            printf "%s\t%s\t%s\t%s\t%d\t%.2f\n", $1, $2, last_t[key], $3, d, d / 1048576
          }
          last_t[key] = $3; last_b[key] = $4 }' \
    | { printf 'NAMESPACE\tPVC\tFROM\tTO\tDELTA_BYTES\tDELTA_MiB\n'; cat; }
}
