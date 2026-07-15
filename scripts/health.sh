#!/usr/bin/env bash
set -euo pipefail

HOST="${WT_MEDIA_AGENT_HEALTH_HOST:-127.0.0.1}"
PORT="${WT_MEDIA_AGENT_HEALTH_PORT:-18765}"
curl --silent --show-error --fail "http://$HOST:$PORT/healthz" >/dev/null
echo "wt-media-agent health ok"
