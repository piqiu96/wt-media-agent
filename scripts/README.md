# Agent Scripts

Expose common commands with stable names.

| Script | Purpose |
|---|---|
| `bootstrap.sh` | Sync the local package and extras with `uv`. |
| `test.sh` | Run Agent unittests through the project virtual environment. |
| `build.sh` | Build the Agent source distribution and wheel with `uv build`. |
| `migrate-storage.sh` | Apply repeat-safe SQLite storage migrations. |
| `start-health.sh` | Start the Local Agent health server in the background and wait for `/healthz`. |
| `health.sh` | Probe the Local Agent health endpoint. |
| `stop-health.sh` | Stop the health process recorded by the PID file. |
| `verify-health.sh` | Run tests, start Local Agent health temporarily, probe health, and stop it. |

`PYTHON_BIN` can override the interpreter. `WT_MEDIA_AGENT_HEALTH_HOST`, `WT_MEDIA_AGENT_HEALTH_PORT`, `WT_MEDIA_AGENT_PID_FILE`, and `WT_MEDIA_AGENT_LOG_FILE` control the health process scripts.
