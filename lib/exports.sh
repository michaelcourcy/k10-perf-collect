# exports.sh - the ExportActions of the pinned pair, with K10's own byte counters.
# Sourced by init.sh. Used by guides 06 and 13.
#
# WHERE THE BYTES ARE. A per-application ExportAction lives in the APPLICATION
# namespace (name scheduled-*, labels k10.kasten.io/policyName, exportProfile,
# runActionName). The ExportAction in the K10 namespace is the policy run's METADATA
# export (isMetadataExport=true) and moves no volume data.
#
# On the object itself, status.progressDetails and status.actionDetails are null - which
# is what makes it look as though K10 records no byte counts. They are populated only on
# the /details subresource:
#
#   GET /apis/actions.kio.kasten.io/v1alpha1/namespaces/<ns>/exportactions/<name>/details
#
# Verified on the reference cluster: the object reported progressDetails null while
# /details reported transferredBytes 10,300,000,000 for the same action - within 0.6 %
# of the 10,243,352,516 bytes Kopia recorded as physically ingested in that window.
#
# It costs about 0.2-0.8 s and ~400 kB per action, so fetch details for the exports you
# are analysing, not for the whole history.
#
#   export_actions            the pair's ExportActions, newest first, no details fetch
#   export_details <name>     progress counters and per-volume operations for one action
#   export_table [N]          export_actions + details + change rate for the newest N
#   policy_runs               the policy's metadata exports in the K10 namespace

_exports_require_focus() {
  if [ -z "${AUDIT_NS:-}" ] || [ -z "${AUDIT_POLICY:-}" ]; then
    echo "${1:-export_actions}: no focus pinned - run 'audit_focus <namespace> <policy>' first" >&2
    return 1
  fi
}

