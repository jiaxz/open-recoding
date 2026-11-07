# 工具与沙箱

[文档目录](index.md)

Builtin 工具是 Agent/MultiAgent/Session 机制的直接接口；正式 non-builtin 位于 `core/tools`，也可默认启用。文件位置与 builtin 身份分开，模型仍统一通过标准 Tool call 使用。AgentLoop 顺序执行完整工具批次，异步执行由独立服务管理，不在 loop 内增加分支。

| 类别 | 工具与责任 |
| --- | --- |
| Agent builtin | sleep、send_user_message、recall_context |
| MultiAgent builtin | Agent 创建/停止/恢复、名称/图标、消息、元数据与 Master 全局消息观察 |
| Session | 整体生命周期、配置、存档、压缩；App API 不等于自动向模型开放同名工具 |
| Non-builtin | Bash 三工具、view_image、Python 三工具、pool_status、read_file/edit_files、repo_query、install_packages/inspect_python_environment |

执行基础设施位于 `core/node`，具体工具位于 `core/tools`；每节点一个共享环境，本地与 SSH 远端共用协议，由 Session 装配，Agent worker 仅调用通用工具端口。完整所有权规则见 [统一执行池](execution-pool-design.md)。

## Python Pool

Python 与 Bash 共用节点的通用 ProcessPool，Tools 仅提供语言 Driver 和模型接口。全部具备工具的 Agent 可使用/读取任意进程；Owner/Master 管理改名、转交、删除。Owner Stop 取消自身提交并退役自己拥有的进程，不自动转交。同步结果返回调用者，异步结果通知提交者。完整接口见 [Python](python-pool.md)、[Bash](bash-pool.md) 与 [统一契约](execution-pool-design.md)。

## Bash 与长命令

Bash 临时调用与显式持久 Shell 都通过同一 Node ProcessPool，同进程 FIFO、不同进程并行；sync/async 与状态寿命独立。长驻 SSH 的控制协议由 BashDriver 实现。Owner、取消、回收语义与 Python 相同，见 [Bash](bash-pool.md)。

## 图片与自身历史

### 看图与图片理解

将 PNG/JPEG 放在工作目录，直接向模型描述任务，例如“分析 sales.png”或“比较之前看过的图和 new.png”。模型自主调用 `view_image`，从实际图片回答；普通视觉描述不要求先安装 OCR 或图像库。编辑、裁剪、缩放和精确测量可使用 Python。

Web 的 `view_image` 卡片同时显示该步骤看到的真实图片；Master/sub 均使用自己的快照接口经 HTTP 传输，点击可打开原图。路径、尺寸等工具元数据默认折叠。工具 context 和页面共用保存的快照，原工作文件删除或覆盖不影响查看。

绘图方式由模型根据图像性质和交付要求选择，不固定使用 Python：数据图可选 Matplotlib/Plotly，精确矢量图可选 SVG，数学排版可选 TikZ/PGF，关系图可选 Graphviz/Mermaid，自定义曲线和程序化图形可选 Paper.js/Canvas/D3.js，三维图可选 Blender/Three.js；这些仅是例子，不限制其他工具或组合方式。通过 Python/Bash 使用已验证可用的运行时和依赖，按需在 session 内准备依赖。矢量、PDF 或 HTML 等源格式需导出 PNG/JPEG 预览，才能经现有图片通道显示和保留到 context；有用的可编辑源文件保留在 workspace。

绘图后的 `view_image` **按需调用**，由模型自主判断是否需要检查布局、文字重叠、裁切、清晰度或外观。数据、几何和导出结果也可通过代码检查；不强制每次生成、裁剪或转换后都看图。未收到实际图片时，不能声称已经视觉检查。最终回答的图片传输与 `view_image` 独立，未调用也能展示和保存回答图片。

路径相对于 `/workspace`，或使用 `/workspace/...`，解析后必须位于工作目录。只接受普通文件，拒绝路径越界、软链接、特殊文件、损坏或超限图片；通过内容识别 PNG/JPEG，不依赖扩展名。看图的 schema 和执行适配器位于 `core/tools/view_image`，复用 Agent 核心的安全图片读取/附件能力；读取由同一个沙箱 supervisor 完成，不启动新沙箱或重启 Python。

图片和文字结果分离：工具消息提供路径、尺寸、字节数及 SHA-256，真实图像在工具批次结束后作为图像内容块送给模型。一个批次内相同图像去重；历史快照始终保留，不会因为下一步只是文本或重启 Python 而消失。重复加载相同历史图片会产生新的记录，因此 System 提示模型直接使用已有快照。

一次典型对话的发送顺序：

```text
system
user: 分析旧图
assistant: view_image(old.png)
tool: 图片元数据
user: [旧图的真实图像附件]
assistant: 旧图分析
user: 与新图比较
assistant: view_image(new.png)
tool: 图片元数据
user: [新图的真实图像附件]
assistant: 对比结果
```

