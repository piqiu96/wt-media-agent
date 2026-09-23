# Agent config

Runtime configuration. This directory is the only one the Agent reads.

- `agent.toml` -- the development configuration. Precedence per key is
  environment variable > this file > built-in default.
- Credentials. `runtime_token` is read from the environment only, and the loader
  ignores *any* sensitive key it finds in a TOML file -- token, password,
  secret, cookie and friends -- reporting it by name and never by value. Do not
  put a working credential here expecting the runtime to pick it up. The
  environment does not ship in the release artifact; this file does.
- A missing file is not an error: every key falls back to its built-in default,
  which is how a bundled sidecar starts before Desktop passes anything in. A
  malformed file *is* an error, and startup fails with the file and the parse
  problem named.

`config_online/` is the release replacement source. Shipping copies that
directory over this one, wholesale. The two directories are kept 1:1 in
filenames and key sets, and a test asserts it.
