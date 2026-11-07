# 多 Agent 协作

[文档目录](index.md)

recoding 0.8 的每个 Session 有一个 Master 和多个 sub-agent。每个 Agent 的核心都是同一个永不停歇的模型—工具循环，没有传统 Turn、最终回答或自动完成状态。

## 循环和 sleep

```text
启动 / Resume：身份与恢复说明进入队列
    ↓
取自身全部未读消息 → Model call
    ↑                   ↓
    └── 工具结果 ← 完整工具批次
                        ↓ 没有 Tool call
                 系统补 sleep 调用及结果
                        └──→ 下一次 Model call
```

工具正常返回或返回错误结果后都继续模型调用。模型只输出文本时，系统保存原响应，再另加一条标为 `_system_generated` 的 assistant sleep 调用及配对结果，不修改原模型签名或把它冒充模型主动调用。默认 10 秒，可在右栏并发上限旁修改 `idle_sleep_seconds`（0.1–3600 秒）；模型主动调用 `sleep(seconds)` 可选 0–3600 秒。

sleep 是可被消息唤醒的工具，不是 Python/Bash sleep。自身队列有任何未读消息就立即返回，包括进入 sleep 前已收到的消息；新消息立即唤醒。它不消费正文，也不因其他 Agent 的状态或队列变化而唤醒。下一次 Model call 前统一 drain 正文。Stop/连接失效可中断等待，自身队列等待使用 sleep；Master 的全局观察工具单独定义。

每个 Agent 独立计算预算和进行[后台 Context 压缩](context.md)，不继承或共享别人的摘要；`recall_context` 也只读自身原始历史。后台进程按已提交快照生成摘要，不阻塞消息读取或正常 Model call；新摘要在后续调用边界加载；摘要本身禁用工具，不参与 Agent 通信或推进任务。其 API 用量仍加入 Session 的 All Agents 汇总。

首次 Model call 由系统启动，输入包含该 Agent 的名字、角色、ID 和运行说明。Resume 首次请求说明先前停止、现在恢复、工具内存重建及上次位置，不自动重放代码；做什么由 Agent 决定。之后没有 Turn 周期提示，普通回答也不停止 Agent。

## 工具和权限

| 工具 | Master | sub | 行为 |
| --- | --- | --- | --- |
| `create_agent(name, icon?, permissions?)` | ✓ | — | 创建有名、有图标、空白历史的 Agent；不携带任务 |
| `stop_agent(agent_id, reason)` | ✓ | — | 停止并回收 sub，保留状态和未读队列 |
| `resume_agent(agent_id)` | ✓ | — | 同 ID 恢复，重建工具内存；不携带指令 |
| `rename_agent(agent_id, name)` | ✓ | — | 改名，保留 ID、context、队列和运行环境 |
| `set_agent_icon(agent_id, icon)` | ✓ | — | 为 Master 自己或任意 sub 选择/修改图标，含已停止项 |
| `send_agent_message` | ✓ | ✓ | `agent_id` 单发或 `agent_ids` 群发，二选一 |
| `broadcast_agent_message` | ✓ | ✓ | 给所有其他可收件 Agent 广播 |
| `send_user_message` | ✓ | ✓ | 当前 Agent 一对一给人类发报告、询问或进展 |
| `read_global_messages` | ✓ | — | 可选读取整个 Session 的人机与 Agent 消息，分页、筛选、正文限额 |
| `listen_global_messages` | ✓ | — | 有超时的全局新消息等待，只返回信号，不读取正文 |
| `list_agents()` | ✓ | ✓ | 名称、图标、状态、模型、权限等安全元数据，无 context |
| `sleep(seconds)` | ✓ | ✓ | 有限等待，自身任何未读消息立即唤醒 |

消息工具都接受 `message`，可附 `images`（workspace 图片路径数组）。示例：

```json
{"agent_ids":["agent-a","agent-b"],"message":"任务：分别核验方法与边界。输入：artifacts/data.csv。输出：各自报告文件和有证据的结论。约束：只写自己的文件；用消息提交结果。","images":["/workspace/problem.png"]}
```

`agent_id` 与 `agent_ids` 不能同时给。群发/广播不能包含 user/system；给用户只能调用一对一的 `send_user_message`。普通 Agent 页面定向给当前 Agent，仍支持向停止项留言；All agents 汇总页的 @ 只列存活项，Master 存活时默认选择 Master。@all 只投递全部 starting/running/sleeping Agent；并列的 @all（除了 master）只投递存活 sub。后端再次检查接收者状态，不自动恢复停止项，没有存活接收者时不发送。Agent 的广播继续只投递可接收 peer mail 的运行项。发送方、目标、消息 ID 与发送时间均持久化，来源不能由模型伪造。

