# 运行控制：请求与完成分开

M1-18 已完成修复复验，状态为 VERIFIED；通过范围及失败历史见 [验收记录](../m1/records/M1-18.md)。

任务表示用户的目标和契约；Run 表示一次执行；控制操作表示用户对执行提出的一次请求。三个对象分别具有版本或身份。重跑会创建新 Run，旧失败和副作用仍可追溯。

## 公开接口

所有接口经过本机 API 认证及来源检查。操作请求携带 `expected_state_version`、`contract_version`、`settings_version` 和 `Idempotency-Key`；正文也可携带相同的幂等键。版本变化或同键不同正文返回 409，无效字段返回 422。客户端应先读取任务／运行和设置就绪信息，提交真实版本。

| 请求 | 用途 |
| --- | --- |
| `POST /v1/tasks/{task_id}/start` | 为 READY 任务冻结配置、创建 Run 并登记启动请求 |
| `POST /v1/runs/{run_id}/pause` | 请求在安全边界暂停 |
| `POST /v1/runs/{run_id}/resume` | 请求查证后继续同一 Run/thread |
| `POST /v1/runs/{run_id}/cancel` | 请求取消非终态 Run |
| `POST /v1/tasks/{task_id}/retry` | 关联原 Run 创建新 Run/thread，保留副作用引用 |

HTTP 202 只表示持久受理。客户端须读取操作结果或等待持久业务完成事件；只有 `APPLIED` 才表示控制已生效，`REJECTED` 表示请求无法在边界执行。操作身份和最终回执不可覆盖。SSE 重放的是这些已提交的业务事件。

启动和重跑使用当前设置版本；已有 Run 的控制使用其冻结设置版本，用户后来修改设置不会改变原 Run。任务详情公开 `task.state_version`，运行详情公开 `state_version`，不能互换。

任务详情和历史运行中的 `settings_version` 是该 Run 冻结的版本号；它不包含模型密钥或配置正文。操作结果可由 `GET /v1/operations/{operation_id}` 查询；`GET /v1/runs/{run_id}/operations` 支持 `after`／`limit` 分页。

## Worker 在安全边界处理

受理控制和新派发授权使用同一个 SQLite 写锁。请求一旦受理，动作、模型调用和新观察的持久授权都会拒绝新派发。已获准的在途操作可以保存真实结果，再由 Worker 应用控制；请求本身不回滚外部数据。

暂停保存 PAUSED 状态、完成回执、持久等待和图进度，再保存 LangGraph interrupt。等待不占活跃槽、不计活跃时间，受管上下文和账号占用保留。继续获得新代号，经 M1-17 核对图引用、契约、证据、当前身份和对象后才继续。单个 Run 的图调用串行执行。

业务已提交暂停但图库尚未保存的崩溃窗口，以业务回执为准补齐图等待。普通继续不交还人工控制权，不清除 UNKNOWN，也不重置预算；终态继续返回冲突。重跑创建新预算并在实际首次开始时重新扣额，原 Run 的账本保留。

物理动作超时撤权后，只有执行器和图调用完全退出，空闲协调器才会结算已受理的暂停或取消；失败的 Worker 不再领取任务，UNKNOWN 和隔离记录保留。业务取消即使遇到损坏的旧图引用也可完成；图库收尾受阻不会撤销已持久化的取消回执。

M1-18 用持久状态夹具覆盖 CI、站点和接管等待下的取消；完整接管和 CI 业务流程依照后续里程碑实现。UNKNOWN 的外部查证与安全重试属于下一项 M1-23。

## 事件表升级

旧事件表的 CHECK 枚举需要通过 schema v17 扩展。实现参照 [SQLite 官方通用表结构变更流程](https://www.sqlite.org/lang_altertable.html#making_other_kinds_of_table_schema_changes)，在专用迁移连接上于事务前临时关闭外键执行，事务内保留事件 ID、自动编号、历史行、索引和触发器；提交前显式检查全部外键，退出时恢复外键执行。普通业务连接始终开启外键，不允许客户端安装迁移或任意 SQL。
