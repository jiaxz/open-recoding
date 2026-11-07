# 开发与测试

[文档目录](index.md)

架构见 [源码地图](architecture.md)，维护入口是 [AGENTS.md](../AGENTS.md)。Core 的导入依赖只能向内，Web/CLI 不相互导入，TUI 库不进入核心。源码先安装到已有 `.venv`，所有项目 Python 命令用 `.venv/bin/python3`。

## 验证策略

默认只运行与修改直接相关的一项小检查；失败、跨模块影响或明确风险才扩大范围，不例行全量。文档改动检查本地链接、锚点、资源、示例与跨环境可移植性，不需要启动模型；涉及图片中的环境信息时重新生成对应合成截图。

Python 测试用 `workspace_support.temporary_workspace()`，浏览器用 `support.cjs::testDirectory()`，在专用 `sessions/tests/` 创建随机工作区。最外层测试命令结束或失败后，先关闭进程、服务、沙箱和浏览器，再自动清空该目录全部内容；子进程继承外层测试所有者，不在夹具仍使用时清理。使用该根目录的独立测试命令串行执行，真实用户任务不得放入此处。

使用 Demo 或本机 HTTPS/SSE 模型替身，不读取真实 `.env` / Session、不连接真实服务、不调用外部模型 API。有限 driver 显式 Stop endless loop，不引入 Turn 完成语义。复制整个 multiagent 包的隔离测试使用通用存储和注入的执行替身，运行 Master/sub、消息、sleep、Stop/Resume，不借用项目安装的原包、其他核心包或外壳。纯 AgentLoop 另行验证不导入具体运行组件。

## 按改动选择检查

| 范围 | 定向入口 |
| --- | --- |
| 包边界与纯 loop | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_architecture test_agent_package -v` |
| 包共享与清单 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_packages test_package_inventory -v` |
| Non-builtin 与迁移 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_non_builtin_tools test_package_migration -v` |
| Bash、编辑、导航、执行生命周期 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_bash_tools test_coding_capabilities -v`；真实 SSH 用 `test_bash_tools.BashPoolTests`（需 OpenSSH client/server，仅 loopback）；Web 日志用 `node tests/browser/executions.cjs` |
| NodeRuntime / 真实 SSH 虚拟节点 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_node_runtime -v`；Web 用 `node tests/browser/nodes.cjs` |
| 原生团队所有权/事件/生命周期/请求路由 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_team_runtime -v` |
| 权威记录、共同回收屏障、发布幂等与只读索引 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_team_records -v` |
| Worker 单次取信、当次模型选择与本地 recall | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_worker -v` |
| Python Session 聚合 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_session_api -v` |
| 消息、sleep、角色 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_teams test_native_loop -v` |
| Session 共享执行环境/所有权/真实本机 SSH | `.venv/bin/python3 -B -m unittest discover -s tests -p test_execution_service.py -v` |
| Python Pool | `.venv/bin/python3 -B -m unittest discover -s tests -p test_python_pool.py -v` |
| 模型、SSE、跨模型 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_model_context test_chat_stream -v` |
| Host / Sandbox 执行模式 | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_host_execution -v`，离线验证真实进程/路径/图片/包和 Web/CLI 切换 |
| Context | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_background_compaction -v`；更细投影检查见 test_context_window |
| Host 与 GUI | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_session_ownership test_gui_connection -v` |
| CLI / TUI | `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_cli test_tui test_tui_directory_picker -v` |
| Web 消息/导航/图形 | `tests/browser/` 中的 `all-agents.cjs`、`session-groups.cjs`、`local-details.cjs`、`tool-inventory.cjs`、`branding.cjs` 等，按改动选一个 |
| Model call 本步说明 | `node tests/browser/model-step-output.cjs`，本机 HTTPS/SSE 替身检查正文/工具参数说明、展开/回放、详情档位与窄屏；参数剥离与签名检查用 `PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_step_notes -v` |
| Web 启动旧选择 | `node tests/browser/stale-session-startup.cjs`，合成元数据检查失效路径、移动 ID、根外存档、旧格式与真实错误分类，全程不启动 Agent |
| Web 浏览与输入解锁 | `node tests/browser/browse-composer.cjs`，离线检查覆盖、窄屏、Resume 和草稿保留 |
| 文档截图复现 | `node tests/browser/readme.cjs`；仅在需要更新图片时运行，见 [品牌资源](branding.md) |

