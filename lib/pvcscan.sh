# pvcscan.sh - measure file count and size on PVCs the kubelet cannot report.
# Sourced by init.sh.  Used by guides 04 and 05.
#
# WHY: for a network-backed volume (NFS, SMB, Azure Files, EFS, CephFS) the kubelet
# runs statfs on the mount point, which returns the statistics of the whole remote
# export - not of the subdirectory backing this PVC. Every PVC served by the same
# server reports an identical, meaningless figure. df -i inside the pod has the same
# flaw, for the same reason. The only way to get a real count is to walk the tree.
#
# STRATEGY, per PVC:
#   1. a Running pod already mounts it  -> exec there, no new workload
#   2. nothing mounts it                -> create a read-only inspector pod
#   3. RWO and attached elsewhere       -> skip, with the reason recorded
#
#   pvc_scan_network [namespace ...]   scan every network-backed PVC (default: all)
#   pvc_scan_one <namespace> <pvc>     scan a single PVC
#
# Tunables (export before calling):
#   PVCSCAN_TIMEOUT=300      seconds allowed per PVC for the tree walk
#   PVCSCAN_IMAGE=...        inspector image (default UBI 9 minimal)
#   PVCSCAN_KEEP_POD=1       leave the inspector pod behind for debugging
#   PVCSCAN_HISTOGRAM=1      also emit a file-size histogram per PVC

PVCSCAN_TIMEOUT=${PVCSCAN_TIMEOUT:-300}
PVCSCAN_IMAGE=${PVCSCAN_IMAGE:-registry.access.redhat.com/ubi9/ubi-minimal:latest}

# StorageClasses whose provisioner is a network/shared filesystem. Azure Files is
# file.csi.azure.com, which a match on the string "azurefile" alone would miss.
_pvcscan_network_classes() {
  kubectl get sc -o json 2>/dev/null | jq -r '
    .items[]
    | select(.provisioner | test("nfs|smb|cifs|file\\.csi|azurefile|efs|filestore|gluster|cephfs"; "i"))
    | .metadata.name'
}

