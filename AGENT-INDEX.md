# wt-media-agent Agent Index

## 依赖

关于文档等事实都在`../wt-media-workspace`，需要执行时优先考虑对应的约束边界

## 定位

本仓库是 WT Media 的 **Python 执行工程**：负责需要独立执行环境的实际操作。Agent **执行任务但不拥有正式业务事实**。

两种运行模式共用同一核心框架：

- **Local Agent**：运行在运营电脑（通常由 Desktop 作为 Sidecar 拉起）。
- **Cloud Agent**：运行在云端执行环境（如云端视频生产、FFmpeg 合成）。

## 本仓库拥有

- Agent Runtime：任务执行、心跳、进度、异常处理、执行日志、检查点与结果回传（`runner/`、`storage/`）。
- 本机实际操作（Local）：BitBrowser 调用、Profile 与窗口操作、代理写入与实际状态读取、Cookie 与登录环境、本地文件、浏览器自动化（`clients/`、`services/`、`executors/`、`local_api/`）。
- 与 Cloud 的通信客户端（`clients/cloud/`）。
- 平台适配（API、页面元素、操作步骤、执行状态识别）——当前 `adapters/` 为占位，未来平台 Playwright 适配在此引入。

## 本仓库不拥有

- 正式业务数据与业务规则（MySQL 归 `../wt-media-cloud`）
- 业务对象最终状态的裁决权（Cloud 决定）
- 内容发现执行（当前 M3 由 Cloud Scheduler + Crawler 承担，归 `../wt-media-cloud`）
- 进程生命周期管理（Sidecar 启停归 `../wt-media-desktop`）

## 需求路由

| 需求是 | 去哪里 |
|---|---|
| 改进程装配与启动顺序 | `src/wt_media_agent/bootstrap/app.py`（唯一装配入口） |
| 改配置键、数据目录、日志 | `src/wt_media_agent/runtime/`（`config.py` 是全 `src/` 唯一读环境变量的模块） |
| 改 BitBrowser 实际执行 | `src/wt_media_agent/clients/bitbrowser/` |
| 改浏览器自动化底层（CDP） | `src/wt_media_agent/services/browser/cdp.py` |
| 改 Cookie 提取 / 平台身份 | `src/wt_media_agent/services/browser/cookies.py`、`clients/{bilibili,baijiahao}/identity.py`、`clients/platform_identity.py` |
| 改平台 Playwright 适配 | `src/wt_media_agent/adapters/`（注意当前为占位） |
| 改任务执行、心跳、恢复 | `src/wt_media_agent/runner/runner.py`、`storage/checkpoint_store.py` |
| 改本地代理写入/检测 | `src/wt_media_agent/services/net/proxy.py`、`executors/proxy*.py` |
| 改 Cookie / 登录环境操作 | `src/wt_media_agent/executors/cookie.py`、`account_check.py` |
| 改与 Cloud 的回传协议 | `src/wt_media_agent/clients/cloud/`，契约变更先经 `../wt-media-workspace` 协调 |
| 改本地控制 API | `src/wt_media_agent/local_api/`（`server.py:main` 是冻结符号） |
| 改本地 FFmpeg 合成执行 | **尚无实现**：`runtime/environment.py` 只探测 ffmpeg 是否存在 |

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

治理上下文（当前 CHG、执行契约）在 `../wt-media-workspace`，按其 `.ai/CURRENT_CONTEXT.md` 指引加载。禁止默认扫描 `__pycache__`、锁文件。
