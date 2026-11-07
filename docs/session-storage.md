# Session 存档与观测数据

[文档目录](index.md)

Python Pool 的进程/执行元数据保存在 Session 的 python-pool.json，完整日志在 workspace runtime/python-pool。只保存记录，不序列化 namespace、线程或 GPU 内存。Sub Stop 取消自身提交并退役其拥有的进程；Session 关闭才回收整个执行环境。Bash 元数据同样保存在 bash-pool.json。恢复不重建进程、不复用旧 ID，详细规则见 [Python Pool](python-pool.md)。

工作文件属于 workspace；对话及运行记录属于 Session。同一个 workspace 可以有多个 Session，共享工作文件和辅助依赖，各自拥有独立的模型 context 与 Python/Bash 进程。所有 Session 的 Agent 和Python Pool 复用 workspace 包目录。浏览器通过 HTTP 与服务器交换数据，存档始终位于服务器。

当前格式为 `recoding.session` v5，Master ID 为 master。每个 Agent 是没有 Turn 的 endless loop：Master 存档在根部，sub 在 agents/<id>/，所有通信记录统一保存在 agents/index.json 的 messages，收件箱是按目标过滤的视图。普通模型文本不自动成为报告或人机消息。Web 打开已有 Session 默认仅浏览，不启动任何 Agent；浏览提醒覆盖输入区，Resume 前禁止输入；用户点击 Resume Master 后才启动 Master，sub 由它决定恢复。新建 Session 仍立即启动。见[多 Agent](sub-agents.md)。

## 目录与数据职责

```text
workspace/
  your-files…
  .recoding/
    inputs/                        上传图片的工具可访问副本
    runtime/home/                  工具 HOME 与配置/数据，避免新增根部隐藏目录
    registry.json                  搜索根的可重建导航索引
    runtime/python-packages/       Workspace 共享辅助包
    runtime/python.json            工具解释器标识
    runtime/packages.lock          托管安装与迁移的进程锁
    <timestamp>-<short-id>/
      session.json                 稳定 ID、名称、置顶、模型/工具设置
      state.json                   当前完整 context、终身 iteration/tool_count 与执行位置
      context-window.json          用户确认后的增量摘要缓存（原 context 不截断）
      context-budget.json          本 Agent/模型的 API Input 锚点与暂缓门槛
      tool-inventory.json          本 Agent 最近工具覆盖报告（不含 API key/完整环境变量）
      events.jsonl                 带 job/sequence/timestamp 的展示事件
      images/<sha256>.png|jpg       按内容寻址的不可变图片快照
      runs/context_<timestamp>.jsonl
                                   每次运行的原始上下文和调用诊断
      owner.lock                   Agent/worker 实际资源锁
      host.lock                    正式 Host 协议的进程独占锁
```

Recoding 在 workspace 根部只创建 `.recoding` 这一个隐藏目录；新建环境和工具数据使用可写的 `runtime/`。输入图片、运行环境、配置和私有存档都放在其中。项目原有的 `.git`、`.venv` 等目录保持不动。沙箱只开放 `inputs/` 与 `runtime/`，其余存档内容继续隐藏；工具 HOME 映射到 `.recoding/runtime/home`，XDG 配置与数据也在其下，公共缓存仍使用 home 级缓存。

旧 `.agent-inputs` 在下一次上传或初始化工具时迁入 `inputs/`；已有目标则只合并无冲突文件，同名不同内容或软链接会报错并保留原数据。原始 context 和模型签名不改写，旧路径标签可由 `view_image` 映射读取；Python/Bash 应使用新路径。升级前先关闭同 workspace 的旧 Host，再启动新版本，避免旧执行进程继续使用原路径。

`state.json` 是恢复用的当前快照，不是每次请求的独立副本；写入采用临时文件加原子替换。`events.jsonl` 与 `runs/` 是追加日志：前者用于界面和统计，后者记录 System、工具定义、完整消息、模型请求、传输观测、取消及回滚等。所有 Agent 的停止/故障都保留已提交步骤，未完成工具配对闭合；没有回滚整个无限历史的路径。日志仍保留曾经执行过的事实。API key 不写入这些存档；对话和图片本身仍属于私有数据。

正式模型的请求可使用后台完成压缩后的投影，但 `state.json` 和原始日志仍完整。Master 与每个 `agents/<id>/` 各有自己的 `context-window.json`，含原前缀长度/哈希、摘要、来源模型和代数；独立 context-budget.json 保存 Input 用量锚点；只在完整成功后原子保存。恢复时校验指纹，不匹配或缓存损坏就回到原历史重新生成。runs 中另记录摘要请求（包括专用 System）、响应、传输和 usage；完整摘要报文不塞入网页导航事件。缓存和摘要观测随 Session 移动，不为导航列表批量加载，不改变 v5 原始存档结构。机制、保留项和额外调用成本见[后台压缩](context.md)。

