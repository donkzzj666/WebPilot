# 持久队列与统一资源租约（M1-10）

调度器把“可以执行哪个 Run、持有什么资源、当前执行资格是否仍有效”保存在业务 SQLite。API、Worker 和浏览器读取同一份持久状态；Python 内存中的协程或旧 Token 不能代替数据库授权。

正常启动仍使用 `./scripts/dev.sh api`、`./scripts/dev.sh worker` 和 `./scripts/dev.sh frontend`。默认 Worker 注册代次、续约并维护登录服务；预算维护及独立截止已接入，当前尚未装配浏览器动作网关和产品图执行器，因此不会领取业务任务。可查询队列状态，但创建任务不会自动启动浏览器执行。

## 代码阅读顺序

| 文件 | 职责 |
| --- | --- |
| `backend/webagent/scheduler/models.py` | 固定资源键、规范化站点／仓库、执行 Token 与取锁顺序 |
| `backend/webagent/scheduler/store.py` | 入队、领取、心跳、等待、扩展、恢复与释放的短事务 |
| `backend/webagent/scheduler/worker.py` | 受信执行器的异步调度、续约、取消与退出 |
| `backend/webagent/scheduler/routes.py` | 只读 `GET /v1/scheduler` 元数据 |
| `backend/webagent/sessions/store.py` | 浏览器创建资格、预留兑现与登录资源冲突 |
| `backend/webagent/sessions/manager.py` | 实际 Chromium 生命周期与每次访问时的资格复查 |
| `backend/webagent/db/sql/0010_scheduler.sql` | 队列、Worker 代次、上下文预留、事件和容量约束 |

`SchedulerStore` 的写方法是项目内部的受信接口。HTTP 没有开放任意入队、抢锁、设置 epoch、确认恢复或交还接管权限的入口；只读接口同样受[本机 API 防护](network-security.md)保护，返回队列、租约、上下文预留和 Worker 元数据。

## 一组资源一起领取

| 资源 | 当前规则 |
| --- | --- |
| `active_slot` | 最多两项活跃执行，模型思考、动作、验证与恢复核对都占用 |
| `site_identity` | 同站点同账号一项；匿名按站点；GitHub 与网页编辑器共享逻辑站点 |
| `repository_write` | GitHub `owner/repo` 规范化后串行，跨账号也不能同时写同一仓库 |
| `webarena_environment` | 首期使用一个全局环境键，所有 WebArena 业务串行 |
| `browser_context` | 最多四个存活或已预留的上下文，包含登录、等待、暂停与人工接管 |

这些上限属于同一个 `WEBAGENT_DATA_DIR` 所配置的业务域。该域只有一个 Worker／浏览器管理器拥有本机进程锁；更换数据目录会建立另一个独立域，目前没有跨多个数据目录的机器级汇总锁。

站点账号键包含公开网站／WebArena 的 realm、规范化站点和账号引用的摘要；匿名有单独标记，不与名称恰好为 `anonymous` 的账号混淆。GitHub 仓库键忽略账号，统一大小写并接受明确的 `owner/repo` 或 GitHub HTTPS 仓库根地址；带凭据、查询、片段或分支路径的地址不能作为仓库资源。

领取在一个 `BEGIN IMMEDIATE` 短事务中完成：检查业务版本、Worker 代次、可用时间、隔离与资源冲突，选择活跃槽和上下文预留，再提交 Run 状态、epoch、全部租约及事件。取锁顺序固定为活跃槽、站点账号、仓库、环境、上下文，同类按资源键排序。任意资源未就绪就继续排队，不持有一部分新锁等待另一部分。

`ordinary`、`monitoring` 和 `webarena` 保留各自队列分类，到期监控优先领取；公开网站与基准仍共用两项活跃上限。M1-11 已通过领取事务中的受信预算钩子接入首次扣额和每日配额，见[预算说明](budgets.md)。这里的配额是运行次数限额，不涉及供应商付款。

## 登录和 Run 共用四个上下文名额

数据库计数是“OPENING／OPEN／CLOSING 浏览器会话 + 尚未兑现的 Run 上下文预留”。一个 Run 目前预留一个上下文。创建时先在同一事务绑定新会话 ID，再插入浏览器会话；延迟外键确保一起提交，兑现不会额外占第二个名额。一个预留不能被重复兑现，也不能绑定其他 Run 或登录会话。

