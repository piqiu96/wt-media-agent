# Local Agent API

Local Agent loopback HTTP control contract owned by `wt-media-agent`.

Paths the formal definition holds, by the change that added them:

- M1: `GET /healthz`, `GET /api/v1/status`, `GET /api/v1/events`
- M2-C: `POST /api/v1/bit-browser/profile-scans`, `POST /api/v1/bit-browser/profile-groups`,
  `POST /api/v1/account-check`, `POST /api/v1/proxy-check`, `POST /api/v1/proxy-extract`,
  `POST /api/v1/proxy-mutation`
- CHG-20260923-057: `GET /api/v1/health` (aggregate health; `/healthz` stays frozen)
- CHG-20260924-061: `GET /api/v1/save-directory`, `POST /api/v1/save-directory`
- CHG-20261023-101: `POST /api/v1/agent-token`, `POST /api/v1/bit-account/bind`,
  `POST /api/v1/sensitive-tasks/{task_id}/preflight`,
  `POST /api/v1/sensitive-permits/{permit_id}/finish`, and the Profile and cookie routes
  that had been served without a definition: `POST /api/v1/bit-browser/profile-create`,
  `POST /api/v1/bit-browser/profile-open`, `POST /api/v1/bit-browser/profile-close`,
  `POST /api/v1/bit-browser/profile-update`, `POST /api/v1/bit-browser/profile-delete`,
  `POST /api/v1/cookie-read`

Formal definition: `v1/local-agent.openapi.yaml`, revision `2026.10.23.1`. The
revision is that file's own `info.version`, and the file is the only place the
number is written down. The list above is checked against the file's `paths:` and
their methods by `tests/test_contract_docs.py`, so an endpoint added to one and
not the other -- or listed here with the wrong method -- fails the suite instead
of drifting.

The old bind route (`/api/v1/bind`) is gone: a machine is no longer bound to a
device identity. The Desktop proves the machine by handing over the Agent Token
its Desktop session exchanged, and BitBrowser's main account is read from
BitBrowser by the Agent rather than sent in by a caller.
