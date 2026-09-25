# Agent Scripts

This directory is the script index for `wt-media-agent`. It states the
classification and the placement rule only — no per-file walkthrough, no runtime
parameters.

## Classification

| Purpose | What the category answers | Scripts (file names only) |
| --- | --- | --- |
| Operations | Bring the Local Agent health process up / confirm it is up / stop it | see [`../bin/control.sh`](../bin/control.sh) |
| Development | Rebuild / regenerate / migrate after a change | `bootstrap.sh`, `build.sh`, `build_desktop_sidecar.py`, `migrate-storage.sh`, `test.sh` |
| Verification | Prove something holds, or does not | `test.sh`, `verify-health.sh` |

Multiple membership is a property, not a mistake: `test.sh` builds and asserts,
so it is both; `verify-health.sh` is a verification script that happens to start
and stop a throwaway server, so it spans Verification and Operations while the
Operations entry point proper is [`../bin/control.sh`](../bin/control.sh).

The Operations row is the one that moved: process start/stop and liveness live in
`bin/`, not here.

## Where a new script goes

- A new **development** script → `scripts/dev/`.
- A new **verification / acceptance** script → `scripts/verify/`.
- **Start/stop and health checks always go to [`bin/`](../bin/)**; do not add them
  under `scripts/`.
- **Existing scripts do not move.** The flat files in this directory are the
  historical landing spots and are left alone. `dev/` and `verify/` take scripts
  written from here on — they are not a target shape to migrate the current files
  into.

Both subdirectories currently hold a single `.gitkeep` each.

## What this file does not own

Per-file facts are not here. Directory facts and the no-scan zones belong to
[`../DIRECTORY_MAP.md`](../DIRECTORY_MAP.md); runtime configuration belongs to
`config/` (see [`../config_online/README.md`](../config_online/README.md) for the
release replacement rule); the loopback control API and the process entrypoints
belong to [`../README.md`](../README.md).

**Numeric runtime parameters — ports, addresses, credentials, timeout and
retention defaults — have their single landing point in configuration files.**
This file does not restate them. [`../bin/control.sh`](../bin/control.sh) is the
one place where a health-harness default stays in a script, because for the
isolated health process the script default *is* the landing point and no
configuration file owns it; that exception is stated in the script's own header.

The script-level operational overrides stay script-level and are named here only:
`PYTHON_BIN`, `WT_MEDIA_AGENT_HEALTH_HOST`, `WT_MEDIA_AGENT_HEALTH_PORT`,
`WT_MEDIA_AGENT_PID_FILE`, `WT_MEDIA_AGENT_LOG_FILE`.