视觉 context 只保留用户粘贴和主动 view_image 的字节，后续模型请求持续发送这些已进入的图片。生成图、普通模型输出图及 Agent 收发图文只保存展示快照/引用，不自动变成视觉输入。messages 的 _display_images 等内部字段只供网页使用，不发送 API。所有通信保存在 agents/index.json 的 messages 中，含 source/target/content/images/message ID/time；人机消息仅在所属 Agent 页展示，不汇入 Master 页面或其 context。

图片与资产在服务器保存，不依赖浏览器路径。Agent 消息图片提供接收方 workspace 副本和独立不可变资产，收到时只有文字/路径/元数据，需要时由 view_image 读取。已有视觉 context 和 _model 推理/签名来源按原样保存，跨模型只转换发送副本。

## 轻量目录同步与按需加载

导航接口 `/api/sessions` 额外提供 `directory_tree`：根目录、Session 分支和用户已展开目录的直属子目录。浏览器通过 `expanded` 查询参数提交已展开路径列表，服务器限制在当前搜索根内，跳过 Session 内部容器和软链接。普通目录也可浏览、创建 Session；无需为展示整个树而一次性遍历所有包目录。结构缓存以目录指纹复用，创建/删除目录会失效；目录请求不加载 Session 对话或图片。

根的一级子目录都显示，其他层默认过滤无 Session 子目录；本层 Display/Hide 只改变浏览器导航可见性，不改服务器文件。“发现全部”重新同步并展开所有 Session 路径，覆盖折叠而不展开无关分支。

启动时先提供网页，由首次目录请求触发扫描。扫描只读取名称、ID、路径、创建时间和置顶等导航信息，不为未打开的 Session 读取完整对话、图片、设置、事件或测速历史。

导航缓存以 `session.json` 的设备号、inode、修改时间和大小作为指纹。根目录 `registry.json` 保存已知位置及轻量字段；再次启动时，指纹匹配可复用，变动或无效缓存回退到真实元数据。索引不决定 workspace 绑定，也不取代递归发现；新增、移动、删除仍会同步。首次读取的小型元数据文件中可能含有设置，但列表仅保留导航字段。

点击 Session 后，后端按需加载该存档的完整设置、最近展示事件、Agent 列表与整个 Session 的测速记录；停止项保持停止，不创建 worker 或沙箱、不调用模型。点击 Resume Master 后才由 worker 恢复模型 context；浏览器刷新或重新选择存档不会自动启动。只有当前 Host 持有的运行 Session 中，才可给停止的单个 Agent 留言；整个停止存档为只读。Web 最近事件窗口和“更早记录”分页限制展示数据量，不截断模型历史。已经打开的后台任务从内存提供状态和完成提示，浏览它们不重启已有 Loop，列表轮询不重读其全部历史。

左栏顶部“运行中”自动列出至少有一个 starting/running/sleeping Agent 的 Session（包含 Master 停止而 sub 仍活跃的情况），组内按创建时间降序。该快捷列表优先于手动置顶，Session 在所属目录中仍保留；停止后自动从快捷列表移除，原手动置顶设置不变。导航的 `active_agents` 仅从已打开对象的内存元数据计算，不为未打开存档加载历史。纯浏览不占同时运行的 Session 名额。

## 恢复、移动与备份

- Resume 恢复完整对话、图片和设置；Python/Bash 重新启动，不恢复旧变量、线程、打开文件或 GPU 对象，也不自动重放工具代码。
- 移动前先关闭该 Session 运行环境，把完整 Session 文件夹移到目标 workspace 的 `.recoding/` 下。绑定按新位置计算，ID 保持不变。
- 工作文件、`.recoding/inputs` 和 `.recoding/runtime/python-packages` 属于 workspace，移动单个 Session 不会移动这些内容。home 缓存属于当前用户；跨 Python/平台需核验依赖。旧包目录保留，显式迁移检查冲突。详见 [包与缓存](packages.md)。
- 可靠备份应在关闭运行环境后复制整个存档目录。单独复制 `state.json` 会遗漏图片、事件及原始日志。
- 不手工删除 `owner.lock` 或 `host.lock` 来解锁运行中的 Session；锁由内核按文件描述符生命周期管理。

当前 `session.json`、`state.json` 和协作索引均为 v5，只读写当前格式。不兼容版本从列表隐藏，不自动补全、导入或转换；历史文件保留。见 [格式与升级策略](migration.md)。

state.json 保存 messages、终身 iteration、tool_count、run_state 和 location。sleep 时记录真实工具名和调用 ID，文本响应不结束任何 Turn。每次 worker 启动对应一个 Run（run_id）；Stop/故障后保存原因和位置。消息按已提交 ID 去重；停止前的未读消息保留。运行 Host 内单个 Agent 停止后，真实用户文字/图片仍可追加到其持久队列；整个 Session 关闭为 stop 后禁止留言。发送事件立即记入该 Agent 日志，不创建 worker、不写入模型 context，恢复后再读取；Agent 间新消息拒收、不入队，宿主通知单独延后保存。

## 用于性能分析的记录

