# Builtin 与 non-builtin 工具

[文档目录](index.md)

Builtin 对应 Agent、MultiAgentSystem 或 Session 的机制。正式 non-builtin 位于 `core/tools`，安装为 `tools` 包，由 Session 静态装配，Agent/MultiAgent 不反向导入。默认安装不等于 builtin。用户 workspace 中自由代码仍无工具目录或 manifest 要求。

当前 non-builtin 包含：Bash 工具（bash_process/bash/bash_execution）、view_image、Python 工具（python_process/python/python_execution）、pool_status、read_file/edit_files、repo_query、install_packages、inspect_python_environment 、list_nodes 和 Host 中转的 transfer_files。

## Builtin 的执行边界

Builtin 是 Host 控制平面机制，以管理对象和职责区分，不以是否调用系统接口或创建 OS 进程区分。创建/关闭 Agent worker、维护线程/队列/存档、休眠、发消息等 Host 指令都允许。Bash/Python 任务进程、Node 文件传输和实际计算属于执行资源，通过 non-builtin 工具调用。

当前模型可见 builtin 共 13 项：

| 类别 | 工具 |
| --- | --- |
| Agent 管理 | create_agent、stop_agent、resume_agent、rename_agent、set_agent_icon、list_agents |
| 消息 | send_agent_message、broadcast_agent_message、send_user_message |
| 等待与观察 | sleep、read_global_messages、listen_global_messages |
| 自身历史 | recall_context |

这些工具的处理逻辑在 Host 侧；recall_context 在本机 Agent worker 读取自身历史，其余主要由 SessionHost/协调器执行。当前 Agent worker 是 Host 控制平面子进程，不是 Node 上的任务进程；这里不承诺全部工具都在同一个 OS PID 内。Agent 管理可以触发 Session 的资源生命周期回调，必要的资源回收仍由执行层负责。

Local 执行环境由 Session 创建/Resume 时统一准备，环境元数据注入 Agent；Agent 启动不再通过 builtin/启动 RPC 查询 Node。execution_environment、inspect_tools、request_context_compaction 等宿主控制 action 不等于模型可调用工具，不在 builtin schema 清单中。

## 三种执行适配器

- command：registry.json 声明标准 function schema、源码指纹、固定 argv 和输出类型。代码只读挂载到目标节点的共享执行环境，参数用 stdin JSON，短子进程结束/超时/Stop 回收，不在 Host 执行模型代码。
- runtime：看图等适配器以受维护源码装配到目标节点的共享执行环境 supervisor。每份源码实例化 Runtime(context, ports)，实现 request(name, request)，可选 updates/direct_pids/close。runtime_includes 可声明可信源码依赖，与主文件一起验证指纹、嵌入沙箱。Bash 临时进程与显式持久进程使用相同执行接口，进程仍各自独立；看图复用核心的安全读取与图像附件规则，不在 Host 执行工具代码。
- service：ExecutionService 在 Host 管理执行工具的公共环境、长驻资源和文件中转。transfer_files 的 Host 部分只运行可信文件协议/协调代码，模型不能注入 Host 程序；文件端点在绑定 workspace 中操作。详见 [文件传输](file-transfer.md)。统一 schema、权限、Tool call/结果/事件，额外提供 request、stop_owner、close/reopen 和各 Pool snapshot。Session 直接持有唯一 ExecutionService，并通过统一 RPC 路由执行请求；MultiAgent 仅通知 owner 停止，不持有或关闭工具服务。

服务管理可在 Host 维护元数据，用户代码仍只进入服务沙箱。注册是受维护的静态代码，不接受模型提供的 Host 模块或动态加载路径。不需要为每个服务建立新的 Host 端口。

## 接入与边界

模型只获得已授予 schema，执行时再次校验。根参数必须普通 object，不使用根 oneOf/anyOf；支持的 Schema 子集见 [tools README](../core/tools/README.md)。重复/保留名称、未知参数、错误类型和越界在执行前报错。可信命令定义可设置 max_output（128–1,048,576 字节）；编辑/导航使用 512 KiB 并保持完整 JSON 和分页。

所有来源文件记录指纹，源码变化后重启 Host；禁止静默混用旧服务与新 worker。普通工具错误回到模型，endless loop 继续；不增加人工审批。AgentLoop 不导入 tools，不增加具体 Pool/编辑器逻辑。

工作代码与正式收录的区别、包维护见 [Host 指南](host-maintenance.md)。详细使用见 [Bash Pool](bash-pool.md)、[Python Pool](python-pool.md) 和 [编辑、Bash 执行、导航](coding-tools.md)。

执行服务由 Session 单独持有并统一关闭各节点资源，Agent/MultiAgent 不保存它。语言工具结果统一为 execution（ID 在 execution.id），列表为 executions；参数中的 execution_id 仍用于指定那次执行。事件与查询不再同时携带 task 副本。


Python/Bash 属于 service + Driver：模型工具由 Host 薄绑定调用 Node 通用 ProcessPool，语言 Driver 在目标节点执行；不再各自注册语言 Runtime 队列。Driver 由 Session 注入，Node 不反向导入 tools。新增语言无需复制 Owner/FIFO/Stop 策略。