后续请求继续包含上面的旧图与新图。图片传输体积、token 和缓存之间的区别见 [API 性能与诊断](api-performance.md)。


`recall_context({start,limit,query,offset})` 只读取自身原始历史正文、工具和图片元数据，不读别的 Agent context、不返回签名、不执行旧代码。默认每页最多 5 条、约 16,000 字符，可分页；不把历史图片自动再次附加到模型请求。

## 包、安装和目录

辅助依赖优先使用 install_packages，安装到 workspace `.recoding/runtime/python-packages`；映射 `/opt/recoding/workspace-packages`，与 Pool、Agent、同 workspace 的其他 Session 共用。缓存位于 home 容器，用户兼容 site 只读，应用 `.venv` 不与沙箱直接共用。已导入的包不会因文件升级自动刷新，按需关闭旧 Pool 进程并创建新进程。见 [包环境](packages.md)。

缺工具时模型应按任务质量自主安装并验证，不要求人类批准 routine 安装或编辑；仍遵守沙箱网络和委派权限。项目自身测试使用项目指定环境，不用辅助包掩盖缺失的项目依赖。

## 执行模式

默认 `sandbox` 使用 Bubblewrap；显式 `host` 将工具放在当前用户的普通宿主子进程中，复用同一 supervisor、RPC 和进程池。选择、真实路径、风险、限制及停止后切换见 [执行模式](execution-modes.md)。下文的挂载表、只读限制、网络/GPU隔离与独立 /tmp 专指 sandbox；Host 不提供这些保证。

## 沙箱、网络与 GPU

