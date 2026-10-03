#!/usr/bin/env bash
# Launcher wrapper for Claude Desktop (macOS/Linux). Creates .venv on first run, then starts the server.
# Claude Desktop config:
#   "nearby-events": { "command": "/ABSOLUTE/PATH/nearby-events-mcp/run_server.sh",
#                   "env": { "NEARBY_EVENTS_DEFAULT_LOCATION": "Orchard Road, Singapore" } }
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  PY="$(command -v python3 || command -v python)"
  "$PY" -m venv "$VENV" >&2
  "$VENV/bin/python" -m pip install --quiet -e "$DIR" >&2
fi
exec "$VENV/bin/python" -m nearby_events_mcp
