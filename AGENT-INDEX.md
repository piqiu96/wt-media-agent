# wt-media-agent Agent Index

## 依赖

关于文档等事实都在`../wt-media-workspace`，需要执行时优先考虑对应的约束边界

## 定位

本仓库是 WT Media 的 **Python 执行工程**：负责需要独立执行环境的实际操作。Agent **执行任务但不拥有正式业务事实**。

两种运行模式共用同一核心框架：

- **Local Agent**：运行在运营电脑（通常由 Desktop 作为 Sidecar 拉起）。
- **Cloud Agent**：运行在云端执行环境（如云端视频生产、FFmpeg 合成）。

## 本仓库拥有

- Agent Runtime：任务执行、心跳、进度、异常处理、执行日志、检查点与结果回传（`runner.py`、`storage/`）。
- 本机实际操作（Local）：BitBrowser 调用、Profile 与窗口操作、代理写入与实际状态读取、Cookie 与登录环境、本地文件、本地 FFmpeg、浏览器自动化（`runtimes/`、`executors/`、`local_api/`）。
- 与 Cloud 的通信客户端（`cloud_agent_client.py`、`cloud_agent_contract.py`）。
- 平台适配（API、页面元素、操作步骤、执行状态识别）——当前 `adapters/` 为占位，未来平台 Playwright 适配在此引入。

## 本仓库不拥有

- 正式业务数据与业务规则（MySQL 归 `../wt-media-cloud`）
- 业务对象最终状态的裁决权（Cloud 决定）
- 内容发现执行（当前 M3 由 Cloud Scheduler + Crawler 承担，归 `../wt-media-cloud`）
- 进程生命周期管理（Sidecar 启停归 `../wt-media-desktop`）

## 需求路由

| 需求是 | 去哪里 |
|---|---|
| 改 BitBrowser 实际执行 | `src/wt_media_agent/runtimes/bitbrowser.py` |
| 改浏览器自动化底层（CDP） | `src/wt_media_agent/runtimes/cdp_client.py` |
| 改平台 Playwright 适配 | `src/wt_media_agent/adapters/`（注意当前为占位） |
| 改任务执行、心跳、恢复 | `src/wt_media_agent/runner.py`、`storage/checkpoint_store.py` |
| 改本地代理写入/检测 | `src/wt_media_agent/executors/proxy*.py`、`proxy_check.py` |
| 改 Cookie / 登录环境操作 | `src/wt_media_agent/executors/cookie.py`、`account_check.py` |
| 改本地 FFmpeg 合成执行 | `src/wt_media_agent/runtimes/`（本地运行时） |
| 改与 Cloud 的回传协议 | `cloud_agent_client.py`、`cloud_agent_contract.py`，契约变更先经 `../wt-media-workspace` 协调 |
| 改本地控制 API | `src/wt_media_agent/local_api/` |

## 禁止

- 直接连 Cloud MySQL。
- 自行决定正式业务对象的最终状态。
- 绕过 Cloud 权限和任务控制自行启动未授权的业务操作。
- 把执行成功等同于正式业务成功。
- 外部副作用操作在结果无法确认时盲目重试。
- 重复实现 Cloud 已有的业务规则和数据管理。
- 为 M3 内容发现重建独立抓取任务体系（该阶段归 Cloud-owned 链路）。

## 上下文加载顺序

1. 本文件（职责与路由）
2. `AGENTS.md` 与 `CLAUDE.md`（边界与规则）
3. `DIRECTORY_MAP.md`（目录导航，含 Local/Cloud 适用范围标注）
4. 只读目标模块的代码、直接依赖与 `tests/` 对应测试

治理上下文（当前 CHG、执行契约）在 `../wt-media-workspace`，按其 `.ai/CURRENT_CONTEXT.md` 指引加载。禁止默认扫描 `generated/`、`__pycache__`、锁文件。
