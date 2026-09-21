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

# ---------------------------------------------------------------------------
# Live repository reads, from the debug-kopia pod left behind by
# `k10_repo_checker.sh -o connect` (guide 12 step 4).
#
# These replace the hand-written jq that guides 04-06 used to carry, and they read
# the SAME fields as generate-export-topology.py, which is why the two agree:
#
#   file count / size   rootEntry.summ.files / summ.fileSize      the snapshot TREE
#   hashed / unchanged  stats.fileCount / stats.cachedFiles       work done in that run
#
# stats.fileCount is NOT the file count. It is the number of files Kopia hashed in that
# run, so it collapses to 0 on an unchanged volume: measured on the reference cluster, a
# 100,003-file PVC reported stats.fileCount 0 on two of five snapshots and 20,000 on two
# more. Dividing totalFileSize by it gives a mean file size that is wrong by 5x or
# infinite. Always take the count from rootEntry.summ.

# kopia_debug_pod   the pod `repo_checker -o connect` left running.
kopia_debug_pod() {
  if [ -n "${DEBUG_POD:-}" ]; then printf '%s\n' "$DEBUG_POD"; return 0; fi
  _kdp=$(kubectl -n "${K10NS:-kasten-io}" get pods -o name 2>/dev/null \
         | sed 's|^pod/||' | grep '^debug-kopia' | tail -1)
  if [ -z "$_kdp" ]; then
    echo "kopia_debug_pod: no debug-kopia pod - run guide 12 step 4 connect first" >&2
    return 1
  fi
  printf '%s\n' "$_kdp"
}

# kopia_exec <command...>   run a kopia command in the debug pod, stdout only.
#
# The command is passed as an ARGUMENT to sh -c, never on stdin: feeding it through
# `kubectl exec -i ... sh -s` intermittently truncates large stdout (a 100 kB
# `kopia ... --json` broke mid-document). Kopia writes log-dir noise to stderr, so
# stderr is dropped - redirect it yourself if you are debugging a failure.
kopia_exec() {
  _ke_pod=$(kopia_debug_pod) || return 1
  kubectl -n "${K10NS:-kasten-io}" exec "$_ke_pod" -- \
    sh -c "export KOPIA_CONFIG_PATH=/tmp/kopia-repository.config; $*" 2>/dev/null
}

