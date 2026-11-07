# Bash 进程工具

[文档目录](index.md) · [Node Process Pool 完整契约](execution-pool-design.md)

Bash 工具使用 Node 通用 ProcessPool。core/tools/bash/driver.py 负责临时 Bash、持久 Shell 和 SSH 协议，worker.py 负责命令输出边界；Owner、FIFO、超时和回收由 Node 统一实现。

## 临时与持久

bash 不传 process_id 为临时 Shell，文件保留、变量不保留。bash_process create 显式建立持久 Shell，再传 ID 复用变量、函数、cwd、export 环境与 shell 设置。两者均支持 sync/async。

```json
{"action":"create","name":"build","cwd":"/workspace"}
```

```json
{"process_id":"<process-id>","command":"value=41; echo $((value+1))","mode":"async"}
```

async 完成通知给提交者，不给当时的 Owner；同一进程 FIFO，不同进程并行。每次调用有独立 execution.id。bash_execution 提供 list/status/read/cancel；临时 async keep_stdin_open=true 可 write 管道输入，持久 Shell 的 stdin 是控制协议，不接受普通 write，用 heredoc 或文件。

每个 Agent 都能使用/读取所有进程。Owner/Master 才可 rename、transfer、delete。Owner Stop 取消排队并退役，当前由其他 Agent 提交的调用可结束，默认 5 秒宽限后强制回收；转交必须发生在 Stop 前。Session Stop 丢失 Shell 状态。

## 长驻 SSH

在 bash_process create 中传 ssh_argv，包含 SSH 可执行文件、选项和目标；驱动追加受管理的远端 Bash 引导，后续命令复用同一个连接。密钥与 known_hosts 路径须在当前执行环境可访问，Host 凭据不会自动挂载。

无 PTY，不支持任意交互终端。不要用普通 bash 调用 ssh 后假设以后调用都在远端。不要 exit/exec 受管持久 Shell、修改 __recoding_* 控制名或把任务 daemonize。

本地持久命令取消尝试保留 Shell 状态；SSH 取消可能关闭连接并丢失状态，也不保证远端脱离监管的后台进程退出。不会自动重连或重放命令。

Session 的 SSH NodeRuntime 连接是另一层：它可以在短暂通道断开后重连同一 Runtime；与上述由 BashDriver 自己创建的 SSH Shell 连接不同。见 [计算节点](nodes.md)。

完整权限、执行排队、日志、取消与回收边界见 [统一契约](execution-pool-design.md)。
