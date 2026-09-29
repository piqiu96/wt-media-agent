# WT Media Agent Index

本文件只记录 Agent 的长期工作规则，不记录当前 Milestone、CHG、临时任务状态或本机路径。

## 1. 仓库职责

- Agent 执行 Cloud 授权的任务并回报进度与结果；正式业务对象和最终状态由 Cloud 裁决。
- Local Agent 负责运营电脑上的 BitBrowser、Profile、Cookie、浏览器自动化、代理和本地文件执行。
- Cloud Agent 与 Local Agent 共用 Python 核心能力，按各自运行环境执行已授权任务。
- 云端内容发现与 Cloud Compose Worker 的视频合成归 Cloud；Agent 不实现该 FFmpeg 合成链路。
- Desktop 负责 Local Agent Sidecar 生命周期和 Tauri 原生桥。
- Workspace 维护系统级 Product、Architecture、Contract、Decision 和 Delivery；Agent 运行时和发布产物不依赖 Workspace。

## 2. Workspace 协作

- 分析、定位和明确的小范围单仓修改可以直接进行，不自动创建 CHG。
- 已关联 CHG 时，以其目标、范围和 References 为任务依据，只实施已确认的 Agent 范围。
- References 是优先入口，不限制任务确实需要的进一步查证。
- 涉及跨仓 Contract、系统架构、仓库职责或扩大 CHG 范围时，先回 Workspace 更新共同定义。
- Workspace 发起的任务直接使用当前 Workspace 上下文；独立执行需要 Workspace 时使用已提供的根目录或环境配置，无法定位则要求提供路径。
- 不复制 Workspace 文档，也不在 Agent 建立第二套 Delivery、任务状态或临时 context。

## 3. 上下文加载

按任务逐步读取：

1. 理解当前任务；有关联 CHG 时读取对应 CHG。
2. 按需要读取相关 Product、Engineering、Contract 或 Decision。
3. 目标位置不明确时用 `DIRECTORY_MAP.md` 区分 Local、Cloud 和共用模块。
4. 进入目标模块后，再读取直接依赖、调用方和相关测试。

默认不做全仓代码扫描，不读取 `delivery/completed`、历史材料和全量 Skills，也不扫描构建、缓存、临时或生成目录。任务需要时可以扩大范围，并说明目的。

## 4. 本仓架构

- `bootstrap/app.py` 统一装配运行资源；Local、Cloud 和 Sidecar 入口复用核心能力，按运行模式启用服务。
- Runner 组织任务，Executor 实际执行；Client、Service 和 Storage 提供底层能力，不反向依赖执行器。
- Agent 不直接连接 Cloud MySQL，不自行裁决正式业务状态；执行成功不等于业务成功。
- 运行期读取 `config/`，`config_online/` 仅作发布替换源；凭据不进入可分发配置。
- Agent 自己装配和保存日志；Desktop 不全量转存 Agent 日志。
- 本地 API 与平台适配只提供执行能力，不复制 Cloud 业务规则。
- 外部副作用必须受任务授权；结果无法确认时不盲目重试。

## 5. 修改与验证

- 从目标代码开始调查；位置或运行模式不明确时使用 `DIRECTORY_MAP.md`。
- 实现事实以当前代码、Contract 和实际运行模式为准。
- 验证从最小相关范围开始，优先使用仓库已有测试和脚本。
- Cloud 通信或本地 API 变化时，核对 Cloud 或 Desktop 的对应接口与消费方。
- 真实浏览器、账号或本地文件验证需明确执行环境和授权，不默认运行全量端到端操作。
- 不修改生成产物或依赖锁文件，不覆盖、还原或提交他人已有工作区改动。
- CHG 任务完成后按 Workspace 当前 Delivery 规则回写状态。