还可缩小到单个测试函数。具体模块以 `tests/` 为准；upgrade/timeline 命名本身不代表过时。已移除依赖 Turn、spawn/wait/terminate、reset、旧折叠/速度滑块等已取消协议的浏览器脚本；相应当前契约由 native loop、local-details、compact-composer、speed-window 等覆盖。CLI 不再保留递归/Ctrl+R 测试；当前目录导航见 test_tui_directory_picker。历史只读 CC 展示仍有实际渲染器，保留其回归夹具。

当前 CLI 指标/跨节点进程投影、真实屏幕缩放和焦点检查见 `test_cli_alignment`；真实 CLI Host 的多 Agent 双向通信与资源查询见 `test_cli.CLITests.test_current_team_messages_and_processes_through_cli_host`。两者均使用离线数据或本机模型替身。

## 测试维护与过时判断

按用例验证的当前行为判断去留，不因文件名含 upgrade、旧版本号或旧工具名就删除：拒绝旧格式、拒绝已移除工具、保存历史消息展示都可能是现行契约。

- 架构改动后先检查测试能否收集；发现已删除的导入、组件或配置时更新夹具，不向生产代码补兼容别名。
- 运行成功要由实际工具回执证明，例如退出状态、精确 stdout、执行结果和文件内容。上下文含有某个词可能只是模型提交的代码或错误回溯，不能证明代码成功执行。
- 需要跨调用保留 Python 对象的用例必须先创建持久进程并使用同一 process_id；省略 process_id 的临时执行不保证变量保留。压缩测试应同时验证对话、未读消息、worker 与显式持久进程，不能只检查 worker PID。
- 模型颜色等可配置资料从 model_config 取值；测试业务行为，不重新维护一套已经删除的展示常量。
- 只有失去现行行为依据、被等价覆盖或验证已取消功能本身的用例才删除。发现失效断言优先修正，避免通过删除测试掩盖产品回归。

## 精简代码的评估方法

先列出调用方、数据所有权和取消/恢复边界，再判断实现能否删除。区分完全无人调用的遗留路径、同一信任域内的重复处理，以及为了独立 Worker/Node 部署保留的依赖隔离；不能只根据名字或相似文本合并队列、锁或权限校验。

减量按同一口径统计项目级实际净变化，包括替代实现；移动文件、缩写名字和分号压行不算逻辑简化。移除 Agent Worker 前先证明模型网络请求、重试等待、RPC 和 sleep 均可独立取消，Stop/Resume、异常回收与 Host 生命周期仍成立。持久 Context、消息、审计日志和界面预览各有职责，不能直接用截断预览恢复模型历史。

机器上的代码统计、实验方案和验证结果放在忽略的维护报告，通用维护约束写入文档。界面与 CLI 对齐优先测试真实屏幕、后台消息、输入焦点、Host 独占与进程回收，避免只验证方法名称或复制实现的断言。

## 完整检查入口

