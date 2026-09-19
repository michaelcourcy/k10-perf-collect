#!/usr/bin/env sh
# thin wrapper so the documented entry point exists; all logic is in the .py
exec python3 "$(dirname "$0")/generate-export-topology.py" "$@"
