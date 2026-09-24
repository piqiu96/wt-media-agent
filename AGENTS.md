# WT Media Agent

## 必须遵守

Follow `AGENT-INDEX.md` for repository boundaries.

## Responsibility

Agent 拥有全部需要独立执行环境的 Python 执行能力：Local Agent 与 Cloud Agent 两种运行模式、任务执行 Runtime、文件与 FFmpeg 运行时、BitBrowser 集成、浏览器自动化、平台适配，以及本地控制契约。

Agent 执行任务，但不拥有正式业务事实：不连 Cloud MySQL，不裁决业务终态。

## Structure

- `src/wt_media_agent`: 可安装 Python 包。
- 入口：`local_main.py`（Local 进程）、`cloud_main.py`（Cloud 进程）、`sidecar_main.py`（Desktop 拉起的 Sidecar 模式，**冻结路径**）、`local_api/server.py:main`（健康脚本与 m2b/verify 脚本调用的冻结符号）。四者都是薄壳。
- `bootstrap`: 唯一的生产装配入口（`app.py` 的有序装配序列）与三个模式面（`local.py`/`cloud.py`/`sidecar.py`），无注册表、无容器。
- `runtime`: 横切基础——`config.py`（**全 `src/` 唯一读环境变量的模块**）、`paths.py`（落盘位置唯一事实源）、`logging.py`（三文件布局与路由、轮转与保留、截断、脱敏；dev 也落盘）、`environment.py`、`constants.py`、`version.py`。
- `clients`: 最底层业务层——`bitbrowser/`（本地接口 + 唯一构造点 + 超时 + 错误类型）、`cloud/`（回传、领取、心跳与契约校验）、`bilibili/`、`baijiahao/`、`platform_identity.py`；`cloud_agent_client.py`/`cloud_agent_contract.py` 是**仅为冻结测试保留的废弃导入 shim**。
- `services`: 建在 `clients/` 之上的能力——`browser/cdp.py`、`browser/cookies.py`、`net/proxy.py`、`profile_guard.py`。
- `local_api`: 环回控制 API（`server.py`）与本地状态（`state.py`），由 Desktop Rust 层代理。
- `storage`: SQLite 检查点存储（`checkpoint_store.py`）、连接策略（`sqlite.py`）与本地存储迁移（`migration.py`，含冻结符号）。
- `runner`: 任务执行 Runner（`runner.py`）、配置（`config.py`）与任务类型注册表（`registry.py`）。
- `executors`: 任务类型编排：`protocol.py`（`Executor`/`ExecutorFactory`，不得 import `runner/`）、`profile.py`、`account_check.py`、`cookie.py`、`proxy.py`、`proxy_mutation.py`、`noop.py`。
- `adapters`: 平台适配占位，尚无实现。

## Rules

- Agent 不连 Cloud MySQL。
- Agent 不创建正式业务任务，不决定正式业务对象的最终状态。
- 平台适配（未来 CHG 引入后）不得直接修改 Cloud 状态。
- 高风险外部操作结果无法确认时必须如实上报，禁止盲目重试。
- 发布构建不依赖系统 Python 或系统 PATH FFmpeg。
- 依赖锁文件（`uv.lock`、`dependency.lock`）禁止手改。
