# 源码快照

[文档目录](index.md)

新建源码快照使用项目根目录 `snapshots/recoding-YYYYMMDD-HHMMSS/`，目录时间使用 Asia/Shanghai。每个快照都是一份可独立运行的源码副本，包含 Web/CLI 入口、完整 `core/`、`apps/` 和 `tools/`、本地渲染资源、打包配置、文档、维护脚本和正式测试，不依赖父目录中的源码。

不复制 `sessions/`、任何 Session 存档、上传图片、已安装包、`.env`、Git/编辑器/本地工具配置、缓存、构建产物、`archives/` 或其他 `snapshots/`。快照不包含 API key 或当前 Python/Bash 内存状态。`snapshots/` 中的源码、文档和文件清单纳入 Git，但不进入 Python 发行包。快照只收录当前源码范围，不建立 CLI 备份或 archives。

根目录与 `snapshots/.gitignore` 共同排除快照运行时产生的 `.env`、Session、包、日志、缓存、构建产物、本地工具配置及嵌套快照。自行放入快照的 `.env` 继续被忽略，运行工作目录建议指定在快照之外。添加快照使用普通 `git add snapshots`，不要用 `-f` 绕过忽略规则；历史快照内的文档与忽略文件保留创建时版本，当前 Git 管理规则以本节为准。

每份快照附带：

- `SNAPSHOT.md`：源码范围、启动方式和可复现验证步骤；不写创建者的路径、环境状态或实测结果。
- `snapshot-manifest.json`：源码内相对文件路径、大小和 SHA-256；清单自身不计算在文件哈希中。

## 在快照目录启动

需要与主项目相同的 Linux、Python 3.10+、Bubblewrap 和 Bash。新布局的快照需独立安装包映射，Web 渲染库已在源码中；交互 TUI 需要 prompt_toolkit 和 Rich。历史快照按其自身文档启动，不改写旧字节。

```bash
cd /path/to/snapshots/recoding-YYYYMMDD-HHMMSS
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/python3 -m pip install -e '.[cli]'
.venv/bin/python3 recoding-web.py --env-file /absolute/path/to/.env -C /absolute/path/to/workspace --port 8001
```

环境文件必须由运行者单独提供，也可以使用已导出的 `GEMINI_*` 环境变量。移动快照后仍可按相同方式运行，不依赖原项目的 `.env` 或源码。使用独立端口可以同时保留原服务；同端口启动遵循正常的旧服务替换规则。

只检查是否能启动时可使用离线模式和临时工作目录，不需要 API key：

```bash
.venv/bin/python3 recoding-web.py --demo --no-network --no-gpu -C /absolute/project/sessions/tests/snapshot-check --port 0
```

读取启动输出中的 URL。正式创建快照时仅做源码一致性及独立启动检查：导入来自快照目录，Web 配置、页面和本地资源可访问，临时服务关闭后自动清理专用测试工作区/Session；不执行完整测试集，不调用外部模型或重启现有服务。每次实际验证应在 `sessions/tests/` 下使用独占目录。

快照是源码备份，不替代工作数据备份。需要恢复任务历史和产物时，另外备份原 workspace 和 `.recoding/`，范围见 [存档与观测数据](session-storage.md)。

已有旧品牌快照保持创建时的目录名、源码与启动入口，不随当前项目更新改写；恢复这类历史快照时，以其内部 `SNAPSHOT.md` 的命令为准。
