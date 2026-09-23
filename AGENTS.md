# WT Media Agent

## 必须遵守

Follow `AGENT-INDEX.md` for repository boundaries.

## Responsibility

Agent 拥有全部需要独立执行环境的 Python 执行能力：Local Agent 与 Cloud Agent 两种运行模式、任务执行 Runtime、文件与 FFmpeg 运行时、BitBrowser 集成、浏览器自动化、平台适配，以及本地控制契约。

Agent 执行任务，但不拥有正式业务事实：不连 Cloud MySQL，不裁决业务终态。

## Structure

- `src/wt_media_agent`: 可安装 Python 包。
- 入口：`local_main.py`（Local 进程）、`cloud_main.py`（Cloud 进程）、`sidecar_main.py`（Desktop 拉起的 Sidecar 模式）、`app.py`（应用装配）、`runner.py`（任务执行 Runner）。
- `core`: 共享运行时原语，当前含 `profile_guard.py`。
- `local_api`: 环回控制 API（`server.py`）与本地状态（`state.py`），由 Desktop Rust 层代理。
- `storage`: SQLite 检查点存储（`checkpoint_store.py`）与本地存储迁移（`migration.py`）。
- `runtimes`: 执行运行时适配：`bitbrowser.py`（BitBrowser 本地接口）、`cdp_client.py`（浏览器 CDP）、`environment.py`（运行环境检测）。
- `executors`: 任务类型编排：`profile.py`、`account_check.py`、`cookie.py`、`proxy.py`、`proxy_mutation.py`、`noop.py`。
- `cloud_agent_client.py` / `cloud_agent_contract.py`: 与 Cloud 的通信客户端与契约类型。
- `modes`: 模式选择占位，尚无实现。
- `adapters`: 平台适配占位，尚无实现。
- `generated`: 仅存放生成的契约类型。

## Rules

- Agent 不连 Cloud MySQL。
- Agent 不创建正式业务任务，不决定正式业务对象的最终状态。
- 平台适配（未来 CHG 引入后）不得直接修改 Cloud 状态。
- 高风险外部操作结果无法确认时必须如实上报，禁止盲目重试。
- 发布构建不依赖系统 Python 或系统 PATH FFmpeg。
- `src/wt_media_agent/generated` 禁止手改。
