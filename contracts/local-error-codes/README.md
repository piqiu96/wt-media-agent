# Local Error Codes

Local Agent error code definitions.

- `v1/bitbrowser.yaml` (revision `2026.07.14.6`): BitBrowser local-API failures.
- `v1/transfer.yaml` (revision `2026.09.27.3`): the transfer API's refusals -- the
  save-directory pair and the bind route's credential answer -- and the download
  executor's terminal reasons. The two are separate mappings in the one file: the
  first is returned with an HTTP status, the second is written onto a task and
  read in Cloud.

Each definition states its own revision in the `revision` field at the top of the
file; `tests/test_contract_docs.py` fails if this list stops matching those fields.
The file is the only place the number is written down.
