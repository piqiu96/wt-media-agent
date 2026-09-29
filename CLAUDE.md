# WT Media Agent（Claude Code 入口）

## 项目定位

本仓是 WT Media 的 Python 执行工程。Local Agent 负责运营电脑上的浏览器、Profile、Cookie、代理和本地文件操作；Cloud Agent 共用核心框架，在不同环境执行已授权任务。正式业务状态由 Cloud 裁决。

## 关联工程

- `wt-media-cloud`：正式业务事实、API、任务状态、云端内容发现和 Cloud Compose Worker 视频合成。
- `wt-media-desktop`：Local Agent Sidecar 生命周期、Tauri 原生桥和桌面交互。
- `wt-media-workspace`：维护系统级产品、架构、跨仓协议、决策和 Delivery；关联工程物理路径由 Workspace 的 `config/repository-map.yaml` 维护。

## 任务执行

先用 [AGENT-INDEX.md](AGENT-INDEX.md) 确认本仓规则；目标位置或 Local／Cloud 模式不明确时使用 [DIRECTORY_MAP.md](DIRECTORY_MAP.md)。
任务关联 Workspace CHG 时，读取其 Agent 范围和 References；单仓分析、定位和明确的小修改可直接处理。
按任务逐步读取相关代码、文档和测试，不默认全仓扫描。

## 相关文档

- 本仓 `contracts/`：本地控制 API、错误和事件契约；`tests/`：相关测试。
- Workspace `docs/product/`、`docs/engineering/`、`docs/contracts/`、`docs/decisions/`：系统级事实来源。
- Workspace `delivery/`：Milestone、CHG 和交付状态；关联任务可从 `.ai/CURRENT_CONTEXT.md` 获取执行快照。

## 相关约束

- Agent 不连接 Cloud MySQL，也不裁决正式业务状态。
- 本机敏感操作须经过授权；结果无法确认的外部副作用不盲目重试。
- 云端内容发现和视频合成归 Cloud；Workspace 不成为 Agent Runtime 的依赖。