仅在确有需要时运行：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 -m unittest discover -s tests -v
```

完整检查可能需要本机端口、Bubblewrap namespace 和相应设备权限；GPU 用例不下载权重，按实际硬件条件处理。浏览器用例也按需选取，不在普通改动后整批运行。

## 浏览器测试环境

需要 Node.js、Playwright、Chromium；本机 HTTPS 模型替身另需 OpenSSL，真实工具执行需要 Linux、Python 3.10+、Bubblewrap 和 Bash。Playwright 可以安装在项目外，避免将 `node_modules` 放进源码目录：

```bash
npm install --prefix /tmp/recoding-test-deps playwright
/tmp/recoding-test-deps/node_modules/.bin/playwright install chromium
PATH="$PWD/.venv/bin:$PATH" NODE_PATH=/tmp/recoding-test-deps/node_modules node tests/browser/native-agents.cjs
```

已有安装时直接复用。`PLAYWRIGHT_CHROMIUM_EXECUTABLE` 可指定浏览器路径，`NODE_PATH` 指定外部依赖目录。测试共用 `tests/browser/support.cjs`，在 `sessions/tests/` 中分配本次目录，统一过滤真实模型环境变量、显式指定测试环境文件路径、绑定随机端口、创建临时 TLS 证书，并在结束时关闭浏览器、测试服务和模型替身。外层 Node 进程退出时调用同一个清理器，自动删除全部测试数据；报告仍输出到终端，临时 `test-report.json` 随测试目录删除。

`AGENT_UI_TEST_REPORT` 可保存 JSON 结果，`AGENT_UI_SCREENSHOT` 在支持的流程保存截图；需要保留时写入 Git 忽略的 `.recoding/maintenance/` 或不含 Session 的项目外临时目录；放在 `sessions/tests/` 的报告和截图会被自动删除。证书、截图、请求报文或合成会话不提交 Git。测试不连接 8000 上的现有服务，也不重启实际 Session。

更新运行代码后，重新启动 Web 并刷新浏览器。静态资源按服务实例冻结，避免新页面连接旧接口。

## 打包

```bash
.venv/bin/python3 -m pip wheel . --no-deps --wheel-dir /tmp/recoding-wheel
```

使用默认隔离构建，构建环境满足 `pyproject.toml` 的 `setuptools>=61`。旧版系统 setuptools 下关闭隔离可能生成缺少源码的空包。wheel 包含 `multiagent`、`session`、`node`、`recoding_web`（含静态资源）、`recoding_cli` 和 `tools`（含静态 registry）；`.env`、API key、Session、context 日志和临时报告不进入发行包。源码入口 `recoding-web.py` / `recoding-cli.py`，安装后使用 `recoding-web` / `recoding`。

## 目录卫生

核心源码在 `core/`、外壳在 `apps/`，可重复执行的检查在 `tests/`，最小图片夹具在 `tests/fixtures/`。通用现行文档放在 `docs/`；不在仓库内保留历史维护记录目录。真实部署信息和验证记录放 Git 忽略的本地报告，任何仓库文档都不能保存特定机器事实。

正式测试代码、可复跑场景与必要夹具在 `tests/`，不随运行数据清理。运行生成的 Session、工作产物、图片、日志和报告在 `sessions/tests/`，外层测试命令结束后全部删除。测试 helper 提供独立 RECODING_HOME，禁止写入真实 home 缓存。清理器固定限定此目录，拒绝软链接根目录，并检查尚未释放的 Host/Agent 锁；资源泄漏时应先关闭资源再清理，不能删除锁文件强行解锁。手工验证后可执行 `.venv/bin/python3 tests/workspace_cleanup.py` 使用相同清理规则。

清理源码时移除字节码、构建产物和一次性实验数据。真实 `sessions/` 工作区、安装包、图片输入、`.env` 和 Session 存档保留；不要因目录名含 `test` 就清除用户数据。不建立 `archives/`，源码快照排除此目录。存档结构和清理边界见 [存档与观测数据](session-storage.md)。

源码根 core 下的 multiagent、session、node、tools 分别安装为同名 Python 包；不保留 recoding 聚合包、旧导入名或兼容别名。包名调整后在已有 .venv 更新 editable 安装，Web/CLI 入口继续使用原命令。

源码删除或移动后，打包不能复用旧 build/lib 中的已移除模块。使用干净构建目录，并核对 wheel 的模块清单；Node process_pool.py 与语言 driver.py、worker.py、schemas.py 和 registry.json 必须包含，旧私有/共享解释器模块不能因缓存残留进入发行包。构建缓存不是运行或 Session 数据。

## 临时 SSH 虚拟节点

需要验证 SSH 时可以直接复用 tests/ssh_support.py::local_ssh。它创建 loopback-only OpenSSH 服务、独立随机端口、临时 host/client keys、known_hosts 与 SSH config，工作文件全部在测试根；无需修改系统 SSH 配置，也不借用用户私钥。需要系统已有 OpenSSH client/server 与 ssh-keygen。

测试包使用相同 NodeRuntime 协议与部署路径，不替换 SSH 子进程。test_node_runtime 覆盖双节点并行、状态、重连、显式 owner 转交与退役、文件中转、停止缓存与租约。可用 RECODING_NODE_REPORT 指向忽略的 .recoding/maintenance 下的 JSON 报告路径；运行结果不写入通用文档。使用同一个测试根的测试命令仍须串行，结束自动清理。

Node 通用 Pool 的定向入口为 PYTHONPATH=tests .venv/bin/python3 -B -m unittest test_process_pool -v。它只清理自身独占子目录；当测试根中留有用户手动 Session 时，不使用会清空整个根的测试入口。
