# 首次初始化：Coding Agent 执行指引

[文档目录](index.md)

理解项目先看 [阅读路线](understanding.md)。当前执行层依赖 Linux；默认 sandbox 还需要 namespace，显式 [Host 模式](execution-modes.md)不需要 Bubblewrap，但没有隔离。Windows 需要 Linux 环境，两者都不是原生 Windows 后端。

当用户让你配置 recoding... 时，请完成下面的准备、验证和交付。先阅读项目根目录 `AGENTS.md`；项目根目录指包含 `pyproject.toml`、`recoding-web.py`、`recoding-cli.py` 的目录，不一定等于包含 `.git` 的目录。按实际环境行动，不能只列出安装建议，就宣称配置已完成。

先检查 `.venv/`、`.env`、`sessions/` 和系统工具。Git checkout 并不代表一定是空白环境：已有的环境、用户配置和 Session 都应复用。本文中的安装与创建仅针对缺失部分；不覆盖已有 `.env`，不重建正常的虚拟环境，不清理用户任务目录，也不修改历史源码快照。

## 1. 系统环境和依赖

本项目需要 Linux、Python 3.10+、Bash；默认 sandbox 另需 Bubblewrap；本机 HTTPS 测试另需 OpenSSL。先检查：

```bash
python3 --version
command -v bash
command -v bwrap
command -v openssl
```

Debian/Ubuntu 缺少系统依赖时，安装相应包；其他发行版使用对应包管理器：

```bash
sudo apt install python3 python3-venv python3-pip bash bubblewrap ca-certificates openssl
```

已有安装权限或用户已授权时继续执行；确实缺少 Host 管理权限时，明确指出缺的是哪个系统包、哪一步需要用户处理，不要把仍未完成的步骤说成已经完成。安装 Bubblewrap 后也要验证 namespace 能实际使用，不能只检查可执行文件是否存在。

仅在 `.venv/` 不存在时创建，然后安装项目及 CLI/TUI 依赖：

```bash
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/python3 -m pip install -e '.[cli]'
```

依赖以 `pyproject.toml` 为准：Core/Web 使用标准库；`[cli]` 安装 prompt_toolkit、Rich 及其依赖。已有 `.venv/` 但解释器不可用时，先定位原因，不直接删除或重建。后续项目命令统一使用 `.venv/bin/python3`，不依赖 `activate`、系统 Python 的导入路径或全局快捷命令。

注意两种 Python 的分工：应用使用 `.venv/bin/python3`；沙箱工具使用 `/usr/bin/python3`，可读取其兼容的系统包/用户 site，并将任务需要的新包安装进 workspace `.recoding/runtime/python-packages/`（沙箱内 `/opt/recoding/workspace-packages`）。把 NumPy 等包安装进应用 `.venv` 并不等于工具解释器可用。任务依赖由 Agent 按需安装；GPU、CUDA 和机器学习框架不是启动项目的必需依赖。详见 [工具与沙箱](tools-and-sandbox.md)。

如果用户用 `python3 recoding-web.py ...` 遇到 `No module named 'recoding_web'`，先核对是否误用了系统解释器。项目包通过 editable 安装注册在 `.venv` 内，应改用下文的 `.venv/bin/python3` 启动命令；只有该环境确实缺少安装时才执行上面的 pip 命令。不要通过复制包或临时修改 `sys.path` 掩盖环境问题。

## 2. 创建工作目录

```bash
mkdir -p ./sessions ./sessions/tests
```

默认给用户的工作目录是 `./sessions`。测试创建的 Session 和工作文件一律放在 `sessions/tests/`，结束后先回收测试进程和沙箱，再清空专用测试目录；手动验证后执行 `.venv/bin/python3 tests/workspace_cleanup.py`。

各 Session 的 `.recoding/`、`images/`、`runs/` 等由程序按需创建，不要手工伪造存档或移动真实工作文件。`.venv/`、`.env`、`sessions/` 和运行时存档均已被 Git 忽略。

## 3. 配置真实模型 API

优先复用用户已有的 `.env` 或明确提供的环境变量；不要为了找 key 搜索 Git 历史或无关文件，也不要把密钥打印出来。完全没有凭据时，向用户索取 API key 和网关地址；未拿到真实凭据之前，可以完成安装和离线验证，但不能宣称真实模型连接已可用。

仅在 `.env` 不存在时，创建权限受限的本地模板：

