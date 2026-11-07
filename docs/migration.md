# Session 格式与升级

[文档目录](index.md)

## 当前格式

产品 `recoding...`，发行名 `recoding`；核心导入为 `multiagent`、`session`。当前 Session 格式是 `recoding.session` v5，state、团队索引和导航 registry 同为 v5，Web API v4。存档容器 `.recoding`，不带尾部句点。

新版只展示和恢复兼容格式。其他版本的文件、图片、工作产物和日志保留，不补旧工具/接口、不自动转换。创建新 Session 会使用当前格式。旧品牌与旧部署操作不作为当前格式的兼容依据。

## 代码升级

使用与存档兼容的源码与独立环境，更新代码后重启 Host 并刷新界面，避免内存中的旧模块与磁盘新 worker 混用。默认同端口启动会替换旧 Recoding 服务，按 [启动规则](configuration.md#服务生命周期与-gui) 处理运行内存；工作文件和已提交历史保留。

需要打开不兼容存档时，使用支持该格式的源码版本或已有快照、单独端口和对应环境。不要求每个中间版本都有快照，不改写冻结的历史快照，也不同时运行同一存档的副本。

## 同版本移动

先关闭运行环境，再将完整 Session 文件夹移到另一 workspace 的 `.recoding/` 下。ID 不变，Resume 依据新位置绑定 workspace；对话、图片、队列和设置恢复，私有执行内存重新建立，不重放代码；Pool 进程仅保留 stopped 描述符，由 Master 决定是否显式重启和初始化。

工作文件、`.recoding/inputs` 和共享依赖 `.recoding/runtime/python-packages` 属于 workspace，移动单个 Session 不会带走它们。旧 `.python-packages` 和旧 Session 私有包通过显式 --migrate 检查锁与冲突后归并，原目录保留；跨解释器/平台需核验或重建，见 [包环境与维护](packages.md)。复制正在运行的存档不提供一致性保证；备份范围与锁规则见 [存档](session-storage.md) / [Host](session-ownership.md)。

## 重命名与结构变更的维护边界

源码包、启动入口、worker 路径、发行元数据、界面品牌和存档标识分别核对。源码改名不等于自动迁移存档，也不要求保留旧导入别名；只实现当前版本明确支持的转换。

涉及数据迁移时，先检查目标冲突、Host 占用和回滚策略，保留原始对话、图片、签名与工作文件；通过预览、合成夹具和必要内容摘要核验。具体部署的迁移数量、运行日志和测试结果只保存在 Git 忽略目录，不维护仓库内的历史操作流水。

## 容器目录名

当前容器名为 `.recoding`，不带尾部句点。迁移旧 `.recoding...` 前关闭同 workspace 的全部运行 Host，检查目标冲突；将整个容器原子改名，保留 Session ID、状态、图片、队列、辅助包和锁文件。Home 共享缓存容器同时按此规则移动。目录改名不转换数据格式，不改写原始对话、模型签名或历史日志；旧图像标签由读取层映射到新目录。浏览器保存的上次存档路径会更新，导航索引可重新发现。源码更新后重新启动 Host 再使用新路径。

## 当前执行契约

产品 0.8 使用 Session/state/team/registry v5 和 Web API v4。旧 v4 及更早存档不再展示或恢复，文件保持原样；如需继续旧存档，使用对应的冻结源码快照。此次升级不提供自动转换，也不在恢复时批量改写历史。

Agent 的 Run 使用 `run_id`；工具 Process 使用 `process_id`；一次代码提交只有 `execution` 记录，调用结果从 `execution.id` 取 ID。Session 查询为 `list_executions` / `execution_output`，TUI 命令为 `/executions`，Web API 为 `/agents/<id>/executions` 和 `/agents/<id>/executions/<execution_id>/output`。不存在旧 `job`/`task` 别名。更新源码后重启 Host，不能让旧宿主继续启动新 worker。
