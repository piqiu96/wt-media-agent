# Local Event Schemas

SSE event payload schemas for Desktop local pages, owned by `wt-media-agent`.

- `v1/status.yaml` (revision `2026.07.14.8`): Local Agent status SSE event.
- `v1/runtime-environment.yaml` (revision `2026.07.14.9`): M2-C4 secret-safe runtime
  environment and verified Profile presence payload.
- `v1/profile-guard.yaml` (revision `2026.07.14.8`): M2-C5 local/Cloud Profile guard
  outcomes without permit credentials.

The status event is:

```text
event: status
data: {"agent_id":"...","status":"idle","current_task_id":null,"pending_result_count":0}
```

Each schema states its own revision in the `revision` field at the top of the file;
the numbers above are a reading convenience, and `tests/test_contract_docs.py` fails
if one of them stops matching the file it names.
