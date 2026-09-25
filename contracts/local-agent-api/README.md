# Local Agent API

Local Agent loopback HTTP control contract owned by `wt-media-agent`.

Paths the formal definition holds, by the change that added them:

- M1: `GET /healthz`, `GET /api/v1/status`, `GET /api/v1/events`
- M2-C: `POST /api/v1/bit-browser/profile-scans`, `POST /api/v1/bit-browser/profile-groups`,
  `POST /api/v1/account-check`, `POST /api/v1/proxy-check`, `POST /api/v1/proxy-extract`,
  `POST /api/v1/proxy-mutation`
- CHG-20260923-057: `GET /api/v1/health` (aggregate health; `/healthz` stays frozen)

Formal definition: `v1/local-agent.openapi.yaml`. The revision is that file's own
`info.version` -- currently `2026.09.24.1` -- and the file is the only place the
number is written down. The list above is checked against the file's `paths:` by
`tests/test_contract_docs.py`, so a path added to one and not the other fails the
suite instead of drifting.
