# Local Agent API

Local Agent loopback HTTP control contract owned by `wt-media-agent`.

M1 active endpoints:

- `GET /healthz`
- `GET /api/v1/status`
- `GET /api/v1/events`

M2-C adds:

- `POST /api/v1/bit-browser/profile-scans`
- `POST /api/v1/proxy-check`
- `POST /api/v1/proxy-mutation`

Formal definition: `v1/local-agent.openapi.yaml`, revision `2026.09.04.1`.
