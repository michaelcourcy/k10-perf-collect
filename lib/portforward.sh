# portforward.sh - robust kubectl port-forward helpers.  Sourced by init.sh.
#
# pf_start <service> <local>:<remote>     start and wait until it really forwards
# pf_stop                                 stop the one started by pf_start
# pf_stop_all                             stop every forward this session started
#
# Why not "kubectl port-forward ... &" followed by "kill %1":
#   %1 is positional, so it points at the wrong job as soon as anything else is
#   backgrounded, and "no such job" only tells you the forward already died - with
#   the reason sitting unread in a log file.

pf_start() {
  _audit_ready pf_start || return 1
  if [ $# -ne 2 ]; then
    echo "usage: pf_start <service> <localport>:<remoteport>" >&2
    return 2
  fi
  _pf_port=${2%%:*}

  # Refuse an occupied port. Otherwise the readiness check below can pass against
  # somebody else's process and every later query silently reads the wrong service.
  if lsof -nP -iTCP:"$_pf_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "pf_start: port $_pf_port is already in use:" >&2
    lsof -nP -iTCP:"$_pf_port" -sTCP:LISTEN 2>/dev/null | tail -1 >&2
    echo "pf_start: free it, or pass a different local port" >&2
    return 1
  fi

  PF_LOG=$(mktemp)
  kubectl -n "${K10NS:-kasten-io}" port-forward "svc/$1" "$2" >"$PF_LOG" 2>&1 &
  PF_PID=$!
  PF_PIDS="${PF_PIDS:-} $PF_PID"

  _pf_n=0
  while [ "$_pf_n" -lt 40 ]; do
    # the readiness line kubectl itself prints - no HTTP semantics involved
    if grep -q "Forwarding from" "$PF_LOG" 2>/dev/null; then
      return 0
    fi
    if ! kill -0 "$PF_PID" 2>/dev/null; then
      echo "pf_start: forward to $1 exited:" >&2
      sed 's/^/  /' "$PF_LOG" >&2
      return 1
    fi
    _pf_n=$((_pf_n + 1))
    sleep 0.25
  done
  echo "pf_start: forward to $1 never became ready:" >&2
  sed 's/^/  /' "$PF_LOG" >&2
  pf_stop
  return 1
}

pf_stop() {
  if [ -n "${PF_PID:-}" ]; then
    kill "$PF_PID" 2>/dev/null
    wait "$PF_PID" 2>/dev/null
    PF_PID=""
  fi
}

pf_stop_all() {
  for _pf_p in ${PF_PIDS:-}; do
    kill "$_pf_p" 2>/dev/null
    wait "$_pf_p" 2>/dev/null
  done
  PF_PIDS=""
  PF_PID=""
}