# export_actions   one row per ExportAction of AUDIT_POLICY in AUDIT_NS.
export_actions() {
  _exports_require_focus export_actions || return 1
  kubectl -n "$AUDIT_NS" get exportactions.actions.kio.kasten.io -o json 2>/dev/null \
    | jq -r --arg pol "$AUDIT_POLICY" '
        def ts: if . == null then null else (sub("\\.[0-9]+(?=Z$)"; "") | fromdateiso8601) end;
        (["EXPORT","STATE","START","END","DUR_S","RUN_ACTION","PROFILE","RUN_NOW"] | @tsv),
        ( [ .items[]
            | select((.metadata.labels["k10.kasten.io/policyName"] // "") == $pol)
            | select((.metadata.labels["k10.kasten.io/isMetadataExport"] // "") != "true") ]
          | sort_by(.status.startTime // "") | reverse | .[]
          | [ .metadata.name,
              (.status.state // "-"),
              ((.status.startTime // "-")[0:19]),
              ((.status.endTime // "-")[0:19]),
              (if (.status.startTime != null and .status.endTime != null)
               then (((.status.endTime | ts) - (.status.startTime | ts)) | tostring)
               else "-" end),
              (.metadata.labels["k10.kasten.io/runActionName"] // "-"),
              (.metadata.labels["k10.kasten.io/exportProfile"] // "-"),
              (.metadata.labels["k10.kasten.io/isRunNow"] // "false") ] | @tsv )'
}

# export_details <name>   the byte counters and per-volume operations of one export.
export_details() {
  _exports_require_focus export_details || return 1
  if [ -z "${1:-}" ]; then
    echo "export_details: usage: export_details <exportaction-name>" >&2
    return 1
  fi
  kubectl get --raw \
    "/apis/actions.kio.kasten.io/v1alpha1/namespaces/$AUDIT_NS/exportactions/$1/details" 2>/dev/null
}

# export_table [N]   the headline table: what each of the newest N exports moved.
#
#   TRANSFERRED  K10's own transferredBytes: what left the cluster for the object store
#   READ         bytes read from the source snapshot
#   CAPACITY     progressDetails.totalBytes - the VOLUME CAPACITY, not the data size
#   RATE         K10's processingRate, bytes/s
#
# CHANGE_RATE is transferred / the logical size of the PVC snapshots this export took,
# so 1.0 is a first export with nothing deduplicated or compressed. It can exceed 1.0 on
# incompressible small files, because transferredBytes includes Kopia's directory
# entries and per-block encryption overhead. It is the best single figure available, not
# a measured change rate - and when it looks wrong, check the workload before the tool.
# The denominator needs kopia-snapshots.json from guide 12 step 4; without it the column
# reads "-".
export_table() {
  _exports_require_focus export_table || return 1
  _et_n=${1:-10}
  _et_snaps=${2:-kopia-snapshots.json}

  _et_src='[]'
  if [ -f "$_et_snaps" ]; then
    _et_src=$(jq -c '
      def ts: if . == null then null else (sub("\\.[0-9]+(?=Z$)"; "") | fromdateiso8601) end;
      [ .[] | select(.incomplete == null)
        | { t: (.startTime | ts),
            bytes: ((.rootEntry.summ.fileSize) // (.stats.totalFileSize) // 0) } ]' "$_et_snaps")
    [ -z "$_et_src" ] && _et_src='[]'
  fi

  printf 'EXPORT\tSTATE\tSTART\tDUR_S\tVOLUMES\tTRANSFERRED\tREAD\tCAPACITY\tRATE_B_S\tCHANGE_RATE\n'
  export_actions | awk -F'\t' 'NR > 1 { print $1 }' | head -n "$_et_n" | while read -r _et_a; do
    [ -z "$_et_a" ] && continue
    export_details "$_et_a" \
      | jq -r --arg name "$_et_a" --argjson src "$_et_src" '
          def ts: if . == null then null else (sub("\\.[0-9]+(?=Z$)"; "") | fromdateiso8601) end;
          . as $d
          | ($d.status.progressDetails // {}) as $p
          | ($d.status.startTime | ts) as $lo
          | ($d.status.endTime   | ts) as $hi
          # the logical size of every PVC snapshot taken inside this export window
          | ( if ($lo == null or $hi == null) then 0
              else ([ $src[] | select(.t >= ($lo - 5) and .t <= ($hi + 5)) | .bytes ] | add // 0)
              end ) as $srcbytes
          | [ $name,
              ($d.status.state // "-"),
              (($d.status.startTime // "-")[0:19]),
              (if ($lo != null and $hi != null) then (($hi - $lo) | tostring) else "-" end),
              ($p.totalVolumes // "-"),
              ($p.transferredBytes // "-"),
              ($p.readBytes // "-"),
              ($p.totalBytes // "-"),
              ($p.processingRate // "-"),
              (if (($p.transferredBytes // null) != null and $srcbytes > 0)
               then (($p.transferredBytes / $srcbytes) * 10000 | round / 10000)
               else "-" end) ] | @tsv'
  done
}

# export_volumes <name>   per-volume operations of one export: which PVC, which data
# format, which CSI snapshot. This is the only place the export names its PVCs - the
# copy-vol-data pod mounts an ephemeral clone, so its spec never reveals the source.
export_volumes() {
  _exports_require_focus export_volumes || return 1
  if [ -z "${1:-}" ]; then
    echo "export_volumes: usage: export_volumes <exportaction-name>" >&2
    return 1
  fi
  export_details "$1" | jq -r '
    (["PVC","OPERATION","DATA_FORMAT","EXPORT_DIRECTIVE","STORAGE_CLASS","STORAGE_TYPE","SNAPSHOT_ID"] | @tsv),
    ( [ .status.actionDetails.phases[]? | .volumeOperations[]? ] | .[]
      | [ (.pvcName // "-"), (.operation // "-"), (.dataFormat // "-"),
          (.exportDirective // "-"), (.storageClass // "-"),
          (.storageType // "-"), (.snapshotId // "-") ] | @tsv )'
}

# policy_runs   the policy's runs, from the metadata ExportActions in the K10 namespace.
# These carry no volume bytes; they are the cadence evidence for guide 02.
policy_runs() {
  _exports_require_focus policy_runs || return 1
  kubectl -n "${K10NS:-kasten-io}" get exportactions.actions.kio.kasten.io -o json 2>/dev/null \
    | jq -r --arg pol "$AUDIT_POLICY" '
        def ts: if . == null then null else (sub("\\.[0-9]+(?=Z$)"; "") | fromdateiso8601) end;
        (["RUN_ACTION","STATE","START","END","DUR_S"] | @tsv),
        ( [ .items[]
            | select((.metadata.labels["k10.kasten.io/policyName"] // "") == $pol) ]
          | sort_by(.status.startTime // "") | reverse | .[]
          | [ (.metadata.labels["k10.kasten.io/runActionName"] // .metadata.name),
              (.status.state // "-"),
              ((.status.startTime // "-")[0:19]),
              ((.status.endTime // "-")[0:19]),
              (if (.status.startTime != null and .status.endTime != null)
               then (((.status.endTime | ts) - (.status.startTime | ts)) | tostring)
               else "-" end) ] | @tsv )'
}
