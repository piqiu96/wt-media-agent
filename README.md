# wt-media-agent

Local Agent and Cloud Agent runtime skeleton for the modular social media operations platform.

## Responsibilities

- Local Agent and Cloud Agent process entrypoints.
- Local health/status/events and secret-safe BitBrowser Profile scans.
- Runtime adapters, task execution, storage, and Cloud Agent communication.

## Bootstrap

This repository is an installable Python package under `src/wt_media_agent`. The BitBrowser adapter uses the Python standard library and calls the configured local API only when a scan is requested.

BitBrowser configuration:

- `WT_MEDIA_BITBROWSER_API_URL`: local service base URL, default `http://127.0.0.1:54345`.
- `WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS`: request timeout, default `5`.

## Key Directories

- `src/wt_media_agent/core`: shared runtime primitives.
- `src/wt_media_agent/local_api`: loopback control API placeholder.
- `src/wt_media_agent/runtimes`: runtime adapter placeholders.
- `src/wt_media_agent/executors`: executor registry placeholder.
- `contracts`: Agent-owned local contracts.

## M0 Verification

From this repository:

```text
scripts/bootstrap.sh
scripts/test.sh
scripts/build.sh
scripts/migrate-storage.sh --data-dir /tmp/wt-media-agent-m0
scripts/verify-health.sh
```

For local health process management in a terminal:

```text
WT_MEDIA_AGENT_HEALTH_PORT=18765 scripts/start-health.sh
WT_MEDIA_AGENT_HEALTH_PORT=18765 scripts/health.sh
scripts/stop-health.sh
```

The storage migration command initializes `local-agent.sqlite3` under `WT_MEDIA_AGENT_DATA_DIR`, `--data-dir`, or an explicit `--db-path`. It records applied versions in `schema_migrations` and skips already applied migrations on repeat runs.
