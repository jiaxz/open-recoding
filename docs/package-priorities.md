# Package 用途与维护优先级

[文档目录](index.md) · [Host 维护操作规程](host-maintenance.md) · [工具完整度集合](tool-inventory.md)

本文供维护 Recoding 的 Host Coding Agent 使用，也供人类审阅。记录哪些包提供什么能力、如何选择和收录，不是锁定版本的 requirements，也不代表这些包均已安装或已经通过实际任务验证。

“把它们维护起来”表示正式加入项目完整工具/能力清单，并补用途、使用入口与维护说明，不只是临时安装。Package 集合在 `core/node/inventory.py`；模型可调用工具在 `core/tools/registry.json`。包收录不自动增加一个模型 Tool call，也不自动授权 Host 安装/升级。执行步骤见 [维护闭环](host-maintenance.md#package-维护的闭环原则)。

## 排序口径

以下是可复用的包选型参考，按**通用任务覆盖、能力增益、使用频率和替代方案**排序；不描述任何部署环境的已装或缺失状态。这是维护者的判断，不是工具完整度的评分权重；不同任务可调整。例如金融工作可提前 yfinance，文档工作可提前 PDF 解析。

部署前先检查目标环境。NumPy、Pillow、Matplotlib、requests、BeautifulSoup 和 lxml 等基础库的可用性会影响后续选择；已有可用实现时优先复用。包的实际版本、可见性和缺失情况记录在部署报告中，不能据此改写通用优先级或声明其他环境具备相同条件。

“缺失”指相应发行版未在工具 `/usr/bin/python3` 的系统和用户 site 中发现；不代表 workspace 里不能用，也不代表其他虚拟环境没有，更不等于相同能力没有替代实现。

## 第一优先级：通用能力，按任务优先补齐

组内从上到下是建议顺序；同组的替代实现不必全部安装。

| 顺序 | 发行版 | 用途与典型任务 | 选择方式 |
| --- | --- | --- | --- |
| 1 | `pandas` | 清洗、关联、分组统计、时间序列及 CSV/表格数据分析。 | 通用数据工作的基础；Excel 等格式还需相应读写引擎。[官方介绍](https://pandas.pydata.org/docs/getting_started/overview.html) |
| 2 | `scipy` | 数值优化、积分、插值、统计、信号处理、稀疏计算。 | 让计算任务使用成熟算法，而非临时手写。[官方指南](https://docs.scipy.org/doc/scipy/tutorial/index.html) |
| 3 | `pypdf`、`pymupdf` | pypdf 适合 PDF 文本/元数据提取、拆分合并；PyMuPDF 侧重高效解析、页面渲染、图像提取和编辑。 | 简单 PDF 操作可先选 pypdf；需要渲染与复杂文档处理时考虑 PyMuPDF，不必一开始同时装齐。[pypdf 文档](https://pypdf.readthedocs.io/en/stable/)、[PyMuPDF 文档](https://pymupdf.readthedocs.io/) |
| 4 | `ddgs` | 聚合互联网搜索服务，帮助 Agent 寻找资料、技术方案和来源页面。 | 搜索结果仍须抓取原文核实；不保证服务始终可用。[项目说明](https://github.com/deedy5/ddgs) |
| 5 | `sympy`、`mpmath` | SymPy 做符号推导、方程、微积分与代数化简；mpmath 做任意精度数值计算，也支撑 SymPy。 | 适合数学问题和独立验算。检查 SymPy 对 mpmath 的版本约束，不能把不同目录的最高版本直接拼起来。[SymPy 文档](https://docs.sympy.org/latest/index.html) |
| 6 | `psutil` | 观察进程、CPU、内存、资源消耗和子进程。 | 对多 Agent 构建、测试与任务排障有价值；可见范围取决于沙箱。[官方文档](https://psutil.readthedocs.io/) |
| 7 | `resvg-cli`、`cairosvg`、`resvg-py` | SVG 光栅化与展示图生成。resvg-cli 是命令行入口，resvg-py 提供 Python 绑定；CairoSVG 可转换 PNG/PDF 等格式。 | 通常先选一种可用实现，保证 SVG 作品能生成浏览器/看图工具可见的 PNG。CLI 必须在沙箱 PATH 中；CairoSVG 还涉及 Cairo 原生库。[CairoSVG 文档](https://cairosvg.org/documentation/) |
| 8 | `networkx` | 图算法、依赖关系、最短路径、拓扑排序和网络分析。 | 适合代码依赖、任务依赖及关系数据分析；不会自动成为 Agent 调度系统。[官方文档](https://networkx.org/documentation/stable/) |
| 9 | `opencv-python-headless` | 图像几何变换、特征、检测、视频和截图的像素级处理。 | 无桌面环境使用的 OpenCV 发行版，import 名为 cv2；与其他 OpenCV 发行版是替代关系。普通看图由模型完成，并不依赖它。 |
| 10 | `httpx` | 同步/异步 HTTP 请求、连接池和流式传输。 | 适合批量抓取/API 并发；已有 requests 时不是完全缺少网络能力。HTTP/2 等能力按官方扩展依赖配置。[官方文档](https://www.python-httpx.org/) |

## 推荐起步组合

下面是覆盖通用任务的**示例组合**，供目标环境评估后裁剪；不要求安装全部包，也不假定基础依赖已存在：

```text
numpy pandas scipy sympy pillow matplotlib pypdf pymupdf ddgs httpx psutil networkx cairosvg opencv-python-headless
```

这套组合覆盖数据、数值/符号计算、绘图、PDF、搜索/异步联网、资源/依赖分析及图像处理。它是选型示例，不是 Recoding 的启动依赖或某次部署的安装记录；工具完整度仍按实际探针结果计算。

SVG 可选择 Python 可调用的 CairoSVG，采用前检查 Cairo 原生库；也可按性能、格式与部署条件选择 resvg。它与 resvg 方案可互补，初期不要求所有渲染器都装齐。pypdf 提供轻量 PDF 操作，PyMuPDF 补页面渲染与复杂解析。

依赖安装到工具 Python 对应环境，选择与已有包相容的版本，让解析器处理 mpmath、HTTP/CFFI 等依赖。保留可用的 NumPy/Pillow/Matplotlib 等公共版本，按具体约束解决冲突；SymPy 对 mpmath 的约束尤其要核验，不直接搬入旧目录的最高版本。

第二批按任务考虑 scikit-image、imageio、tabulate、curl-cffi；其中部分会由其他包作为依赖安装。金融、本地模型、版面分析和专门矢量化任务出现时，再选择相应专业包。

## 第二优先级：任务出现时补充

| 顺序 | 发行版 | 用途与选择依据 |
| --- | --- | --- |
| 11 | `imageio`、`scikit-image`、`tifffile` | 分别用于图像/视频文件读写、科学图像算法、TIFF/多页或科学影像读写。与 Pillow/OpenCV 有交集，按格式和算法需要选择；部分视频格式需额外插件。 |
| 12 | `pymupdf4llm`、`pymupdf-layout` | PDF 内容结构化、阅读顺序与版面分析相关能力，适合文档检索和模型阅读。组件关系随发行版本变化，仅有名字/版本的元数据不能证明有效安装，需按官方说明核验。[PyMuPDF4LLM 文档](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) |
| 13 | `tabulate` | 生成终端或 Markdown 等文本表格，让检查结果更清楚；主要改善展示便利性。 |
| 14 | `curl-cffi` | 基于 libcurl 的 Python 客户端，适用于有特定协议/浏览器兼容需求的网络访问；不是 JavaScript 浏览器。常规请求先用已有客户端。 |
| 15 | `svgpath2mpl` | 将 SVG 路径转换为 Matplotlib 可用的路径，适合复杂图形嵌入和图形衔接。 |
| 16 | `uharfbuzz` | 文字塑形，处理复杂文字组合及字形位置；用于排版/渲染链路，不代替字体或完整排版工具。 |
| 17 | `vtracer` | 位图转矢量图，例如将标识或线稿转换为 SVG；不是普通看图的前置依赖。 |
| 18 | `peewee` | 轻量 ORM，适合持久任务表和数据库项目；简单 SQLite 场景也可用标准库。 |
| 19 | `websockets` | WebSocket 客户端/服务端通信，适合实时双向连接；现有模型 SSE 不因安装它而自动改变。 |

## 第三优先级：专业任务按需准备

| 顺序 | 发行版 | 用途与选择依据 |
| --- | --- | --- |
| 20 | `onnxruntime` | 执行本地 ONNX 模型，可用于推理组件或特定图像/文档任务。还需要模型文件和合适执行后端；安装它不会增强远端 API 模型，也不自动获得 GPU 推理。[官方说明](https://onnxruntime.ai/docs/) |
| 21 | `yfinance` | 获取 Yahoo Finance 相关行情与金融数据，适合金融研究任务；对一般代码工作非必需。数据口径和来源仍需核对。 |

## 配套依赖：跟随上层能力维护

下面列出的配套发行版很重要，但通常不需要独立决定安装顺序。上层能力需要它们时，应一起满足依赖；“不是优先单独挑选”不表示可以跳过必要依赖。

| 发行版 | 实际作用 | 常见关联 |
| --- | --- | --- |
| `httpcore`、`h11`、`anyio`、`primp` | 分别提供底层 HTTP 传输、HTTP/1.1 协议、异步并发基础及网络客户端实现。 | HTTPX、搜索客户端等网络链路；具体依赖以所选版本为准。 |
| `cairocffi`、`cffi`、`pycparser` | 分别是 Cairo 绑定、调用 C 接口的桥梁、C 语法解析组件。 | CairoSVG、curl-cffi 等原生库绑定链路。 |
| `defusedxml` | 对标准 XML 解析提供防护封装。 | SVG/XML 处理，例如 CairoSVG。 |
| `protobuf`、`flatbuffers` | 两种结构化数据序列化格式的运行支持。 | 本地模型、ONNX 及其他使用这些格式的库。 |
| `lazy-loader`、`typing-extensions`、`tzdata`、`platformdirs`、`multitasking` | 分别支持延迟加载、类型接口兼容、时区数据、跨平台缓存/配置目录、轻量并发辅助。 | 科学图像、数据分析、金融及其他上层包。 |

## 给维护 Agent 的执行规则

1. 先按 [Host 维护指南](host-maintenance.md) 做只读扫描，不把扫描结果当作安装许可。维护入口为 `maintenance/recoding-packages.py`，使用已有 `.venv/bin/python3`。
2. 比较工具 Python 的系统与用户 site，区分缺失、旧版本、同功能替代、未知兼容性和旧目录冲突。不要把每一个发行版缺失都解释成一种能力缺失。
3. 明确用户要的是用途评估、正式收录、安装还是迁移。“维护起来”默认至少完成正式收录和文档，不只留下本机安装结果。
4. 收录时登记发行版名、import/命令名、用途、基线/替代/可选/配套依赖分类及最小使用或验证方式。基线按能力计分，总分仍为 100；专业扩展与传递依赖不自动加扣分。
5. 选择上层能力组合，让依赖解析器处理依赖约束。不机械复制所有旧包，不固定使用扫描到的最高版本，也不把所有辅助包放进 Recoding Apps 的运行依赖。
6. 按明确授权安装到项目环境、workspace 辅助目录或 Host 用户/系统环境；依赖更新后检查需要重启的解释器和 CLI PATH。元数据可见性、import 成功和实际任务成功分别说明。
7. 只做与修改相关的最小验证。真实 Session 和工作数据保持不动；专用 sessions/tests 测试数据在资源关闭后自动清空，机器报告放忽略目录；仓库文档维护通用原则、完整能力清单与用途。
8. 若发现本文或维护指南与当前代码不一致，修正当前文档，不以旧聊天或历史快照替代现行约定。上层依赖包与能力边界变化时，同步更新本文。
