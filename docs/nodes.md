# 计算节点与 NodeRuntime

[文档目录](index.md) · [架构](architecture.md) · [文件传输](file-transfer.md)

每个运行 Session 必须有 Local NodeRuntime，还可添加多个 SSH 远程节点；每节点绑定一个目录。所有 Agent 共用这些资源，进程创建后固定属于某个节点。Local workspace 保存权威认知归档，远端只保存工作文件、执行数据、辅助包与运行诊断。

## 在 Web 添加

右栏 **SESSION RESOURCES** 将模型选择和 Remote nodes 放在同一卡片。Local 始终隐式存在，不显示选择项。点击 `+`，填写节点名称、SSH 目标和远端绝对工作目录，选择 sandbox 或 direct Host；高级设置可填写远端 Python 和服务器 SSH 配置文件引用。

连接由 SessionHost 发起，使用服务器上的 SSH key/agent/config，浏览器不读取本机文件或执行 SSH。默认使用现有 SSH 别名；例如已经能在服务器上以非交互方式运行 `ssh gpu-box true`，就可使用该别名。需事先完成主机身份验证，Recoding 不禁用 host-key 校验，不弹交互密码终端。

远端目录需存在。远端需 Linux、Bash、Python 3.10+；默认 sandbox 还要求可用 Bubblewrap。远端无需安装 Web/CLI 或 Recoding Agent。NodeRuntime 会传输校验后的最小运行代码，并报告该节点的 CPU、内存、磁盘、GPU 与工具能力；探测不代表已实施资源配额。

添加/连接失败会显示错误，不自动降低隔离模式。可以取消添加，或全局 Stop；启动资源回收后才结束这次启动。绑定在成功连接后保存，已发出的 Model call 不变，活跃 Agent 通过系统消息得知新资源。删除绑定不会删除远端工作文件；有存活进程/传输时应先关闭它们。

停止存档只展示节点元数据，不启动 Local 或 SSH；需要 Resume 后才能添加或连接。

## Python Session API

```python
session.configure_remote_node(
    "compute-a",
    name="Compute A",
    ssh_target="gpu-box",
    workspace="/path/to/project",
    execution_mode="sandbox",
    python="/usr/bin/python3",
    # ssh_config="/path/to/server/ssh_config",  # 可选：Host 上的配置文件
    lease_seconds=60,
)
session.connect_node("compute-a")
session.remove_node("compute-a")  # 先关闭该节点存活资源
```

上述名称和路径是合成占位。绑定保存于存档 `remote-nodes.json`，只保存认证配置引用，不保存私钥正文；Resume 后绑定仍在，进程内存不会恢复。也可在创建 Session 时使用 `config["remote_nodes"]` 预置绑定，此时远端按连接/首次使用准备，不改变 Local 必启规则。

## Agent 怎样使用

先调用 `list_nodes` 获取可用节点和实际能力。list_nodes 的 workspace 是节点 Host 绑定，tool_workspace 才是代码中的工作目录（sandbox 通常为 /workspace）。新进程或路径工具用 `node_id`，省略时为 local：

```json
{"action":"create","name":"Data worker","node_id":"compute-a"}
```

以上为 `python_process` 参数；得到 process.id 后，后续 `python` 只需 process_id 与 code。Bash Pool 相同。read_file/edit_files/repo_query/install_packages/view_image 等路径工具也支持 node_id。

已有 process_id/execution_id 自动路由，显式指定另一个 node_id 会拒绝。不同节点 Python 内存不共享；需要协作状态时使用同一个进程，或通过消息/显式文件传输交换数据。所有具备工具的 Agent 可使用/读取各节点进程，Owner/Master 可改名、转交和删除；转交不迁移节点或重放执行。

本地执行卡片保持原样，远程工具卡/进程行附加节点名。Runtime 连接、日志与文件使用相同节点协议；不会要求两个远端互通。生成图文时，远端图片可直接主动 view_image；发给用户/其他 Agent 前应 transfer_files 回 Local，再使用图文消息。

## 断线、Stop 与归档

短暂 SSH 通道断开会连接回同一 Runtime，保留已有进程和工作；成功重连不会触发执行重试。失去提交确认时返回 unknown 与原执行 ID，查询实际状态后再决定，不盲目重放有副作用的操作。

默认租约 60 秒，允许 10–300 秒。Host 持续状态请求续约；Host 失联且租约到期后，远端关闭该 Runtime 的环境。正常 Stop/关闭主动请求回收所有节点；远端不可达时记录未确认回收，由租约兜底。新 Host 不接管旧 token；旧 Runtime 的锁未释放时，需等待其退出。显式“连接”先尝试原 Runtime；只有远端探测确认内核锁已释放，才重建新代际并通知 Master 内存已丢失。网络不可达仍是 unknown，不据此创建第二个 Runtime。

远端日志通过 RPC 读取，已读取部分写入控制端存档缓存；停止浏览不发起 SSH。未缓存内容保留在远端执行数据中，浏览结果会标 cache_only/remote_more_available，不把部分缓存当成完整日志。

完整流程可以用项目的临时 SSH 虚拟节点测试，见 [开发检查](development.md)。同机 loopback 端点能验证真实认证和协议，不等于异构机器测试。
