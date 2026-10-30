# Local Error Codes

Local Agent error code definitions.

- `v1/bitbrowser.yaml` (revision `2026.10.30.1`): BitBrowser local-API failures,
  the offline and Cloud-unreachable refusals used by the Cloud-facing routes, and
  the fields that must never appear in an answer.
- `v1/transfer.yaml` (revision `2026.10.23.1`): the save-directory and Agent Token
  refusals, and the download executor's terminal reasons. The two are separate
  mappings in the one file: the first is returned with an HTTP status, the second
  is written onto a task and read in Cloud.

Each definition states its own revision in the `revision` field at the top of the
file; `tests/test_contract_docs.py` fails if this list stops matching those fields.
The file is the only place the number is written down.
