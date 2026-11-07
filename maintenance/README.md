# Host Coding Agent 维护工具

这里放由 Codex、WorkBuddy 等 Host Coding Agent 按用户要求执行的项目维护脚本。入口和参数解析集中在本目录，复用 Core 的业务能力，不依赖 Web/CLI 外壳，常规工具不启动 Session Host、界面或模型调用；`recoding-context.py --live` 与 `benchmarks/` 中的人工实测入口是经用户明确授权后启用真实模型/网络的例外。

它们与 `tools/` 中的正式 non-builtin 工具用途不同：`tools/` 的工具由运行中的 Agent 在沙箱内调用；本目录的脚本由外部维护者在 Host 执行。普通用户的 Web/CLI 启动入口仍在项目根目录。

## 包盘点

从项目根目录使用已有虚拟环境：

```bash
.venv/bin/python3 maintenance/recoding-packages.py --help
.venv/bin/python3 maintenance/recoding-packages.py --root ./sessions --exclude ./sessions/tests --json
```

省略 `--root` 会扫描当前用户 home；可重复 `--root` / `--exclude`。扫描只读，比较工具 Python 的系统/用户 site，记录当前及旧目录中的包、版本差异和项目集合的待审核项。不读取对话、不自动安装或升级。

`--requirements` 输出当前共享目录中经过机械筛选的安装候选，仍需维护者评估。只有显式 `--migrate` 才尝试在相关 Host 已释放的条件下迁移旧目录，保留原文件并拒绝冲突。

包收录、安装和最小验证的原则见 [Host 维护指南](../docs/host-maintenance.md)，目录与扫描格式见 [包环境](../docs/packages.md)。测试入口为 `tests/test_package_inventory.py`，普通改动只选择相关用例，不例行全量扫描或测试。

各包用途、优先级、替代关系与配套依赖分类见 [包用途与维护优先级](../docs/package-priorities.md)。

## 人工并行实测

`benchmarks/gold_news_parallel.py` 是显式的真实模型/联网实验，不由单元测试发现，也不连接现有 Host 或读取既有 Session。使用 `--live` 才加载项目模型配置并运行；参数和方法见 [实验说明](benchmarks/README.md)。原始运行记录只写入 Git 忽略的本地维护报告，不将本机结果写进通用文档。

## Context 压缩与检查点恢复

`recoding-context.py` 通过 Core 的同一摘要驱动维护已停止、未被 Host 持有的存档。默认目标 Master；`--live` 必须显式提供，`--probe` 可验证压缩后的真实请求而不执行工具：

```bash
.venv/bin/python3 maintenance/recoding-context.py -s /path/to/session \
  --env-file .env --live --probe --report-dir .recoding/maintenance/context-recovery
```

`--recover-journal` 可恢复旧失败任务已成功生成的摘要；必须通过原快照和前缀校验，不能把任意外部摘要写成 Context。检查点、原子生效、锁、API 用量及报告边界见 [Context 契约](../docs/context.md#host-coding-agent-显式维护)。测试为 `tests/test_compaction_checkpoint.py` 与 `tests/test_background_compaction.py`，不使用真实 Session/API。