# Shared jq preamble: PVC resolution and the block-mode test.
_KOPIA_JQ_DEFS='
  def ts: if . == null then null else (sub("\\.[0-9]+(?=Z$)"; "") | fromdateiso8601) end;

  # K10 stores block-mode volumes (KubeVirt disks) as a tree of fixed-size chunks:
  # summ.files is then a CHUNK count and a file histogram over it is meaningless.
  def mode: if ((.description // "") | startswith("volume:"))
               or (((.source.path) // "") | startswith("/volume/"))
            then "block" else "filesystem" end;

  # The tree, not the run. See the header of this file.
  def treefiles: (.rootEntry.summ.files) // (.stats.fileCount);
  def treesize:  (.rootEntry.summ.fileSize) // (.stats.totalFileSize);

  # Source.host is <applicationID>.<workload>.<pvc>. Dots are legal in a PVC name, so
  # splitting on "." and taking the last field is wrong. Match against the live PVC
  # names of the namespace, longest first; fall back to structure.
  def resolve($host; $pvcs):
    ( [ $pvcs[] | select($host | endswith("." + .)) ] | sort_by(-length) | first ) as $hit
    | if $hit != null then $hit
      else ($host | sub("^[^.]*\\."; "") | sub("^[^.]*\\."; "")) end;
'

# kopia_snapshots [snapshots.json]   one row per COMPLETE snapshot.
#
# Checkpoints are excluded. While a long export runs Kopia writes an incomplete
# manifest every 45 min so an interrupted upload can resume; `snapshot list --all`
# returns them with the same startTime as the running snapshot and
# incomplete: "checkpoint". They are not restore points - counting them is how one
# export becomes "3 snapshots with the same timestamp". kopia_checkpoints lists them.
kopia_snapshots() {
  _ks_f=${1:-kopia-snapshots.json}
  [ -f "$_ks_f" ] || { echo "kopia_snapshots: $_ks_f not found - guide 12 step 4" >&2; return 1; }
  _ks_pvcs=$(kubectl -n "${AUDIT_NS:?kopia_snapshots: AUDIT_NS not set}" get pvc -o json 2>/dev/null \
             | jq -c '[.items[].metadata.name]')
  [ -z "$_ks_pvcs" ] && _ks_pvcs='[]'
  jq -r --argjson pvcs "$_ks_pvcs" "$_KOPIA_JQ_DEFS"'
    (["PVC","MODE","START","END","DUR_S","FILES","SIZE_BYTES","HASHED","UNCHANGED","DIRS","D_FILES","D_BYTES","SNAPSHOT_ID"] | @tsv),
    ( map(select(.incomplete == null))
      | group_by(.source.host)
      | map(sort_by(.startTime))
      | .[]
      | . as $g
      | range(length) as $i
      | $g[$i] as $s
      | (if $i > 0 then $g[$i-1] else null end) as $p
      | [ resolve($s.source.host; $pvcs),
          ($s | mode),
          ($s.startTime[0:19]),
          ($s.endTime[0:19]),
          ((($s.endTime | ts) - ($s.startTime | ts)) | tostring),
          (($s | treefiles) // "-"),
          (($s | treesize)  // "-"),
          (if ($s | mode) == "block" then "-" else ($s.stats.fileCount   // "-") end),
          (if ($s | mode) == "block" then "-" else ($s.stats.cachedFiles // "-") end),
          ($s.stats.dirCount // "-"),
          (if $p == null then "-" else ((($s | treefiles) // 0) - (($p | treefiles) // 0)) end),
          (if $p == null then "-" else ((($s | treesize)  // 0) - (($p | treesize)  // 0)) end),
          $s.id ] | @tsv )' "$_ks_f"
}

# kopia_checkpoints [snapshots.json]   exports still running, from the repository side.
kopia_checkpoints() {
  _kc_f=${1:-kopia-snapshots.json}
  [ -f "$_kc_f" ] || { echo "kopia_checkpoints: $_kc_f not found" >&2; return 1; }
  jq -r "$_KOPIA_JQ_DEFS"'
    (["SOURCE_HOST","STARTED","LAST_CHECKPOINT","CHECKPOINTS","FILES_SO_FAR","BYTES_SO_FAR"] | @tsv),
    ( map(select(.incomplete != null))
      | group_by(.source.host) | .[]
      | sort_by(.endTime) | . as $g | last
      | [ .source.host, .startTime[0:19], .endTime[0:19], ($g | length),
          (.stats.fileCount // 0), (.stats.totalFileSize // 0) ] | @tsv )' "$_kc_f"
}

# kopia_pvcs [snapshots.json]   one row per PVC, from its most recent complete snapshot.
kopia_pvcs() {
  _kp_f=${1:-kopia-snapshots.json}
  [ -f "$_kp_f" ] || { echo "kopia_pvcs: $_kp_f not found - guide 12 step 4" >&2; return 1; }
  _kp_pvcs=$(kubectl -n "${AUDIT_NS:?kopia_pvcs: AUDIT_NS not set}" get pvc -o json 2>/dev/null \
             | jq -c '[.items[].metadata.name]')
  [ -z "$_kp_pvcs" ] && _kp_pvcs='[]'
  jq -r --argjson pvcs "$_kp_pvcs" "$_KOPIA_JQ_DEFS"'
    (["PVC","MODE","SNAPSHOTS","LAST_SNAPSHOT","FILES","SIZE_BYTES","AVG_FILE_BYTES","DIRS","ROOT_OBJECT_ID"] | @tsv),
    ( map(select(.incomplete == null))
      | group_by(.source.host) | .[]
      | (length) as $n | sort_by(.startTime) | last
      | (treefiles) as $f | (treesize) as $b
      | [ resolve(.source.host; $pvcs), mode, $n, .startTime[0:19],
          ($f // "-"), ($b // "-"),
          (if (mode == "block") then "-"
           elif (($f // 0) > 0) then (($b // 0) / $f | floor) else "-" end),
          (.stats.dirCount // "-"),
          .rootEntry.obj ] | @tsv )' "$_kp_f"
}

# kopia_ingest [snapshots.json] [contents.json]   PHYSICAL ingest per snapshot.
#
# Every content block Kopia writes carries its creation time, its logical size
# (originalLength) and its stored size (length). A block whose time falls inside a
# snapshot's window is attributed to it - this is what "how much actually reached the
# object store for this export" means, after dedup and compression.
#
# Two limits, both reported rather than hidden:
#   * Maintenance. full-rewrite-contents re-stamps every rewritten content with the
#     maintenance time, so snapshots older than the last full run report UNRECOVERABLE,
#     never 0. Guide 06 explains why that distinction matters.
#   * Overlap. The disks of one VM, and the PVCs of one namespace, are exported at the
#     same time, so windows overlap and a block can fall in several. Such bytes are
#     counted in every window they fall in and flagged AMBIGUOUS.
kopia_ingest() {
  _ki_s=${1:-kopia-snapshots.json}
  _ki_c=${2:-contents.json}
  _ki_m=${3:-kopia-maintenance-info.json}
  for _f in "$_ki_s" "$_ki_c"; do
    [ -f "$_f" ] || { echo "kopia_ingest: $_f not found - guide 12 step 4" >&2; return 1; }
  done
  _ki_rw=null
  if [ -f "$_ki_m" ]; then
    _ki_rw=$(jq -r '(.schedule.runs["full-rewrite-contents"] // []) | last
                    | if . == null then "null"
                      else (.end | sub("\\.[0-9]+(?=Z$)"; "") | fromdateiso8601 | tostring) end' \
                   "$_ki_m" 2>/dev/null)
    [ -z "$_ki_rw" ] && _ki_rw=null
  fi
  _ki_pvcs=$(kubectl -n "${AUDIT_NS:?kopia_ingest: AUDIT_NS not set}" get pvc -o json 2>/dev/null \
             | jq -c '[.items[].metadata.name]')
  [ -z "$_ki_pvcs" ] && _ki_pvcs='[]'

  jq -r --slurpfile contents "$_ki_c" --argjson pvcs "$_ki_pvcs" \
        --argjson rewrite "$_ki_rw" "$_KOPIA_JQ_DEFS"'
    # Windows are padded by 1 s: content timestamps are whole seconds and an export
    # writes its last block a moment after the manifest is closed.
    ( map(select(.incomplete == null))
      | map({ pvc: resolve(.source.host; $pvcs), id: .id,
              start: (.startTime[0:19]),
              lo: ((.startTime | ts) - 1), hi: ((.endTime | ts) + 1),
              mid: (((.startTime | ts) + (.endTime | ts)) / 2) })
      | sort_by(.lo) ) as $w
    | ( [ $contents[0][] | select(.deleted == false) ] ) as $c

    # Each block goes to exactly ONE snapshot - the nearest window by midpoint.
    # Summing per window instead double counts: the PVCs of one namespace are exported
    # concurrently, so their windows overlap (the five mastodon snapshots summed to
    # 2.3x the repository). Blocks that fell in more than one window are still counted
    # once, and their bytes are also reported as AMBIGUOUS so the guess is visible.
    | ( reduce $c[] as $x ({};
          ( [ $w[] | select($x.time >= .lo and $x.time <= .hi) ] ) as $h
          | ( if ($h | length) == 0 then "_unassigned"
              else ($h | min_by((($x.time) - .mid) | length) | .id) end ) as $k
          | .[$k].phys = ((.[$k].phys // 0) + $x.length)
          | .[$k].log  = ((.[$k].log  // 0) + ($x.originalLength // $x.length))
          | .[$k].n    = ((.[$k].n    // 0) + 1)
          | .[$k].amb  = ((.[$k].amb  // 0)
                          + (if ($h | length) > 1 then $x.length else 0 end))
        ) ) as $a

    | (["PVC","SNAPSHOT_START","PHYSICAL_BYTES","LOGICAL_BYTES","CONTENTS","RATIO","AMBIGUOUS_BYTES","NOTE"] | @tsv),
      ( $w[]
        | . as $s
        | ($a[$s.id] // {phys:0, log:0, n:0, amb:0}) as $v
        | if ($rewrite != null and $s.hi < $rewrite)
          then [ $s.pvc, $s.start, "-", "-", "-", "-", "-",
                 "UNRECOVERABLE: full maintenance re-stamped every content written before it" ]
          else
            [ $s.pvc, $s.start, $v.phys, $v.log, $v.n,
              (if $v.log > 0 then ($v.phys / $v.log * 1000 | round / 1000) else "-" end),
              $v.amb,
              (if $v.amb > 0
               then "AMBIGUOUS: another PVC of this namespace was exported at the same time"
               elif $v.n == 0 then "no content block carries a timestamp in this window"
               else "-" end) ]
          end
        | @tsv ),
      ( ($a["_unassigned"] // {phys:0, log:0, n:0, amb:0}) as $u
        | select($u.n > 0)
        | [ "(unattributed)", "-", $u.phys, $u.log, $u.n, "-", 0,
            "written outside every snapshot window: maintenance, or an export whose snapshot is gone" ]
        | @tsv )' "$_ki_s"
}

# kopia_histogram <tree-listing>   file-size distribution of a snapshot tree.
#
# The listing comes from `kopia ls -l -r <rootObjectId>` in the debug pod, which
# enumerates exactly summ.files regular files as
#   mode size date time UTC objectID path
# so the distribution costs one repository read and never touches the PVC. That is the
# whole point: guide 05 used to need a second full walk of the live volume for this.
kopia_histogram() {
  [ -f "${1:-}" ] || { echo "kopia_histogram: usage: kopia_histogram <tree-listing>" >&2; return 1; }
  awk '/^-/ {
         n = $2 + 0
         if      (n < 4096)      b = "<4KiB"
         else if (n < 65536)     b = "4KiB-64KiB"
         else if (n < 1048576)   b = "64KiB-1MiB"
         else if (n < 16777216)  b = "1MiB-16MiB"
         else if (n < 268435456) b = "16MiB-256MiB"
         else                    b = ">256MiB"
         c[b]++; s[b] += n; t++; tot += n
         if (t == 1 || n < mn) mn = n
         if (n > mx) mx = n
       }
       END {
         printf "BUCKET\tFILES\tBYTES\tPCT_FILES\n"
         nb = split("<4KiB 4KiB-64KiB 64KiB-1MiB 1MiB-16MiB 16MiB-256MiB >256MiB", o, " ")
         for (i = 1; i <= nb; i++) {
           k = o[i]
           printf "%s\t%d\t%d\t%.1f\n", k, c[k] + 0, s[k] + 0, (t ? c[k] * 100.0 / t : 0)
         }
         printf "TOTAL\t%d\t%d\t100.0\n", t, tot
         printf "min=%d max=%d mean=%d\n", mn + 0, mx + 0, (t ? tot / t : 0)
       }' "$1"
}
