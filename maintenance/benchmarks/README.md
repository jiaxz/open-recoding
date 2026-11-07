# 人工真实模型实验

这里的脚本供 Host Coding Agent 在用户明确要求真实任务评估时使用；不是默认测试，不由 `unittest discover` 执行。它会消耗模型 API 配额并联网检索/下载。只加载必要的模型环境变量，不连接现有 Web/CLI Host，不读取用户的既有 Session。

## 黄金资讯并行实验

从项目根目录使用已有虚拟环境：

```bash
.venv/bin/python3 maintenance/benchmarks/gold_news_parallel.py --live --case baseline --seconds 600 --report-root .recoding/maintenance/gold-news-parallel
```

- `--live` 显式启用真实模型和联网；否则直接拒绝执行，不读取 `.env`。
- `--case` 是单次实验标签，报告子目录必须尚不存在；不覆盖旧报告。
- `--seconds` 是有限 driver 的最大墙钟时间；完成消息或超时后显式关闭 endless Session。
- `--model` 可固定模型；省略时使用项目默认选择。同一对照应保持模型、任务、权限、超时和预算一致。
- `--report-root` 使用 Git 忽略的 `.recoding/maintenance/` 路径。真实运行数据、模型历史和本机观察不得写入 `docs/`。

任务固定业务验收条件：优先最近七天、至少六条、至少三个站点，保存可访问原文、独立中文摘要、索引和总览。任务不指定 Agent 数量或并行实现；由当前 System 与模型自主决定。成功标记只是 driver 的终止约定，不改变核心 loop 语义，也不自动证明产物合格。

每次创建新的 `sessions/tests/` workspace 和 Session，缓存使用独立 RECODING_HOME。运行中每十五秒输出摘要；结束后关闭资源，复制 workspace、Session 与执行日志到报告目录（排除辅助包和临时 HOME），再清空 `sessions/tests/`。报告中的原始 Session 路径已是历史位置，保留的 workspace 副本可供事后检查。

## 评估口径

至少同时检查行为、时序和产物，不能仅以创建多少 Agent 判断并行成功：

1. 有无任务相关的职责拆分、及时委派、有效通信和可核验成果。
2. Model call 的峰值重叠数、至少两次调用重叠的秒数、总调用数和用量。同时统计首个任务被实际读取的时间，排除空白 Agent 启动/休眠通知，避免把等待任务的请求算成有效工作并行。
3. Python/Bash 实际执行区间的峰值重叠数，以及 sync/async 使用情况。同步工具等待时长不是实际后台执行时长；不要混算两者。
4. 审计代码中实际使用的线程/异步 I/O。单个池进程内的 HTTP 并发不在池执行峰值中，源码配置的 worker 上限也不等于实测并发。
5. 原文是否真抓取、是否错误页/搜索页、日期与来源是否可靠、摘要是否能由原文支持、失败是否如实记录。分别统计合格的单条成果与总索引/总览/交付通知，不能以文件总数代替资讯数量，也不能因未收尾就抹掉有效的部分产物。

`analysis.json`、`progress.jsonl`、`messages.json`、角色 prompt 和 workspace 存在各 case 下。模型区间包括失败/中断请求并标为 partial；执行区间根据服务记录的 started_at/finished_at，缺失时明确回退 created_at/实验结束。用量为已成功回报的正常 Model call，不推断未知失败调用的计费。

System 优化应针对实际观察到的瓶颈，记录每个版本的改动、指纹和证据；不以任务消息暗中要求更多 Agent，不通过生产代码强制调度。案例次数少时不能把单次墙钟差异称为普遍加速倍数；应说明随机性、网络/API波动、缓存和任务产物差异。

对照报告应列出累计 Input、Cache hit、已知 Non-cache 与缓存报告缺失覆盖；累计请求量不是单次 context 长度，也不能直接换算成费用。记录用户允许的最大 System 优化次数，每轮保存角色原文、哈希和差异；试验不保证单调改进，最终保留已测试的较好版本，不能把未经测试的混合提示当成验证结果。
