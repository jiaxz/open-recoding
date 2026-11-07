# 模型可调用工具清单

[文档目录](index.md) · [工具接入](non-builtin-tools.md) · [Node Process Pool](execution-pool-design.md)

默认完整配置中，Master 可见 28 项，sub-agent 可见 21 项。以下名称是模型请求的 function.name；action 是同一工具的操作参数，不另计工具数量。委派 permissions.tools 或显式工具配置可缩减 non-builtin 清单。

## 所有 Agent 默认可用：21 项

| 工具 | 用途 |
| --- | --- |
| `send_agent_message` | 向一个或多个 Agent 发图文消息 |
| `broadcast_agent_message` | 向其他存活 Agent 广播 |
| `send_user_message` | 向用户发图文消息 |
| `list_agents` | 列出 Agent 公共状态，不读取其 context |
| `sleep` | 休眠；自身收到消息立即唤醒 |
| `recall_context` | 查询自身未压缩的原始历史 |
| `python_process` | 创建、列出、改名、转交、删除 Python 进程 |
| `python` | 同步或异步执行 Python 代码 |
| `python_execution` | 查询、读取日志、取消 Python 执行 |
| `bash_process` | 创建、列出、改名、转交、删除 Bash/SSH 进程 |
| `bash` | 同步或异步执行 Bash 命令 |
| `bash_execution` | 查询、读取日志、取消执行；支持临时管道输入 |
| `pool_status` | 查看 Session 跨节点的进程与执行元数据 |
| `list_nodes` | 列出节点、工作目录与可用能力 |
| `transfer_files` | 经 Host 中转复制节点间文件/目录，支持状态与取消 |
| `read_file` | 读取文件内容及版本指纹 |
| `edit_files` | 按版本条件编辑、打补丁及撤销 |
| `repo_query` | 查询文件、文本、代码结构与定义候选 |
| `install_packages` | 安装辅助依赖到 workspace 包目录 |
| `inspect_python_environment` | 检查 Python 包环境与发行版元数据 |
| `view_image` | 读取节点中的图片并加入模型视觉输入 |

## Master 额外可用：7 项

| 工具 | 用途 |
| --- | --- |
| `create_agent` | 创建空历史 sub-agent；任务另发消息 |
| `stop_agent` | 停止 sub-agent 并触发资源回收 |
| `resume_agent` | 恢复 sub-agent；新指令另发消息 |
| `rename_agent` | 修改 sub-agent 名称 |
| `set_agent_icon` | 修改 Master 自身或 sub-agent 图标 |
| `read_global_messages` | 按需读取 Session 的全局人机/Agent 消息 |
| `listen_global_messages` | 等待全局消息变化，不自动读取正文 |

## 代码与边界

- 基础历史工具在 core/multiagent/context_window.py；协作和角色工具在 core/multiagent/tools.py。
- 正式 non-builtin 由 core/tools/registry.json 和 core/tools/__init__.py 装配。
- Python/Bash 的语言接口在 core/tools/python 和 core/tools/bash，适配器为 PythonTools / BashTools；真正的通用 ProcessPool 在 core/node。
- 模型工具名称不随源码目录改名。进程操作无需 share/revoke，Owner/Master 可 rename/transfer/delete，所有具备权限的 Agent 可使用/读取。
- Web/CLI 内部接口、Node RPC 和后台压缩调度不是模型可调用工具；默认没有单独的互联网搜索工具，Agent 通过 Python/Bash 使用网络能力。
