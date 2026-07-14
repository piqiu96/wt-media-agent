# wt-media-agent

Local Agent and Cloud Agent runtime skeleton for the modular social media operations platform.

## Responsibilities

- Local Agent and Cloud Agent process entrypoints.
- Minimal app shell and health surface.
- Future package locations for local API, generated contracts, storage, runtimes, and executors.

## Bootstrap

This repository is scaffolded as an installable Python package under `src/wt_media_agent`. Runtime dependencies are not installed yet.

## Key Directories

- `src/wt_media_agent/core`: shared runtime primitives.
- `src/wt_media_agent/local_api`: loopback control API placeholder.
- `src/wt_media_agent/runtimes`: runtime adapter placeholders.
- `src/wt_media_agent/executors`: executor registry placeholder.
- `contracts`: Agent-owned local contracts.
