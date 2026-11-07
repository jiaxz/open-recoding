# 模型与 API 的维护边界

[文档目录](index.md)

模型 ID、API 网关和 wire 协议分别处理。当前提供三个预置资料，但接受任意合法模型 ID，不把它们当成产品能力上限。AgentLoop、消息队列、沙箱和多 Agent 调度不按模型名称分支。

## 配置入口

```dotenv
RECODING_API_KEY=your-key
RECODING_BASE_URL=https://your-compatible-gateway.example/v1
RECODING_MODEL=your-model-id
```

RECODING_MODEL 是可选的新 Session 默认，未设置时使用 catalog 的 DEFAULT_MODEL；显式创建参数与已保存的 Session 模型选择继续优先。旧 GEMINI_API_KEY、GEMINI_BASE_URL 继续可用；GEMINI_MODEL 是旧的选择提示，不覆盖新 Session 默认。

同一类 key/base_url 的实际环境变量优先于 `.env`，即使跨新旧名称；同层同时出现时 RECODING 名称优先。显式空 key 不回退到另一个别名。不要把密钥写进模型资料、Session 设置、前端配置或日志。源码入口和启动命令不变，已有 `.env` 不需要迁移。

## 集中模型资料

`core/multiagent/model_config.py` 是当前唯一的模型预置资料位置：模型 ID、显示名/短名/配色、输入窗口、协议选择、请求附加参数及特定历史工具签名标记。当前三个预置维持既有行为，未来增加或替换模型主要修改这里。

未知 ID 使用通用资料：不根据 gemini/grok 等前缀推断协议或添加签名标记；显示完整名称，Web/CLI 使用相同的确定性颜色。未知 context 窗口使用标为 fallback 的 200,000 预算，**不是模型真实上限**。应在 catalog 或 Session context_limits 中填入实际网关限制。API Input 实测校准、后台自动压缩和原始历史保留规则见 [Context](context.md)。

预置 request_options 可以声明网关实际支持的参数，例如 stream=false、temperature 等；不能覆盖 model/messages/tools/tool_choice。没有资料时不自动猜测 reasoning 参数或输出长度。用户传入图片、工具调用和历史仍保留；这不代表任意新 API 都支持它们，接入时必须验证。

## 协议适配器

当前真正实现的是 Chat Completions，协议适配器和唯一的 Client 均位于 `core/multiagent/client.py`：处理 endpoint、认证头、请求体、SSE/JSON 响应及 HTTPS、重试、脱敏和观测，输出内部统一的 message/usage/model/finish_reason；SSE 增量组装在 `chat_stream.py`，模型 ID 清单在 `model_config.py`。不额外保留只设置默认参数的客户端子类。

兼容网关只需要改 URL/key/model。协议不同的 API 需要增加并注册适配器，再在模型资料选择协议；不能把原生 Gemini、其他厂商原生接口或不同 endpoint 当作兼容协议直接拼接。适配器需要规范化文本、tool calls、图像、Input/Output/Cache/Thinking usage 以及结束/错误语义。内部 usage 继续使用现有规范字段，应用不解析厂商原始响应。

历史转换仍由 `models.py` 从未修改的原始消息派生目标请求，按目标资料采用兼容标记；不覆盖来源模型、推理或签名。新网关的签名规则需要在适配边界验证，不能因模型 ID 相同就认定两个网关的私有签名格式可互换。

## 给未来 Host Coding Agent 的最小修改路线

1. 读取用户提供的 API 说明，确认它是否真的兼容当前协议，以及支持的 tools、图片、stream、usage 和 context 限制。
2. 对同协议更新配置和 model_config；对不同协议在 client.py 增加并注册适配器。保持 Session/MultiAgentSystem/AgentLoop 的标准消息与调用方式。
3. 不在 Web CSS/JS、CLI、调度器或 budget 中增加新的模型 ID 判断。前端资料由 `/api/config` 提供，CLI 读取同一 catalog。
4. 用本机替身准备请求/响应并定向检查：未知 ID、完整工具批次、SSE/JSON、A→B→A 原历史、图片、取消/重试、usage 与窗口预算。需要真实付费 API 验证时按用户授权执行，不用完整用户 Session 充当默认测试输入。

当前定向命令：

```bash
PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_model_catalog test_model_context -v
```

维护流程见 [Host Coding Agent](host-maintenance.md)，配置与启动见 [配置](configuration.md)。

## Model call 的本步说明

协议可同时携带 assistant `content` 和 `tool_calls`，但具体模型或网关不一定在工具调用时返回正文。System 要求每次响应给简短操作说明，包含休眠；这不等同于公开内部推理或替代人机消息。

Worker 在组装最终模型工具 schema 时，通过 `multiagent/tools.py::with_step_notes` 为每个工具增加必填字符串 `_step_note`（最多 240 字符）。它是公开声明的模型参数，和消息中仅宿主使用的 `_model` 等私有字段不同。模型写出本步观察/判断及下一操作，不能把未执行工具声称为成功；批次可复用同一句说明。无需另发报告工具或增加 Model call。

原始响应、正文、参数字符串和厂商签名不变。Worker 从参数派生独立 `model_end.step_note`；Web 优先展示正常正文，缺失时再用此字段；部分模型省略自定义说明时，可用其工具 `message` 参数中的实际正文兜底，并标明来源，不生成或伪造摘要。批次中已有 `_step_note` 则不再重复消息正文，相同说明只展示一次。`StepNoteTools` 包在具体执行器外面，仅从执行参数副本剥离 `_step_note`，实际工具仍按原始业务 schema 验证，权限、队列、睡眠和 recall 不增加例外。缺少或非法说明不编造，也不为此重放请求或工具。摘要维护请求不使用这层工具 schema。

## 可读 Thinking 展示

Model call 返回的 `reasoning_content`、`reasoning` 或 `thinking` 字符串均可作为可读 Thinking 文本。SSE 按原字段累积完整分片，保留原始字段与签名；展示投影按上述顺序取第一个非空文本，避免同一段重复出现。跨模型请求也使用这个可读投影，切回原模型仍使用原字段。

Web 在 Model call 卡片内独立于“本步说明”显示它，默认紧凑两行、可展开完整 Markdown 文本；没有文本时不占位。Thinking token 数量不等于有可读文本，签名、密文和结构化 opaque 数据不当作文本显示。不为了展示主动要求模型泄露内部推理，也不额外请求生成 Thinking。
