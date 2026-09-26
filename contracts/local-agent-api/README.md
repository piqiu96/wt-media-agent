# Local Agent API

Local Agent loopback HTTP control contract owned by `wt-media-agent`.

Paths the formal definition holds, by the change that added them:

- M1: `GET /healthz`, `GET /api/v1/status`, `GET /api/v1/events`
- M2-C: `POST /api/v1/bit-browser/profile-scans`, `POST /api/v1/bit-browser/profile-groups`,
  `POST /api/v1/account-check`, `POST /api/v1/proxy-check`, `POST /api/v1/proxy-extract`,
  `POST /api/v1/proxy-mutation`
- CHG-20260923-057: `GET /api/v1/health` (aggregate health; `/healthz` stays frozen)
- CHG-20260924-061: `GET /api/v1/save-directory`, `POST /api/v1/save-directory`

Formal definition: `v1/local-agent.openapi.yaml`, revision `2026.09.27.1`. The
revision is that file's own `info.version`, and the file is the only place the
number is written down. The list above is checked against the file's `paths:` and
their methods by `tests/test_contract_docs.py`, so an endpoint added to one and
not the other -- or listed here with the wrong method -- fails the suite instead
of drifting.

The list above is the paths this definition *holds*, and the file is the only
place that list comes from. It is not a claim that the Agent serves nothing else:
seven paths the process answers today -- `/api/v1/bind`,
`/api/v1/bit-browser/profile-{create,open,close,update,delete}` and
`/api/v1/cookie-read` -- were implemented without a definition being written down
for them, and they are registered as an open gap rather than described here,
because a definition invented after the fact would be a description of the
implementation rather than an agreed interface.
