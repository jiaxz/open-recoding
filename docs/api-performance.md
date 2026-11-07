# API 性能与诊断

[文档目录](index.md)

本页描述现行客户端的观测、重试与排查方法。部署方的性能测量、原始报文和服务健康状态保存在本地忽略目录，不作为其他环境的事实。累计指标与速度口径见 [运行观测](observability.md)。

## 先确定等待在哪一段

```text
准备请求 → HTTP headers → SSE 心跳/模型增量 → 完整响应校验
                                                   ↓
                               工具批次 → 下一次 Model call
```

| 字段 | 含义 |
| --- | --- |
| `request_bytes` / `request_sha256` | 实际序列化请求大小与指纹 |
| `system_sha256` / `tools_sha256` | System 和工具是否相同 |
| `headers_seconds` | 连接、TLS、发送和服务端等待，不是纯推理时间 |
| `first_output_seconds` | 首次非空正文、可读推理或工具增量 |
| `first_text_seconds` | 首次可见正文 |
| `heartbeat_count` / `stream_events` | 心跳数 / JSON 数据事件数，不能当作模型进度 |
| `elapsed_seconds` | 一次传输耗时；Model call `elapsed` 还包含重试等待 |
| API `usage` | 成功回报的 Input/Output/Thinking/Cache，缺失为未知 |

HTTP 200 只说明进入响应阶段；心跳说明连接可能存活，不证明模型已生成内容。区分客户端传输、网关排队/凭据、模型生成和工具等待；仅凭客户端不能还原服务商内部原因。

## SSE 与完整性

请求使用 `stream: true`、`stream_options.include_usage: true`，让界面分清 headers、心跳、首次输出和完成。收到 `[DONE]` 即结束读取，不等连接关闭。SSE 提高可观测性，不保证减少模型计算耗时。

增量按索引组装，保留有效角色、工具 ID/类型和签名，不让后续空值覆盖。工具参数完整且协议校验通过才执行；断流不提交半截响应或执行半个工具。心跳不重置无模型输出超时。

EOF 前没有任何模型增量为 `empty_response`；已有部分输出却缺 `[DONE]` 为 `incomplete_response`。保存字节数、事件/心跳数、待完成事件和是否有输出；上游没给原因时，不推断为模型不兼容或签名错误。

## 自动重试

Master/sub 的同一次 Model call 最多追加 3 次尝试，共 4 次；重发相同请求，不增加 Iteration，不重放工具。退避 1/2/4 秒，参考 Retry-After，单次最多 30 秒，Stop 可取消 IO 和退避。

HTTP 408/425/429/5xx、暂时连接/TLS 中断、超时、空或不完整 SSE、上游临时错误可重试。鉴权、证书校验、参数和非法结构错误不重试。HTTP 200 内的 SSE `invalid-argument` / `invalid_request` / `invalid_client_tool_schema` 也属于永久参数错误。

达到上限才使该 Agent failed，已提交步骤保留；sub 的真实失败和位置通知 Master。不会自动切换模型。卡片详情列各次失败；总耗时包含退避，未回报的失败 token 不虚构，服务商是否计费不能由客户端重试去重保证。

## 对照排查

1. 对照相同 endpoint、模型、context、工具、推理设置、stream 和代理路径；相同 Key 不代表相同请求。
2. 用序列化指纹确认完整报文，一次只改一个因素，重复成对检查，再回到真实 loop；问候/禁用工具与编程任务不可直接比较延迟。
3. 模型切换时检查历史模型来源、异源转换数量、完整工具配对和目标 schema；一次断流不能证明历史结构错误。
4. 优先本机 HTTPS/SSE 替身。真实网关诊断按用户任务与授权范围执行，不把真实 Session/图片当作默认测试夹具；日志只保存脱敏信息。

客户端在发送边界对普通用户文本使用稳定的 `<user_query>` 包装，原文保留；它不减少原始输入，也不应被宣传为所有任务的固定加速机制。

## 缓存、图片与页面连接

历史顺序、System/工具和序列化稳定有利于可比较的缓存观测，但命中只看 API 回报；不能按 JSON 字段先后推断 KV cache。图片已进入视觉 context 后每次仍发送 Base64；图片 token 不按 Base64 字符计，完整输入预算见 [Context](context.md)。

目录轻量索引只降低导航磁盘读取，不删 context。页面最多一个 Session SSE，所有 sub 共用，切换替换旧连接；未查看的 Session 可后台继续。单 GUI Host 拒绝第二个页面，隐藏/关闭释放 GUI/SSE 的边界见 [Host](session-ownership.md)，`streams` 正常为 0/1。

实现入口为 `core/multiagent/client.py`、`chat_stream.py`、`multimodal.py`；本机替身测试与日志格式见 [开发](development.md) / [配置](configuration.md)。
