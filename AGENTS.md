# 项目维护约定

先看 [文档目录](docs/index.md)。理解项目按 [阅读路线](docs/understanding.md)；帮助首次使用者配置环境按 [首次初始化](docs/first-run.md)，完成实际准备、最小验证和启动命令交付。

本文和 `docs/` 是维护 Recoding 的项目级操作规程（skill）。执行前按任务读取对应文档；维护包与工具时还读 [用途与优先级](docs/package-priorities.md) 和 [Host 维护指南](docs/host-maintenance.md)。约定发生变化时同步文档，不能只留在聊天里；用户当前指令优先。

## 项目与命令

- 产品为 `recoding...`，发行名 `recoding`。核心包直接导入 `multiagent`、`session`，源码位于 `core/`；应用位于 `apps/web/recoding_web` 和 `apps/cli/recoding_cli`。两个源码入口是 `recoding-web.py`、`recoding-cli.py`。
- 使用已有 `.venv/`；项目 Python、pip、测试和脚本命令用 `.venv/bin/python3`，无需 activate。仅首次创建环境使用宿主 `python3 -m venv .venv`，不重建正常环境。源码通过 editable 安装，不在入口修改 sys.path。
- 推荐启动命令显式使用 `-C ./sessions`；配置时缺少 `sessions/` 就创建，已有内容保留。省略 `-C` 仍使用当前目录。应用 venv 与沙箱 `/usr/bin/python3` 的包环境分开。

```bash
.venv/bin/python3 recoding-web.py --env-file .env -C ./sessions --port 8000
.venv/bin/python3 recoding-cli.py chat --env-file .env -C ./sessions
```

## 架构与执行

- [架构总览](docs/architecture.md) 持续维护当前控制/执行平面、目录与边界。每 Session 每 Node 一 workspace，一份 NodeRuntime；Local 固定必选，随创建/Resume 运行启动，纯浏览不启动。SSH NodeRuntime 与 Local 复用同一工具/协议，短断线连接回原 runtime_id，不重放代码；默认 60 秒租约回收失联环境，远端未确认回收需明确记录。Web 模型和 Remote nodes 共用紧凑 Session Resources，不显示 Local 选择。Kubernetes/多根目录/通用进程 RPC 尚未实现。

