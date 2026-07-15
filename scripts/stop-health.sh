#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${WT_MEDIA_AGENT_PID_FILE:-$ROOT_DIR/.cache/wt-media-agent-health.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "wt-media-agent health not running"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" >/dev/null 2>&1; then
  kill "$PID"
fi
rm -f "$PID_FILE"
echo "wt-media-agent health stopped"