| 分析问题 | 主要记录 | 口径与边界 |
| --- | --- | --- |
| 模型为什么慢 | `model_start`、`model_progress`、`model_end` 和原始传输记录 | 对照 headers、首输出、首文本、心跳、完整响应耗时；不能直接还原服务商内部排队 |
| 生成速度变化 | 完成事件的 `usage.completion_tokens`、`elapsed`、模型和时间 | `Output / Model call 总耗时`，包含等待与传输；不额外叠加 Thinking |
| context 与缓存 | API 用量、请求字节数/指纹、消息计数和来源模型 | API 未回报的 token 不估算，缓存命中不等于无需发送图片 |
| Run 花在哪里 | loop_end、停止/错误与逐次 Model/Tool 事件 | Run 耗时、模型/工具累计；并行累计可能超过墙钟时间，sleep 属工具等待 |
| 出图与看图 | 展示/消息图片资产、成功 view_image | 按 Run 的 SHA-256 去重；不从代码猜测，展示不等于模型已看图 |
| 失败与取消 | 错误事件、传输计数、`state.json` 执行位置和原始日志 | 不把半截响应当完整工具调用；已发生的文件修改不会因 context 回滚而撤销 |

分析旧存档时应读取保存的历史模型和用量，不能用当前选择的模型补写旧事件。缺失字段表示未知；可读摘要和 Thinking token 数也不能重建模型的完整内部推理。阶段计时与排查方法见 [API 性能与诊断](api-performance.md)。

## 清理边界

源码缓存与构建产物可按维护任务清理；正式测试、最小夹具和 vendor 许可证/哈希清单继续维护。测试在专用 `sessions/tests/` 建立随机工作区，结束或失败先关闭进程、沙箱和浏览器，再自动清空该目录全部内容；正式 tests 源码、其他 workspace 和真实 Session 不在清理范围内。

`.env`、真实 `sessions/`、任意 workspace 中的存档、工作文件、图片和安装包属于用户数据，不按文件名中的 `test`、`demo` 或旧品牌名判断其是否可删除。删除 Session 使用 Web 管理接口或 `.venv/bin/python3 recoding-cli.py delete -s PATH --yes`（先关闭运行环境），只删除对应存档与运行环境，保留 workspace 工作文件和其他 Session。Session 存档和 workspace 的生命周期分开管理。

## 格式与恢复约束

当前名称、包、入口及容器为 `recoding...`、`recoding`、`recoding-web.py`、`.recoding`。0.8 的 endless multi-agent 使用 v5：旧 v4 及更早格式原样留在磁盘，但不显示、不恢复、不转换。原始源码快照保持原字节，旧 Session 需用相应旧源码继续。兼容范围与移动规则见 [格式与升级](migration.md)。

Session 根部 context-maintenance.json 记录各 Agent 最近的后台维护状态、增长门槛及暂时 API 错误的时间退避；snapshot 的 context_compactions 与现有 SSE 提供排队、运行、成功、失败、取消状态。Stop/关闭取消后台进程，Host 异常退出后的未完成任务标为取消；Resume 后按预算重新申请，不接续旧进程。

`tool-inventory.json` 与 `tool_inventory` 展示事件保存检查时间、评分目录、模块发现结果和最多 512 项发行版名/版本/来源，不保存环境变量、包源码或凭据。只在打开对应 Agent 时读取，不参与导航扫描；报告缺失/损坏不阻塞恢复。Master 的严重缺失通知去重摘要保存在协作索引；完整检查列表不自动进入模型 context。

正式 Web/CLI Host 的单进程独占、运行排斥浏览，以及直接 Python API 的实验豁免，见 [Session Host 协议](session-ownership.md)。

全局消息观察复用 `agents/index.json` 唯一的 messages，不额外复制正文。各记录的 `_global_seq` 为持久追加序号，用于 Master 增量读取和监听；系统通知不进入通信投影。published 标记展示事件已写入，deferred 表示停止期间的系统通知尚待恢复，不代表模型已读；实际读取仍由 Context 内的持久消息 ID 判断。当前 v5 原有分散索引在加载时归一化，只读浏览不写文件。工具结果与读取游标按普通工具历史保存。新的发送不接受 `cc`；已有抄送记录保留历史展示，全局观察跳过副本。

## 代码工作运行数据

Workspace `.recoding/runtime/executions/<Agent 存档 ID>/` 保存后台任务记录和完整日志；edits/ 保存可核验的编辑 journal 与旧字节；repo-index/ 是可丢弃的惰性代码定义缓存。这些都不是模型历史，不纳入源码快照或 Git，移动单个 Session 不会自动带走它们。使用和生命周期见 [代码工作能力](coding-tools.md)。

Python Pool 的 Session 元数据在 python-pool.json；完整执行日志在 workspace runtime/python-pool 下，移动/备份单个存档不会自动带走这些日志。只读浏览强制显示旧进程已关闭，不恢复内存或启动沙箱。见 [Pool 存储与生命周期](python-pool.md)。

各 Agent 的 context-checkpoint.json 是成功分块的候选恢复点，不是已生效的 Context；保存基线身份、原始前缀长度/哈希和摘要。失败、取消与进程退出后保留；只有校验通过才续做，全部完成后仍由协调器一次提交 context-window.json。备份/移动应将检查点一并保留。
