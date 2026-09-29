# WT Media Agent（Codex 入口）

## 项目定位

本仓是 WT Media 的 Python 执行工程。Local Agent 负责运营电脑上的浏览器、Profile、Cookie、代理和本地文件操作；Cloud Agent 与其共用核心框架，运行环境不同。
Agent 执行已授权任务并回报结果，正式业务状态由 Cloud 裁决。云端内容发现和 Cloud Compose Worker 的视频合成归 Cloud。

## 关联工程

- `wt-media-cloud`：正式业务事实、API、任务状态和已确认的云端执行能力。
- `wt-media-desktop`：Local Agent Sidecar 生命周期、Tauri 原生桥和桌面交互。
- `wt-media-workspace`：系统级产品、架构、跨仓协议、决策和 Delivery；关联工程物理路径由其 `config/repository-map.yaml` 维护。

## 任务执行

用本仓 [AGENT-INDEX.md](AGENT-INDEX.md) 确认职责和工作规则；目标位置或运行模式不明确时用 [DIRECTORY_MAP.md](DIRECTORY_MAP.md) 定位。
任务关联 Workspace CHG 时，读取其 Agent 范围和 References；分析、定位和明确的小修改可直接处理。按任务读取相关代码、契约与测试，不默认全仓扫描。

## 相关文档

- 本仓 `contracts/`：本地控制 API、错误和事件契约；`tests/`：相关测试。
- Workspace 的 `docs/product/`、`docs/engineering/`、`docs/contracts/`、`docs/decisions/`：系统级事实来源；`delivery/`：CHG 与交付状态。
- Workspace 发起或关联 CHG 的任务，可从 `.ai/CURRENT_CONTEXT.md` 获取执行快照，并以对应 CHG 确认范围。

## 相关约束

- Agent 不直接连接 Cloud MySQL，不自行决定正式业务对象的最终状态。
- 运营电脑上的敏感操作须经过授权；外部副作用结果无法确认时不盲目重试。
- Agent 不承担 Cloud 的内容发现或视频合成，不成为 Workspace 的运行时依赖。
