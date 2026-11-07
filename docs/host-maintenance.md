# 借助 Host Coding Agent 维护项目与 Sessions

[文档目录](index.md)

Host Coding Agent 指运行在宿主机上的 Codex、WorkBuddy 等外部维护者。它可以按用户要求检查 Recoding 源码、项目依赖和 Session 文件；Session 内的 Master/sub 则在沙箱中完成任务。项目不自动启动一个周期性维护 Agent。

本项目把 `AGENTS.md` 和 `docs/` 作为维护操作规程（skill）：`AGENTS.md` 提供约束入口，本文提供执行步骤，[包用途与优先级](package-priorities.md) 提供具体选择依据。每次做事先按当前文档理解范围，完成后同步发生变化的原则、清单和命令，不能只在聊天中约定。用户当前指令优先于文档，历史快照不定义当前行为。

## 开始维护

先阅读 AGENTS.md、本文，以及对应的 [包环境](packages.md)、[工具接入](non-builtin-tools.md)、[Context](context.md)、[Host 协议](session-ownership.md)。确认用户要求维护的 workspace、Session 和范围，识别目前由 Web 还是 CLI Host 持有。

先检查现状，再执行必要修改。复用已有 `.venv`、`.env` 和 sessions，不重建正常环境、不打印密钥、不按目录名字含 test/demo 就删除数据。历史源码快照冻结。项目命令使用已有 `.venv/bin/python3`。

只读包盘点可以在运行时进行，它不读取对话；修改依赖、迁移存档或升级服务前，要考虑正在使用这些资源的任务。运行 Session 的独占由内核 flock 决定，不能删除锁文件或按 PID/文件存在判断是否可用。外部维护者也应遵守这一边界，不借直接 Python API 绕过其他 Host。

## 公共文档与本地事实

README、AGENTS、docs、维护工具说明和文档截图都面向不同机器上的未来维护者，属于可提交 Git 的通用操作规程。只记录产品契约、选择依据、参数说明和可复现方法；例子使用相对路径、显式占位路径或合成数据。

部署方的用户名、绝对工作路径、机器/硬件信息、实际安装/缺失包、Session 数量与 ID、运行端口/PID、临时恢复文件、性能测量和本轮验证结果放入 Git 忽略的 `.recoding/maintenance/` 报告。它们可以用于当前分析，不能写成项目文档的前提。历史维护、部署、实验与测试执行记录同样不进入 Git；可复用经验直接整理到对应现行指南。

“缺包”“已配置”“服务已运行”等结论只能针对明确检查过的目标环境，并在该环境报告中说明口径。通用指南应描述如何检查和处理，而非断言某台机器已经具备什么。更新文档时检查 Markdown、命令、图片文字/元数据和生成脚本；真实界面截图只使用合成内容、虚构路径和固定示例时间。

## Package：检查、迁移和依赖维护

用户可以这样委托：

> 检查这些 workspace 的辅助依赖，说明版本冲突和缺失项；能安全迁移的旧目录进行迁移，冲突保留原样并给出建议。不要升级未涉及的包，不删除旧安装目录。

先进行只读盘点：

```bash
.venv/bin/python3 maintenance/recoding-packages.py --root ./sessions --exclude ./sessions/tests --json
```

可重复 `--root`/`--exclude`，默认搜索当前用户 home。输出按 workspace 去重统计，列出关联 Session、工具 Python 标识、共享包、旧 workspace/Session 包目录和版本冲突。不要用应用 `.venv` 的包列表判断沙箱 `/usr/bin/python3` 是否可用。

需要迁移时，先关闭并释放相关 Session 的 Host 占用；只停某个 Agent 不等于释放 Session。迁移命令会复核 workspace 包锁和 Session 的资源/Host 锁：

```bash
.venv/bin/python3 maintenance/recoding-packages.py --root ./sessions --exclude ./sessions/tests --migrate --json
```

迁移保留旧目录，冲突则阻止该 workspace，不阻止其他 workspace。一个旧目录内可能残留多个版本的 dist-info；需要检查实际文件、RECORD、版本约束和项目测试，不能只删一份元数据来假装问题已经解决。确定版本后可在 workspace 环境中重装并验证；不要静默选择较新版本覆盖。

