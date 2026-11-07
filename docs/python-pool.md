# Python 进程工具

[文档目录](index.md) · [Node Process Pool 完整契约](execution-pool-design.md)

Python 工具是 Node 通用 ProcessPool 的语言接口，不维护独立的 Owner/授权/队列实现。PythonDriver 位于 core/tools/python/driver.py，通过 worker.py 的 JSON 管道执行代码。变量、函数、导入与线程保留在该 Python 进程中。

## 调用

用 python_process 创建带名字的持久进程：

```json
{"action":"create","name":"analysis","node_id":"local"}
```

用返回的 process.id 调用 python：

```json
{"process_id":"<process-id>","code":"value = 41\nprint(value + 1)","mode":"async"}
```

省略 process_id 则创建临时解释器，执行后回收。async 返回 execution.id，完成通知发给提交者；sync 等待并返回输出。python_execution 支持 list/status/read/cancel，read 提供 stdout/stderr 独立字节游标。

全部 Agent 可使用/读取任意进程，不需要分享。只有 Owner/Master 可 rename、transfer、delete；转交不改变执行提交者。进程名字可重复，ID 不复用。

同一解释器 FIFO，不能在其中等待同一队列后续代码改变状态。共享 Python 适合短通信/同步，长计算使用独立进程；对象变化不唤醒 Agent，通知仍走消息。依赖更新后需要新解释器时显式创建新进程。

## 取消与生命周期

check_cancel()、有界 I/O 和 try/finally 有助于合作式取消。运行代码可能收到 KeyboardInterrupt；部分修改不回滚，忽略中断的持久代码可成为 unresponsive，Owner/Master 可 delete。

Agent Stop 取消其提交的执行；它拥有的进程拒绝新任务并退役，允许其他提交者的当前调用结束，最多等待默认 5 秒后回收。要保留状态，在 Stop 前显式转交 Owner。Session Stop 丢失全部内存，Resume 只保留对话、记录和文件，不恢复对象或重放代码。

完整身份、权限、并发、日志和 SSH 故障语义见 [统一契约](execution-pool-design.md)。
