# WT Media Agent

## Responsibility

Agent owns all external execution: Local Agent and Cloud Agent modes, task running, file and FFmpeg runtime, BitBrowser integration, browser automation, platform adapters, and Local Agent contracts.

## Structure

- `src/wt_media_agent`: installable Python package.
- `modes`: mode selection placeholder.
- `core`: shared runtime primitive placeholder.
- `local_api`: loopback control API placeholder.
- `storage`: SQLite and local file indexes.
- `runtimes`: FFmpeg, browser, BitBrowser, subprocess, download, and object storage adapters.
- `executors`: task-type orchestration placeholder.
- `generated`: generated contract types only.

## Rules

- Agent never connects to Cloud MySQL.
- Agent does not create formal business tasks.
- Platform adapters do not mutate Cloud state directly once they are introduced by future CHGs.
- High-risk external actions must report uncertain results instead of blind retries.
- Do not depend on system Python or system PATH FFmpeg in release builds.
