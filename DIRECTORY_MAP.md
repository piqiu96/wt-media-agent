# WT Media Agent：目录地图

本文件只用于代码定位、运行模式、验证入口和默认扫描边界。架构与执行规则见 `AGENT-INDEX.md`，实现事实以当前代码为准。

## Python 运行时

| 位置 | 主要内容 | 模式 | 适用任务 |
| --- | --- | --- | --- |
| `src/wt_media_agent/bootstrap/` | 生产装配与进程入口 | Local／Cloud／Sidecar | 启动顺序、服务装配 |
| `src/wt_media_agent/runtime/` | 配置、路径、日志和运行环境 | 共用 | 配置、日志、发布环境 |
| `src/wt_media_agent/clients/cloud/` | Cloud 通信与兼容检查 | 共用 | 领取、心跳、回传协议 |
| `src/wt_media_agent/clients/bitbrowser/` | BitBrowser 客户端 | Local | Profile 与浏览器窗口 |
| `src/wt_media_agent/clients/` | 其他平台客户端与身份适配 | 主要为 Local | 平台接入、出站调用 |
| `src/wt_media_agent/services/` | 浏览器、Cookie、代理等执行辅助能力 | 主要为 Local | 本机执行细节 |
| `src/wt_media_agent/executors/` | 具体任务动作 | 按任务类型 | 执行结果与副作用 |
| `src/wt_media_agent/runner/` | 任务循环、进度、取消和重试 | 共用 | 任务执行语义 |
| `src/wt_media_agent/storage/` | 本进程状态与检查点 | 共用 | 恢复、本地持久化 |
| `src/wt_media_agent/local_api/` | Desktop 使用的本地控制 API | Local | 本机接口、健康状态 |
| `src/wt_media_agent/adapters/` | 平台适配预留目录，当前不视为已交付实现 | 按任务确认 | 新平台适配 |
| `contracts/` | 本地 API、错误与事件契约 | Local／共用 | 对外接口变化 |
| `config/`、`config_online/` | 运行配置与发布替换源 | 共用 | 配置和打包 |

## 测试与验证入口

| 范围 | 优先入口 |
| --- | --- |
| Python 模块 | `tests/` 和目标模块附近的测试；仓库已有 `pyproject.toml` 配置 |
| Cloud 任务协议 | `contracts/`、`src/wt_media_agent/clients/cloud/` 与 Cloud 提供方 |
| Desktop 本地接口 | `src/wt_media_agent/local_api/`、`contracts/` 与 Desktop 调用方 |
| Sidecar 与发布 | `scripts/` 中已有的构建、打包和验证脚本 |

优先复用仓库已有验证方式，不在本文件复制具体命令。

## 工具

- `scripts/`：开发、验证和发布脚本。
- `bin/`：本地进程控制及运行辅助。

## 默认跳过

日常代码检索默认跳过：

- Python 缓存、虚拟环境和依赖目录，如 `__pycache__/`、`.venv/`。
- 构建产物、日志、缓存、临时文件和覆盖率结果。
- 依赖锁文件与其他生成内容。
- `.git/` 等版本控制元数据。

任务直接涉及这些内容时再进入。
