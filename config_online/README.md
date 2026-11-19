# Agent config -- release replacement source

Nothing reads this directory at runtime. Shipping copies it over `config/`,
wholesale, so that the release artifact carries the production configuration.

- Keep this directory 1:1 with `config/` in filenames and in key sets. A test
  asserts it, because replacing one directory with the other must not be able to
  silently drop a key the runtime expects.
- Credentials. Same rule as `config/`: `runtime_token` is read from the
  environment only and the loader ignores every sensitive key it finds in a TOML
  file. ADR-0016 §7 permits credentials in this directory; the loader is
  deliberately narrower today (recorded as a deviation in CHG-20260923-056), so
  a credential placed here would be ignored, not honoured.
- `cloud.base_url` holds the loopback value, and that is the decided production
  value: Q-01 was closed on 2026-09-25 (CHG-20260923-059, D-08) in favour of the
  local Cloud service. The address itself is a runtime parameter and lives in the
  TOML beside this file, not in prose here. Nothing here is waiting on a
  decision, so this directory can be copied over `config/` as it stands.


Cloud 连接使用 `[cloud].cloud_env`（`dev/test/pre/prod`，环境变量 `WT_MEDIA_CLOUD_ENV`）。
它与应用的 development/production、debug/release 分开；开发版连接正式 Cloud 时应显式配置 `prod`。
地址和环境必须对应；更换地址别名不改变环境身份。发布替换源为 prod，开发配置为 dev。

`[database].synchronous` 默认 `FULL`，`timeout_seconds` 默认 `5.0`；新连接统一设置持久化策略、外键与 WAL。
业务代码使用 ORM 默认事务。SQLite 文件仍由既有 data_dir 规则决定，不按 Cloud 环境拆文件。

旧库升级遇到缺少环境或用户的记录时会保留原库并拒绝猜测身份。核实后使用迁移入口的
`--legacy-cloud-env`、`--legacy-user-id`，涉及历史发布或未确认领取时再提供 `--legacy-cloud-url`。
升级失败不删除旧数据，不会创建新领取键绕过结果未知的旧请求。
