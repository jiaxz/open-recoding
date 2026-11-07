# 工具完整度

[文档目录](index.md)

右栏 Session Speed 下方显示**Session 共享执行沙箱**的工具完整度，默认紧凑显示 0–100 分、状态与检查时间，展开查看分类覆盖、缺失项、可见 Python 发行版版本及来源（workspace / user-site / system）。All agents 页面显示 Master 的结果，标题旁标明来源；切换到 sub 后展示同一环境的最近报告。停止后保留最近结果，并明确标记；没有记录时提示 Resume 后检查，不自动启动 Agent。

首次检查在共享执行环境启动后异步进行，之后约每 **5 分钟**检查一次，右侧“↻ 检查”可手动刷新（10 秒节流）。前台工具正在占用沙箱通信时，检查会延后，避免相互干扰；不延迟目录同步，也不扫描其他未打开 Session 的历史。

刷新接口为 `POST /api/sessions/<id>/tool-inventory`（Master）或 `POST /api/sessions/<id>/agents/<agent-id>/tool-inventory`（sub），空 JSON 正文，返回 `202 {"queued":true}`；新结果由原有 Session SSE 和快照提供。已停止 Agent 拒绝刷新，不因此恢复运行。

这是一个有明确范围的**通用 coding 工具覆盖指标**，不是模型能力、任务成功率或所有包均可 import 的认证：

| 类别 | 满分 | 检查范围示例 |
| --- | ---: | --- |
| 基础与安装 | 20 | 固定 Python/Bash、pip 或 uv |
| 数值与数据 | 25 | NumPy、pandas/polars、SciPy/SymPy、Excel/Arrow |
| 绘图与图像 | 25 | Matplotlib/Plotly、Pillow/OpenCV、SVG/Graphviz |
| 网络与解析 | 15 | 第三方 HTTP 库、HTML 解析、结构化校验 |
| 文档 | 10 | PDF 读取与文档生成 |
| 开发验证 | 5 | pytest |

每项支持多种实现选择，发现一个候选即可得到该项分数；不按包数量叠加，不因缺少全部替代库而重复扣分。PyTorch、TensorFlow、JAX 及 EXTRA_PACKAGES 中的专业/补充库不计入基线扣分。缺少某个第三方库也不表示 Python 标准库没有相关基础能力。80–100 为较完整，50–79 为部分缺失，低于 50 为常用扩展工具缺失较多。

低于 50 时显示 **Host 安装建议**和首选包名，并向 Master 提交一条可信系统通知，要求它向用户说明情况、请求在 Host 为沙箱实际使用的 `/usr/bin/python3` 配置这些工具；相同严重结果跨周期/Resume 去重，不从每个 sub 重复通知。检查失败/超时只显示“暂不评分”，不能算作全部缺包或触发安装建议。Session 自行安装仍然可用，检查不会自动安装或越过沙箱修改 Host。

探针在**已有长驻沙箱中的独立短进程**运行，不再创建 Bubblewrap；使用相同的 workspace 包目录、挂载的用户 site 与系统包。只做顶层模块发现与发行版元数据读取，不 import 第三方包或执行 `.pth` 启动代码，不修改 Python namespace、Bash 变量/函数或共享 cwd/env。未运行启动钩子可能漏掉依赖这些钩子的包，发行版元数据也不能证明二进制依赖正常。单次限约 4 秒、256 MiB，发行版列表最多展示 512 条；扫描本身不请求模型，严重缺失通知可按普通消息规则唤醒 Master。

Host 在已挂载 site 中安装后通常下次检查可见；首次新建用户 site 目录时，需重建 Session 执行环境才能挂载。已有 Python 可能仍持有旧导入缓存，安装/更新后由 Agent 按需调用 关闭旧 Pool 进程并创建新进程。完整对话和工作文件仍保留。

## 持续积累包集合

Host Coding Agent 盘点 workspace/Session 包时，应把有复用价值的新发现记录回项目，而不只安装到本机。集合位于 `core/node/inventory.py`：CAPABILITIES 定义 100 分基线，PACKAGES 负责 import 名到发行版名的映射，OPTIONAL/EXTRA_PACKAGES 记录不扣分的补充能力。扫描结果中的未审核项目不自动成为必备包。

以下扩展按可复用用途纳入集合；这是通用能力说明，不表示任一部署环境已经安装或通过执行验证：

| 发行版 | 用途 |
| --- | --- |
| ddgs、curl-cffi、websockets | 搜索、网络访问与双向通信 |
| networkx、peewee、psutil | 图/依赖分析、持久数据与资源检查 |
| imageio、scikit-image、opencv-python-headless | 图像/视频读写与图像处理 |
| resvg-cli、svgpath2mpl、uharfbuzz | SVG 渲染、图形转换与文字排版 |
| tabulate | 文本表格 |
| onnxruntime、yfinance | 按需模型推理与金融数据 |

OpenCV headless 是 cv2 的另一发行版实现；resvg 可作为矢量渲染候选。不为同一能力重复计分，也不因安装更多传递依赖而提高分数。新包收录、Host 安装和版本选择的完整原则见 [Host 维护闭环](host-maintenance.md#package-维护的闭环原则)。

## 维护入口

评分目录和实现位于 `core/node/inventory.py`、`inventory_probe.py`；ToolInventoryMonitor 在共享环境启动后异步运行，每个已连接 NodeRuntime 一份扫描；主界面工具完整度保留 Local 报告，远端评分随节点资源投影，不覆盖 Local 报告。探针通过 `NodeClient` 的 RLock 保护通信，以短 RPC 提交并查询独立探针，避免长工具独占控制通道；Session Stop 先停止监视器，再回收沙箱。检查使用同 namespace 的 `-I -S` 短进程，不占用持久 Python / Bash namespace。

报告分别保存在各 Agent 的 `tool-inventory.json` 与 `tool_inventory` 事件中，默认不进入模型 context，也不参与未打开 Session 的导航扫描。通过已有 SSE 更新，不新增订阅或轮询。浏览器只显示后端结果，不检查浏览器所在机器的软件。

各包用途、优先级、替代关系与配套依赖分类见 [包用途与维护优先级](package-priorities.md)。
