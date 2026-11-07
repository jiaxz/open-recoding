# 模型 Context、预算与压缩

[文档目录](index.md)

每个 Master / sub 都有独立 context。原始历史、界面展示和发送给 API 的投影分开；查看消息、切换显示档位或浏览历史不消费模型队列，也不改变 context。

## 一次 Model call 的输入

Agent 的 System 由 `core/multiagent/prompts.py` 组装：单 Agent 基础行为 + MultiAgentSystem 的角色/工具片段 + Session 的环境约束。System 保存为历史首条消息，Web / CLI 不拼接它。

后续历史保留模型响应、完整工具批次和工具结果。每次 Model call 前，读取自身全部未提交消息，按持久 ID 与实际 context 去重；批次中途不插入新输入。请求已发出后到达的消息进入下一次调用。首次启动和 Resume 分别加入身份/生命周期系统消息，不注入 Turn 通知或强制首轮工具选择。

协作信封和工具回执可带宿主生成的 `_model_content` 精简视图。完整正文、图片、投递记录仍保留在存档和界面；只有 API 序列化时使用精简视图，并移除内部下划线字段。非空 stdout、stderr、错误不额外截断；assistant 推理和签名不走此替换。

新 Peer 输入的原始正文放在 content，发送者、收件者、ID、时间及图片元数据放在 `_agent_message`。接收事件从该结构直接产生，不解析正文；历史旧记录不改写。内部信封不计入请求 token 估算；摘要和 recall 保留其来源、收件者及图片路径，图片像素仍须主动 view_image。每次请求先加载已提交压缩投影，再一次取信并选定模型，预算记录与实际发出的投影和新输入一致。

## 图片与模型切换

视觉输入只包含真实用户粘贴和主动 `view_image` 得到的图片字节；已进入的图片持续保留。Agent 给用户的正文内嵌图自动传输为不可变展示快照；相同图片不再重复放入 images 附件参数。生成图、普通模型输出图及 Agent 图文消息只有展示快照和可 view 的路径，不自动加入图像输入。每次 API 请求仍会传输已保留图片，缓存命中不等于省去上传。

原始 assistant 保留 `_model` 来源、可读摘要和专用签名。向另一个模型发请求时，只转换副本并移除异源专用字段；可读摘要转成来源明确的历史文字。切回原模型可继续使用原字段。不能把 A 的签名发给 B，也不能覆盖原始历史。

## 自动压缩预算

按每个 Agent 的下一次请求 **Input tokens** 判断窗口占用，不看 Session 累计、`total_tokens` 或字符阈值，也不扣 Cache hit。代码默认窗口为 Grok 500,000、两个 Gemini 1,048,576；阈值约为窗口 80%，并保留输出余量。Argon 的输入/Thinking/输出共享约束同样需要余量；实际网关限制可用 Session `context_limits` 覆盖。

优先使用 API Input 实测锚点，加上正文/System/工具变化的预算。`ContextBudget` 按 Agent/模型持久化正文、System、工具指纹和暂缓门槛；System 或工具变化只增加相应预算。缺失锚点时，最多读取本 Agent 最近 8 份 runs、总计 64 MiB，仅完整请求指纹匹配的成功调用可恢复校准。

无法校准时标明本地估算可能高估，展示最后 API 实测、估算来源、图片/工具预算、触发原因及操作影响；不能按 Base64 字符数算图片 token，也不能把旧 usage 当成新增消息后的精确占用。

达到约 80% 门槛且存在可压缩的旧完整批次时，自动申请后台压缩，不弹出确认框。保留约 20% 的窗口余量供正常任务继续推进；估算和摘要延迟不能保证极端大输入永不触及上限。没有可压缩旧历史时不会凭空压缩当前大输入。

## Session 后台压缩

`SessionCompaction` 是 Session 的维护功能，Apps 只显示和取消它。Agent 在每次 Model call 边界检查自己的预算，通过宿主请求调度；AgentLoop 本身不依赖 Session、界面或摘要进程。独立使用 Agent 且没有维护调度器时，只使用已有的合法投影。

