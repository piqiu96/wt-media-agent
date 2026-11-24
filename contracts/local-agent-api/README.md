# Local Agent API

Local Agent loopback HTTP control contract owned by `wt-media-agent`.

Paths the formal definition holds, by the change that added them:

- M1: `GET /healthz`, `GET /api/v1/status`, `GET /api/v1/events`
- M2-C: `POST /api/v1/bit-browser/profile-scans`, `POST /api/v1/bit-browser/profile-groups`,
  `POST /api/v1/account-check`, `POST /api/v1/proxy-extract`,
  `POST /api/v1/proxy-mutation`
- CHG-20261028-099 routes dynamic `proxy-extract` through Desktop on the operator's
  workstation; Cloud no longer invokes the loopback route for preview or refresh.
- CHG-20261028-099: `POST /api/v1/proxy-write` (ack only),
  `POST /api/v1/proxy-verification` (read-only local comparison),
  `POST /api/v1/proxy-assignment` (explicit BitBrowser success;
  the existing `proxy-mutation` endpoint retains its read-back contract)
- CHG-20260923-057: `GET /api/v1/health` (aggregate health; `/healthz` stays frozen)
- CHG-20260924-061: `GET /api/v1/save-directory`, `POST /api/v1/save-directory`
- CHG-20261023-101: `POST /api/v1/agent-token`, `POST /api/v1/bit-account/bind`,
  and the Profile and cookie routes that had been served without a definition: `POST /api/v1/bit-browser/profile-create`,
  `POST /api/v1/bit-browser/profile-open`, `POST /api/v1/bit-browser/profile-close`,
  `POST /api/v1/bit-browser/profile-update`, `POST /api/v1/bit-browser/profile-delete`,
  `POST /api/v1/cookie-read`

Formal definition: `v1/local-agent.openapi.yaml`, revision `2026.11.25.2`. The
revision is that file's own `info.version`, and the file is the only place the
number is written down. The list above is checked against the file's `paths:` and
their methods by `tests/test_contract_docs.py`, so an endpoint added to one and
not the other -- or listed here with the wrong method -- fails the suite instead
of drifting.

The old bind route (`/api/v1/bind`) is gone: a machine is no longer bound to a
device identity. The Desktop proves the machine by handing over the Agent Token
its Desktop session exchanged, and BitBrowser's main account is read from
BitBrowser by the Agent rather than sent in by a caller.

- `POST /api/v1/account-cookie/prepare` — live scoped snapshot and identity.
- `POST /api/v1/account-cookie/apply` — compare persisted fingerprint, write scoped cookies and read back.
- `POST /api/v1/account-cookie/inspect` — read-only reconciliation; never repeats writes.

- CHG106: `GET /api/v1/publication-draft/{draft_type}`, `PUT /api/v1/publication-draft/{draft_type}`, `DELETE /api/v1/publication-draft/{draft_type}` — native authenticated new-task input, revisioned and scoped by configured environment and Cloud-authenticated user.

- CHG-20261120-106: `POST /api/v1/publication-run/{attempt_id}/scene` and `POST /api/v1/publication-run/{attempt_id}/resume`, only for the authenticated user’s still-live original human scene; Cloud strict session and original attempt/revision checks apply.

CHG107 adds `batch` to the existing publication-draft type enum. Task and batch inputs have independent revisions per authenticated user/environment. Cloud batch save success clears only the matching unsubmitted batch input; existing Cloud batch editing never uses this local input. CHG109 adds `plan` in revision `2026.11.25.2`.

CHG109 `plan` uses the same authenticated native identity, configured environment, bounded JSON input and monotonic compare-and-set revision as task/batch, with a separate row per type. It is only unsubmitted new-plan input (request key, name, period, note, goal matrix and ordered batch structure). It never creates Cloud plans, tasks, output occupation or execution authorization. Existing Cloud plan editing is saved directly to Cloud and must not write this local new-plan input. After confirmed Cloud creation, clear only the matching local plan revision; an uncertain creation response must first be recovered by the original Cloud request key, without discarding or regenerating the input. The extension is additive; task/batch paths and identity checks are unchanged.
