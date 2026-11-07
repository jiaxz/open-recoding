# 包、缓存与工具环境

[文档目录](index.md)

一个 workspace 是一个项目。项目代码、构建产物和辅助依赖由所有 Session 共同使用；各 Session 保持独立的对话、日志与 Agent 内存。缓存在本机同一用户的 home 共享。

## 四层目录

```text
~/.recoding/
  cache/                         所有 workspace 的公共缓存
    pip/ uv/ npm/ cargo/ go/ huggingface/ torch/ ccache/ sccache/
  runtime/python-packages/       home 自身作为 workspace 时的依赖
  registry.json
  <session>/                    home 自身的 Session 存档

project/
  src/ scripts/ tests/ ...       项目自行组织代码与工具
  .recoding/
    inputs/                      共享上传图片副本
    runtime/
      home/                      工具 HOME 与配置/数据
      python.json                系统工具 Python 的版本、cache tag、平台
      packages.lock              托管安装与迁移使用的进程文件锁
      python-packages/            所有本项目 Session/Agent/Python Pool 共用
    registry.json
    <session>/                  对话、图片、消息、日志；不再拥有私有包
```

home 下的 `.recoding` 也是正常 workspace 容器；只有其中 `cache/` 承担本机共享职责。导航跳过 cache/runtime/server-logs，不遍历依赖或缓存查找 Session。`RECODING_HOME` 可显式覆盖默认的 `~/.recoding`，主要用于测试和特殊部署；它是 Host 环境变量，不从模型 `.env` 文件读取。推荐启动命令不需要设置它。

## 三个默认包来源

应用使用项目 `.venv/bin/python3`；工具在两种模式下默认使用系统 `/usr/bin/python3`。[Host 模式](execution-modes.md)直接使用真实 workspace 包目录、home 缓存和用户 site；下面的挂载路径与只读保护专指 sandbox。用户 site 的版本目录由目标工具解释器探测，下表用 `pythonX.Y` 表示版本占位：

| 来源 | Host 路径 | 沙箱路径 | 权限 |
| --- | --- | --- | --- |
| workspace 辅助依赖 | `<workspace>/.recoding/runtime/python-packages` | `/opt/recoding/workspace-packages` | 读写，优先 |
| 兼容的 Host 用户 site | 通常 `~/.local/lib/pythonX.Y/site-packages` | `/opt/recoding/host-user/lib/pythonX.Y/site-packages` | 只读，第二优先 |
| 系统标准库及第三方包 | 系统 Python 自身目录 | `/usr/...` | 只读 |

包目录是 pip target，不是项目 venv，不处理其中的可执行 `.pth`。Host 用户 site 的钩子在所选后端的工具进程内按原规则处理，Host 模式下也会在宿主子进程执行；探针不运行钩子。应用 `.venv` 不自动作为工具解释器环境；项目自己的 venv、node_modules 和锁文件仍按项目规范使用。

Recoding 自身只在 workspace 根部创建 `.recoding` 隐藏目录。工具 HOME 设为 `.recoding/runtime/home` 的沙箱映射 `/opt/recoding/workspace-runtime/home`，XDG 配置、数据与状态在其下，防止第三方工具默认把隐藏目录放到项目根部。Agent 另须把工具专用环境和新缓存目录放进容器，保留项目已有配置。

辅助工具环境导出 `PIP_TARGET`、`PYTHONPATH` 和对应 bin PATH；持久 Python 显式添加同一路径。已导入包留在解释器缓存中，升级后按需关闭旧进程并创建新进程。Stop/Resume/删除 Session 都不删除 workspace 包。

工具 `PATH` 依次包含 workspace 包的 `bin`、`/usr/bin`、`/bin` 和 `/usr/local/bin`。因此 Host 安装到 `/usr/local/bin` 的公共命令也可直接调用，同时系统命令仍优先于本地全局同名命令；工具 Python 固定使用 `/usr/bin/python3`。这些系统目录在沙箱内保持只读，安装仍由 Host 完成。

## home 缓存

`~/.recoding/cache` 读写挂到 `/opt/recoding/cache`，设置 XDG_CACHE_HOME、PIP_CACHE_DIR、UV_CACHE_DIR、npm_config_cache、CARGO_HOME、GOMODCACHE、GOCACHE、HF_HOME、TORCH_HOME、CCACHE_DIR、SCCACHE_DIR。只暴露 Recoding 专用缓存，不暴露整个 home 或已有私有包管理器配置。

