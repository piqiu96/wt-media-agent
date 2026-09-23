# wt-media-agent 目录地图

本文只记录实际存在的目录。定位代码时从本文件出发，禁止全仓库扫描。

分层遵循 ADR-0016：`runner → executors → {clients, services, storage}`，`runtime/`、`utils/` 为横切与叶子层，`local_api/` 与 `runner/` 平级。`clients/` 是最底层业务层，`services/` 建在其上；`clients/`、`services/`、`utils/` 一律不得 import `executors/`，`utils/` 不得 import 任何业务层。

Agent 有两种运行模式：**Local Agent**（运行在运营电脑）与 **Cloud Agent**（运行在云端执行环境），共用同一 Python 核心框架。下表对共享能力标注适用范围（Local / Cloud / 共用）。

## 一、进程入口与装配（共用）

| 路径 | 职责 | 适用 | 何时进入 |
|---|---|---|---|
| `src/wt_media_agent/bootstrap/app.py` | **唯一的生产装配入口**（ADR-0016 §2）。九步显式有序装配：配置 → RuntimePaths → 日志 → BitBrowserClient（`src/` 内唯一构造点）→ CloudAgentClient → 迁移+CheckpointStore → executor 工厂 → TaskRunner → LocalAgentState。无注册表、无容器 | 共用 | 改模块组装、改组件顺序 |
| `src/wt_media_agent/bootstrap/local.py` | Local 模式面：起本机 API | Local | 改本地启动行为 |
| `src/wt_media_agent/bootstrap/cloud.py` | Cloud 模式面：默认只打印环境事实 JSON 并返回 0；`WT_MEDIA_AGENT_RUN_RUNNER` 为真才 `runner.start()` | Cloud | 改云端启动行为 |
| `src/wt_media_agent/bootstrap/sidecar.py` | Sidecar 模式面：消费 bootstrap 装配结果并起 API；开关打开时 runner 走守护线程 | Local | 改 Sidecar 启动行为 |
| `src/wt_media_agent/local_main.py` | Local 进程入口（`wt-media-local-agent`），3 行委托 `bootstrap.local` | Local | 一般不必进 |
| `src/wt_media_agent/cloud_main.py` | Cloud 进程入口（`wt-media-cloud-agent`），3 行委托 `bootstrap.cloud` | Cloud | 一般不必进 |
| `src/wt_media_agent/sidecar_main.py` | Sidecar 入口（由 Desktop 拉起），委托 `bootstrap.sidecar`，自身不接任何命令行参数。**冻结路径**：Desktop 打包直接以本文件为 PyInstaller 入口，不得搬移或改名 | Local | 改 Sidecar 启动行为 |

`local_api/server.py:main` 是第四个进程入口（`wt-media-local-health`、`scripts/verify-health.sh`、workspace 的 m2b/verify 脚本都调它）：保留原位与符号不动，内部同样委托 `bootstrap` 装配。四个入口都是薄壳，装配实现只有 `bootstrap/app.py` 一处。

## 二、运行时基础（`src/wt_media_agent/runtime/`，共用）

| 路径 | 职责 | 何时进入 |
|---|---|---|
| `runtime/config.py` | 配置加载：强类型 `AgentConfig` + 声明式键表，优先级 env > 文件 > 默认值。**全 `src/` 只允许本模块读环境变量**。凭据键仅环境变量可给，文件里出现即按名忽略 | 改配置键、改优先级 |
| `runtime/paths.py` | 数据/日志/版本目录解析的唯一事实源（override / dev / installed 三态） | 改落盘位置 |
| `runtime/logging.py` | 日志初始化（级别、轮转文件、目录创建失败降级到 stderr） | 改日志 |
| `runtime/environment.py` | 运行环境检测（frozen / production / dev 判定） | 改环境校验 |
| `runtime/constants.py` | 全局常量 | — |
| `runtime/version.py` | 版本字符串唯一来源 | 改版本号 |

## 三、出站客户端（`src/wt_media_agent/clients/`，共用）

| 路径 | 职责 | 适用 | 何时进入 |
|---|---|---|---|
| `clients/bitbrowser/client.py` | BitBrowser 本地接口适配（脱敏 Profile 扫描、打开/关闭/更新、Cookie 读取） | Local | 改 BitBrowser 调用、窗口操作 |
| `clients/bitbrowser/factory.py` | `BitBrowserClient` 的唯一构造点（`bitbrowser_from_config`） | Local | 改客户端装配 |
| `clients/bitbrowser/timeouts.py` | 操作级超时规则（override 只能抬高、不能压低） | Local | 改超时策略 |
| `clients/bitbrowser/errors.py` | 适配器异常类型 | Local | 改错误语义 |
| `clients/cloud/client.py` | 与 Cloud 的通信客户端（回传、领取、心跳），请求超时来自 `cloud.timeout_seconds` | 共用 | 改回传、领取、心跳协议 |
| `clients/cloud/contract.py` | Cloud 响应契约兼容性校验 | 共用 | 改契约映射 |
| `clients/platform_identity.py` | 从 Cookie 直接读身份的通用平台（抖音等） | Local | 改通用身份读取 |
| `clients/bilibili/identity.py` | B 站身份（Cookie + 页面导航补全） | Local | 改 B 站身份 |
| `clients/baijiahao/identity.py` | 百家号身份（公开 logininfo 接口） | Local | 改百家号身份 |
| `clients/platform_urls.py` | 登录校验目的地 URL 表（`LOGIN_URLS` / `login_url()`）。**唯一**允许出现平台 `https://` 字面量的地方，`executors/` 不得自带 | Local | 改平台登录地址 |
| `cloud_agent_client.py`、`cloud_agent_contract.py` | **废弃导入路径**：转发到 `clients/cloud/` 的 re-export shim，仅为冻结的 `tests/test_runner_session.py` 保留 | 共用 | 不要在此处改逻辑 |

