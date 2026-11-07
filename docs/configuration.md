# 配置与启动

[文档目录](index.md)

首次安装和最小验证按 [初始化指南](first-run.md)。本页作为启动参数、API 配置和运行服务的参考；界面操作见 [Web](guide.md) / [CLI](cli.md)。

## 推荐启动命令

在项目根目录使用已有 `.venv/`，默认给用户的目录是 `./sessions`：

```bash
.venv/bin/python3 recoding-web.py --env-file .env -C ./sessions --port 8000
.venv/bin/python3 recoding-cli.py chat --env-file .env -C ./sessions
```

Web 浏览器地址为 [http://127.0.0.1:8000](http://127.0.0.1:8000)。`-C` 指定服务器的 workspace 与 Session 搜索根，省略时使用启动当前目录；它不是“为每个 Session 新建一个直接子目录”的开关。环境文件相对路径仍以命令的工作目录为准。

Web 也接受一个目录位置参数，与 `-C` 二选一。CLI Shell、后台运行和参数组合见 [CLI](cli.md)。远程浏览器通过用户配置的 SSH 转发或代理访问；文件和工具运行在服务器，不是浏览器本机。

## API 配置

`.env` 读取以下模型配置，保留已有文件和配置，真实 Key 不提交 Git：

```dotenv
RECODING_API_KEY=your-api-key
RECODING_BASE_URL=https://your-gateway.example/v1
# 可选：RECODING_MODEL=your-model-id
```

示例地址需要替换为用户的兼容 Chat Completions 网关。配置优先使用 `RECODING_API_KEY`、`RECODING_BASE_URL`；旧 `GEMINI_` 名称仍可用。可选 `RECODING_MODEL` 设置新 Session 默认，未设时为 catalog 默认；`GEMINI_MODEL` 仅作旧的选择提示。模型 ID 不限于预置清单，实际能力与可用性取决于网关。详见 [模型/API 边界](model-api.md)。

环境文件支持注释、单双引号和可选 `export` 前缀，不执行 shell 或变量展开；只加载这些模型配置名称及旧的 GEMINI 别名。已有环境变量优先，包括显式空值。`--env-file` 选择文件；源码 editable 安装默认找项目根 `.env`，普通 wheel 默认找启动目录。

新建用显式模型参数或界面选择；运行中通过 Session Model、TUI `/model` 或配置接口修改。整个 Session 下一次 Model call 使用新选择，不打断当前响应或清空工具内存。跨模型历史转换见 [Context](context.md)。

## 运行参数

| Web 参数 | 默认 | 含义 |
| --- | --- | --- |
| `-C` / `--directory` | 当前目录 | 初始 workspace 和递归 Session 搜索根 |
| `--env-file` | 默认 `.env` | 模型配置文件，相对路径以命令工作目录为准 |
| `--port` | `8000` | 监听 `127.0.0.1`；`0` 自动选空闲端口 |
| `--no-replace` | 关闭 | 同端口有旧服务时直接失败，保留旧进程 |
| `--tool-timeout` | `30` | 工具执行超时秒数 |
| `--api-timeout` | `120` | HTTP socket 与无模型输出超时；心跳不是模型输出 |
| `--no-network` | 关闭 | 禁止沙箱工具联网，宿主模型请求仍可联网 |
| `--no-gpu` | 关闭 | 不向工具沙箱接入 GPU |
| `--demo` | 关闭 | 固定响应离线演示，不读取模型凭据 |

完整参数以 `.venv/bin/python3 recoding-web.py --help` 为准；CLI 独立参数见 [CLI](cli.md)。

Session 默认允许工具联网/GPU，sub 并发 16（不含 Master）。设备仍需存在且当前用户有权限；启动限制优先。创建窗口只填写名称、模型和 workspace。运行后在 AGENTS 的折叠“设置”面板修改并发 0–99 和兜底 sleep 0.1–3600 秒（默认 10）；停止存档的改动仅暂存，Resume 成功才提交。

降低并发不终止已有 Agent，仅限制后续创建/恢复；回收完成才释放名额。配置保存并经可信系统消息通知 Master，不重写 System。已经执行的 sleep 不被改长/改短，但通知可唤醒它。模型主动 sleep 可选 0–3600 秒，不受兜底值限制。

对应配置 API 为 `POST /api/sessions/<id>/agent-limit`（`{"max_subagents":8}`）和 `/idle-sleep`（`{"idle_sleep_seconds":10}`）。参数类型和范围由后端校验；见 [Host](session-ownership.md) 和 [多 Agent](sub-agents.md)。

## 服务生命周期与 GUI

执行 Web 启动命令默认替换同端口、同用户的旧 Recoding 服务：先正常关闭 Session 与沙箱，再绑定端口，必要时逐级回收旧进程。其他程序不被终止。替换清空运行内存，已提交历史与工作文件保留；`--no-replace` 可保留旧服务，`--port 0` 总是选空闲端口。

一个 GUI Host 可持有多个 Session，但只接入一个页面。第二页收到 `gui_busy`，可显式接管；同页刷新重连，正常关页通过 beacon 释放 GUI，异常失联约 30 秒兜底。GUI 断开或接管不停止后台 Session。终端 Ctrl+C 退出 Host 并回收其全部 Session，页面不提供退出应用按钮。详细锁与身份规则见 [Host 协议](session-ownership.md)。

代码更新后重新启动 Host，再刷新网页。每个实例固定前端资源并公开 instance/PID/frontend revision；worker 启动另校验源码版本，不混用旧协调器和新执行代码。

## 离线演示

```bash
.venv/bin/python3 recoding-web.py --demo --no-network --no-gpu -C ./sessions/tests/web-demo --port 0
```

页面标记 OFFLINE DEMO，固定响应可驱动真实沙箱，但不代表真实模型规划、网关连通或 API 用量。测试数据按 [开发约定](development.md) 在验证后自动清理。

## 监听与日志

终端打印 LISTENING、PAGE LOAD、WEB CONNECT/RECONNECT/SWITCH、DISCONNECT 和停止状态，时间使用 Asia/Shanghai。`streams` 是实际 SSE 连接数，正常 0/1，不是 Agent 或已打开 Session 数；同页切换替换旧订阅，sub 共用。关闭/隐藏页释放连接，心跳检测可能略有延迟。

详细脱敏日志异步写到启动 workspace 的 `.recoding/server-logs/server_<时间>_<PID>_<实例>.jsonl`，每次启动独立文件，权限 600。日志不属于单个 Session，删除 Session 不删除它；有界队列写盘，拥挤时记录丢弃数量，不阻塞 HTTP。

HTTP 事件以请求编号关联读取头部、准备响应、压缩、发送和总耗时；响应给出 `X-recoding-Request` / `Server-Timing`。`SESSION_OPERATION` 记录锁、扫描和加载阶段；`BROWSER_TIMING` 仅回传经过筛选的数值耗时。同源资源路径不含查询参数，不记录正文、名称、图片、提示或 Key。

浏览器 queue/connect、TTFB、服务端准备和传输耗时可能重叠，不能直接相加；相邻 WEB/PAGE 日志的时间差也不是某个请求耗时。指标解释见 [运行观测](observability.md)，模型链路诊断见 [API 性能](api-performance.md)。

## 包与缓存位置

包按 workspace 共享；home 缓存默认 `~/.recoding/cache`，可通过 Host 环境变量 RECODING_HOME 指定 Recoding 用户目录。模型 `.env` 仅加载模型配置，不加载此路径。home 自身仍可作为 workspace 创建 Session。正式工具更新需在已有环境重新 editable 安装并重启 Host。详见 [包环境](packages.md)、[Non-builtin](non-builtin-tools.md) 与 [Host 维护](host-maintenance.md)。

## 工具执行模式

默认 `sandbox`，显式 `--execution-mode host` 让新 Session 的工具直接运行于宿主。Web 新建/停止后 Resume 可选择；CLI 恢复可显式覆盖并保存。Host 不兼容 `--no-network`、`--no-gpu`；不自动降级，运行中不切换，详见 [执行模式](execution-modes.md)。
