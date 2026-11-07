# Recoding non-builtin tools

这里保存纳入 Recoding 源码、测试和发行的正式 non-builtin 工具。用户 workspace 中自由编写的代码没有目录、接口或 manifest 要求，写完即可通过 Python/Bash 使用。

当前实现位于 `core/tools/`，command 类静态列表为 `registry.json`，Pool 和看图的定义由 definitions() 静态装配。每条记录包含标准 function schema 和可信执行适配器配置。与 builtin 一样由模型按名称直接调用，经过统一权限、工具事件、超时、Stop 和结果提交；不增加通用 run_tool，也不改变 endless loop。

- `transfer_files`：Host 分块中转节点间文件/目录，同步/异步、进度与取消；详见 [文件传输](../../docs/file-transfer.md)。
- `list_nodes`：公共节点、连接状态和实际能力；新进程/路径工具用 node_id，旧资源 ID 自动路由。
- `pool_status`：所有具备工具的 Agent 可读的 Session 全局资源元数据总览；不返回代码、日志正文或 Agent context。
- `python_process` / `python` / `python_execution`：Node 通用 ProcessPool 的 Python Driver、开放使用、同步/异步和软取消；由服务适配器托管。
- `install_packages`：在 workspace 包锁下运行 pip，使用 workspace target 和 home cache。
- `inspect_python_environment`：不加载第三方包的元数据检查。
- `read_file` / `edit_files`：版本条件编辑、严格补丁与可核验撤销，无 Git 前置条件。
- `repo_query`：有界文件/文本查询与惰性定义索引，明确解析器与启发式边界。

Bash Pool（bash_process/bash/bash_execution）和看图（view_image）也是 non-builtin。Bash/Python 的 Driver 注入 Node 通用 ProcessPool；看图仍由 definitions() 装配 runtime 到目标节点；Bash/Python 不传 process_id 为临时执行，传入则复用持久进程；同步/异步独立于进程寿命，均保留执行记录。核心不硬编码其工具入口。完整参数与使用边界见 [代码工作能力](../../docs/coding-tools.md)。

command/runtime 的执行代码进入目标 Node 的执行环境。服务适配器可在 Host import 可信控制代码，例如 Pool 元数据和文件中转；Host 不执行模型生成代码。command 适配器的参数通过 stdin JSON 传递，不插入 shell。短进程不污染 Pool/Python 或持久 Bash 内存，结束/超时/Stop 回收其进程组。命令适配器不用于托管长驻服务；Python Pool 使用有 stop_owner/close 生命周期的服务适配器。

由用户要求 Host Coding Agent 收录工具时：提取项目专用路径为参数，在 registry.json 增加 plain-object Schema 和 argv，补最小离线回归，更新本页和文档。参数 Schema 支持 object/array/string/boolean/integer/number/null、required/additionalProperties、enum、长度/数值/条目边界和 uniqueItems；不支持的关键字在启动时报错。子 Agent 只能使用已授予的工具，不因实现使用 Python 就获得 python 工具权限。

Session 装配时注入定义，独立复制的 Agent 不依赖此包。源码发生变化后重启 Host；已加载定义的源码指纹不匹配会拒绝运行，避免无意热更新。维护说明见 [Host 维护](../../docs/host-maintenance.md)，机制见 [工具接入](../../docs/non-builtin-tools.md)。

runtime_includes 声明可信源码依赖；requires 声明委派工具所依赖的执行权限（例如 bash_process 需要 bash）。

沙箱 runtime 接口为 Runtime(context, ports).request(name, request)，可选 updates()/direct_pids()/close()。ports 只提供可信沙箱内能力（图片读取、进程清理），不能由模型传入源码/模块路径。Agent 包可独立运行并显式注入 schema、提示和外部工具 RPC；不安装扩展时不提供 Bash 或看图工具。

完整模型工具名单见 [工具清单](../../docs/tool-calls.md)。语言包为 python/ 与 bash/，薄适配器为 PythonTools / BashTools；通用 ProcessPool 及 Host 投影在 core/node。
