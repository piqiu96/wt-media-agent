#!/usr/bin/env bash
#
# The one entry point for the Local Agent health process: start, stop, restart
# and report on it. It merges what `scripts/start-health.sh`,
# `scripts/stop-health.sh` and `scripts/health.sh` used to do, unchanged.
#
# This is the one repository where the port literal stays in the operational
# script. `WT_MEDIA_AGENT_HEALTH_PORT` has no configuration-file landing point:
# its default *is* the isolated health harness's value, and CHG-20260926-067
# ruled that moving it into `config/` would break the isolation design. So the
# default below is the landing point, not a copy of one -- do not "de-duplicate"
# it away. The other three repos keep their address in one place and restate it
# nowhere.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT_DIR="$(pwd)"

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
HOST="${WT_MEDIA_AGENT_HEALTH_HOST:-127.0.0.1}"
PORT="${WT_MEDIA_AGENT_HEALTH_PORT:-18765}"
PID_FILE="${WT_MEDIA_AGENT_PID_FILE:-$ROOT_DIR/.cache/wt-media-agent-health.pid}"
LOG_FILE="${WT_MEDIA_AGENT_LOG_FILE:-$ROOT_DIR/.cache/wt-media-agent-health.log}"

usage() {
  cat <<'EOF'
Usage: bin/control.sh <command>

  start    Start the health server in the background and wait for /healthz.
  stop     Stop the process recorded by the PID file.
  restart  Stop, then start.
  status   Report whether that process is alive and whether the endpoint answers.
  help     Show this help.

Script-level overrides: PYTHON_BIN, WT_MEDIA_AGENT_HEALTH_HOST,
WT_MEDIA_AGENT_HEALTH_PORT, WT_MEDIA_AGENT_PID_FILE, WT_MEDIA_AGENT_LOG_FILE.
EOF
}

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  printf '%s' "$pid"
}

probe_health() {
  curl --silent --show-error --fail "http://$HOST:$PORT/healthz" >/dev/null 2>&1
}

do_start() {
  mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

  if running_pid >/dev/null; then
    echo "wt-media-agent health already running: $(cat "$PID_FILE")"
    return 0
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
      return 1
    fi
    if probe_health; then
      echo "wt-media-agent health started: $(cat "$PID_FILE")"
      return 0
    fi
    sleep 0.1
  done

  echo "wt-media-agent health did not become healthy" >&2
  sed -n '1,120p' "$LOG_FILE" >&2 || true
  return 1
}

do_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "wt-media-agent health not running"
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid"
  fi
  rm -f "$PID_FILE"
  echo "wt-media-agent health stopped"
}

do_status() {
  local pid alive=no health=down
  pid="$(running_pid || true)"
  [[ -n "$pid" ]] && alive=yes
  probe_health && health=ok

  echo "agent: pid=${pid:--} alive=$alive health=$health url=http://$HOST:$PORT/healthz"
  [[ "$alive" == yes && "$health" == ok ]]
}

case "${1:-help}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop && do_start ;;
  status)  do_status ;;
  help|-h|--help) usage ;;
  *) echo "unknown command: $1" >&2; usage >&2; exit 2 ;;
esac
