# Session Host 协议

[文档目录](index.md)

`recoding-web.py` 和 `recoding-cli.py` 启动的是 Session Host。Host 是拥有 Session 运行资源的进程；HTTP server 只是 GUI Host 的界面传输层，不是独立 Session 服务。两个入口继续保留，内部按职责命名：

| 对象 | 职责 |
| --- | --- |
| `GuiSessionHost` | 持有多个运行 Session；只接受一个 GUI 页面的连接 |
| `GuiHttpServer` | 为这个 Host 提供 HTTP、静态页面和 SSE；仍支持页面内部的并发请求 |
| `CliSessionHost` | 持有一个运行 Session，提供同用户的私有 Unix socket 控制通道 |
| `SessionCatalog` | 发现存档、读取导航信息；不创建运行资源 |
| `SavedSession` | 只读历史投影；浏览不等于 host |
| `SessionHostProtocol` | 两个 Host 共用的运行独占与浏览互斥协议 |

## Session 只有 stop / run

- **run**：某个 Host 进程持有这个 Session，其他 Host 不能恢复或浏览它。
- **stop**：没有 Host 持有，可以同时从多个 GUI Host 只读浏览。

这与 Agent 的 starting/running/sleeping/stopping/stopped/failed 阶段不同。所有 Agent 休眠、停止或故障，都不自动释放 Host 所有权；须明确 Stop/关闭 Session 或退出 Host。压缩维护期间也仍持有独占。

## 创建、Resume 与关闭

**CLI** 默认先显示未创建 Session 的空白输入界面，首条真实消息才创建；帮助、草稿和退出不创建存档。显式创建或 Resume 总会取得独占并唤醒 Master，没有只读打开存档的操作。一个 CLI 同时只打开一个 Session；`/resume` 切换时先停止、回收旧 Session 并释放独占，再建立新 Session。`/stop` 关闭当前 Session，返回未打开 Session 的界面，可再次 `/resume`。Ctrl+D、`/quit`、`/exit` 关闭当前 Session；连接后台 CLI Host 的 `attach` 界面退出也会关闭该 Session。显式 `--detach` 才让后台 Host 在启动命令返回后继续持有它。

**Web** 点击存档、刷新、消息定位默认只浏览，停止项不会创建 worker/沙箱/Model call。恢复提醒覆盖输入区，Resume 前禁止输入；已有草稿保留。点击 **Resume Master**，或新建 Session，才取得独占并唤醒 Master。一个 GUI Host 可以同时持有多个 Session；切换当前显示对象不停止其他 Session。顶部 Stop All 关闭当前 Session，停止所有 Agent、等待资源回收并释放独占；终端结束 Web 进程时关闭它持有的全部 Session。页面不提供退出应用按钮。Resume 只启动 Master，由它决定是否恢复 sub。

只读 Web 不发消息、不上传、不改名、不置顶、不删除存档。模型、并发上限和默认 sleep 的调整只暂存在页面内存；真正 Resume 成功后才保存，并以系统消息通知 Master。失败或抢锁失败不能保存设置。停止的单个 Agent 仍可在**已被本 Host 持有的运行 Session** 内接受用户留言；这不等于给只读存档留言。

## 运行排斥浏览

任何 Host 取得独占之后，其他 Host 的历史、消息、图片、工具详情等接口均拒绝读取。列表仍可显示轻量名称、位置和占用标记。

已经进行中的 Web 浏览会在下一次目录同步检查时关闭视图，清空当前历史展示与缓存；通常约 2 秒。后端的每次历史/图片读取都直接检查锁，不依赖浏览器及时收到通知。已经传到浏览器的内容无法从用户的屏幕截图、下载或内存中远程撤回。

所有现在和未来的 Host 均须使用 `core/session/hosting.py` 中的公共协议：运行用 `SessionHostProtocol.resume()`，浏览用 `browse()`，原始存档读取用 `read()` 的短期共享租约，列表可用 `available()` 探测。先取得运行独占，再创建资源；关闭资源后才释放独占。HTTP 读取持有短期共享租约，保证同一次读取不会跨越所有权切换。

## 进程死亡不会留下永久占用

`host.lock` 使用操作系统 `flock`，锁绑定打开的文件描述符，不以文件存在或 PID 记录判断占用。进程正常退出、异常崩溃或被 SIGKILL 后，内核关闭描述符并释放锁；遗留空锁文件无须清理，**不要删除锁文件解锁**。

Host 描述符设置 `CLOEXEC`，不传给 worker/沙箱子进程。已有 `owner.lock` 单独保护底层 Agent 的实际运行资源，并在必要时传给 worker；Host 死亡后 worker 通过控制通道 EOF 回收沙箱，资源锁随后释放。在这一短暂回收期，下一次启动可能提示资源仍在释放，稍后重试即可。

不要移动、删除或复制正在持锁的存档后让另一个 Host 使用其副本；协议针对同一存档文件系统位置的锁文件，不提供跨机器复制品的一致性协调。共享文件系统须自身正确支持 `flock`。

## 一个 GUI Host 连接一个页面

首次 GUI API 请求登记页面身份；同页刷新和短暂断线可以重连，另一个页面收到 `409 gui_busy` 并显示提示。页面 ID 保存在该页 `sessionStorage`，每次文档加载另外生成独立 document ID，只用于 GUI 接入，不是 Session 所有者。API、图片和 SSE 都携带接入身份。

正常关闭或离开页面时，`beforeunload`/`pagehide` 共用一次 `sendBeacon` 向 `/api/gui/release` 发送释放请求，失败时尝试 keepalive fetch；收到后立即允许新页面接入，无需等 30 秒。只释放匹配当前 page/document 的 GUI 登记并关闭其 SSE：刷新时旧文档迟到的释放，或被接管页面的关闭请求，不会踢掉新页面。释放请求受同源、CSRF 和服务实例校验，不停止 Agent、不释放 Session 的 Host 锁。

浏览器崩溃、强杀、断网或未能发送关页通知时，无法保证立即检测；保留约 30 秒无请求的兜底过期机制。仅切到后台仍暂停轮询和 SSE，不立即主动释放登记。BFCache 返回时重新加载页面以建立新文档身份。

占用提示框提供 **接管此页面**：显式向 `/api/gui/takeover` 发送同源、CSRF 和实例校验的 POST，请求方立即成为当前 GUI；旧 SSE 主动关闭，旧文档的后续 API/轮询被拒绝，即使之后超过超时也不能靠轮询自动抢回来。旧页面会显示被接管提示，需要用户明确接管或重新连接。普通重试不会强制踢人。

页面断开、释放、超时或接管 **都不会停止已持有的 Session**，也不会重启模型调用或恢复新的 Agent。在终端结束进程才退出整个 Host。服务器在 `.recoding/server-logs/` 中追加 `GUI_RELEASE`、`GUI_TAKEOVER` 元数据日志，不记录令牌或消息正文。

页面内部仍可以并行获取资源、发送请求与接收 SSE。运行视图最多一条 SSE，sub 共用订阅；切页/重连主动取消旧流。只读视图没有 SSE。日志 `streams` 是当前 Host 的 SSE 总数，正常应为 0 或 1；`page subscription replaced` 表示旧流被替换，不是模型错误。

## Python API 的边界

直接调用 `Session.create/resume()`、`Session(...)` 或 `SavedSession(...)` 的测试与实验不属于 Host，默认不取得/检查 `host.lock`，后果由调用方承担。底层运行资源锁仍保留。它们不应被宣传为遵守 Host 协议的应用；未来正式外壳必须显式接入 `SessionHostProtocol`，不能以 Python API 的实验豁免绕过协议。