运行 Agent 优先通过 `install_packages` 安装辅助包，它串行获取 workspace 包锁，使用 home cache。手动 Bash pip 不自动获取此锁，应协调同 workspace 的其他 Session。依赖更新后，已有解释器可能需要 关闭旧 Pool 进程并创建新进程；Python Pool 由 Master 协调重启。模块发现和发行版元数据不是导入成功证明，应补最小真实导入或相关项目检查。

只有用户明确需要维护 Host 用户包时，再审核 `--requirements` 候选并决定是否安装到系统工具 Python 对应的 user site。默认不升级、卸载公共版本，不自动 sudo 或使用强制覆盖系统包管理的选项。缓存位于 `~/.recoding/cache`；清缓存不能删除整个 `.recoding`，home 本身也可能是 workspace 并存有 Session。

## Package 维护的闭环原则

用语约定：用户说“把它们维护起来”，指**正式加入项目完整工具/能力清单**，同时补充用途与使用方式，不是只在当前机器或 Session 中临时安装。Package 记录到工具完整度集合，模型可调用工具记录到 tools 的 registry；两类清单互相链接，包不自动变为一个新的 Tool call。安装/升级仍按具体任务范围执行。

包维护同时改善本机环境与项目的长期能力集合：**发现 → 评估用途 → 按用户要求安装/归并 → 更新项目集合 → 定向验证与记录**。安装成功和项目收录是两件事，不能只在一台机器上安装后就结束。

- 以沙箱实际使用的 `/usr/bin/python3` 为比较对象，同时检查它的系统 site 和用户 site（通常位于 `~/.local`），不能只比较用户 site，也不能拿 Apps 的 `.venv` 代表 Agent 环境。缺少发行版、版本不同、能力已有替代实现分别说明。
- 扫描当前 workspace 共享包，也单独记录旧 workspace/Session 包；按名称归一化和 workspace 去重，保留来源与版本。测试与快照数据明确排除，不从目录名猜测可删除；不完整扫描不能生成可安装结论。
- 新发现的包由 Host Coding Agent 判断是否有跨任务复用价值。核对用途、发行版名与 import/命令名；有价值的加入 `core/node/inventory.py` 的集合，并在工具完整度文档记下用途。用途尚不明确的保留为未审核候选，不凭安装频率直接收录。
- 通用基础能力进入 CAPABILITIES 或作为现有能力的替代实现；研究、金融、ML 等专业扩展通常进入 EXTRA_PACKAGES/OPTIONAL，缺少它们不扣基线分。单纯传递依赖由包管理器解决，不逐个变成必备工具。基线仍为 100 分，只有改变指标设计时才调整权重。
- 项目集合描述可复用能力，不绑定任何部署环境的实测版本，不把这些包全塞进 Recoding 的 pyproject 运行依赖。具体安装版本根据目标 Python、项目锁定、依赖关系和二进制条件决定。
- 安装目标保持分工：项目依赖用项目环境，Agent 辅助包用 workspace 共享目录；按用户要求推广的公共包安装到工具 Python 对应的用户 site 或由系统包管理器管理。扫描/收录不等于自动升级、卸载公共包或覆盖冲突版本。
- 元数据“存在”不等于 import 或实际命令可用。CLI 包还要核对沙箱 PATH；包更新后按需重启解释器、刷新工具完整度。检查只用必要的导入/小任务，不为维护运行全量测试。
- 交付包含缺包、已有版本差异、未决冲突、实际安装范围、项目集合变更和报告位置。原目录、Session、真实工作文件保留；仅更新源码时说明是否需要重启 Host。

`maintenance/recoding-packages.py --json` 的 package_collection 将已收录与未审核包分开；host_missing_packages 覆盖当前及旧目录的本机缺失名称，legacy_host_comparison 保留旧目录位置。candidate_requirements 仅是当前共享目录的安装候选，不将旧目录或未知兼容性的包直接升级为安装计划。

## Tools：自由开发与正式收录

用户可以这样委托：

> 将项目中这个有复用价值的脚本整理成 Recoding non-builtin 工具，放入 Recoding 源码的 tools 目录，保持相同 Tool call 接口、权限和取消逻辑，补一项最小离线测试与说明。

workspace 中的工具就是普通项目代码，目录和接口由 Agent 决定。无需 manifest 或登记即可通过 Python/Bash 运行。home 没有公共 tools 库。

