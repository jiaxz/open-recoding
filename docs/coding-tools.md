# 编辑、Bash 执行与代码导航

[文档目录](index.md) · [工具与沙箱](tools-and-sandbox.md)

这三种能力服务于自主开发和探索：Agent 读代码、修改、测试、处理冲突和失败，不增加人工审批环节。Bash 同步/异步执行、Python Pool、可靠编辑与导航都是项目正式 non-builtin 工具，由 Session 静态装配，均在已有沙箱中执行。核心 AgentLoop 仍只负责模型与完整工具批次的顺序。

## 可靠编辑：read_file / edit_files

先读取当前文件及全文字节的 SHA-256，再以这个版本为条件修改。返回行片段也会给出完整文件的版本；不能拿片段自行计算全文版本。

```json
{"path":"src/example.py","start_line":1,"end_line":100}
```

`read_file` 返回路径、行号、正文、sha256、权限、换行类型和截断标志。默认正文最多 16,000 字符，可设 max_chars 128–64,000；单文件不超过 16 MiB。结果使用独立的 512 KiB 结构化传输预算，避免普通 stdout 上限切坏 JSON；正文仍按 max_chars 有界返回。超长单行以 partial_last_line 标记，next_line 指向尚未完整读完的行，需要更大范围或 Python/Bash 分片读取。只处理 workspace 内普通 UTF-8 文件，拒绝软链接、特殊文件和路径越界；二进制等特殊操作仍可自主使用 Python/Bash。

以下是 `edit_files` 参数示例，其中版本字符串需要换成实际读到的 sha256：

```json
{"changes":[{"path":"src/example.py","expected_sha256":"<read_file 返回的 sha256>","replacements":[{"old":"return 1","new":"return 2","count":1}]}]}
```

每个 change 可选择 create、modify（默认）、delete、rename：

- create 使用 expected_sha256=`absent` 和 content；文件已存在就冲突。
- modify 在 content、replacements、patch 中选一个。替换要求旧文本恰好出现 count 次（默认一次）；patch 是包含 `@@` 和旧上下文的 unified diff，旧上下文必须唯一且完整匹配，不模糊猜测位置。
- delete 仍要匹配当前版本。
- rename 指定 new_path，目标必须不存在；移动和删除分步发布，失败时查看 journal。

这些机制不依赖 Git，也不要求目录是仓库。Git 可以另用于查看改动或项目历史。CRLF 修改维持 CRLF，已有可执行权限保留；创建默认 0644，可明确给 mode。

### 冲突、部分提交与撤销

一次最多 32 个 change。短 workspace 文件锁让使用本工具的 Agent 串行完成预检和发布；批次预检失败不改目标文件。每个文件使用同目录临时文件、fsync 和原子替换发布，但多文件批次并非整体原子事务。

每次实际编辑把旧字节、版本和提交进度写到 `.recoding/runtime/edits/<journal_id>/`。结果给出 committed、revisions、diff 预览及 journal_id；中途失败明确给出 partial 和已提交路径。预写 pending 记录用于识别“文件已发布、进度尚未更新”的中断窗口。

```json
{"undo_id":"<journal_id>"}
```

撤销先核验每个目标仍是该批次留下的版本，再恢复旧字节；后续有人修改则拒绝覆盖。撤销也记录新 journal，可检查其结果。原始 Python/Bash 写文件仍被允许，它们不会自动遵守此锁；版本复核能发现多数竞争，但无法把任意外部写入变成全局事务。发生冲突后模型应读新版本、协调归属并重新修改，不需要让用户批准。

## Bash：同步与异步

`core/tools/bash_pool` 提供三个 non-builtin 工具。省略 process_id 使用临时 Shell，传入时使用显式持久 Shell；两者支持同步/异步，均在 Session 的唯一执行沙箱内。SSH 示例和完整生命周期见 [Bash Pool](bash-pool.md)。

| 工具 | 用途 |
| --- | --- |
| bash_process | create/list/delete 持久本地或 SSH Shell |
| bash | process_id（可选）、command、mode（sync/async）、cwd、env、timeout、name、keep_stdin_open |
| bash_execution | action=list/status/read/cancel/write；除 list 外需要 execution_id |

```json
{"command":"python3 -m unittest discover -s tests -v","mode":"async","cwd":"/workspace/project","env":{"TEST_MODE":"1"},"timeout":1800,"name":"test suite"}
```

sync 默认等待结束，返回唯一的 execution 记录、实际退出码与 stdout/stderr；async 立即返回 execution 记录（ID 为 execution.id），完成、失败或超时后向本 Agent 持久队列放一条系统通知，唤醒 sleep。同步结果不会重复产生完成通知，日志每行不触发模型。工具批次仍按顺序调用，多个 async 执行可以并行。

省略 timeout：sync 使用 Session 配置的 tool_timeout，async 无限运行；显式 0 为不设期限。执行可取消，Sub Stop 只回收临时执行并移交持久进程；Session Stop 回收全部。每 Session 最多 32 个活跃 Bash 执行，超过时先取消无用任务。Python 有状态计算使用 [Python Pool](python-pool.md)。

### 环境与输入

临时执行 cwd 默认 `/workspace`，相对 cwd 也从这里解析。env 是变量名到字符串的覆盖表，最多 64 KiB；初始环境来自沙箱，不继承 Host 的模型密钥。临时执行的 cd/export、变量、函数、Shell 设置不跨调用保留；持久进程会保留，初始 pipefail 开启。需要复用状态时把命令放在同一个脚本，或使用明确的项目配置与文件。

