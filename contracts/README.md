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
