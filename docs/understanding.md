# 理解 Recoding

[文档目录](index.md)

这份文档写给第一次接手项目的 Coding Agent。用户要求“理解这个项目”时，先沿阅读路线建立对象和运行地图，再解释 Recoding 的用途与使用方式；用户希望实际运行时，再按 [首次初始化](first-run.md) 配置环境。需要用户提供 API 或其他信息时，说明缺少什么。

## 项目定位

Recoding 是原生 multi-agent coding agent，提供 Web、CLI / TUI、Shell 命令和 Python API。Master 和 sub 都是持续运行的 Agent，通过消息接受任务、协作和报告；普通模型文本不是任务完成或人机交互的边界。

## 能力与实现

| 方面 | 当前实现 | 详细说明 |
| --- | --- | --- |
| 核心协作 | 独立 Agent 历史、endless loop、持久消息、命名Python Pool | [多 Agent](sub-agents.md) |
| 执行环境 | 每 Session 每 Node 一份共享 NodeRuntime；Local 必选，支持 SSH 远端、sandbox/direct、Bash/Python Pool 和 Host 中转文件 | [工具与沙箱](tools-and-sandbox.md) |
| 图文与视觉 | 用户粘贴、主动看图、代码绘图、图文消息和不可变服务器资产；展示与模型视觉输入分开 | [图片与工具](tools-and-sandbox.md#看图与图片理解) |
| Session | 对话、队列、图片、设置与执行位置持久化；Stop 保留已提交步骤，Resume 重建执行内存 | [存档](session-storage.md) |
| 模型 | Grok / Gemini 共用 Session 配置；切换只转换请求副本，原始来源和签名保留 | [模型与 Context](context.md) |
| 观测 | 调用阶段、用量、缓存与 Non-cache、逐调用速度、并发合计速度、工具完整度 | [运行观测](observability.md) |
| 组合使用 | Apps → Session → multiagent；团队统一持有 Master/sub，Session 可由 Python 直接调用 | [架构](architecture.md) |

默认 sub 并发上限为 16，不含 Master，运行中可改 0–99；默认兜底 sleep 为 10 秒。Core / Web 使用 Python 标准库，TUI 使用 `prompt_toolkit` 与 Rich。当前执行层依赖 Linux、Bash 和系统 Python；默认 sandbox 另需 Bubblewrap，显式 Host 模式不需要。

## 逐步阅读路线

源码路径均相对项目根目录；先按顺序建立运行地图，再按具体问题深入。

1. **确认目标与维护约定。** 阅读 [README](../README.md)、[AGENTS.md](../AGENTS.md) 和 [首次初始化](first-run.md)，理解 endless loop、消息输入、Stop/Resume、Host 独占和测试数据约定。区分 Agent 的睡眠/停止阶段与 Session 的 run/stop 状态。
2. **建立对象和依赖地图。** 阅读 [架构说明](architecture.md)，先建立控制/执行平面以及 Agent、Node、执行环境和 Process 的概念边界，区分当前实现与目标，再辨清 `Session`、`MultiAgentSystem`、`Agent` 与 `AgentLoop` 的职责；Web/CLI 是外壳，HTTP 服务由 Web 启动。
3. **从公共 Session 接口往里走。** 对照 [Python 使用示例](python-session.md) 阅读 `core/session/session.py`、`store.py`、`saved.py` 和 `hosting.py`。追踪创建、浏览、Resume、发送用户消息和关闭过程；理解只读存档与运行对象的区别。
4. **理解团队怎样协作。** 阅读 `core/multiagent/system.py`、`tools.py`、`prompts.py`，再看 [多 Agent 协作](sub-agents.md)。团队创建并持有 Master/sub；Host Agent 直接引用 team，经 dispatch 处理协作请求，执行和维护才交给 Session。追踪消息投递与读取、sleep 唤醒及失败通知；Pool 管理在 `core/node/process_pool.py`，语言接口与驱动在 `core/tools/python/`，不是 Agent loop 的职责。
5. **理解单 Agent 如何执行。** 先读 `core/multiagent/loop.py`，再看 `agent.py`、`worker.py`、`tools.py` 和 `events.py`。沿“加载压缩投影 → 一次读取消息和模型设置 → Model call → 完整工具批次 → 提交记录”走一遍，区分 Host Agent 的团队引用与纯 AgentLoop 的窄接口；锁、Context/图片存储集中在 `storage.py`。随后结合 [工具与沙箱](tools-and-sandbox.md) 看 `core/node`、`core/tools/python` 和 `core/tools/bash` 的进程、IPC、所有权和持久状态；[文件传输](file-transfer.md) 对应 `core/node` 的受限端点/SSH/中转及 `core/tools/file_transfer`，Local/SSH 共用 NodeRuntime 协议；进一步阅读 [计算节点](nodes.md)。
6. **连接到界面和观测。** Web 从 `apps/web/recoding_web/server.py`、`host.py` 和 `static/` 进入；CLI 从 `apps/cli/recoding_cli/main.py`、`host.py`、`tui.py` 进入。对照使用说明，区分展示事件、模型 context 与持久消息。模型传输与指标见 [API 性能与诊断](api-performance.md)。
7. **用小范围示例验证理解。** 阅读 `tests/test_architecture.py`、`test_agent_package.py` 和 `test_session_api.py`，再按关注点选择消息、Python Pool 或 Host 测试。需要执行时只运行相关用例，按 [开发与测试](development.md) 使用本机替身；新测试数据放在专用 `sessions/tests/`，结束后关闭资源并自动清空。

完成这条阅读路线后，应能向用户说明：一次输入经过哪些对象、下一次 Model call 何时读到消息、代码在哪个进程执行、什么状态会持久化，以及 Stop/Resume 会保留和清空什么。

## 接着做什么

- 用户希望安装和运行：按 [首次初始化](first-run.md) 检查已有环境，完成必要步骤并交付实际结果。
- 用户希望了解界面：[Web](guide.md)、[CLI / TUI](cli.md)。
- 用户希望调用核心：[Python Session 示例](python-session.md)。
- 用户希望修改代码：先看 [架构](architecture.md)，再按模块选择 [开发检查](development.md)。

只要求理解时，阅读和说明即可；安装、启动、模型连通验证按用户的实际任务推进。