1. 为目标 Agent 记录已提交历史的长度及指纹。正常 loop 立即继续，不 Stop、不重启 worker，不清除 Python/Bash/Python Pool。多个 Agent 的申请在同一 Session 内排队，最多一个摘要进程占用 API；各自的历史和摘要仍独立。
2. 独立 `context_maintenance` 进程只读取申请时的历史前缀，使用同一网关和申请时的模型做无工具摘要。不启动 Agent/沙箱，摘要请求同时省略 `tools` / `tool_choice`；可恢复 API 错误最多重试 3 次，永久参数/鉴权错误不重试。新的消息与工具结果继续追加到原历史。
3. 旧完整批次分块摘要，复用有效增量记忆。每个成功分块原子写入独立的 `context-checkpoint.json`，包含候选记忆、已覆盖前缀指纹及活跃投影的基线；它不属于已生效 Context。重试先校验模型、基线和原前缀，匹配才从检查点续做，避免重新消耗已成功分块。通常保留近期 12 个块，必要时缩到至少 4 个；继续保留最新两条真实用户要求、最近停止记录和历史图像输入的完整批次。摘要有损，原始 state、runs、签名和图片不改。
4. 所有分块成功、未取消且待替换前缀指纹仍一致时，原子提交 `context-window.json`。只追加的新历史不会使候选失效；被改变的原前缀则使整份候选作废。摘要只校验它所覆盖的完整前缀；后续工具批次尚未返回全部结果时，统计和展示仍保留摘要及历史图片，不撤销有效投影。Agent 在下一次 Model call 边界重新加载已提交投影，后接全部新历史和当时的新消息。已发送的请求和完整工具批次不会被中途替换。
5. 取消或摘要错误不切换活跃投影，正常 loop 继续；已经落盘的成功检查点保留，未完成响应不保存为摘要。临时 API 错误耗尽当前重试后，调度器按 30/60/120/240/300 秒退避，下次 Model call 边界到期即可再次申请，无需上下文继续增长。成功后以新投影的预算和可压缩历史为准，不沿用压缩前的高估量阻挡下一次申请；人工取消或非暂时性失败仍使用约 5% 窗口增长间隔，避免反复消耗。Session Stop/关闭取消维护，不启动新 API 调用；之后恢复可复用校验通过的检查点。

Web 右栏最上方显示正在压缩或排队的 Agent 与取消按钮，不弹窗、不抢焦点。CLI/TUI 顶部有后台压缩状态，`/compact` 查看，`/compact cancel [REQUEST_ID]` 取消；Esc 仅关闭面板。Shell 可执行：

```bash
.venv/bin/python3 recoding-cli.py compact -s /path/to/session --json
.venv/bin/python3 recoding-cli.py compact -s /path/to/session --cancel --request-id REQUEST_ID --json
```

维护状态保存在 Session 根部 `context-maintenance.json`，通过 snapshot 和现有事件流展示。Host 意外退出后的未完成状态显示取消，不在重启后接续旧进程；Agent 恢复后按自身预算重新申请，并复用有效检查点。摘要 API 用量计入 Session 累计，但不增加正常 Iteration。

`recall_context` 只读本 Agent 原始历史和图片元数据，供模型找回精确细节，不读取其他 Agent 私有 context 或重放工具。实现入口为 `core/multiagent/context_window.py`、`context_maintenance.py` 和 `core/session/compaction.py`。

## Host Coding Agent 显式维护

当用户明确要求压缩一个停止的存档时，可使用同一摘要驱动的维护入口；不会 Resume Agent 或运行工具：

```bash
.venv/bin/python3 maintenance/recoding-context.py -s /path/to/workspace/.recoding/session-folder \
  --env-file .env --live --probe --report-dir .recoding/maintenance/context-recovery
```

`--live` 明确允许向该存档配置的模型 API 发送摘要请求；`--probe` 额外发送一次压缩后的正常工具协议请求验证输入与响应，但不执行模型返回的工具或写入原始对话。结果区分压缩与验证是否成功，验证失败不撤销已提交的有效摘要。日志/备份写入指定的私有忽略目录，不提交 Git。

工具同时取得 Session 的 `host.lock` 与目标 Agent 的 `owner.lock`，运行 Host 未释放时拒绝维护。`--agent-id` 可选目标，默认 master。旧失败任务尚未保存检查点时，可通过 `--recover-journal /path/to/agent/runs/context_timestamp.jsonl` 从其成功摘要记录恢复：必须验证原压缩快照完整指纹、模型、Run、Iteration 和完整工具边界，不能随手拼入外部摘要；已有更新检查点不会被旧日志覆盖。原 state、消息、签名和图片保持不变。