登录用户可能在准备中切换账号，因此采取站点级保守互斥：同站点存在执行租约时不能新开登录窗口；存在存活登录窗口时，调度器不领取该站点的任务。网页编辑器别名归入同一 GitHub 站点。正常关闭尚处于 CLOSING 时仍占名额；关闭或丢失后的恢复必须重新取得名额并重查身份与业务状态。

已进入队列的 Run 创建浏览器、取得上下文或发布认证引用，都必须提供当前执行 Token，且持有匹配站点账号与该 Run 上下文的资源。测试和历史迁移中未进入队列的非终态 Run 保留原有生命周期接口；这项兼容仅供内部使用，不能作为已排队 Run 绕过调度的入口。

## epoch、心跳与过期处理

执行 Token 包含 `run_id`、`worker_id`、递增 `worker_generation`、`epoch`、Run 状态版本、租约时间和完整资源键。默认短租约为 30 秒；Worker 为自身和活跃项续约。每次授权从 SQLite 重新检查代次、Run 状态与版本、epoch、资源集合、期限和控制权，不依赖旧缓存。

Worker 换代、资格过期或退出会使旧资格失效，释放活跃槽，并把执行中的 Run 留在 RECONCILING／RECOVERY。过期不等于外部写入失败，也不证明浏览器已经结束；账号、仓库、接管占用和未决写入隔离不会仅因为短租约到期而释放。

恢复先取得仅用于核对的资格。RECONCILING 时不能通过普通上下文访问取得自动动作权限；受信调用方完成外部状态和浏览器归属核查后，才调用 `reconcile` 获得新的普通执行资格。`INTENT`／`UNKNOWN` 写入或仍存在的资源隔离会阻止恢复执行，即使 Token 尚未到期。已经发往网站的动作无法通过 epoch 撤回，需要由后续业务恢复流程查证结果。

当前 `QueueWorker` 要求执行器主动进入等待或结束，M1-11 已装配[独立预算截止](budgets.md)，预算耗尽会持久终止当前 Run。执行器返回、抛错或不能按时退出都不会自动判为业务成功，而是撤销资格并留下恢复工作；未明确结束／等待时停止当前调度器，防止立即重派恢复 Run。生产动作网关须在每次实际派发前继续使用此持久核验接口；M1-10 不包含完整网页动作和 LangGraph 业务恢复实现。

## 等待、接管与探索扩展

`PAUSED`、`WAITING_CI`、`WAITING_SITE`、`WAITING_HANDOFF` 释放活跃槽，保留账号逻辑占用和上下文。人工接管设置 `control_owner=human`；短租约过期与 Worker 重启都不能替用户交还控制权。`resume` 由受信编排明确处理控制权交还，转入 RECONCILING，不能直接恢复普通动作。

探索新增站点资源时，调用方须先在安全检查点保存进展，再将已提交的业务 `checkpoint_id` 交给 `expand`。调度器核对检查点所属 Run、任务、契约、当前 epoch 与事件，再撤销旧执行资格，释放可以安全释放的旧资源，保留上下文以及未决写入／隔离保护，更新已知资源全集后按固定次序整体重取。这样避免两项任务分别持有站点 A／B 再互相等待；本项不替业务图伪造检查点，也不会移除未知写入隔离。

终态释放同样需要区分逻辑与物理资源：即使 Run 已结束，只要 Chromium 会话仍存活，它仍计入四个名额。真实上下文由浏览器管理器关闭；未决操作隔离由可信业务核对流程处理。

## 单独验证

从项目根目录运行：

```sh
.venv/bin/python -m pytest tests/unit/test_scheduler_resources.py tests/unit/test_queue_worker.py tests/storage/test_scheduler.py tests/storage/test_scheduler_sessions.py tests/storage/test_scheduler_api.py
PYTHONPATH=backend .venv/bin/python scripts/verification/verify_scheduler.py --output-dir /tmp/webpilot-scheduler-verification
```

验证使用独立临时数据目录、合成任务和注册过的本机业务页，覆盖竞争 Worker、持久重启、资格失效、资源串行、等待和混合上下文容量。完整检查入口仍为 `./scripts/check.sh`，证据写入 `artifacts/verification/M1-11/`。这些验证不读取日常浏览器或现有账号凭据。

M1-11 的首次扣额、预算初始化与资源领取现已共用同一事务；`wait_site` 保存共享冷却而释放活跃槽，`resume` 结算原等待后进入不计时的排队。每次派发前还须调用统一预算记账；完整调用次序见[预算说明](budgets.md)。
