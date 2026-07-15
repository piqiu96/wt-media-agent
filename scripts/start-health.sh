#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
HOST="${WT_MEDIA_AGENT_HEALTH_HOST:-127.0.0.1}"
PORT="${WT_MEDIA_AGENT_HEALTH_PORT:-18765}"
PID_FILE="${WT_MEDIA_AGENT_PID_FILE:-$ROOT_DIR/.cache/wt-media-agent-health.pid}"
LOG_FILE="${WT_MEDIA_AGENT_LOG_FILE:-$ROOT_DIR/.cache/wt-media-agent-health.log}"
mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "wt-media-agent health already running: $(cat "$PID_FILE")"
  exit 0
fi

WT_MEDIA_AGENT_PYTHON_BIN="$PYTHON_BIN" \
WT_MEDIA_AGENT_HEALTH_HOST="$HOST" \
WT_MEDIA_AGENT_HEALTH_PORT="$PORT" \
WT_MEDIA_AGENT_PID_FILE="$PID_FILE" \
WT_MEDIA_AGENT_LOG_FILE="$LOG_FILE" \
WT_MEDIA_AGENT_PYTHONPATH="${PYTHONPATH:-src}" \
"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import os
import subprocess
from pathlib import Path

python_bin = os.environ["WT_MEDIA_AGENT_PYTHON_BIN"]
host = os.environ["WT_MEDIA_AGENT_HEALTH_HOST"]
port = os.environ["WT_MEDIA_AGENT_HEALTH_PORT"]
pid_file = Path(os.environ["WT_MEDIA_AGENT_PID_FILE"])
log_file = Path(os.environ["WT_MEDIA_AGENT_LOG_FILE"])
env = os.environ.copy()
env["PYTHONPATH"] = os.environ["WT_MEDIA_AGENT_PYTHONPATH"]

with log_file.open("ab") as log:
    child = subprocess.Popen(
        [
            python_bin,
            "-m",
            "wt_media_agent.local_api.server",
            "--host",
            host,
            "--port",
            port,
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env=env,
    )

pid_file.write_text(f"{child.pid}\n", encoding="utf-8")
PY

for _ in {1..50}; do
  if ! kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
    echo "wt-media-agent health failed to stay running" >&2
    sed -n '1,120p' "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl --silent --show-error --fail "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then
    echo "wt-media-agent health started: $(cat "$PID_FILE")"
    exit 0
  fi
  sleep 0.1
done

echo "wt-media-agent health did not become healthy" >&2
sed -n '1,120p' "$LOG_FILE" >&2 || true
exit 1
