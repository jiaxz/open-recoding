# 从 Python 调用 Session

[文档目录](index.md)

`session` 是公共聚合包，`multiagent` 是统一认知运行包，主要对象为：

| 包 | 主要类 | 责任 |
| --- | --- | --- |
| `multiagent` | `MultiAgentSystem` | 创建并持有全部 Master/sub，路由消息，管理角色、并发和团队事件 |
| `multiagent` | `Agent` / `AgentLoop` | 各自的 worker、模型、收件箱、Context 与持续循环 |
| `session` | `Session` | 绑定 workspace，装配团队、Node 执行资源、设置、Stop/Resume 和压缩 |

对象关系是组合：

```text
普通 Python 程序
└── Session
    ├── team: MultiAgentSystem
    │   ├── agents["master"]: Agent
    │   ├── agents[sub_id]: Agent
    │   └── event_stream: EventStream
    ├── execution: ExecutionService → 多个 NodeRuntime
    └── compaction: SessionCompaction

session.master 是 team.agents["master"] 的便捷引用。
每个 Agent 的独立 worker：AgentLoop + 模型客户端 + 通用工具 RPC。
```

Master/sub 共用 Agent 实现，差异来自角色权限和提示。只运行 Master 即为单 Agent 使用，不需要另一套独立产品入口。

`Session.create()` 自动启动 Master。`submit()` 只把用户消息放入持久队列，不阻塞等待任务完成；下一次 Model call 才读取。任务是否完成由应用根据明确的结果消息和产物判断。收到结果后 Agent 仍处于 endless loop，调用方应在不再需要运行时显式停止或关闭。

Session 的 `status` 表示整体 run/stop；检查 Master 的失败/睡眠阶段使用 `session.master.status`。

Session 不导入 Web，也不监听应用 HTTP 端口。Web 是使用 Session 的一个外壳；下面的程序可以独立调用同一套核心。

## 最小程序

此示例调用 `.env` 配置的真实模型，需已有 Python、Bubblewrap 和 Bash。`.env` 由调用方显式加载；`Session` 不自动读取它。自动测试使用本机模型替身，见后面的测试说明。

```python
import json
from pathlib import Path
import time
import uuid

from session import Session
from session.config import load_env_file

load_env_file(".env")
marker = "SUM_DONE_" + uuid.uuid4().hex
workspace = (Path("sessions/tests") / ("python-example-" + uuid.uuid4().hex[:8])).resolve()
workspace.mkdir(parents=True)

session = Session.create(
    workspace,
    "python-sum-example",
    config={"model": "grok-4.7", "network": False, "gpu": False},
)
try:
    session.submit(
        "请用 Python 计算 1 到 100 的和，把 {\"sum\": 计算结果} 保存到 result.json。"
        f"核验文件后，用 send_user_message 向我报告结果，并包含标记 {marker}。"
    )
    deadline = time.monotonic() + 120
    while True:
        if session.master.status == "failed":
            raise RuntimeError(session.snapshot(events=False).get("error"))
        reports = session.messages(limit=100)["messages"]
        report = next(
            (item for item in reports
             if item["kind"] == "human_output"
             and item["source"] == "master"
             and marker in item["content"]),
            None,
        )
        if report is not None:
            result = json.loads((workspace / "result.json").read_text())
            assert result["sum"] == 5050, result
            print(report["content"])
            print("Workspace:", workspace)
            print("Session:", session.store.folder)
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("等待任务结果超时")
        with session.condition:
            session.condition.wait(timeout=0.2)
finally:
    session.close()
```

标记是应用的任务完成约定，不是新增的 Turn/最终回答机制。`messages()` 只读取已保存的通信记录，不消费模型收件箱；在更长的应用中可以用 `after`/`before` 分页游标跟踪消息。`network=False` 控制工具沙箱联网，模型 API 仍由宿主进程连接。

工作文件位于 `workspace/result.json`，Session 状态位于 `workspace/.recoding/<timestamp>-<short-id>/`。这个手动示例保留产物；同版本存档可以由 Web 打开，也可以用 `Session.resume(session_directory, config=...)` 恢复。Resume 自动启动 Master，sub 由 Master 决定是否逐个恢复，Python/Bash 内存重建。

## 自动测试

测试代码位于 [tests/test_session_api.py](../tests/test_session_api.py)，不是放在工作目录中。执行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests .venv/bin/python3 -m unittest test_session_api test_workspace_support -v
```

其中 `test_python_program_completes_task_via_session` 启动普通 Python 子进程，直接调用 Session API：

1. 在 `sessions/tests/session-task-<随机值>/workspace/` 创建 Session。
2. 用本机 HTTPS/SSE 模型替身返回 `sleep`、`python`、`send_user_message` 等工具调用。
3. 通过 `Session.submit()` 投递任务，验证真实 Python 沙箱创建的文件为 `{"sum":5050,"count":100}`，并核对工具 stdout 和发给用户的结果消息。
4. 验证任务报告后循环仍在休眠，由调用方 Stop/close，并确认 worker 退出。
5. 验证应用进程从未导入 `recoding_web`。

这是 Session 到模型协议、消息队列、真实工具和结果回传的联通检查；模型回复由测试固定，不评估模型自身的推理能力。测试不读取项目 `.env`，使用临时测试密钥和本机模型地址，不调用外部 API。

原有 `test_session_aggregate_without_web` 继续检查成员独立历史、图片输入、配置、Stop All、停止时留言和只恢复 Master。

## 测试数据位置

所有正式测试代码留在 `tests/`。Python 使用 `tests/workspace_support.py::temporary_workspace()`，浏览器使用 `tests/browser/support.cjs::testDirectory()` 创建本次运行独占目录；Session、workspace、模型夹具等运行数据统一放在 `sessions/tests/` 下，已由现有 `sessions/` 忽略规则排除 Git 和源码快照。

自动测试结束或失败后，先关闭 worker、沙箱、本机模型和测试服务，再自动清空 `sessions/tests/` 全部内容。临时图片、日志和 test-report.json 一并删除；要保留的脱敏诊断先写到 `.recoding/maintenance/`。手动运行本页示例后执行 `.venv/bin/python3 tests/workspace_cleanup.py` 清理；真实任务请使用专用测试目录之外的 workspace。依赖安装和不创建 Session 的一次性诊断可使用项目外临时目录。
