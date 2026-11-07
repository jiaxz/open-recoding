# Recoding 文档

README 负责介绍核心思路和界面。本目录按任务组织当前使用、设计和维护说明；[AGENTS.md](../AGENTS.md) 是维护者的简短约束入口。

## 先理解项目

1. [项目概览与阅读路线](understanding.md)：从使用目的开始，沿 Session → MultiAgentSystem → Agent 理解源码。
2. [架构总览与源码地图](architecture.md)：控制/执行平面、Agent/Node/Process、当前实现边界、依赖、进程和 RPC。
3. [多 Agent 协作](sub-agents.md)：消息、权限、sleep、Python Pool 和全局观察。

## 开始使用

| 文档 | 解决的问题 |
| --- | --- |
| [首次初始化](first-run.md) | Coding Agent 怎样帮助用户安装、配置、验证并启动 |
| [配置与启动](configuration.md) | 环境变量、模型选择、启动参数、远程访问和服务日志 |
| [Web 使用](guide.md) | Session 管理、图文消息、显示范围和停止恢复 |
| [CLI / TUI](cli.md) | 终端聊天、`@`、`/`、速度图和 Shell 工作流 |
| [Python Session](python-session.md) | 在普通 Python 程序中创建 Session、收发消息和关闭 |
| [常见问题](troubleshooting.md) | 依赖、连接、模型错误、锁和恢复 |

## 设计与维护

| 文档 | 负责范围 |
| --- | --- |
| [包环境与维护](packages.md) | Workspace 共享依赖、home 缓存、清单和迁移 |
| [统一执行池设计](execution-pool-design.md) | 每节点通用 ProcessPool、开放访问、Owner 退役与 Master 管理 |
| [Bash 工具](bash-pool.md) | 临时/持久、同步/异步、逐命令结果、有状态 SSH 与回收 |
| [Python 工具](python-pool.md) | 进程状态与 Owner、同步/异步、软取消、恢复与日志 |
| [计算节点与 NodeRuntime](nodes.md) | Local/SSH 节点、Web 资源管理、寻址、重连与租约 |
| [Host 中转文件传输](file-transfer.md) | 节点文件绑定、同步/异步复制、进度、取消与完整性 |
| [模型工具清单](tool-calls.md) | Master/sub-agent 实际可见的全部工具与权限范围 |
| [Non-builtin 工具](non-builtin-tools.md) | 自由项目代码与正式工具的边界、静态接入和沙箱执行 |
| [维护脚本](../maintenance/README.md) | Host 侧维护入口与包盘点命令 |
| [Host Coding Agent 维护](host-maintenance.md) | 借助外部 Coding Agent 维护项目、包、工具、Session 与 context 压缩 |
| [编辑、Bash 与导航](coding-tools.md) | 版本冲突与撤销、Bash 后台任务与 Python Pool、有界代码定位 |
| [工具与沙箱](tools-and-sandbox.md) | 工具语义、执行进程、包、网络、GPU 和隔离边界 |
| [Host 协议](session-ownership.md) | run / stop、进程独占、GUI 连接和只读浏览 |
| [存档与数据](session-storage.md) | 文件布局、轻量索引、移动、备份和数据保留 |
| [模型与 API](model-api.md) | 中性配置、集中模型资料、协议适配和未来更换路线 |
| [模型 Context](context.md) | 原文、请求投影、模型切换、预算与后台自动压缩 |
| [运行观测](observability.md) | 用量、缓存、速度与计时口径 |
| [API 性能与诊断](api-performance.md) | HTTP / SSE、重试、错误分类和对照排查 |
| [包用途与维护优先级](package-priorities.md) | 包的用途、重要程度、替代关系和依赖维护规则 |
| [工具完整度](tool-inventory.md) | 覆盖评分、发现机制、刷新和 Host 建议 |
| [界面行为约定](ui-contracts.md) | 消息顺序、局部展开、过滤、导航与输入状态 |
| [开发与测试](development.md) | 定向检查、完整测试入口、打包和测试数据 |
| [格式与升级](migration.md) | 当前版本、兼容范围和同版本移动 |
| [源码快照](snapshots.md) | 独立源码副本与验证边界 |
| [品牌资源](branding.md) | Logo、Logomark、打包副本和文档截图 |

- [执行模式：Sandbox 与 Host](execution-modes.md)：显式选择、路径、风险、启动/恢复与回收边界。
