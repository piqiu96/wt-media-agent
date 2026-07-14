# Agent Contracts

Agent owns the following local contract areas. M0 keeps directories only as ownership placeholders; no formal OpenAPI, schema, DTO, SSE event, or error-code definitions are active yet.

- `local-agent-api`
- `local-event-schemas`
- `local-status-enums`
- `local-error-codes`

## Consumed Cloud Contracts

Agent consumes the Cloud-owned Cloud-Agent API. M1-C1 locks the expected Cloud-Agent contract major version and revision in Agent source code, without copying the provider's formal OpenAPI definition.
