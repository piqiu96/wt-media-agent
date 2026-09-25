# wt-media-agent Agent Index

> 本文件是本仓**全部正式内容**的唯一落点：定位、职责边界、需求路由、**本仓规则**、禁止项与本仓内加载顺序。`CLAUDE.md` 与 `AGENTS.md` 是指针，只声明本文件的位置，不承载任何规则。

## 依赖

关于文档等事实都在`../wt-media-workspace`，需要执行时优先考虑对应的约束边界。

治理上下文（当前 CHG、执行契约）在 `../wt-media-workspace`，按其 `.ai/CURRENT_CONTEXT.md` 指引加载。

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
- **Agent 自己的日志**（`runtime/logging.py`）：三个纯文本文件（`agent.log` / `task.log` / `error.log`）、按 logger 名与级别路由、**按小时切割**（活文件恒为稳定名 `agent.log`，归档 `agent.log.<YYYY-MM-DD-HH>`，本机时区、对齐整点）、**只按天保留**（默认 14 天，**不控总量**）、单条截断、脱敏。**dev 也落盘**（`<repo>/.local/logs/`，装态 `~/Library/Logs/WTMedia/Agent/`）。Desktop 的日志是**另一套**，见 `../wt-media-desktop`；Agent 不写 Desktop 的文件，也不被 Desktop 转存。**两侧机制有意不对称**：Desktop 的按天删除由 `file-rotate` 承担，Agent 的仍由 `LogBudget` 承担（标准库没有按天删除）。

## 本仓库不拥有

- 正式业务数据与业务规则（MySQL 归 `../wt-media-cloud`）
- 业务对象最终状态的裁决权（Cloud 决定）
- 内容发现执行（当前 M3 由 Cloud Scheduler + Crawler 承担，归 `../wt-media-cloud`）
- 进程生命周期管理（Sidecar 启停归 `../wt-media-desktop`）

## 需求路由

| 需求是 | 去哪里 |
|---|---|
| 改进程装配与启动顺序 | `src/wt_media_agent/bootstrap/app.py`（唯一装配入口） |
| 改配置键、数据目录、日志 | `src/wt_media_agent/runtime/`（`config.py` 是全 `src/` 唯一读环境变量的模块）。日志落点、三文件路由、小时切割、按天保留、截断与脱敏都在 `runtime/logging.py`；**Logger 只允许在 `bootstrap/app.py` 装配一次**（有 AST 规则钉着，server/component/executor 都不得再初始化） |
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

## 本仓规则

本仓全部规则的唯一落点。一条一行，写清做什么／不做什么。逐项目录事实与禁止扫描区见 `DIRECTORY_MAP.md`。

### 配置

- `config/` 是运行期唯一读的目录；`config_online/` 是发布替换源——运行期代码零引用它，出货时由 `scripts/build_desktop_sidecar.py --config-dir` **整目录覆盖** `config/`，两个目录的文件名与键集合保持 1:1。
- 冻结后的 Agent 由**可执行文件的位置**推导配置目录（macOS `.app` 里的 `Contents/Resources/config`，否则可执行文件旁边的 `config/`），命中不了会先 WARNING 再回落内置默认值——「发布包没带配置」正是安静回落会藏住的那件事。
- 凭证不从这两个目录来：加载器忽略 TOML 里所有敏感键，只报键名、从不报值。

### 平台适配

平台适配（未来 CHG 引入后）不得直接修改 Cloud 状态。

### 发布构建

发布构建不依赖系统 Python 或系统 PATH FFmpeg。

### 依赖锁文件

依赖锁文件（`uv.lock`、`dependency.lock`）由依赖工具生成，**禁止手改**。

## 禁止

- 直接连 Cloud MySQL。
- 自行决定正式业务对象的最终状态。
- 绕过 Cloud 权限和任务控制自行启动未授权的业务操作。
- 把执行成功等同于正式业务成功。
- 外部副作用操作在结果无法确认时盲目重试。
- 重复实现 Cloud 已有的业务规则和数据管理。
- 为 M3 内容发现重建独立抓取任务体系（该阶段归 Cloud-owned 链路）。

## 本仓内加载顺序

本节只写**本仓内**的入口顺序；跨仓读取顺序与全部红线的唯一落点是 `../wt-media-workspace/AGENT-INDEX.md` §4 与 §2，本节不复述。

1. 本文件（职责、路由与本仓规则）
2. `DIRECTORY_MAP.md`（目录导航，含 Local/Cloud 适用范围标注）
3. 只读目标模块的代码、直接依赖与 `tests/` 对应测试

禁止默认扫描的目录见 `DIRECTORY_MAP.md` 的「禁止扫描区」。
