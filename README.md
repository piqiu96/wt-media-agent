# wt-media-agent

Local Agent and Cloud Agent runtime skeleton for the modular social media operations platform.

## Responsibilities

- Local Agent and Cloud Agent process entrypoints.
- Local health/status/events and secret-safe BitBrowser Profile scans.
- Runtime adapters, task execution, storage, and Cloud Agent communication.

## Bootstrap

This repository is an installable Python package under `src/wt_media_agent`. All four process entrypoints are thin shells over a single ordered assembly sequence in `bootstrap/app.py`. The BitBrowser adapter uses the Python standard library and calls the configured local API only when a scan is requested.

BitBrowser settings come from `config/agent.toml`; each key can be overridden by environment variables:

- `WT_MEDIA_BITBROWSER_API_URL`: local service base URL. Its default is a runtime
  parameter and lives where runtime parameters live — `config/agent.toml`
  (`runtime/constants.py` carries the built-in fallback). It is not restated here.
- `WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS`: request timeout, default `5`.

## Key Directories

- `src/wt_media_agent/bootstrap`: the only production assembly path, plus one module per run mode.
- `src/wt_media_agent/runtime`: config, paths, logging, environment detection, constants, version.
- `src/wt_media_agent/clients`: outbound clients (`bitbrowser/`, `cloud/`, platform identity) — the lowest business layer.
- `src/wt_media_agent/services`: capabilities built on `clients/` (browser, net, profile guard).
- `src/wt_media_agent/executors`: per-task-type orchestration, wired in through `runner/registry.py`.
- `src/wt_media_agent/local_api`: loopback control API (proxied by the Desktop Rust layer).
- `src/wt_media_agent/storage`: checkpoint storage and local schema migration.
- `contracts`: Agent-owned local contracts.

`DIRECTORY_MAP.md` is the full navigation map, including frozen module paths and symbols that must not move.

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
bin/control.sh start
bin/control.sh status
bin/control.sh stop
```

`bin/control.sh status` reports process liveness and endpoint health as two
separate readings, and exits non-zero unless both hold. `bin/control.sh help`
lists the remaining verbs.

The storage migration command initializes `local-agent.sqlite3` under `--db-path` if given, else `--data-dir` or `WT_MEDIA_AGENT_DATA_DIR` if given, else the deployment-shaped default: `<repo>/.local/data` from a checkout, `~/Library/Application Support/WTMedia/Agent` when frozen or in production. It prints the database path it used. It records applied versions in `schema_migrations` and skips already applied migrations on repeat runs.

The default moved in CHG-056 T-03; it used to be `~/.wt-media-agent` for every deployment shape. A development database left there is not migrated — pass `--db-path ~/.wt-media-agent/local-agent.sqlite3` to reach it.

Runtime configuration lives in `config/agent.toml`, overridden per key by environment variables (see `config/README.md`). `config/` is what the runtime reads and `config_online/` is the release replacement source: shipping copies that directory over this one wholesale, so a release artifact carries the production values while no runtime code ever names `config_online/`. A frozen Agent finds its configuration by where its executable sits rather than by an environment variable, and warns before falling back to the built-in defaults. Credentials are never read from either directory (see `AGENTS.md`).