Agent 间只向 starting/running/sleeping 投递新消息，stopping/stopped/failed 拒收且不入队。群发先验证全部目标；广播只选择当前可收 peer mail 的目标并排除自身。真实用户可给停止或失败的 Master/sub 留言（含图片），持久保存为未读，Resume 后读取；发送不自动恢复或创建 attempt。停止前的未读消息保留。停止期间的宿主故障/配置通知仍单独延后保存，不自动恢复。

用户可以直接查看所有 Agent 的消息，包括 Agent 间对话。Master 不必重复转述 sub 的报告；需要时给出自己的判断、决策或有价值的综合结论。协调密集时应继续推进可做的工作，必要等待采用短且贴合下一次协调检查或截止时间的 sleep；真正空闲时才选择长等待。消息仍会立即唤醒 sleep，不能靠反复短休眠制造无意义的模型轮询。

Master 创建 Agent 后必须另发消息给它全部必要背景、任务、输入、期待输出、权限和文件归属。新 sub 不复制 Master 的对话、图片、推理、签名或 Python/Bash 内存。普通模型输出不自动转为给 Master 的结果；所有成果和问题必须显式发送。Master 主动寻找并行结构、及时指导和处理失败，也可设计共享文件、锁、任务表、SQLite 或 Python 多进程协议；重要发现和决策仍应发消息。

Master/sub 的共同 System 明确要求善用并管理并行，区分推理并行与执行并行：多个 sub 负责独立研究/判断/实现/验证；多个 Python 进程及有界多进程、线程、异步 I/O 处理计算；多个异步 Bash 执行构建/测试/服务；多个已授权 SSH 连接处理远端工作。应按依赖设计并行阶段和流水线，不把一个 Agent 固定对应一个进程，也不把同一持久进程中的 async 队列当成并行。长工作优先异步或委派，使各自 loop 能处理消息、失败和资源事件。Master 负责全局规划、Agent 容量与瓶颈协调；sub 自主管理任务内的 Python/Bash/SSH 并行，需要额外 Agent 时向 Master 申请。大家共用 Session 资源与 API 预算，避免重复写入、同进程 FIFO 自等待、嵌套过度并发和无效工作，完成后清理或转交资源。


## Python Pool 协作

任何 Agent 可创建、分享、撤销分享和删除自己拥有的 Python 进程。所有 Python/Bash 及其他工具共用一个 Session 沙箱；访问相同进程就是访问相同 namespace。使用 python_process / python / python_execution，Master 可管理和访问全部资源，不必先成为 owner。

共享主要用于短通信和同步，长计算应放入独立、未共享进程。进程内 FIFO，async 不改变这一点；别在执行中等待另一个同进程排队调用写字典。字典写入不唤醒 Agent，通知仍走消息。

所有具备工具的 Agent 可使用/读取任意进程，无需分享。Sub Stop 取消其提交并退役拥有的进程，默认 5 秒关闭宽限；可提前显式转交 Owner 保留状态。普通取消先软中断，不自动强杀其他 Owner 的持久进程；Session 关闭回收全部，Resume 不恢复内存、不复用旧进程 ID。完整规则见 [Python Pool](python-pool.md)。

## Master 按需观察全局消息

消息工具不再接受 `cc`，也不自动把其他人的消息转送给 Master。单发、群发、广播保留。旧存档中的抄送记录仍作为历史保留；全局观察不重复读取这些旧副本。

Master 可以主动选择两个只读工具。sub 不具备这两个工具，执行器和协调器也会拒绝越权调用。观察范围为**本 Session 的用户输入、Agent 间消息、Agent 发给用户的消息**，不含系统生命周期通知、模型正文、Thinking、工具结果或其他 Agent 的 context。

`read_global_messages` 默认返回最近 20 条匹配消息；可用 `senders`、`recipients` 指定人物 ID（含 `user`），同栏取“或”、两栏取“且”。未指定条件时包含所有人物及已停止 Agent 的历史消息。`after=0` 从最早消息开始，后续传上次的 `next_cursor` 逐页读取；`limit` 为 1–50。广播或群发的一次消息合并返回，保留原发送者与完整实际接收者。

例如，只看最近发给用户的报告，最多 10 条、正文合计最多 6,000 字符：

```json
{"recipients":["user"],"limit":10,"max_chars":6000}
```

`max_chars` 默认 12,000、范围 256–32,000，约束本次返回的正文字符总量，并非精确 token 数。长消息带 `truncated`、`message_id`、`next_offset`；需要时另用 `{"message_id":"...","offset":256,"max_chars":2000}` 分段读取。列表游标可在正文被截断后继续前进，长正文的剩余部分按 ID 单独读取。修改筛选条件后，如要回看被跳过的历史，应重新选起点或省略 `after` 查看最近记录。

`listen_global_messages` 等待全局新消息，默认 30 秒，允许 0–3600 秒。通常传入上次读取的 `next_cursor`；省略 `after` 表示从调用时刻开始监听。返回 `reason`、`read_after`、`latest_cursor` 和耗时，**不含消息正文或图片字节**。收到信号后，给读取工具传 `read_after`，不能直接跳到 `latest_cursor`，否则会跳过尚未读取的消息。读取有 `has_more` 时先继续分页，再等待。例如：

