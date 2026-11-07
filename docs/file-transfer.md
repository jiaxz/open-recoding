# Host 中转文件传输

[文档目录](index.md) · [架构](architecture.md) · [Non-builtin](non-builtin-tools.md)

`transfer_files` 是正式 non-builtin。它在已配置 workspace 间显式复制文件或目录：local → remote、remote → local、remote → remote，以及同一本地 workspace 内复制。所有文件字节都经过当前 SessionHost；远程节点无需互相可达，不交换彼此的 SSH 凭据。

## 当前接入范围

Local 和 Remote 都使用所属 NodeRuntime 的文件 RPC 端点；Host 只转流，不假定能直接打开远端路径，不为传输启动另一套 SSH 服务。Node 基础模块位于 `core/node/`，工具 schema 和生命周期位于 `core/tools/file_transfer/`。

通过 Web 的 SESSION RESOURCES 或 Session.configure_remote_node 绑定远端目录，详见 [计算节点](nodes.md)。连接由 SessionHost 的 SSH 配置认证，远端无需互相持有密钥；绑定保存为存档 remote-nodes.json。Local 固定存在，GUI 不提供关闭或选择 Local 的控件。

## 模型调用

先查询文件目标：

```json
{"action":"nodes"}
```

复制一个目录，目标路径是复制后的根目录，不会隐式再追加源目录名：

```json
{
  "source_node":"local",
  "source_path":"artifacts",
  "destination_node":"gpu-box",
  "destination_path":"inputs/artifacts",
  "mode":"async"
}
```

`action` 默认 `copy`；两端 node 默认 `local`。路径可以是 workspace 相对路径、`/workspace/...`，或位于该节点 workspace 内的真实绝对路径。每个节点仅有一个绑定目录。同节点不允许复制到源本身或其内部。

结果用 `transfer.id` 标识；这是一次 Host 管理的文件操作，不是语言 Process ID。`mode=sync` 默认等待结果；`async` 立即返回，完成后向提交 Agent 投递一次摘要通知。正文含路径、完成数量及错误，不携带文件字节。最多并发 4 份传输。

```json
{"action":"status","transfer_id":"<transfer-id>"}
```

```json
{"action":"cancel","transfer_id":"<transfer-id>"}
```

`action=list` 查看记录。普通 Agent 仅能查询/取消自身传输，Master 可管理全部；模型必须具备 `transfer_files` 工具权限，远程传输还要求 Session 与 Agent 的网络权限均允许。

## 完整性、取消与边界

- Host 按 256 KiB 分块中转，不一次读入整个文件。传输编码只存在于内部通道，不进入模型 Context 或语义消息。
- 源文件以清单版本与读取前后元数据检查变化；两端计算 SHA-256 和字节数。目标先写临时普通文件，确认完整后逐文件原子发布；默认不覆盖，只有 `overwrite=true` 才替换既有普通文件。
- 目录可包含空目录，单次清单最多 10,000 项、2 MiB。目录复制不是整体事务：失败/取消保留已发布文件，未完成临时文件由文件端点关闭时清理。进度中的已传字节不等于已发布字节，完成文件数单独报告。
- 拒绝越界、软链接、设备/FIFO 等特殊文件；默认排除 `.env` 类凭据和 `.recoding` 私有存档/runtime，仅允许传递公开 `.recoding/inputs`。目录遍历排除私有项，显式请求私有项则拒绝。
- SSH 通道断线可重连原 Runtime，但不自动重试写入。调用方检查完成记录后自行决定如何续做；不可把中断复制宣称为整体回滚。
- Agent Stop 取消其传输；Session Stop/关闭取消全部并等待文件端点回收。Resume 保留绑定与有界历史记录，不自动重放中断操作。文件端点由 NodeRuntime 管理；控制端失联的远程环境由租约兜底回收。
- 从远程取得的图片，先复制到 local workspace，再通过现有 view_image 或图文消息使用。文件传输本身不把图片字节加入模型 Context。

完整运行时的节点生命周期、Web Session Resources 和跨节点故障语义见 [架构总览](architecture.md)。
