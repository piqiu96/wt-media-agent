# Local Event Schemas

Reserved for SSE event payload schemas for Desktop local pages.

M0 status: placeholder only; no formal event schema is active.
M1 active event:

```text
event: status
data: {"agent_id":"...","status":"idle","current_task_id":null,"pending_result_count":0}
```

Formal definition: `v1/status.yaml`, revision `2026.07.14.6`.
