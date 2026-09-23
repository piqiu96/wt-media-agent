# wt-media-agent 目录地图

本文只记录实际存在的目录。定位代码时从本文件出发，禁止全仓库扫描。

Agent 有两种运行模式：**Local Agent**（运行在运营电脑）与 **Cloud Agent**（运行在云端执行环境），共用同一 Python 核心框架。下表对共享能力标注适用范围（Local / Cloud / 共用）。

## 一、入口与 Runtime（共用）

| 路径 | 职责 | 适用 | 何时进入 |
|---|---|---|---|
| `src/wt_media_agent/local_main.py` | Local Agent 进程入口 | Local | 改本地启动方式 |
| `src/wt_media_agent/cloud_main.py` | Cloud Agent 进程入口 | Cloud | 改云端启动方式 |
| `src/wt_media_agent/sidecar_main.py` | Sidecar 模式入口（由 Desktop 拉起） | Local | 改 Sidecar 启动行为 |
| `src/wt_media_agent/app.py` | 应用装配 | 共用 | 改模块组装 |
| `src/wt_media_agent/runner.py` | 任务执行 Runner（会话与执行循环） | 共用 | 改任务执行、心跳、进度、异常处理 |
| `src/wt_media_agent/config.py` | 配置加载 | 共用 | 改配置结构 |
| `src/wt_media_agent/constants.py` | 常量 | 共用 | — |
| `src/wt_media_agent/log_setup.py` | 日志初始化 | 共用 | 改日志 |
| `src/wt_media_agent/proxy_check.py` | 代理可用性检查 | 共用 | 改代理想检测 |

## 二、Cloud 通信（共用）

| 路径 | 职责 | 何时进入 |
|---|---|---|
| `src/wt_media_agent/cloud_agent_client.py` | 与 Cloud 的通信客户端 | 改回传、领取、心跳协议 |
| `src/wt_media_agent/cloud_agent_contract.py` | Cloud Agent 契约类型 | 改契约映射 |

## 三、本地控制 API（Local）

| 路径 | 职责 | 何时进入 |
|---|---|---|
| `src/wt_media_agent/local_api/server.py` | 环回控制 API（Desktop Rust 层代理到这里） | 改本地接口 |
| `src/wt_media_agent/local_api/state.py` | 本地状态 | 改状态上报 |

## 四、执行器（`src/wt_media_agent/executors/`，按任务类型编排）

| 文件 | 职责 | 适用 |
|---|---|---|
| `profile.py` | Profile 打开/关闭/扫描等本地操作 | Local |
| `account_check.py` | 本地账号检查 | Local |
| `cookie.py` | Cookie 读取/写入 | Local |
| `proxy.py`、`proxy_mutation.py` | 代理读取与写入 | Local |
| `noop.py` | 空执行（用于链路验证） | 共用 |

## 五、运行时适配（`src/wt_media_agent/runtimes/`）

| 文件 | 职责 | 适用 | 何时进入 |
|---|---|---|---|
| `bitbrowser.py` | BitBrowser 本地接口适配 | Local | 改 BitBrowser 调用、窗口操作 |
| `cdp_client.py` | 浏览器 CDP 客户端 | Local | 改浏览器自动化底层 |
| `environment.py` | 运行环境检测 | 共用 | 改环境校验 |

## 六、执行状态与恢复

| 路径 | 职责 | 何时进入 |
|---|---|---|
| `src/wt_media_agent/storage/checkpoint_store.py` | 检查点存储（SQLite） | 改断点恢复 |
| `src/wt_media_agent/storage/migration.py` | 本地存储迁移 | 改本地库结构 |
| `src/wt_media_agent/core/profile_guard.py` | Profile 保护规则 | 改 Profile 校验 |

## 七、占位与生成（如实标注）

| 路径 | 现状 |
|---|---|
| `src/wt_media_agent/modes/` | 模式选择占位，尚无实现 |
| `src/wt_media_agent/adapters/` | 平台适配占位，尚无实现（平台 Playwright 适配未来在此引入） |
| `src/wt_media_agent/generated/` | 生成的契约类型，**禁止手改** |

## 八、契约与测试

| 路径 | 职责 |
|---|---|
| `contracts/` | 本地契约：`local-agent-api/`、`local-error-codes/`、`local-event-schemas/`、`local-status-enums/` |
| `tests/` | 单元测试（bitbrowser、cookie、proxy、profile、sidecar、runner 会话等，按文件名对应模块） |
| `config/` | 运行时唯一读取的配置目录（`agent.toml` + README）；`config_online/` 为发布整目录替换源 |
| `scripts/` | 构建与运行脚本 |

## 九、禁止扫描区

- `src/wt_media_agent/generated/`（生成代码，除非任务就是核对生成结果）
- `__pycache__/`、`.venv/`、`uv.lock`、`dependency.lock`（除非诊断依赖问题）
