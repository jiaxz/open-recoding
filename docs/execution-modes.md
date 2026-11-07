# 执行模式：Sandbox 与 Host

[文档目录](index.md) · [执行池](execution-pool-design.md) · [工具与沙箱](tools-and-sandbox.md)

每个 Session 选择一种执行模式，全部 Agent、Bash/Python Pool、命令工具、看图与工具检查共用。默认 `sandbox`；显式 `host` 让工具直接作为当前用户的宿主机子进程运行。Agent loop、消息、模型调用、进程所有权和同步/异步接口保持一致。

| 行为 | sandbox（默认） | host（显式选择） |
| --- | --- | --- |
| 执行边界 | Bubblewrap 的文件系统、PID 等 namespace | 当前用户的普通 Linux 进程，无上述隔离 |
| Workspace | `/workspace` 映射到项目目录 | 项目真实绝对路径，初始 cwd 为此目录 |
| Python | `/usr/bin/python3` | `/usr/bin/python3`；项目命令可显式使用自身 venv |
| 辅助包 | `/opt/recoding/workspace-packages` | workspace `.recoding/runtime/python-packages` |
| 公共缓存 | `/opt/recoding/cache` | `~/.recoding/cache`，可由 `RECODING_HOME` 覆盖 |
| 工具 HOME | `/opt/recoding/workspace-runtime/home` | workspace `.recoding/runtime/home` |
| 临时目录 | Session 独立 `/tmp` | `TMPDIR` 指向 runtime 下的 Session 专用目录；绝对 `/tmp` 仍是宿主公共目录 |
| 系统和用户包 | 按沙箱挂载只读暴露 | 真实用户/系统权限，不提供只读挂载保护 |
| 凭据与存档 | 指定凭据文件和私有 Session 元数据被隐藏 | 同用户可读写的文件、凭据、存档和进程都可能被工具访问 |

Host 模式仍过滤工具环境，不直接继承 API key、代理或私有索引凭据；保留 workspace 包、HOME、缓存与日志的默认落点。这些是便利和防误操作措施，cwd、HOME、工具 ACL 都不是安全边界。结构化编辑、导航、看图和图片传输继续限定在 workspace；原始 Bash/Python 可以访问更广的宿主资源。

## 启动与切换

推荐的默认启动命令保持不变。显式 Host 模式：

```bash
.venv/bin/python3 recoding-web.py --env-file .env -C ./sessions --port 8000 --execution-mode host
.venv/bin/python3 recoding-cli.py chat --env-file .env -C ./sessions --execution-mode host
```

Web 启动参数设定新 Session 的默认模式，已有存档继续使用保存的模式。新建对话框可以选择；已有 Session 须先 Stop，在右栏 AGENTS → 设置中选择，成功 Resume 后才写入存档。只读浏览不改变模式或启动执行进程；运行中控件禁用，后端也拒绝切换。

CLI 在创建或恢复时用 `--execution-mode sandbox|host` 明确选择并保存。省略时使用已有存档设置；没有字段的当前格式存档按 sandbox 处理。当前 CLI Session 须先 `/stop` 或关闭，再用新参数 Resume；`config` 不能热切换模式。TUI 顶栏显示 Sandbox 或 Host 无隔离。

Host 模式无法兑现断网或 GPU 隔离，因此与 `--no-network`、`--no-gpu` 不兼容，组合时直接报错。恢复曾保存这些限制的存档时，如确定改用 Host，CLI 须明确启用相应参数：

```bash
.venv/bin/python3 recoding-cli.py resume --env-file .env -s /path/to/project/.recoding/session-folder --execution-mode host --network --gpu
```

Web 服务自身禁用网络/GPU 时，也不能通过 Resume 绕过该限制。执行模式不写入模型 `.env`，也不通过模型工具修改。

Python API 可以显式传 `config={"execution_mode":"host"}` 给 `Session.create` / `Session.resume`。调用方仍须结束时 close，有限 driver 必须显式停止 endless loop；直接 Python 实验与正式 Apps 的 Host 协议区别不变。

## 实现和资源回收

选择集中在 `core/node/environment.py`：默认 `Sandbox` 与 `HostExecution` 共用环境准备、`NodeClient` JSON RPC、supervisor、进程池、输出、取消和工具检查。Host 后端使用真实路径，通过文件描述符启动独立 supervisor；模型代码不会在 Web/CLI 应用进程中 exec。没有第二套 Agent loop 或 Pool，也没有沙箱失败后自动切 Host 的路径。

模式改变须回收原执行环境；Python/Bash 内存及连接不跨模式恢复，旧代码不重放。历史中的 `/workspace` 标签不改写；结构化文件/图片工具接受旧标签或当前真实路径，原始 Bash/Python 须根据新的 System 环境说明使用实际路径。消息中的真实 workspace 图片仍保存为服务器资产，经原 HTTP 接口传给远端浏览器。

两种模式都保留进程组、子进程跟踪、subreaper、取消与关闭；Host supervisor 另设置父进程死亡通知并处理控制管道 EOF。Host 无 PID namespace 的最终回收边界，主动脱离监管或强杀时的清理只尽力完成，不能承诺绝无残留。不要用宽泛 pkill、重置 GPU 或操作无关任务。网络和远端 SSH 副作用在两种模式下都不能通过本地停止撤销。

当前两个后端都依赖 Linux、`/proc`、Bash 和系统 Python。Host 模式不需要 Bubblewrap，但不因此成为 Windows/macOS 后端。缺少沙箱能力时可以向用户说明显式 Host 选项，不能擅自降级。

## SSH 节点

上述两种隔离模式也可用于 SSH NodeRuntime，能力在目标节点检查；添加节点时独立选择模式，不自动降级。Local 使用 Session 的 execution_mode，远端绑定保存各自选择，运行环境中的进程不原地迁移。连接与租约见 [计算节点](nodes.md)。
