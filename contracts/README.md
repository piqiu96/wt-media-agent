# Agent Contracts

Agent owns the following local contract areas. Every area carries an active formal
definition under its own `v1/` directory; the area README names the file and states
the revision it is at.

- `local-agent-api`
- `local-event-schemas`
- `local-status-enums`
- `local-error-codes`

## Consumed Cloud Contracts

Agent consumes the Cloud-owned Cloud-Agent API. M1-C1 locks the expected Cloud-Agent contract major version and revision in Agent source code, without copying the provider's formal OpenAPI definition.


Publication execution consumes Cloud contract revision `2026.11.20.1`:
`/api/v1/cloud-agent/publication-task/claim`, `/{id}/report`, and `/{id}/complete`.
Keepalive omits `stage`; progress and the pre-submit check supply it. These reports
share increasing sequence numbers, while completion keeps the saved original
packet for replay. The original-attempt recovery read uses the singular
`/{id}/attempt/{attempt_id}` path supplied by the Cloud recovery capability;
it is separate from the session-authenticated task history API.
