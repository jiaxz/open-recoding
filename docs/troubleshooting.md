# 常见问题

[文档目录](index.md)

先判断问题属于初始化、模型传输、工具、Host 所有权还是界面投影，再检查对应日志。模型阶段见 [API 诊断](api-performance.md)，服务日志见 [配置](configuration.md)。

| 现象 | 检查与处理 |
| --- | --- |
| `No module named recoding_web` 等 | 使用项目 `.venv/bin/python3`，确认已经 editable 安装；按 [初始化](first-run.md) 检查，不用 sys.path 转发掩盖环境问题。 |
| 端口被占用或仍是旧界面 | 重新执行启动命令默认替换同端口旧 Recoding；其他程序不终止。可用 `--port 0` / `--no-replace`，核对实例 ID，刷新页面。 |
| Host 已连接其他页面 | 同一个 Web Host 只接入一个 GUI。正常关页会释放；异常失联约 30 秒兜底，或在新页显式接管，不停止后台 Session。 |
| Session busy / 无法浏览 | 被其他 Web/CLI Host 持有的 Session 排斥浏览。先关闭原 Host 所持有对象；不要删除 host.lock，内核会随进程死亡释放。 |
| 启动时提示旧 Session 路径不存在 | 项目移动、存档删除或格式升级后，浏览器上次选择可能失效。启动先按当前列表中的 ID 定位；路径不存在/格式不兼容时仅清除浏览器选择，保留目录同步结果并提示重新选择，不创建旧目录或改写存档。权限、连接和真正扫描失败仍显示错误。重启当前源码的 Web 服务并刷新页面。 |
| 打开存档后没运行 / 不能发送 | Web 默认只读浏览，提醒覆盖输入区；点 Resume Master 后恢复输入。只读设置不持久保存；运行 Session 内才可给停止 Agent 留言。 |
| sub 留言未回复 | 用户消息持久未读但不自动恢复，Master 需 resume sub；停止项拒收其他 Agent 的新消息。 |
| Agent 输出后继续 sleep | endless loop 的正常行为。普通正文不结束任务；人机报告需 send_user_message，Stop 才回收资源。 |
| 只有 SSE 心跳或 TLS EOF / IncompleteRead | 心跳不是模型输出，检查首输出与错误分类。半截响应不执行工具；可恢复错误在同调用内重试，耗尽后停止并保留已提交记录。 |
| HTTP 503 / credential_unavailable | 保存 request ID 和阶段，检查网关/上游状态；客户端最多追加 3 次重试，不保证上游稳定。 |
| invalid_client_tool_schema / invalid-argument | 检查目标工具根为普通 object、必需参数和收件者互斥。HTTP 200 中的 SSE 参数错误也不重试；不要反复发同一无效请求。 |
| 没有看到必要报告 | Agent 需要显式 send_user_message；普通模型输出和 peer 对话属于可隐藏过程。 |
| 包已装但 Python 仍找不到 | 应用 .venv 与工具 /usr/bin/python3 不同；核对用户 site 或 workspace 包目录，按需重启对应私有/Pool 进程。 |
| Python Pool 旧变量消失 | 查看 generation/status 和系统通知。Stop/重启/进程故障会失去对象；Master 显式 restart 后初始化，不自动重放旧代码。 |
| 沙箱 / GPU 不可用 | 检查 Bubblewrap namespace、Bash、设备/驱动权限、启动禁用参数与空闲显存；不降级到 Host 无沙箱执行。 |
| 网页有图片，模型却看不到 | 展示不等于视觉输入；让模型 view_image，或用户粘贴图片。服务器资产传输与模型输入是两条通道。 |
| Context 提醒看起来过早 | 查看 API 实测锚点、校准状态、估算图片/工具预算。可在右栏取消这次后台压缩，Agent 继续使用原上下文；规则见 [Context](context.md)。 |
| 更新后旧存档不显示 | 只展示兼容的 v5。其他格式文件保留，使用兼容源码，见 [升级策略](migration.md)。 |
| streams 随切换累加 | 同页应为 0/1，sub 共用；检查页面/服务器实例及替换、断开日志，不将 Agent 数当连接数。 |
| 合计速度不是 Agent 平均 | 设计为并发 token 相加的 10 秒估计，逐调用速率和缺失用量边界见 [观测](observability.md)。 |

涉及实际用户任务时保留已提交状态与诊断信息。正式回归使用 `sessions/tests/` 和本机替身，不拿真实 .env/Session 当默认测试夹具。