缓存是下载/编译复用材料，不是 Python 的导入目录。项目源码、自由构建的脚本、报告、模型最终成果和 build/dist/target 仍可按项目习惯放在 workspace；不强制放入 Session。普通 Python/Bash 可以自行选择路径，不保证任意命令都遵守缓存变量。

清理缓存只操作 cache 子目录，不能删除整个 home `.recoding`，因为其中可能有 Session。使用中的缓存由各管理器处理并发；缓存不是可信的永久发布存档。

## 安装依赖

Session 默认提供正式 non-builtin 工具 `install_packages`，与 Python/Bash 使用相同 Tool call 协议：

```json
{"packages":["requests"],"upgrade":false}
```

它在现有沙箱中执行 pip，使用 workspace 文件锁串行安装，强制写 workspace 包目录并复用 home 缓存。它不 sudo、不写 Host 用户 site，也不修改应用 `.venv`。`no_index`、`no_deps` 支持离线 wheel 验证。等待锁包含在工具超时内。

手动 Bash `python3 -m pip install --upgrade PACKAGE` 仍可用，但不自动获取这个锁，应与其他 Session 协调。`inspect_python_environment` 只读列出工具解释器、包来源、发行版元数据与缓存位置，不 import 第三方包，不等价于逐包导入验证。

项目安装或测试使用项目自己的环境。可对单次子进程使用 `env -u PYTHONPATH -u PIP_TARGET -u PYTHONUSERBASE .venv/bin/python -m pytest`，避免辅助包掩盖项目依赖缺失，覆盖仅作用于这次命令。此处 `.venv` 是目标项目环境。

## 本机清单与旧目录迁移

```bash
.venv/bin/python3 maintenance/recoding-packages.py --json
.venv/bin/python3 maintenance/recoding-packages.py --root ./sessions --exclude ./sessions/tests --json
.venv/bin/python3 maintenance/recoding-packages.py --root /data/project-a --root /data/project-b --requirements
.venv/bin/python3 maintenance/recoding-packages.py --root ./sessions --exclude ./sessions/tests --migrate --json
```

默认搜索当前用户 home；多个 root 覆盖其他磁盘。`--exclude` 可重复。纯扫描不读取 context/history/events/图片，不 import 包，不启动 Agent、不获取 Host 所有权、不修改存档。按 workspace 去重统计包；Session 条目仅关联 workspace。缓存、运行目录、Git/venv 等不递归扫描。软链接、错误或目录数量上限会明确报告；不完整时退出码 1，不生成安装候选。

JSON schema 为 `recoding.package-inventory` v2，提供 workspaces、sessions、packages、legacy_workspaces、legacy_session_packages 和 errors，另有 system_packages、host_missing_packages、legacy_host_comparison 与 package_collection。`candidate_requirements` 仅包含工具 Python 的系统与用户 site 均缺少、跨 workspace 版本一致、解释器匹配且没有 direct reference 标记的候选；仍需维护者检查来源和依赖，不是自动安装许可。

旧 workspace `.python-packages` 与上一版 `<session>/runtime/python-packages` 不再默认注入。显式 `--migrate` 持有 workspace 包锁和所有相关 Session 的资源/Host 锁，在临时目录完成合并，再发布新目录。正在运行、版本冲突、同路径内容冲突、异常元数据、软链接或已知解释器不兼容都会阻止该 workspace 迁移；其他 workspace 可继续。原目录全部保留，已有目标发生替换时也保留备份。重复合并相同文件不重复复制。

迁移不 import/安装包，未知来源或二进制依赖仍需事后核验。同目录可能残留多个版本的 dist-info，即使 Python 看似能 import，也不能直接删除元数据后假称依赖一致；先检查实际文件，再决定重装哪个版本。

更多操作见 [Host Coding Agent 维护指南](host-maintenance.md)。

扫描新发现的可复用包，除按用户要求推广到 Host，还要更新项目工具完整度集合；普通传递依赖与专业可选包不自动成为基线。维护流程见 [包维护闭环](host-maintenance.md#package-维护的闭环原则)。

各包用途、优先级、替代关系与配套依赖分类见 [包用途与维护优先级](package-priorities.md)。