平台 URL 与端点常量随各自的 `clients/` 模块走：平台登录地址在 `clients/platform_urls.py`，代理探针地址在 `clients/bitbrowser/client.py` 的 `PROXY_PROBE_URL`。`executors/` 与 `local_api/` 下的 `http://`/`https://` 字面量**已清零**，并由 `tests/test_dependency_boundaries.py` 的 R9 常驻守护——在该处写 URL 会直接测试失败，不必靠人工记忆。

## 四、服务能力（`src/wt_media_agent/services/`，共用）

| 路径 | 职责 | 适用 | 何时进入 |
|---|---|---|---|
| `services/browser/cdp.py` | 浏览器 CDP 客户端（裸 WebSocket） | Local | 改浏览器自动化底层 |
| `services/browser/cookies.py` | 账号身份检查用的 Cookie 提取 | Local | 改 Cookie 提取 |
| `services/net/proxy.py` | 代理连通性探测与代理地址解析 | 共用 | 改代理检测、改地址解析 |
| `services/profile_guard.py` | Profile 保护规则 | Local | 改 Profile 校验 |

## 五、本地控制 API（`src/wt_media_agent/local_api/`，Local）

| 路径 | 职责 | 何时进入 |
|---|---|---|
| `local_api/server.py` | 环回控制 API（Desktop Rust 层代理到这里）。`main` 为**冻结符号**：打包与健康脚本按模块路径调用 | 改本地接口 |
| `local_api/reporting.py` | 本地控制面的响应装配（安全分组投影、检查项拼装、耗时） | 改响应形状 |
| `local_api/state.py` | 本地可观测状态与待上报结果队列 | 改状态上报 |

## 六、执行器（`src/wt_media_agent/executors/`，按任务类型编排）

| 文件 | 职责 | 适用 |
|---|---|---|
| `protocol.py` | `Executor` / `ExecutorFactory` 协议（声明在此，`executors/` 不得 import `runner/`） | 共用 |
| `profile.py` | Profile 打开/关闭/扫描等本地操作 | Local |
| `account_check.py` | 本地账号检查 | Local |
| `cookie.py` | Cookie 读取/写入 | Local |
| `proxy.py`、`proxy_mutation.py` | 代理连通性校验与写入 Profile | Local |
| `noop.py` | 空执行（用于链路验证） | 共用 |

## 七、任务 Runner（`src/wt_media_agent/runner/`，共用）

| 文件 | 职责 | 何时进入 |
|---|---|---|
| `runner.py` | `TaskRunner` 主循环（领取、租约、重试、取消、进度、异常） | 改任务执行语义 |
| `config.py` | `TaskRunnerConfig` | 改 Runner 配置 |
| `registry.py` | 任务类型 → executor 工厂的注册表 | 新增任务类型 |

## 八、执行状态与恢复（`src/wt_media_agent/storage/`）

| 路径 | 职责 | 何时进入 |
|---|---|---|
| `storage/checkpoint_store.py` | 检查点存储（断点恢复、离线结果） | 改断点恢复 |
| `storage/sqlite.py` | 本进程打开自身 SQLite 的连接策略（WAL 等） | 改连接策略 |
| `storage/migration.py` | 本地存储迁移。`DEFAULT_DB_NAME`、`MIGRATIONS`、`default_data_dir`、`default_db_path`、`apply_migrations`、`main` 均为**冻结符号** | 改本地库结构 |

`utils/time.py`：UTC 时间戳格式化的唯一实现，供落库与比较共用。

## 九、占位与生成（如实标注）

| 路径 | 现状 |
|---|---|
| `src/wt_media_agent/modes/` | 模式选择占位，尚无实现 |
| `src/wt_media_agent/adapters/` | 平台适配占位，尚无实现（平台 Playwright 适配未来在此引入） |
| `src/wt_media_agent/generated/` | 生成的契约类型，**禁止手改** |

## 十、契约、配置与测试

| 路径 | 职责 |
|---|---|
| `contracts/` | 本地契约：`local-agent-api/`、`local-error-codes/`、`local-event-schemas/`、`local-status-enums/` |
| `config/` | 运行时唯一读取的配置目录（`agent.toml` + README）；`config_online/` 为发布整目录替换源，运行时代码不得读取（`src/` 内不得出现该字面量） |
| `tests/` | 单元测试（按文件名对应模块） |
| `scripts/` | 构建与运行脚本 |

## 十一、禁止扫描区

- `src/wt_media_agent/generated/`（生成代码，除非任务就是核对生成结果）
- `__pycache__/`、`.venv/`、`uv.lock`、`dependency.lock`（除非诊断依赖问题）