本实现使用 [Bubblewrap](https://github.com/containers/bubblewrap) 建立 Linux namespace 沙箱，**不以 `cwd` 或提示词作为隔离边界**。启动时先做真实沙箱检查；没有 bwrap、系统禁止所需 namespace 或沙箱启动失败时，该 Session 拒绝执行，不会自动降级；只有明确选择 Host 模式才直接在宿主执行。

工具内的文件系统视图：

| 沙箱路径 | 来源 | 权限与用途 |
| --- | --- | --- |
| `/workspace` | 绑定的工作目录 | 持久读写，持久工作文件位于这里 |
| `/workspace/.recoding` | 元数据容器根 | 根只读，仅开放下面的 inputs/runtime，Session 历史与日志不可见 |
| `/workspace/.recoding/inputs` | 上传图片的 workspace 副本 | 持久读写，图片资产快照另由宿主管理 |
| `/workspace/.recoding/runtime` | 工具环境、配置和辅助包 | 持久读写，供新建工具环境和其他运行数据使用 |
| `/opt/recoding/workspace-runtime/home` | Workspace `.recoding/runtime/home` | 工具 HOME，XDG 配置/数据/状态与 Matplotlib 配置在其下 |
| `/opt/recoding/host-user/lib/pythonX.Y/site-packages`（若存在） | 当前用户为系统 Python 安装的包目录 | 只读，复用第三方依赖和包数据；不挂载用户 home 的其他内容 |
| `/usr`、`/bin`、`/lib`、`/lib64`（若存在） | 宿主系统运行时 | 只读，提供 Python、Bash、命令和动态库 |
| `/etc/passwd`、`/etc/group` | 合成的 recoding 用户/组，匹配当前 UID/GID 和工具 HOME | 只读，供 OpenSSH 等 getpwuid/getgrgid 使用，不挂载 Host 账号数据库 |
| `/etc/ld.so.cache`（若存在） | 宿主动态链接缓存 | 只读 |
| `/etc/resolv.conf`、`/etc/hosts`、`/etc/nsswitch.conf`、`/etc/gai.conf`（若存在） | DNS 与名称解析配置 | 只读，支持联网解析 |
| `/etc/ssl/certs`、`/etc/pki/tls/certs`（若存在） | 公共 CA 证书 | 只读，支持 HTTPS 证书校验；不挂载私钥目录 |
| `/etc/alternatives`、`/etc/fonts`、`/etc/matplotlibrc`、`/etc/texmf`、`/var/lib/texmf`、`/var/cache/fontconfig`（若存在） | 发行版公共库链接、字体配置/缓存、Matplotlib 默认配置和 TeX 配置/格式 | 只读，支持 BLAS/LAPACK、绘图、字体渲染和 LaTeX；工具生成的文件仍写 workspace 或工具 HOME |
| `/opt/recoding/workspace-packages` | Workspace 的共享辅助包 | 读写，同项目所有 Session 共用 |
| `/opt/recoding/cache` | Host `~/.recoding/cache` | 读写，本机同用户共享缓存 |
| `/opt/recoding/tools/<n>` | 已安装的正式 non-builtin 工具代码 | 只读，在现有沙箱子进程执行 |
| `/tmp` | 每个沙箱独立的 tmpfs | 本 Session 全部工具共享；Session 沙箱关闭时清除，不映射宿主 `/tmp` |
| `/proc`、`/dev` | 沙箱自己的进程视图和设备 | 支持进程运行，不暴露宿主进程列表 |
| `/dev/nvidiaN`、`/dev/nvidiactl`、`/dev/nvidia-uvm*`（GPU 开启且宿主存在时） | 当前用户可读写的 NVIDIA 计算设备 | 设备访问，所有工具共用；不挂载整个宿主 `/dev` |
| `/sys/devices`、`/sys/bus/pci`、`/sys/module/nvidia*` 中选定目录及 NVIDIA 的部分 `/proc/driver` 元数据（GPU 开启时） | 驱动发现与硬件拓扑信息 | 只读，供 CUDA/NVML 初始化；不挂载整个宿主 `/sys` 或 `/proc` |
| 其他宿主路径 | 不挂载 | 不可通过原宿主路径访问 |

“仅限当前目录”在这里具体指：**项目文件的持久写入范围是 workspace；额外允许写入 Recoding 用户缓存 `/opt/recoding/cache`**。Python、Bash 及其子进程所需的系统运行时、当前用户的 Python 包目录只读可见，并有临时目录；不是让整个进程只能打开一个目录中的文件。只读包目录内的文件可被工具读取，因此应只在该目录放包及其运行数据。

Recoding 和 Agent 新建的 workspace 根部隐藏目录只允许 `.recoding`。项目已有隐藏配置保持不动；工具专用环境与新的隐藏运行目录放入 `.recoding/runtime`。默认 HOME/XDG 路径帮助第三方工具遵守这一约定，任意 Python/Bash 代码仍需自行遵守，不通过拦截 mkdir 来限制项目操作。

沙箱初始不继承宿主环境，只获得 `PATH`、`HOME=/opt/recoding/workspace-runtime/home`、`LANG`、`TMPDIR`、workspace 包路径和 home 缓存路径、固定的 `PYTHONUSERBASE` 和公共 CA 路径等配置；单个执行可显式覆盖自身环境，不能隐式改写下次 Bash 的基线环境。API key 仅在宿主 HTTP 客户端中使用。宿主 `PYTHONUSERBASE` 仅参与挂载来源探测，不原样传入沙箱。不会自动继承宿主代理变量或 pip 私有索引凭据。supervisor 与 Python worker 使用 Linux subreaper，结合进程组与后代跟踪做回收；不保证任意主动脱离监管的后台后代按资源精确回收，全 Session PID namespace 关闭提供本地最终边界。所有工具之间不再隔离：可以通过共享 `/proc` 观察进程、发送信号或影响对方状态，但仍不能访问未挂载的宿主文件。

**网络默认开启**：在 `--unshare-all` 后使用 `--share-net`，保留宿主网络 namespace，其他文件系统和 PID 隔离不变。这允许访问外网、局域网和宿主 localhost 服务，没有域名白名单；网络服务自身的访问权限仍适用。工具可以发送其可读的 session 内容，应按联网程序选择工作目录。HTTPS 证书校验保持开启。此配置方式见 [Bubblewrap 官方参数说明](https://github.com/containers/bubblewrap/blob/master/bwrap.xml)。

需要离线工具时这样启动：

```bash
.venv/bin/python3 recoding-web.py --env-file .env -C sessions --no-network
```

该参数同时禁止 Python、Bash 及其子进程联网；宿主 Session worker 调用模型 API 仍需要网络。界面和 system prompt 会记录工具网络已关闭。


## GPU 与资源

开启 GPU 且有可访问设备时，Session 共享沙箱可见相应 NVIDIA 设备、驱动信息和运行库。模型用合适的 CUDA 库/编译器，并先验证设备、显存和实际运行结果。Pool 保留 Session 与调用者的环境委派校验，不能通过开放访问绕过限制。多个进程共享真实硬件预算，删除进程释放其对象/GPU上下文；不要重置设备或干扰其他任务。

默认 sandbox 的同步/异步执行都在沙箱中；显式 host 使用独立宿主子进程，不在 Web/CLI 应用进程中执行模型代码。短工具结束回收其子进程；Bash 同步/异步执行和 Python Pool 均有明确的执行记录与生命周期。源码改变后重启 Host，使 worker、schema 和工具代码指纹保持一致。

## 多节点执行

所有路径工具可指定 node_id，语言进程后续凭 ID 自动定位。Local 必选，额外 SSH 节点由 Session 管理；界面只标记远程位置。连接、权限、生命周期见 [计算节点](nodes.md)。本页固定挂载路径描述单个 sandbox 环境，不代表多个节点共享文件或内存。
