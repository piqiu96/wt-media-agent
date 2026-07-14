#!/usr/bin/env sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${WT_MEDIA_AGENT_HEALTH_HOST:-127.0.0.1}"
PORT="${WT_MEDIA_AGENT_HEALTH_PORT:-18765}"
export PYTHONPATH="${PYTHONPATH:-src}"

"$PYTHON_BIN" -m unittest discover -s tests

"$PYTHON_BIN" -m wt_media_agent.local_api.server --host "$HOST" --port "$PORT" &
server_pid=$!

cleanup() {
  kill "$server_pid" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl --silent --show-error --fail "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then
    echo "wt-media-agent health ok"
    exit 0
  fi
  sleep 1
done

echo "wt-media-agent health failed" >&2
exit 1