```json
{"after":42,"seconds":30,"recipients":["user"]}
```

监听中的 Master 若收到自己的未读消息，会立即返回，即使该消息不匹配监听筛选；下一次 Model call 仍通过正常队列读取。Stop 或连接断开会取消等待，其他 Agent 可以继续发送和工作。普通 `sleep` 语义不变，仍只监听自身收件箱。这不是后台永久订阅，每次观察都由模型主动选择一次 tool call。

这些读取不出队、不标记别人已读、不生成转发消息、不改目标 Agent 的 context。结果只作为本次工具结果进入 Master 的后续 Model call，因此正文越多，Master 的 Input tokens 越多；不用工具不会自动增加全局消息正文，但新增工具 schema/提示有少量固定开销，监听信号也有少量元数据开销。图片只有共享 workspace 路径和元数据，Master 需要时自行 `view_image`。观察到的内容是别人之间的原始消息，不是对 Master 的新指令或新的用户授权。

## 图文与 context

发送消息时，服务器将本地图片保存为发送者和接收者的不可变资产；接收者得到可 view 的 workspace 副本。网页按所属 Agent 的 HTTP 接口读取字节，原文件删除后仍能显示快照。

模型收到 Agent 消息时只有文字、图片元数据及路径，不自动包含图像字节。需要视觉理解时主动 `view_image(path)`。普通模型输出和发给用户的图也只用于显示，不因给人看过就加入视觉 context。只有用户粘贴图片和主动 view_image 才进入图像 context；这些已进入的图片在后续调用、Resume 和跨模型切换时继续保留。

## 人机交互

模型应使用 send_user_message 报告发现、询问、说明阻碍和提交结果，让用户只读人机消息即可了解所有必要内容。普通 Model call 文本属于内部过程，不代替消息。

每个 Agent 页面只显示该 Agent 自己发给用户的消息，Master 页面不汇总 sub 的报告。其他 Agent 的消息到各自页面查看；未查看的消息保留该 Agent 的未读提示，不因浏览 Master 消失。隐藏细节后仍保留双向人机消息；模型输出、peer 通信、系统通知和执行卡片均可隐藏。

## 停止、恢复与存储

全局 Stop 停全部 Agent；用户 Resume 只启动 Master，由它决定恢复哪些 sub。停止/失败保留 ID、名称、完整对话、图片、队列及设置，重建 Python/Bash，不反序列化内存或重放旧工具。Master/sub 共享 workspace 文件，进程、沙箱、Python/Bash 和 /tmp 各自独立。

并发 sub 默认 16，运行后可调 0–99，不含 Master；sleeping 仍占名额，回收完成才释放。降低上限不停止已有 Agent。创建/恢复与 Stop 的并发由代际、attempt 和启动登记保护，不能留下孤儿进程或误停新的恢复实例。

当前 Session/state/team index 为 v5，Web API v4；不兼容格式保留但不展示、不恢复、不转换。每次启动对应一个 Run，Iteration 为 Agent 终身模型调用序号，Resume 不归零；Session 累计量包含全部 Agent 和所有 Run。

Master 可用 `rename_agent(agent_id, name)` 给运行或已停止 sub 改名，1–64 字符，ID、文件夹、历史、队列和运行环境保持不变；其他 Agent 没有此管理权限。新名称保存到该 Agent 元数据与协作索引，列表和 TAB 即时更新。改名不分派新任务，任务变化仍通过消息发送。

System prompt 要求 Master 为自己与每个 sub 选择适合角色且不同的 Unicode 图标，包括已停止和失败的 Agent；启动/Resume 时通过 `list_agents` 检查并修正已有重复选择。这是模型的行为指引，不是后端唯一性约束。例如 `create_agent({"name":"文献核验","icon":"🔬"})`（此处表示工具参数），以后用 `set_agent_icon({"agent_id":"<id>","icon":"📚"})` 更新；Master 自身用 `agent_id="master"`。图标是短 emoji/符号，最多 16 个 Unicode 码点，支持肤色、旗帜和组合 emoji，不接受 URL、文件路径或多行文本。未指定时 Master 显示 🧭、sub 显示 🤖，由 Master 主动按角色选择。

后端只校验图标格式和修改权限，创建与修改允许重复，不维护图标占用表或自动分配唯一图标。是否更换、如何区分由 Master 依据 System prompt 和用户消息决定。

图标属于持久显示元数据：Master 保存在 `agents/index.json` 的 `master.icon`，sub 保存在自身索引条目的 `icon` 及自身 `session.json`。Stop/Resume、服务重启和移动 Session 后保留；修改不重建 worker，不改变 ID、历史或消息队列。tabs、Agent 列表、消息署名/路由、All agents、@ 接收者及速度图使用相同图标，历史消息也展示当前图标。sub 只能查看或向 Master 提出修改建议，不能自行调用管理工具。