```bash
umask 077
if [ ! -e .env ]; then
    cat > .env <<'ENV'
RECODING_API_KEY=
RECODING_BASE_URL=https://your-gateway.example/v1
ENV
fi
```

将用户提供的真实 key 写入本地 `RECODING_API_KEY`，将 `RECODING_BASE_URL` 改成用户实际的 Chat Completions 网关根地址。这里的示例地址不是可用凭据，也不是必须使用的服务。不要把 key 写进源码、文档、测试、报告或命令行参数；不要提交 `.env`。已有环境变量优先于 `.env`，排查时核对变量是否设置即可，不输出其内容。

新 Session 默认使用 catalog 的默认模型，可通过 RECODING_MODEL 或显式 --model 配置；合法模型 ID 不限于预置列表，实际工具/图片/usage 能力需按网关验证。旧 GEMINI key/base 名称仍可用，GEMINI_MODEL 不覆盖新建默认。未来更换 API 按 [模型/API 边界](model-api.md) 修改。

## 4. 做一个最小验证

先验证两个入口与依赖能够导入，不启动模型：

```bash
.venv/bin/python3 recoding-web.py --help
.venv/bin/python3 recoding-cli.py --help
```

需要验证工具沙箱时，执行一次有明确退出条件的离线 Demo：

```bash
.venv/bin/python3 recoding-cli.py run --demo --no-network --no-gpu \
  -C ./sessions/tests/first-run-smoke -p '检查当前 workspace' \
  --max-messages 1 --timeout 30 --format jsonl
```

这会创建临时测试 Session，用固定响应驱动真实 Python 沙箱，再由命令行驱动显式停止 endless loop。检查消息中的实际工具结果；安装了 bwrap 却没有 namespace 权限时，应说明具体失败原因，不自动改成 Host 执行。只有用户明确选择 `--execution-mode host` 才使用无隔离后端，且不能携带断网/GPU 隔离参数。Demo 不读取真实 API key、不访问真实模型，也不能证明模型网关已经连通。

不要例行跑全量测试。代码修改只做相关定向检查；正式测试必须使用本机模型替身，不读取真实 `.env`。若当前任务已授权真实 API 连通验证，使用最小合成消息检查，不重放用户 Session/图片，已有授权无需重复确认。若用户让你实际启动服务，启动后核对真实监听地址和 PID；没启动就只提供命令，不能说服务已运行。

## 5. 给用户直接可运行的单行命令

在项目根目录启动 Web：

```bash
.venv/bin/python3 recoding-web.py --env-file .env -C ./sessions --port 8000
```

在项目根目录启动 CLI 的全屏 TUI：

```bash
.venv/bin/python3 recoding-cli.py chat --env-file .env -C ./sessions
```

用户可以任选界面。Web 默认替换同端口的旧 recoding 服务；有既有运行环境时遵循 `AGENTS.md` 的所有权和替换规则。同一个 Session 只允许一个外壳持有写入锁。远端浏览器需要按用户的 SSH 转发/代理方式访问服务，不把浏览器本机目录当作服务器工作目录。

TUI 默认展示人机和 Agent 间全部消息，输入框固定下置，`/help` 查看操作。`/resume` 依次列出当前目录 Session、上一级和直属子目录，选择目录继续导航，选择 Session 才恢复；列表不递归、不使用 Ctrl+R，边界不循环。`/wake` 唤醒当前 Master，`/stop` 停止当前 Session 全部 Agent，`/speed` 查看输出速度曲线，Ctrl+D、`/quit` 或 `/exit` 正常退出；Esc 和功能键不会停止 Agent。

交付时简要说明：已安装/复用的依赖、API 配置是否齐全（不展示 key）、创建的目录、实际通过的最小检查、尚缺的条件，以及上述两条启动命令。保留现有用户数据，清理专用测试数据，不把未完成项隐藏在“配置成功”之下。

后续维护项目、依赖、正式工具和 Session context，见 [Host Coding Agent 维护指南](host-maintenance.md)。

有状态 SSH 是可选能力：工具沙箱需可见 OpenSSH client，远端需 /bin/bash，凭据和 known_hosts 由用户明确提供。Recoding 不自动挂载 Host SSH 私钥。仅开发时的真实 loopback SSH 回归另需 OpenSSH server，详见 [Bash Pool](bash-pool.md)。