# TSV: namespace, pvc, storageclass, accessmode
_pvcscan_network_pvcs() {
  _pn_sc=$(_pvcscan_network_classes | sort -u)
  [ -z "$_pn_sc" ] && return 0
  kubectl get pvc -A -o json 2>/dev/null | jq -r --arg sc "$_pn_sc" '
    ($sc | split("\n") | map(select(length > 0))) as $net
    | .items[]
    | select((.spec.storageClassName // "none") | IN($net[]))
    | [ .metadata.namespace, .metadata.name,
        (.spec.storageClassName // "none"),
        (.spec.accessModes[0] // "-") ] | @tsv'
}

# First usable Running pod that mounts the PVC at its ROOT.
# TSV: pod, container, mountPath   (empty if there is none)
#
# Two exclusions matter:
#   - our own inspector pods, which may be Terminating from an earlier run;
#   - subPath mounts, which expose only a subdirectory of the PVC. Measuring one
#     under-reports the volume, so such a pod is not usable and we fall back to an
#     inspector that mounts the PVC root.
_pvcscan_mounting_pod() {
  kubectl -n "$1" get pods -o json 2>/dev/null | jq -r --arg pvc "$2" '
    [ .items[]
      | select(.status.phase == "Running")
      | select(.metadata.deletionTimestamp == null)
      | select(.metadata.labels["app.kubernetes.io/name"] != "k10-audit-inspector")
      | . as $p
      | ($p.spec.volumes[]? | select(.persistentVolumeClaim.claimName == $pvc) | .name) as $vol
      | $p.spec.containers[]
      | . as $c
      | ($c.volumeMounts[]? | select(.name == $vol))
      | select(has("subPath") | not)
      | select(has("subPathExpr") | not)
      | [ $p.metadata.name, $c.name, .mountPath ]
    ] | first // [] | @tsv'
}

# Does the container have the tools the walk needs? A missing "find" makes
# "find ... | wc -l" return 0, which is a silently WRONG answer rather than an error.
_pvcscan_has_tools() {
  kubectl -n "$1" exec "$2" -c "$3" -- sh -c \
    'command -v find >/dev/null 2>&1 && command -v du >/dev/null 2>&1 && echo ok' 2>/dev/null \
    | grep -q ok
}

# The measurement, run inside whatever pod we ended up with. POSIX/BusyBox safe:
# no find -printf (BusyBox lacks it), no bashisms.
_pvcscan_remote_script() {
  cat <<'INNER'
MP="$1"; TO="$2"; HIST="$3"
if command -v timeout >/dev/null 2>&1; then TP="timeout $TO"; else TP=""; fi
FILES=$($TP find "$MP" -xdev -type f 2>/dev/null | wc -l); FRC=$?
DIRS=$($TP find "$MP" -xdev -type d 2>/dev/null | wc -l)
LINKS=$($TP find "$MP" -xdev -type l 2>/dev/null | wc -l)
KB=$($TP du -sk "$MP" 2>/dev/null | awk '{print $1}')
# df wraps a long device name (NFS exports always are) onto a second line, so NR==2
# points at the wrapped remainder. Count back from the end of the LAST line instead:
# for df -i the columns are Filesystem Inodes IUsed IFree IUse% Mounted, so IUsed is
# NF-3; for df -k, Used is likewise NF-3.
SI=$(df -i "$MP" 2>/dev/null | awk 'END{print $(NF-3)}')
SB=$(df -k "$MP" 2>/dev/null | awk 'END{print $(NF-3)}')
[ -z "$KB" ] && KB=0
[ -z "$FILES" ] && FILES=0
printf 'files=%s\ndirs=%s\nlinks=%s\nused_kib=%s\nstatfs_inodes=%s\nstatfs_used_kib=%s\nrc=%s\n' \
  "$FILES" "$DIRS" "$LINKS" "$KB" "${SI:-0}" "${SB:-0}" "$FRC"
if [ "$HIST" = "1" ] && [ "$FILES" -gt 0 ]; then
  echo "histogram_begin"
  $TP find "$MP" -xdev -type f -exec stat -c '%s' {} + 2>/dev/null | awk '
    { n++
      if      ($1 <       4096) b["1_lt4KiB"]++
      else if ($1 <      65536) b["2_4KiB_64KiB"]++
      else if ($1 <    1048576) b["3_64KiB_1MiB"]++
      else if ($1 <   16777216) b["4_1MiB_16MiB"]++
      else if ($1 <  268435456) b["5_16MiB_256MiB"]++
      else                       b["6_gt256MiB"]++ }
    END { for (k in b) printf "%s=%d\n", k, b[k] }' | sort
  echo "histogram_end"
fi
INNER
}

_pvcscan_run_in_pod() {
  # $1 ns  $2 pod  $3 container  $4 mountPath
  _pvcscan_remote_script \
    | kubectl -n "$1" exec -i "$2" -c "$3" -- sh -s -- \
        "$4" "$PVCSCAN_TIMEOUT" "${PVCSCAN_HISTOGRAM:-0}" 2>/dev/null
}

_pvcscan_inspector_yaml() {
  # $1 ns  $2 pvc  $3 pod name
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $3
  namespace: $1
  labels:
    app.kubernetes.io/name: k10-audit-inspector
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    seccompProfile: {type: RuntimeDefault}
  containers:
  - name: inspector
    image: $PVCSCAN_IMAGE
    command: ["sleep", "3600"]
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: ["ALL"]}
    volumeMounts:
    - {name: target, mountPath: /target, readOnly: true}
  volumes:
  - name: target
    persistentVolumeClaim: {claimName: $2, readOnly: true}
YAML
}

# pvc_scan_one <namespace> <pvc> [storageclass] [accessmode]
# Emits one TSV row. Progress and errors go to stderr, so stdout stays parseable.
pvc_scan_one() {
  _ps_ns=$1; _ps_pvc=$2; _ps_sc=${3:-?}; _ps_am=${4:-?}
  _audit_ready pvc_scan_one || return 1

  _ps_method=""; _ps_note=""; _ps_out=""
  set -- $(_pvcscan_mounting_pod "$_ps_ns" "$_ps_pvc")
  _ps_pod=${1:-}; _ps_ctr=${2:-}; _ps_mp=${3:-}

  if [ -n "$_ps_pod" ] && [ -n "$_ps_mp" ] && _pvcscan_has_tools "$_ps_ns" "$_ps_pod" "$_ps_ctr"; then
    echo "  $_ps_ns/$_ps_pvc: exec in $_ps_pod ($_ps_mp)" >&2
    _ps_method="pod-exec"
    _ps_out=$(_pvcscan_run_in_pod "$_ps_ns" "$_ps_pod" "$_ps_ctr" "$_ps_mp")
    [ -z "$_ps_out" ] && { _ps_method="failed"; _ps_note="exec produced no output"; }
  else
    [ -n "$_ps_pod" ] && echo "  $_ps_ns/$_ps_pvc: $_ps_pod lacks find/du, using an inspector instead" >&2
    case "$_ps_am" in
      ReadWriteOnce)
        if kubectl -n "$_ps_ns" get pvc "$_ps_pvc" -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Bound &&
           [ -n "$(kubectl get volumeattachment -o json 2>/dev/null \
                   | jq -r --arg pv "$(kubectl -n "$_ps_ns" get pvc "$_ps_pvc" -o jsonpath='{.spec.volumeName}' 2>/dev/null)" \
                     '.items[] | select(.spec.source.persistentVolumeName == $pv and .status.attached) | .metadata.name')" ]; then
          echo "  $_ps_ns/$_ps_pvc: SKIP - RWO and still attached to a node" >&2
          _ps_method="skipped"; _ps_note="RWO attached elsewhere"
        fi ;;
    esac
    if [ -z "$_ps_method" ]; then
      # DNS-1123 label: <=63 chars, lowercase alphanumeric and dashes, no trailing dash
      _ps_ipod=$(printf 'k10audit-%s-%s' "$(echo "$_ps_pvc" | tr -c 'a-z0-9' '-' | cut -c1-40)" "$$" \
                 | tr -s '-' | sed 's/-*$//' | cut -c1-63)
      echo "  $_ps_ns/$_ps_pvc: no pod mounts it, creating $_ps_ipod" >&2
      if _pvcscan_inspector_yaml "$_ps_ns" "$_ps_pvc" "$_ps_ipod" | kubectl apply -f - >/dev/null 2>&1 &&
         kubectl -n "$_ps_ns" wait --for=condition=Ready "pod/$_ps_ipod" --timeout=180s >/dev/null 2>&1; then
        _ps_method="inspector"
        _ps_out=$(_pvcscan_run_in_pod "$_ps_ns" "$_ps_ipod" inspector /target)
      else
        _ps_method="failed"
        _ps_note=$(kubectl -n "$_ps_ns" get pod "$_ps_ipod" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}' 2>/dev/null)
        [ -z "$_ps_note" ] && _ps_note="inspector pod did not become Ready"
        echo "  $_ps_ns/$_ps_pvc: FAILED - $_ps_note" >&2
      fi
      [ "${PVCSCAN_KEEP_POD:-0}" = "1" ] || kubectl -n "$_ps_ns" delete pod "$_ps_ipod" --wait=false >/dev/null 2>&1
    fi
  fi

  _ps_f=$(printf '%s\n' "$_ps_out" | sed -n 's/^files=//p')
  _ps_d=$(printf '%s\n' "$_ps_out" | sed -n 's/^dirs=//p')
  _ps_l=$(printf '%s\n' "$_ps_out" | sed -n 's/^links=//p')
  _ps_kb=$(printf '%s\n' "$_ps_out" | sed -n 's/^used_kib=//p')
  _ps_si=$(printf '%s\n' "$_ps_out" | sed -n 's/^statfs_inodes=//p')
  _ps_rc=$(printf '%s\n' "$_ps_out" | sed -n 's/^rc=//p')
  [ "${_ps_rc:-0}" = "124" ] && _ps_note="tree walk timed out after ${PVCSCAN_TIMEOUT}s - figures are a lower bound"

  _ps_avg=$(awk -v kb="${_ps_kb:-0}" -v n="${_ps_f:-0}" 'BEGIN{printf "%.0f", (n>0 ? kb*1024/n : 0)}')

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$_ps_ns" "$_ps_pvc" "$_ps_sc" "$_ps_am" "$_ps_method" \
    "${_ps_f:-}" "${_ps_d:-}" "${_ps_l:-}" "${_ps_kb:-}" "$_ps_avg" \
    "${_ps_si:-}${_ps_note:+ ${_ps_note}}"

  if [ "${PVCSCAN_HISTOGRAM:-0}" = "1" ]; then
    printf '%s\n' "$_ps_out" | sed -n '/^histogram_begin$/,/^histogram_end$/p' \
      | sed '1d;$d' | sed "s|^|$_ps_ns/$_ps_pvc\t|" >> "${PVCSCAN_HIST_FILE:-/dev/null}"
  fi
}

# pvc_scan_network [namespace ...]
pvc_scan_network() {
  _audit_ready pvc_scan_network || return 1

  # Inspector pods left by an interrupted run still mount their PVC and would be
  # picked up as "a pod already mounts this", so clear them first.
  if [ -n "$(kubectl get pods -A -l app.kubernetes.io/name=k10-audit-inspector \
             -o name 2>/dev/null)" ]; then
    echo "pvc_scan_network: removing inspector pods left by an earlier run" >&2
    kubectl delete pods -A -l app.kubernetes.io/name=k10-audit-inspector \
      --wait=true --timeout=120s >/dev/null 2>&1
  fi

  _pn_tmp=$(mktemp)
  _pvcscan_network_pvcs > "$_pn_tmp"
  if [ $# -gt 0 ]; then
    _pn_filter=$(mktemp); printf '%s\n' "$@" | tr ' ' '\n' > "$_pn_filter"
    awk -F'\t' 'NR==FNR{ok[$1];next} ($1 in ok)' "$_pn_filter" "$_pn_tmp" > "$_pn_tmp.f"
    mv "$_pn_tmp.f" "$_pn_tmp"; rm -f "$_pn_filter"
  fi
  _pn_total=$(grep -c . "$_pn_tmp" 2>/dev/null || echo 0)
  echo "pvc_scan_network: $_pn_total network-backed PVC(s) to walk, ${PVCSCAN_TIMEOUT}s each at most" >&2

  printf 'NAMESPACE\tPVC\tSTORAGECLASS\tACCESS\tMETHOD\tFILES\tDIRS\tLINKS\tUSED_KIB\tAVG_FILE_BYTES\tSTATFS_INODES_AND_NOTES\n'
  _pn_i=0
  while IFS='	' read -r _pn_ns _pn_pvc _pn_sc _pn_am; do
    [ -z "$_pn_ns" ] && continue
    _pn_i=$((_pn_i + 1))
    echo "[$_pn_i/$_pn_total]" >&2
    pvc_scan_one "$_pn_ns" "$_pn_pvc" "$_pn_sc" "$_pn_am"
  done < "$_pn_tmp"
  rm -f "$_pn_tmp"
  echo "pvc_scan_network: done" >&2
}
