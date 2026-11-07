# Node Process Pool

[文档目录](index.md) · [当前架构](architecture.md) · [计算节点](nodes.md)

## 职责和权威状态

每个 NodeRuntime 有一个通用 ProcessPool，同时管理 Python、Bash 及后续注册的进程类型。Pool 位于 core/node/process_pool.py，统一负责 ID、名字、Owner、创建/删除/转交、FIFO 队列、Execution、超时、中断请求、退役与回收。不是两套语言 Pool 各自实现生命周期。

core/tools/python_pool 与 bash_pool 提供模型 schema、薄服务绑定和 Driver。Driver 只负责解释器启动、代码协议、结果边界、中断及关闭；Python 对象、Bash 变量/函数/cwd 仍在实际语言进程里。Driver 不维护另一个任务队列或 Agent 权限表。

core/node/pool_client.py 在 Host 保存展示/归档投影、增量事件游标并投递系统通知；它不是第二个队列或 Owner 权威。NodeRuntime 产生带版本的元数据，Host 断线后按游标补齐；超出有界事件窗口则取当前记录。投影保留有界历史，完整日志另存。

Agent 经注入工具端口自主使用节点，Node 操作不经过 MultiAgent 代办。MultiAgent 管 Agent 生命周期和通信，Session 将停止通知传给各节点。节点仅接受可信 Host 信封中的不透明调用者 ID、attempt 和管理权限，不导入 Agent/MultiAgent，也不接受模型自行声明管理员。

## 访问与管理

所有具备对应工具且满足节点网络/GPU 委派要求的存活 Agent，都可列出、使用、读取本 Session 任意进程与执行记录，不需要 share/revoke/participants。

- creator 永久记录创建者；owner 初始为创建者，可显式 transfer。
- Owner 或 Master 可 rename/transfer/delete；名字为 1–100 字符展示标签，允许重复。调用始终使用稳定 ID，改名不改历史或内存。
- 所有 Agent 可 list/status/read。只有提交者、当前 Owner 或 Master 可 cancel；Bash 管道 write 使用相同控制权限。
- Master 可管理所有节点进程，无需先取得 ownership。
- 每个 Execution 独立保存 submitted_by。同步结果返回原调用者；异步完成只通知提交者，Owner 转交不会重定向执行结果。

这些是同 Session 合作协议，不是对抗性安全隔离。任意代码仍可能通过共享文件、进程或外部系统互相影响。Node 的环境/设备约束及正式 Host 协议继续生效。

## 排队与并发

进程可临时或持久，调用可 sync 或 async，两者相互独立。不传 process_id 自动创建临时进程，结束回收；显式 create 才保留状态。同进程 FIFO，最多 128 个待执行调用；不同进程和节点可并行。当前每种类型每节点最多 32 个存活进程。

执行超时从真正开始执行算起，不包含排队。sync 省略 timeout 使用工具超时，async 默认无限，显式 0 为无限。Python 使用 check_cancel、取消标记与 SIGINT；Bash 持久进程尝试中断当前命令，SSH Shell 中断可能丢失连接。取消不是回滚，部分对象、文件和外部副作用保留。

共享状态适合短通信/同步；不能在一个进程中等待同进程尚未出队的代码来修改字典。长计算使用独立进程或异步执行。并行策略和预算由 Agent 决定，Pool 只保证约定的排队和生命周期。

## Agent Stop

1. Host 将被停止 Agent 的 ID 和 attempt 通知每个已启动 Node Pool。
2. 取消它提交的全部运行/排队 Execution，包括其他 Owner 的进程。
3. 它当前拥有的进程原子进入 closing：拒绝新提交，取消尚未开始的队列。
4. 当前是其他 Agent 的执行时，允许结束后关闭进程；空闲进程立即进入回收。默认关闭宽限期 5 秒，届满强制关闭，不能无限等待。
5. 已转交给其他 Owner 的进程不因旧 Owner Stop 被删除；但旧 Owner 自己提交的未完成执行仍被取消。
6. 普通取消不自动强杀其他 Owner 的持久进程。2 秒未响应标 unresponsive 并通知 Master，Owner/Master 可明确 delete。
7. 同一 attempt 的迟到创建/提交拒收；Resume 的新 attempt 可创建全新资源，不重放旧调用。

Owner Stop 不再自动转交资源给 Master。要保留有用状态，在 Stop 前显式 transfer。Master 的单个语言脑 Worker 生命周期不是整个 Session Pool 的终点；Session Stop/close 回收所有节点环境。

## 工具与持久化

- python_process / bash_process：create、list、rename、transfer、delete。
- python / bash：提交代码；mode=sync/async。
- python_execution / bash_execution：list、status、read、cancel；Bash 临时异步管道还支持 write。
- pool_status：所有具备工具的 Agent 均可查看有界元数据总览，不返回 namespace、代码、日志正文或 Agent Context。

Host 存档仍保存 python-pool.json、bash-pool.json 两个按类型过滤的投影，便于原有 Web/CLI 展示与停止浏览。它们不是两个运行时 Pool。节点的通用进程记录在 workspace/.recoding/runtime/process-pool/<session-id>/，输出沿用 python-pool 和 executions 日志位置。进程内存和 SSH 连接不写入存档，Resume 不恢复对象或重放代码。

Local 与 SSH 使用相同实现；已有资源 ID 自动路由，新进程可指定 node_id。节点之间不隐含共享文件，使用 Host 中转 transfer_files。网络不可达时执行状态未知，不自动重放或假报回收成功；失联租约负责最终节点环境回收。

## 回收边界

Pool 回收语言进程与可追踪后代；Node supervisor、进程组、subreaper 和 sandbox PID namespace 提供额外回收边界。Host 模式没有 namespace 隔离。未使用每进程 cgroup，不保证任意脱离监管后代或 SSH 远端副作用都被完整回收。

新增语言主要实现 Driver 的 start/execute/close，必要时提供 write；经 Session 注入 drivers，不给 Node 加 tools 反向依赖，也不复制 Owner、队列和回收逻辑。