正式收录时修改 Recoding 的 `core/tools/`：提取硬编码项目路径为参数，在 registry.json 增加 plain-object function schema、固定 argv 和输出类型；工具参数通过 stdin JSON 传递，不拼进 shell。实际代码仍由 Agent 现有沙箱执行，Host 只加载静态定义，不 import 工具脚本。

同时检查依赖、allowlist、超时、Stop、日志与模型结果。multiagent 运行包不反向导入正式工具包，loop 保持无具体执行依赖。更新已有 editable 安装，重启 Host 后生效；不在运行时偷偷替换已加载的源码。当前两个例子是 install_packages 和 inspect_python_environment，详见 [tools README](../core/tools/README.md)。

## Context：判断、执行与排错

用户可以这样委托：

> 检查这个 Agent 的 context 预算是否准确，解释 API Input 实测、校准锚点与本地估算；需要压缩时使用项目既有流程，保留原始记录，不手工截断历史。

先看当前 Agent 的 context-budget.json、压缩请求与运行观测，区分最近请求 Input、Session 累计量、本地估算和 API 实测；不从预算中扣 Cache hit。Master/sub 各自独立压缩。分析具体历史前先遵守所在 Host 的访问边界，避免在运行期间并发改 state/context-window 或消息队列。

压缩由 Session 自动后台启动。Web 右栏顶部显示目标 Agent 和取消按钮；CLI Host 通过本机控制通道查看/取消：

```bash
RECODING_SESSION=/path/to/workspace/.recoding/session-folder
.venv/bin/python3 recoding-cli.py compact -s "$RECODING_SESSION" --json
.venv/bin/python3 recoding-cli.py compact -s "$RECODING_SESSION" --cancel --request-id REQUEST_ID --json
```

这些 compact 命令只连接已经存在的 CLI Host，不能接管 Web Host。停止存档不为检查而 Resume。后台只压缩目标 Agent 的已提交历史前缀，不停止任何 Agent，不回收 Python/Bash/Python Pool。成功分块先写候选检查点，完整成功才原子提交 API 投影，后续 Model call 加载它并保留期间新增的历史；取消/失败时 loop 继续使用旧投影，重试复用检查点。

Host Coding Agent 可以帮助评估预算、查看维护状态和取消错误触发，不应手工删消息、拼签名、重写原始 state.json 或把外部随手摘要当作合法 context-window。保留 API 错误和必要诊断，不把完整私有 context 自动上传到其他服务。机制见 [Context](context.md)。

## Session 与项目维护

移动或备份 Session 前，先关闭其运行环境并释放占用，再复制完整存档。共享依赖位于 workspace 的 `.recoding/runtime`，移动单个 Session 不会带走它们；迁移整个项目时一并考虑。原始图片、runs、消息队列和模型来源字段都属于存档的一部分。

升级 Recoding 时，先完成代码、文档和当前改动的必要离线验证，再更新已有环境、按任务需要重启 Host。重启会回收内存状态，不应声称旧 Python 对象或后台进程可以恢复。停止的存档只作浏览，不为检查擅自唤醒 Master。

测试代码放 tests，测试 Session/workspace 放专用 sessions/tests 随机目录；测试结束或失败先关闭运行资源，再自动清空其中全部内容。不要清理其他 sessions 子目录；需要保留的脱敏报告先写到 `.recoding/maintenance/`。普通修改不跑全量，也不连接真实模型做测试。交付时说明改了什么、检查了什么、迁移了哪些目录、仍有哪些冲突，以及是否需要重启。

更换模型或 API 时，按 [模型/API 维护边界](model-api.md) 集中修改配置、资料与适配器，不把厂商判断散布到 Agent loop、Web 和 CLI。

用户明确要求对已停止 Session 直接压缩时，使用 `maintenance/recoding-context.py --live`，该工具取得 Host/Agent 内核锁，调用同一 Core 摘要驱动。支持校验后从失败日志恢复成功前缀，并可 `--probe` 验证压缩后的 API 请求（不执行返回工具）；完整参数和通用命令见 [Context 维护](context.md#host-coding-agent-显式维护)。不要删除锁文件或绕过运行 Host，不把真实存档/摘要/报告提交 Git。