```json
{"command":"read line; printf 'reply:%s' \"$line\"","mode":"async","keep_stdin_open":true}
```

```json
{"action":"write","execution_id":"<id>","text":"hello\n","close_stdin":true}
```

stdin 默认立即 EOF。只有临时 async keep_stdin_open=true 才能后续写入；持久命令用 heredoc/文件提供数据，不能往控制管道写输入；这是管道而非 PTY。部分写入返回 written_bytes/unwritten_bytes，重试剩余文本；关闭只在本次文本完整写入后生效。

### 日志与生命周期

```json
{"action":"read","execution_id":"<id>","stdout_offset":0,"stderr_offset":0,"max_bytes":16000}
```

read 每条流默认 16,000 字节，允许 128–64,000。使用返回的独立游标续读，保持 UTF-8 边界；sync 首次输出另受配置的总输出预算限制。完整日志落盘，返回结果只取有界片段。stdout_has_more/stderr_has_more 表明还有输出；不要重复执行命令来取日志。日志不自动轮转，长期服务应控制输出体积。

文件在 workspace `.recoding/runtime/executions/<owner_id>/<execution_id>/`，含 metadata.json、stdout.log、stderr.log。owner_id 使用稳定 Session 存档 ID，与当前逻辑进程 owner 无关。状态为 starting/queued/running/stopping/cancelling/unresponsive/succeeded/failed/cancelled/timed_out/interrupted；元数据附时间、退出码及可用的 CPU/RSS 观测。

```json
{"action":"cancel","execution_id":"<id>"}
```

临时执行取消先向进程组发 SIGTERM，约 0.5 秒未退出则强制回收可追踪子进程和进程组；取消不销毁整个沙箱，也不回滚文件或外部副作用。持久执行先软中断，SSH 取消关闭连接；详细差异见 Bash Pool。正常命令完成同样回收其普通后台子进程。服务应直接以前台命令使用 async；不要 nohup、setsid 或自行守护化。脱离进程组/进程树的程序不承诺被单次取消完整追踪，Session 沙箱整体停止仍回收全部。

Stop/Resume 保留记录和日志，不重放旧命令，不恢复 Shell 内存。移动 Session 存档不自动搬运 workspace runtime 日志；备份时一起保存对应 executions 数据，不纳入源码快照或 Git。

## 导航：repo_query

```json
{"mode":"search","path":"src","query":"build_config","globs":["*.py"],"limit":50}
```

| mode | 返回内容 |
| --- | --- |
| files | 路径列表 |
| search | 字面文本或 regex 匹配行、行号、文件版本 |
| outline | 文件中的类/函数等定义轮廓和起止行 |
| symbol | 按名称筛选定义候选 |

默认 max_files=2000（最高 20000），limit=100（最高 500），offset 分页；输出标明扫描边界、是否仍有候选和 backend。结构化结果过大时继续缩小本页并给出 response_limited 和准确 next_offset，不返回半截 JSON。大仓库优先缩小 path/globs，再有界分页，不把一页当全仓库结论。

文件枚举优先使用 ripgrep 的 ignore 规则；未安装时采用明确标记的 filesystem_defaults，不能承诺完整 `.gitignore` 语义。默认排除 `.git`、`.recoding`、依赖、构建和常见缓存目录。Python 用标准 AST；其他语言尝试 tree-sitter-language-pack，缺失/不可用时返回 text_heuristic 候选。Agent 可自主安装更合适的解析依赖。

定义缓存位于 `.recoding/runtime/repo-index/outline.sqlite3`，按实际源文件字节和解析器能力/版本失效；只在访问文件时更新，允许删除后重建。它不是 LSP，当前不承诺跨文件语义引用、类型解析或重命名重构；结果明确 semantic_references=false，改代码前仍应读原文和验证测试。

## 查看任务

Web 右栏 PROCESSES 为 Session 全局的当前资源投影，合并 Bash/Python，每个进程一行；标题按进程数显示 run 与 ready。run 包含运行、启动、排队、取消和未响应等仍需管理的非终态；ready 为仍保留内存/连接状态但无活动或排队工作的持久进程。历史执行、已关闭/失效进程不进入列表。默认整体折叠，列表紧凑、可滚动，底部手柄支持拖动或方向键调整高度；实时更新保留展开、输出游标和高度。运行行可读取当前执行日志；ready 不重复展示上次已完成输出。复用已有 Session SSE，无新增轮询。历史记录/日志仍可通过存档和执行工具读取，隐藏历史不删除数据。

Agent-loop 的 python/bash 卡标题显示 sync/async，默认 sync；长源码预览保留该参数，展开仍加载完整高亮源码。这个标记只描述调用者是否等待，与临时/持久进程寿命无关。

TUI 使用 `/executions` 查看当前查看 Agent（否则当前收件人，群发目标退回 Master）的列表，`/executions ID` 查看日志，面板可继续读取后续输出。也可显式给字节游标：`/executions ID STDOUT_OFFSET STDERR_OFFSET`。查看不会发模型请求、启动沙箱或停止任务。

Python Session 对外提供 `list_executions(agent_id="master")` 和 `execution_output(agent_id, execution_id, stdout_offset=0, stderr_offset=0, max_bytes=16000)`；SavedSession 也可只读查询已有数据。Execution 的执行、权限和日志属于 tools；Session 提供统一查询，MultiAgent 不管理执行实现。
