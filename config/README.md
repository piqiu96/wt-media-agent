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


Cloud 连接使用 `[cloud].cloud_env`（`dev/test/pre/prod`，环境变量 `WT_MEDIA_CLOUD_ENV`）。
它与应用的 development/production、debug/release 分开；开发版连接正式 Cloud 时应显式配置 `prod`。
地址和环境必须对应；更换地址别名不改变环境身份。发布替换源为 prod，开发配置为 dev。

`[database].synchronous` 默认 `FULL`，`timeout_seconds` 默认 `5.0`；新连接统一设置持久化策略、外键与 WAL。
业务代码使用 ORM 默认事务。SQLite 文件仍由既有 data_dir 规则决定，不按 Cloud 环境拆文件。

旧库升级遇到缺少环境或用户的记录时会保留原库并拒绝猜测身份。核实后使用迁移入口的
`--legacy-cloud-env`、`--legacy-user-id`，涉及历史发布或未确认领取时再提供 `--legacy-cloud-url`。
升级失败不删除旧数据，不会创建新领取键绕过结果未知的旧请求。