- Builtin 按控制职责区分，可直接调用 Host 的进程创建/关闭等指令；Agent worker 属于 Host 控制平面，Bash/Python 任务进程属于 Node 执行资源。模型可见 builtin 仅为 Agent 管理、消息、sleep/全局观察和自身 recall_context，不通过执行环境完成工作；生命周期清理回调仍由 Session 协调。transfer_files 是 non-builtin，所有节点文件复制由 Host 中转，节点基础包 node 不导入认知层或 tools，运行处理器由 Session 注入。见 [工具边界](docs/non-builtin-tools.md) 和 [文件传输](docs/file-transfer.md)。
- 认知包依赖 Apps → Session → multiagent，内层不反向导入。MultiAgentSystem 统一创建和持有全部 Agent（含 Master）；Host Agent 必须持有 team/agent_id，直接提交用户消息、访问 Mailbox 和上报事件，不保留独立 Agent.create 或多组注入回调。Worker JSON RPC 经 Agent._coordinate → team.dispatch：协作在团队内处理，执行/维护才走 Session 提供的 resource_request；release_owner 通知资源回收。团队唯一消息日志提供 Mailbox 视图，直接拥有 EventStream，Master 复用团队流，sub 经 agent_event 汇入；保持现有存档和事件协议。纯 AgentLoop 不引用团队。Session 持有唯一 ExecutionService 并作为对外聚合对象，HTTP 由 Web 启动，不建立 Session Service 或 agent 兼容包。
- `AgentLoop.run()` 只负责消息提交、模型/工具顺序、事件和中断配对，不依赖具体 Web、队列或文件系统。Master/sub 均为 endless loop；普通文本不结束循环，无工具响应由宿主另存 system-origin 的 assistant sleep 调用，不改原模型签名。
- System 要求每次正常模型响应在 assistant content 附简短本步说明，包含 tool calls/sleep 时也一样；概括已观察/判断及即将执行的操作，不预先声称工具成功，不输出私有推理。Web 在所属 Model call 卡片内醒目展示，长文可展开，仍属于可隐藏细节，不代替 send_user_message。原生正文优先；模型可见 schema 的必填 _step_note 工具参数为无正文响应提供结构化兜底（multiagent/tools.py），Worker 产生独立 step_note 事件字段。工具未附说明时可复用其 message 参数正文并标明来源；执行前仅剥离参数副本，不改原始 assistant、tool_calls/签名，不增加模型或工具调用；缺失说明不编造。
- 每次 Model call 前 drain 自身全部未提交消息，按持久 ID 与实际 context 去重；完整工具批次中途不注入，已发送请求不被新消息改写。可选 context_processor 只派生请求副本，并在 drain 前运行。
- 每 Agent 的唯一权威 record 由团队持有，Master 存 master、sub 存 agents 条目；Agent.record 是同一对象，status/run_id/error/sleep 只作为属性视图，不保存第二份或复制同步。Master/sub 共用 start_agent/_launch/finish 和资源回收屏障，stopping 期间不得 Resume；Master 保留随 Session 的控制器/锁，sub 回收后释放名额。全部通信只存 messages，published/deferred 是记录状态；Mailbox 无第二写入口。团队统一持有各 Agent 事件流，emit/_publish_message 处理活跃/停止/恢复发布；按消息 ID 幂等，禁止 Session 手工补写子 Agent 日志。load_team 共用于运行和浏览，当前 v5 旧派生索引只读归一化、Resume 写回，不改原始 Context。测试覆盖共享对象身份、立即停止/回收/Resume、重复发布及只读归档不写回。
- multiagent 按职责集中模块：worker.py 的 WorkerRuntime 装配一次 Run，先载入压缩投影，再一次 drain 获取消息/模型/休眠设置；不再额外推送模型更新。events.update_runtime 只修改共享 record，控制器属性直接反映状态，迟到事件不能恢复 stopping 资源。messages.py 统一收件箱、唤醒、结构化信封和全局观察；新 peer 正文与 _agent_message 元数据分离，不能从正文反解析信封。tools.py 集中所有 builtin schema、说明剥离、权限校验及本地 recall/RPC 分发；不恢复多层工具包装器。storage.py 统一日志、检查点、锁、图片和成员存储；提示词在 prompts.py，模型 Client/协议在 client.py，模型资料与环境配置在 model_config.py。源文件改名更新全部调用方，不建旧导入兼容层。
- `sleep(0–3600)` 被自身任何未读消息立即唤醒，不消费正文。兜底默认 10 秒，可设 0.1–3600；首次启动/Resume 各有一条身份/生命周期系统消息，不强制首轮工具。独立工具可一次成批返回，仍按顺序执行，sleep 放最后。
- Agent 构造时按角色装配协作 schema/System 片段和执行工具权限，最终 System 由 `multiagent/prompts.py` 组装；Session 仅给环境约束，Apps 不拼提示。模型工具经通用 JSON RPC 调用；真实用户提交入口不开放给模型。
- 各 Agent 独立 Host worker；每个 NodeRuntime 一份共享执行环境，默认 sandbox，也可显式 execution_mode=host；本节点 Bash/Python/看图/命令/工具检查共用环境，节点之间不隐含共享内存或文件。core/node 管理连接、执行环境、IPC、owner 和回收，具体工具留在 core/tools；Agent 不创建/关闭沙箱。Sub Stop 保留步骤并闭合工具配对，取消该 Agent 的提交，并退役它当前拥有的进程；排队取消，其他提交者的当前调用可结束，默认 5 秒宽限后关闭；全 Session Stop 才清除全部执行内存，Resume 不反序列化或重放代码。普通工具错误交回模型；不可恢复故障持久化并通知 Master。
- Workspace 根部由 Recoding 或维护 Agent 新建的隐藏目录只允许 `.recoding` 一个。上传副本在 `.recoding/inputs`；工具 HOME/配置/数据在 `.recoding/runtime/home`，公共缓存仍在 home 级容器。新建临时环境、测试缓存等也收进此容器，不创建根部 `.agent-inputs`、`.config` 等目录；项目已有 `.git`、`.venv` 等不迁移、不删除。升级前关闭同 workspace 的旧 Host，旧输入目录在下一次上传/工具初始化迁移，冲突或软链接不得覆盖。
- 辅助包位于 workspace `.recoding/runtime/python-packages`，映射 `/opt/recoding/workspace-packages`，本项目所有 Session/Agent/Python Pool 共用；兼容用户 site 只读映射到 `/opt/recoding/host-user`。home `~/.recoding/cache` 是全用户共享缓存，home 本身也可有 Session。`maintenance/recoding-packages.py` 按 workspace 只读汇总；显式 --migrate 锁住相关存档、检查冲突并保留旧目录，不自动安装。见 [包环境](docs/packages.md)。
- 用户说“把它们维护起来”，表示正式加入项目完整工具/能力清单，并维护用途、发行版/import/命令映射及使用说明，不只是临时安装。包收录到工具完整度集合，真正的模型可调用工具收录到 tools 的 registry；不因包收录而自动新增 Tool call 或执行 Host 安装/升级。
- 包维护形成闭环：扫描工具 Python 的系统/用户 site 及 workspace/旧 Session 包，区分真缺失、版本差异与替代能力；按用户要求推广公共包时，也评估并更新 core/node/inventory.py 中的可复用包集合及文档。基线/替代/可选分类明确，传递依赖不自动列为必备，不钉死本机版本或塞入 Apps 运行依赖。扫描不等于安装/升级许可，元数据不等于执行验证；完整规则见 [Host 包维护原则](docs/host-maintenance.md#package-维护的闭环原则)。
- workspace 自由代码无目录或接口要求。正式 non-builtin 位于 core/tools，导入包 tools；Session 装配，Agent/MultiAgent 不反向导入。command 适配短沙箱子进程，runtime 适配现有沙箱内执行服务，service 适配独立长驻资源，公共 schema/权限/结果/事件一致；生命周期需 stop_owner/close，模型代码由独立执行进程运行，默认沙箱或显式 Host，不在应用进程中 exec。源码变化重启 Host，不设 home 公共工具库。
- 每个 NodeRuntime 一套通用 ProcessPool，core/node/process_pool.py 统一 Owner/名字/队列/Execution/取消/超时/回收；Python/Bash tools 只提供 schema、薄服务绑定和 Driver，不能再实现第二份队列或 Owner 权威。Host pool_client.py 仅投影/归档/通知。所有具备工具及环境委派权限的 Agent 可 list/use/read 任意进程，无 share/revoke/participants；Owner/Master 可 rename/transfer/delete，creator 不变、名字可重复、ID 不变。异步结果给 submitted_by。Stop 取消其全部提交、关闭所属进程的接单与队列，其他提交者当前执行可结束，默认 5 秒宽限后强制关闭。保留状态必须提前 transfer，不自动交给 Master。新 attempt 可创建新资源，旧 attempt 迟到请求拒绝。MultiAgent 仅经 Session 回调传生命周期事件，不管理 Node/语言进程。见 [执行池契约](docs/execution-pool-design.md)。
- read_file/edit_files/repo_query 是正式 non-builtin。严格版本/旧上下文检查、短 workspace 锁、每文件原子发布与带版本条件的 journal 撤销；多文件批次可部分提交，原始 Bash/Python 可绕过锁。无 Git 前置条件，无人类审批；冲突由模型重读、协调和修复。导航有界，解析缓存可丢弃，不宣称语义引用。
- 共享 Python 主要用于短通信/同步；同 namespace FIFO，长计算应独立进程。字典写入不唤醒 Agent，通知仍走消息。持久执行取消先软中断，普通取消未响应保留 unresponsive，不自动强杀其他 Owner 的持久进程；Owner Stop 的有界退役另行关闭，不假报完成；明确删除与 Session 关闭回收。进程组、子进程跟踪及 subreaper 不等于 cgroup 隔离；不承诺任意脱离监管后代的每资源精确回收，Session PID namespace 是最终本地边界，远端 SSH 副作用另行管理。Owner 管理限制是合作协议，不能当 Agent 间安全边界。委派网络/GPU低于共享沙箱能力时拒绝执行。

- 执行模式只在创建或停止后 Resume 时选择并保存；sandbox 失败不得自动降级。Host 不提供文件/PID/网络/GPU 隔离，不兼容 --no-network/--no-gpu，清理为尽力回收；模型提示、路径、包、图片传输和工具检查必须反映真实模式。Web 只读选择只暂存，运行中前后端都拒绝切换；CLI 用 --execution-mode。详见 [执行模式](docs/execution-modes.md)。

## 协作、模型与图片

- 新 sub 从自身 System 和空历史开始，不 fork Master。只有 Master 可 create/stop/resume/rename sub、管理图标和观察全局消息（语言进程权限由工具层管理）；create/resume 不携带任务，任务另发消息。角色、schema、执行器和协调器共同校验权限。
- Master/sub 可 list 安全元数据、peer 单发/群发/广播、显式一对一给 user 发消息。人机报告必须 `send_user_message`；模型正文和 sub 输出不自动转成报告。发送不支持 cc；Master 按需读取全局消息，不能借此读取 peer context。
- peer 只向 starting/running/sleeping 投递；stopping/stopped/failed 拒收且不入队。运行 Session 中用户可给停止 Agent 留言，持久未读且不自动恢复；整个停止存档只读。只有宿主可创建真实 user/system 来源。
- sub 并发默认 16，不含 Master，可改 0–99；sleeping 占名额，回收完成才释放，降低上限不杀已有 Agent。创建/恢复与 Stop 使用 generation、attempt 和资源登记防止孤儿。
- Master 与所有 sub 都主动寻找并组合各层有效并发：多 Agent 推理、Python 多进程/线程/异步 I/O、多个异步 Bash、多个授权 SSH 连接。推理与执行并行分别规划，同进程仍 FIFO；Master 长工作优先异步/委派以保持人机交互和协调响应，统一控制全 Session CPU/内存/GPU/连接/进程/API预算，及时处理失败和回收资源；sub 在自身任务范围使用执行并行，与 Master 协调共享预算，需要额外 Agent 时向 Master 申请；用户可看全部消息，无需逐条转述。图标由 Master 按角色选择，唯一性是提示要求，不是代码限制。共享文件协议明确归属、发布、锁和失败条件。
- 所有 Agent 共用 Session 模型/API，修改在下一 Model call 生效。模型 ID 不限于预置三项；默认、窗口、显示和协议特例集中在 multiagent/model_config.py，未知模型不按名字猜测能力。配置使用 RECODING_API_KEY/BASE_URL/MODEL，旧 GEMINI key/base 别名继续可用，GEMINI_MODEL 不覆盖新建默认；协议变化只改适配边界，见 [模型/API](docs/model-api.md)。可恢复 API 错误最多重试 3 次，退避 1/2/4 秒，Retry-After 上限 30 秒；永久参数/鉴权/证书错误不重试，工具不重放。
- 原始 assistant 来源、摘要和签名保留；异模型只转换请求副本，不把 A 签名发给 B。内部下划线字段不发 API；user/tool 的宿主精简视图不覆盖原文或额外截断实际 stdout/stderr/错误。
- 展示图是不可变服务器资产，经受限 HTTP 接口传输；生成图、模型正文图和 peer 图片不自动成为视觉输入。只有真实用户粘贴和主动 view_image 加字节，已进入的图片持续保留。不能声称未进行的视觉验证。

## Session 与维护事务

- 正式 Apps 遵守 `SessionHostProtocol`：同一存档仅一个 Host 持有 run，运行排斥其他 Host 全部浏览，原浏览下次同步清除。CLOEXEC 内核 flock 随进程死亡释放，不删除锁文件或按 PID/文件存在判占用；资源关闭后才释放。直接 Python Session 实验默认不受 Host 协议约束。
- Session 只有 run/stop；Agent 阶段不自动释放 Host。GUI Host 可持有多个 Session，只连接一个 GUI 页面；关闭/接管页面不停止后台任务。Web browse 不建 worker/沙箱；显式新建/Resume 才持有并唤醒 Master。CLI 只持有一个运行 Session，无只读打开，切换/退出关闭旧 Session，attach 退出也关闭；只有显式 detach 保留后台。
- 全局 Stop 停全部，用户 Resume 只启动 Master，由它决定恢复 sub。Web 顶部 Stop 始终可见；TUI 仅 `/stop` 停止，Esc/Ctrl+C 关闭面板保留草稿，Ctrl+D 正常退出。`/resume` 选 Session，`/wake` 唤醒 Master；TUI resume 按本层 Session、上一级、子目录导航，不递归、不循环。
- 当前产品 0.8，格式 `recoding.session` / state / team / registry 为 v5，Web API v4；不兼容存档隐藏、保留且不自动转换。容器为 `.recoding`，不带尾部句点。模型原文和历史图片保留，Session 移动与 workspace 文件移动分开。
- Context 预算按各 Agent 下一请求 Input，优先 API 实测校准，不按字符阈值、累计 tokens 或扣 Cache hit。约 80% 门槛自动向 SessionCompaction 申请后台压缩；同 Session 串行摘要，各 Agent 独立历史。只读已提交快照，全部成功且前缀一致才原子提交，下一 Model call 边界加载并后接新增历史。不得 Stop/重启 Agent/清空 Python/Bash/Python Pool；失败和取消保留旧投影且 loop 继续；每个成功分块独立保存候选检查点，重试仅在模型/基线/原前缀校验通过时续做。摘要可恢复 API 错误最多重试 3 次；仍失败则按 30–300 秒时间退避，不要求 Context 增长。Web 右栏顶部提示和取消，CLI 顶部状态与 /compact cancel；不再人工 compress/defer。详见 [Context 契约](docs/context.md)。

## 界面与观测

- Web 启动保存的 Session 选择失效（服务端 session_not_found/unsupported_session）时，只清除浏览器选择并保留目录同步成功；不创建旧路径或改写存档。优先按同步结果中的 ID 找移动项，根外有效存档仍可按保存路径只读打开。权限、连接及真正扫描错误不可被此回退吞掉。
- Web 读存档默认 browse，只读浏览的 Resume 提醒覆盖输入区，Resume 前禁用输入并保留已有草稿；运行 Session 中给停止 Agent 留言不受影响。只读设置先暂存，成功 Resume 后提交。新消息、模型和工具事件不能抢焦点或强制到底部；消息按发送与实际读取边界就地展示，READ 在消费它的 Model call 上方。
- Web 的 Python/Bash 调用卡在标题显示 sync/async，省略 mode 为 sync；长源码预览保留 execution_mode，重复投影不损坏原源码与标记，不改模型 context。右栏 PROCESSES 合并当前 Bash/Python 资源，按进程一行，Session 全局统计 run/ready；run 含启动/排队/取消/未响应等非终态，ready 为无正在执行或排队工作、仍保留状态的持久进程。历史/已关闭项不显示，历史日志接口与存档保留。面板默认整体折叠，列表紧凑、可滚动并拖动调高；实时更新保留展开、日志游标和高度，切换 Session 恢复折叠。
- 单 Agent 页只显示自己的消息；All agents 仅消息投影，不是 Agent、不建 context/worker/SSE。过滤、局部细节、草稿与阅读锚点的精确规则见 [界面约定](docs/ui-contracts.md)，用户操作见 [Web](docs/guide.md) / [CLI](docs/cli.md)。
- 累计量属于整个 Session，Resume 不清零；速度区分逐调用与并发相加的 10 秒估计，不重复加 Thinking，缺失用量不虚构。工具完整度使用现有沙箱中的只读短进程，不占持久执行 namespace；工具忙延后，失败显示未知。见 [观测](docs/observability.md) / [工具检查](docs/tool-inventory.md)。
- 同页一个 SSE，sub 共用；隐藏页释放连接，文档代际阻止旧页抢回。日志脱敏，不记录正文、密钥或查询参数。启动默认仅替换同端口同用户的 recoding 服务，`--no-replace` 保留旧服务。

## 验证、数据与文档

- README 同一文件中先放完整中文版，再附完整英文版；中文是唯一内容基准。修改中文的功能、架构、使用方式、命令、链接或配图时，同步更新对应英文内容与资源引用，保持章节和信息一致。README 之外的文档体系暂时保持中文，除非用户另行要求。

- 默认只做针对修改的必要检查，不例行全量；完整命令保留在 [开发文档](docs/development.md)。有限 driver 显式 Stop endless loop。测试使用离线 Demo/本机替身，不读真实 `.env`、真实 Session 或外部模型 API。
- 用户明确要求真实模型/联网行为实测时，可使用 maintenance/benchmarks 的显式 --live 入口；默认自动测试仍离线。真实实测只创建 sessions/tests 下的新工作区，模型配置不输出密钥，不读既有 Session；先关闭资源并归档原始证据到忽略的 .recoding/maintenance，再做派生分析与清理，不能因分析失败删除唯一证据。原始 JSONL 以文件行读取，不用 str.splitlines() 切割合法 JSON 字符串内的 Unicode 分隔符。
- 供 Host Coding Agent 使用的维护脚本集中在 `maintenance/`，不放根目录或 Web/CLI 应用包；模型可调用的正式 non-builtin 工具仍放 `tools/`。维护脚本直接调用 Core，不启动 UI/Host；用项目 `.venv/bin/python3` 执行。
- 测试代码在 `tests/`，所有测试 Session/workspace 在专用 `sessions/tests/`。Python 用 `workspace_support.temporary_workspace`，浏览器用 `support.cjs::testDirectory`；最外层测试命令结束（成功或失败）后，先回收测试进程、沙箱和浏览器，再自动清空该目录全部内容。子进程继承测试所有者，不中途清理共享夹具；使用同一测试根的独立命令串行运行。不得把真实任务放入此目录，不清理其他 sessions 子目录。需要留档的脱敏报告先保存到 `.recoding/maintenance/`。
- 可按需创建真实 SSH 虚拟测试节点：复用 tests/ssh_support.py::local_ssh，独立 loopback 端口、临时密钥/known_hosts 与 workspace，不改系统 SSH 或用户配置，不连接真实主机。tests/test_node_runtime.py 覆盖计算/状态/重连/租约/文件；同机端点不能代替异构机器验证。沿用 sessions/tests 自动清理，实测报告只留忽略的维护目录。
- 架构/执行改动优先选择 `test_architecture`、`test_agent_package` 和相关 Session/worker 替身用例。复制 multiagent 运行包后仍须独立执行注入工具、收消息、sleep 和 Stop/Resume，不借用外层包或真实配置。
- 所有提交 Git 的文档（README、AGENTS、docs、维护/工具说明及截图）必须跨机器复用。只写产品契约、通用原则、可复现步骤和明确标注的合成示例；禁止真实用户目录、用户名、机器标识、实际 Session/包清单、已装/缺失状态、PID、运行服务、临时恢复路径与实测结果。项目公开仓库 URL、产品固定沙箱路径和显式占位路径可保留。机器事实与审计报告放 Git 忽略的 `.recoding/maintenance/`，历史维护、部署、实验和测试执行记录同样不提交 Git；截图生成器使用虚构路径与固定示例时间。
- `.env`、key、Session/context、工作文件和安装包不提交、不入源码快照。历史源码快照字节冻结；不生成 archives。文档只描述产品当前契约、通用原理和操作方法；不建立仓库内历史维护记录目录。引用专题文档，避免复制整份变更记录。
- 根目录 `logo.svg` / `logomark.svg` 是品牌原件，Web static 内为相同字节副本；保持比例，经固定 HTTP 端点提供。文档截图只用合成示例，注明来源。文档修改检查本地链接、锚点、资源和示例，不需启动真实模型。

- 统一术语 Run（Agent 启动记录，run_id）/ Process（语言进程，process_id）/ Execution（一次代码提交，execution.id）。工具响应只返回 execution，列表为 executions；不恢复 task/job 别名。Agent 不维护工具执行缓存，Session 与 SavedSession 使用相同投影。TUI 查看执行使用 /executions。

- 右栏 Agent 列表显示最近正常 API Input 与“已压缩”的 token 量（原始历史减当前有效投影，同口径本地估算、标 ≈、不累加），移除创建时间文字但保留创建顺序。数字单独作为 Session 元数据投影，不扩展 list_agents 的 Context 可见范围；恢复缺失数字只读最多 512 KiB 事件尾部，不全扫历史、不用于导航。Process 列表标记当前调用 sync/async 和状态保留方式，计时从本次 Execution.started_at 开始，不含排队/Process 存活时间；ready 显示空闲，无当前模式。

- Host Coding Agent 经用户明确授权，可用 maintenance/recoding-context.py --live 对未被 Host 持有的现有存档进行真实压缩/检查点恢复和 --probe 验证；这是默认测试不读真实存档规则的显式维护例外。必须持有 Host/Agent 内核锁，同一 Core 摘要驱动，不执行验证响应的工具，不改原始 state/history/图片。备份、响应证据与本机结果只放 Git 忽略的维护报告。

- TUI `/agents` 使用 status.agent_token_usage 显示最近正常 Input 和“已压缩 ≈”，未知用 —，不把这些显示字段塞进 list_agents 的模型白名单。`/processes` 从现有 Host 快照展示所有 Node 当前运行/ready 的 Bash/Python；`/python` 为同规则 Python 子集，`/executions` 可查历史。标明本次调用 sync/async、有无状态、Execution.started_at 耗时；排队不从创建时间计时，ready 显示空闲；Local 无节点标签、Remote 用节点名。面板只读，保留草稿、焦点与常驻底部输入，不新增 Core 工具或模型调用。
