# 业务数据库与显式迁移（M1-02～M1-19、M1-23）

## 先理解两个数据库

- `data/business.sqlite3`：由项目自己的迁移管理，保存任务、契约、运行历史、事件、证据索引和账本。
- `data/graph.sqlite3`：仅由 LangGraph `AsyncSqliteSaver` 管理其内部表。

业务连接不打开图库，也不把两库视为同一个事务。文件路径由 `backend/webagent/config.py` 决定。默认使用本机 `data/`，也可以导出绝对路径 `WEBAGENT_DATA_DIR`；数据库文件不提交 Git。

## 目录与入口

```text
backend/webagent/
├── storage.py                  # API、Worker 共用的业务库初始化入口
└── db/
    ├── connection.py           # 连接配置、短事务、忙锁错误
    ├── migrations.py           # 迁移版本、校验与原子执行
    ├── repository.py           # 任务、契约、运行的最小持久化辅助函数
    └── sql/
        ├── 0001_core.sql       # 核心记录、引用、历史保护与查询索引
        ├── 0002_resources_quotas.sql # 资源、预算与额度账本
        ├── 0003_state_events.sql # 转移矩阵、版本检查与状态事件
        ├── 0004_task_api.sql    # 草稿修订与持久幂等响应
        ├── 0005_model_calls.sql # 模型调用组、尝试记录与历史保护
        ├── 0006_settings_snapshots.sql # 不可变配置与 Run 绑定
        ├── 0007_task_compilations.sql # 自然语言编译准备与持久回执
        ├── 0008_browser_sessions.sql # 浏览器生命周期与认证密文引用
        ├── 0009_identities.sql  # 登录准备与身份原子发布
        ├── 0010_scheduler.sql  # 持久队列、Worker 代次与统一上下文预留
        ├── 0011_budgets.sql    # 冻结预算、计时锚点、派发与监控额度来源
        ├── 0012_gateway.sql    # 观察绑定、页面头与不可变动作派发审计
        ├── 0013_evidence.sql   # 工件可用性、过滤观察、保留与存储故障
        ├── 0014_verification.sql # 不可变检查、聚合结果与终态门禁
        ├── 0015_graph_runtime.sql # 追加图进度、真实业务事件和版本绑定
        ├── 0016_graph_recovery.sql # 双库检查点与只读恢复记录
        ├── 0017_run_controls.sql # 持久控制请求与完成回执
        └── 0018_write_protocol.sql # 稳定写入语义、查证证明与逐次派发
```

从项目根目录显式执行：

```sh
./scripts/dev.sh migrate
# 可在隔离数据目录验证旧版本；不支持降级
WEBAGENT_DATA_DIR=/absolute/path/to/test-data ./scripts/dev.sh migrate --target 1
WEBAGENT_DATA_DIR=/absolute/path/to/test-data ./scripts/dev.sh migrate
```

API 和 Worker 启动时也调用同一编号迁移入口，确保空库、M1-01 空 WAL 库和已迁移库都可重复启动。这里的“显式迁移”指版本化 SQL 文件、版本日志与事务性升级；没有使用 ORM 的自动建表替代迁移。数据有价值时，应在升级前停止写入并通过 SQLite 备份机制制作备份，不能仅复制仍有活跃 WAL 的主文件。

输出含 `previous_version`、`schema_version`、`applied`。无待执行迁移时 `applied=0`；出错会非零退出。API 健康响应增加 `storage.schema_version`，单独的 schema 版本不表示模型就绪或任务已成功。

## 已实现的表

| 表 | 保存内容与主要约束 |
| --- | --- |
| `schema_migrations` | 连续版本、SQL 名称／SHA-256、结构摘要、UTC 应用时间 |
| `tasks` | 原始指令、准备状态、当前契约／运行引用、请求补充字段；当前引用必须属于本任务 |
| `contracts` | `(task_id, contract_version)`、规范化正文和哈希、格式版本及场景；禁止覆盖、更新、删除 |
| `runs` | 固定契约版本／哈希、父运行、状态版本、图版本、配置哈希、开始／结束／等待时间；`thread_id=run_id` |
| `task_revisions` | 不可变准备版本链、请求／完整草稿、缺失字段与授权 provenance |
| `api_idempotency` | 路由范围内的请求键、规范化摘要与首次响应，同业务记录原子提交 |
| `run_transitions` | 编号迁移登记的允许转移；运行时禁止增删改，变更需要新迁移 |
| `run_controls` | 已受理请求、版本、幂等摘要、请求事件与不可覆盖完成回执；每 Run 至多一个 PENDING |
| `run_retry_operations` | 新 Run 与同任务历史副作用的固定关联，不覆盖旧状态 |
| `task_events` | 数据库生成的递增 ID、任务／运行、事件类型、版本和 JSON payload；已提交事件不可改写 |
| `observations / steps` | 页面快照、原子尝试、动作序号、epoch、输入快照和结果；同运行序号唯一 |
| `evidence` | 工件相对路径、SHA-256、采集上下文、敏感级别和派生引用；工件索引不可改写 |
| `evidence_artifacts / evidence_availability / evidence_retention` | 不可变工件细节、独立可用状态、过期与正式保留策略 |
| `filtered_observations / evidence_events / evidence_orphans` | 已核验脱敏观察、追加审计与孤儿登记 |
| `run_verifications` | 冻结契约、候选、字段绑定、规则／语义检查和实际工件摘要；追加不可覆盖 |
| `run_results` | 每 Run 唯一聚合结果及摘要；和终态／事件同事务提交 |
| `run_checkpoints` | 业务事件、契约、预算、子目标和页面引用；追加保存，不能冒充图库 checkpoint |
| `graph_progress` | 阶段、业务事件／检查点／快照／验证／等待引用和有界诊断；幂等身份及当前 Run 版本受触发器校验 |
| `graph_input_requests` | 当前暂停等待的有界补参字段名称；关联真实等待事件并禁止覆盖历史 |
| `write_intents` | 跨重跑复用的业务操作键、来源运行、目标、预期变化、四态和回执；业务键唯一，固定绑定不可变 |
| `run_budgets` | 动作／页面／活跃时间／恢复累计值、心跳、扣额引用；每 Run 一条，累计值不能回退 |
| `resource_leases` | 资源键、持有者、worker、epoch、租约、控制权和逻辑占用；资源键唯一 |
| `resource_quarantines` | 资源与未决业务操作的关联；独立于短租约，删除租约不删除隔离记录 |
| `site_gates` | 站点开放／阻塞／冷却状态及下次可用时间 |
| `quota_buckets / quota_debits` | 上海日历日期桶、已用量、普通／监控扣额；每 Run 扣额唯一，账目不可覆盖 |
| `scheduler_workers / scheduler_generations` | Worker 当前资格、心跳和不可回退的启动代次 |
| `scheduler_queue / scheduler_requirements` | 每 Run 唯一队列项、可用时间、epoch、Run 版本和已知资源全集 |
| `scheduler_context_reservations` | 与浏览器共用的未兑现名额；延迟组合外键确保只能绑定本 Run 的上下文 |
| `scheduler_events` | 入队、领取、续约、撤销、等待、扩展与恢复的持久记录；禁止改写和删除 |

证据与步骤／观察／检查点，以及写入操作与尝试／证据之间，另用关联表存引用。组合外键防止将其他运行的证据或其他任务的运行误挂到当前记录；同一任务的新 Run 可以引用旧业务操作。

所有业务表使用 SQLite `STRICT`、外键、唯一键及适用的 `CHECK`。时间统一保存为 `YYYY-MM-DDTHH:MM:SS.ffffffZ`；`utc_text()` 将带时区时间转换为 UTC，拒绝无时区输入。额度桶的 `quota_date` 是业务日期，不是时间戳；上海切日与额度分配由 M1-11 的预算模块实现。

`repository.add_contract()` 按 UTF-8、排序键、紧凑 JSON、禁止 NaN/Infinity 计算正文摘要；正文完整保留为 JSON，常用身份字段单独建列。数据库校验身份绑定与 JSON 形状，**不代替完整的契约、权限和业务语义校验**，这些仍由后续可信服务实现。数据库不是任意外部 SQL 的安全执行环境。

## 升级与事务规则

1. 未标识但无任何业务对象的空库可升级；有其他表的数据库（包括图库）会被拒绝。
2. `application_id` 标识业务库，`user_version` 与迁移日志交叉核对。缺失日志、未知较新版本、已应用 SQL 改动或结构漂移会阻止启动。
3. 当前待执行的全部迁移在一次 `BEGIN IMMEDIATE` 事务中完成，DDL、数据与版本日志一起提交；失败全部回滚。`PRAGMA journal_mode=WAL` 在事务外设置。
4. 逐条执行完整 SQL 语句，使用 `sqlite3.complete_statement()` 识别触发器体；不使用会隐式提交的 `executescript()`。
5. 每个业务连接都设置 `foreign_keys=ON`、`recursive_triggers=ON`、`synchronous=FULL`、默认 5 秒忙锁等待。连接使用完毕显式关闭。
6. WAL 初始化竞争也可能立即返回 BUSY，因此迁移会在同一 5 秒截止时间内重试已回滚的纯数据库操作。普通业务写入不自动重放；忙锁转换为 `StorageBusyError`，由调用方决定是否重试整个事务。

调用方式：

```python
from webagent.db import connect, transaction
from webagent.db.repository import create_task

with connect(settings.business_db) as connection:
    with transaction(connection):
        create_task(connection, task_id="example", instruction="合成示例",
                    requested_fields=["contract"])
        # 同一个事务中可继续写有关记录；抛异常或提交失败都会回滚。
```

浏览器、模型、下载和长时间文件操作必须在事务之外执行。不要为普通改动使用 `INSERT OR REPLACE`：契约、运行等历史身份受到触发器保护。已结束的 Run 不能原地恢复或改写；重跑创建新 Run 并关联 `parent_run_id`。

新迁移必须追加编号文件并登记到 `MIGRATIONS`，不能改已应用文件。先用带历史数据的旧版本库测试升级，再测试重复执行、失败回滚和多进程竞争。当前尚未提供降级或跨版本业务数据自动修复。

## 本项边界

M1-02 提供持久化结构和事务基础。M1-03 已追加[合法状态转移、状态事件自动配对与 SSE 重放](state-events.md)。M1-04 已追加[任务 HTTP API 与持久幂等](task-api.md)。M1-10 追加[租约调度与 epoch 发放](scheduler.md)。M1-11 已接入[预算与额度分配](budgets.md)，额度、状态及资源在同一领取事务提交。M1-14 已实现[真实工件校验](evidence.md)，M1-15 已实现[检查与终态聚合](verification.md)。M1-16 追加[图循环及结构化进度](graph-loop.md)。M1-17 接入[两类检查点与只读崩溃恢复](recovery.md)。

实现依据：[SQLite 事务](https://www.sqlite.org/lang_transaction.html)、[连接级 PRAGMA](https://www.sqlite.org/pragma.html)、[Python sqlite3 事务与 executescript 行为](https://docs.python.org/3.12/library/sqlite3.html)。

## M1-05 调用账本

v5 追加 `model_generations` 和 `model_attempts`，不改写已有四次迁移。每次调用前的 STARTED 记录与模型调用次数同事务提交；完成后保留用量、耗时和受限错误分类。网络访问不持有数据库事务；未知结果不自动重试。详见[模型适配器说明](model-adapter.md)。

## M1-07 配置快照

v6 追加 `model_settings_versions` 与 `run_config_snapshots`，密钥只保存在 OS 凭据设施，表中仅保存随机引用。设置版本连续追加，Run 同事务固定模型／运行配置摘要，不为历史 Run 猜测配置。密钥与 SQLite 的发布／失败补偿边界见[配置与密钥说明](settings.md)。

## M1-06 自然语言编译记录

第七个迁移 `0007_task_compilations.sql` 新增 `task_compilations`。请求范围和幂等键唯一预留调用，独立记录配置快照、提示词版本、用量和结论；外键与触发器强制绑定准确的设置版本摘要。网络等待在事务外进行，任务版本、终态日志和成功回执同事务提交；历史终态记录不可修改或删除。编译失败也有持久回执，未知 STARTED 记录禁止盲重试。它不属于任何 Run，不向执行期模型日志伪造 Run。详见[自然语言编译](natural-tasks.md)。

## M1-08 浏览器会话

第八个迁移 `0008_browser_sessions.sql` 新增 `browser_sessions`、`browser_auth_snapshots`、`browser_session_events`。前者记录上下文归属、管理器代次、OPENING／OPEN／CLOSING／CLOSED／LOST、恢复来源和重查标记；后两者仅保存不可变密文引用／范围／摘要和状态事件。普通 SQLite 不含认证 JSON。生命周期与事件在短事务内提交，四上下文名额在数据库中受限。管理器拿到独占进程锁后才能把前一实例的非终态会话标为 LOST。详见[浏览器会话](browser-sessions.md)。

## M1-09 身份发布

第九个迁移 `0009_identities.sql` 追加 `login_requests`、`identities`、`identity_verifications`、`identity_login_events`，保留 v1～v8 原样。登录准备不等于身份；仅在两次页面核查和认证密文保存后，单事务发布身份、不可变核验、密文元数据与登录状态。版本比较防止重复确认，触发器约束上下文归属、站点／账号范围与历史不可变。网络和加密均在事务外；未发布密文不会成为可恢复身份。详见[身份说明](identities.md)。

## M1-10 持久调度

第十个迁移 `0010_scheduler.sql` 追加 Worker 代次、持久队列、已知资源、上下文预留及调度事件，继续使用 v2 的 `resource_leases` 和独立 `resource_quarantines`。领取原子提交 Run 版本、epoch、完整资源集合和浏览器名额；SQL 容量约束把存活会话与未兑现预留一起限制为四个。兑现先绑定再插入会话，延迟组合外键让整个事务失败时一起回滚。

同一配置数据目录最多两项活跃、四个存活或已预留上下文。等待释放活跃槽并保留逻辑占用；资格过期触发核对，不能按 TTL 直接释放未决写入或接管资源。持久资格在浏览器访问和认证引用发布时复查，普通动作必须在可信核对后才能恢复。API 当前仅提供只读调度状态；M1-16 的 Worker 已注册只读图执行器，处理由可信内部代码配置并入队的 Run。详见[调度说明](scheduler.md)及[图循环说明](graph-loop.md)。

## M1-11 预算持久化

第十一个迁移保留 v1～v10，追加 `budget_limits`、`budget_timers`、`budget_attempts`、`quota_monitor_sources` 和 `site_pacing`，继续使用原 `run_budgets`／`quota_debits`。限额和派发记录不可变，停止原因和首次接管截止不能回退。显式增加 RECONCILING、WAITING_SITE、PAUSED 到 FAILED 的截止边，随后恢复转移表插入保护。历史迁移及其摘要不变；详情见[预算说明](budgets.md)。

## M1-13 动作日志

`0012_gateway.sql` 保留 v1～v11，追加不可变 `gateway_observations`、最新观察指针 `gateway_page_heads` 与绑定执行资格的 `gateway_attempts`，复用原 `observations`／`steps`／`write_intents`。预算扣数、写入意图及 action_recorded 事件与动作 INTENT 同事务提交；终结后不得改写该步骤的审计。原始页面／截图不写入日志，观察保持 BLOCKED，详情见[网关说明](browser-gateway.md)。

## M1-16 图进度与上下文

`0015_graph_runtime.sql` 保留 v1～v14，追加 `graph_progress` 与 `graph_input_requests`；图内部 saver 表仍只在独立图库。业务进度绑定真实 Run／契约／图版本和同版本已提交事件，不接受模型或框架消息作为审计正文。补参名称关联当前暂停的真实等待事件，不保存原始模型原因文字。图重建只保存业务引用，观察检查点中的已验证／待办条件来自最新持久验证，模型证据索引只含 FILTERED 展示引用。

`AsyncSqliteSaver` 的 `durability="sync"` 等待图库提交后才进入下一步，不能让网页、业务库与图库原子提交。M1-16 新进程恢复范围是无副作用的持久等待；M1-17 增加 schema v16 的不可变恢复检查及受控导航记录，节点崩溃后先查证业务和浏览器状态。详见[图循环说明](graph-loop.md)。

M1-18 追加 `0017_run_controls.sql`（schema v17），沿用 v1～v16 已应用摘要。异步受理／完成和控制边界见[运行控制说明](run-controls.md)；实际验收状态见[M1-18 记录](../m1/records/M1-18.md)。

M1-23 追加 `0018_write_protocol.sql`（schema v18），固定稳定业务语义、逐次派发、查证证据和显式只读重跑关联，保留 v1～v17 与已有业务历史。新协议的 CONFIRMED／NOT_APPLIED 状态需要持久证明；未决写入阻止任务完整成功。见[写入协议说明](write-intents.md)。

## M1-19 只读观测投影

本阶段沿用 **schema v18**，没有新增或修改 SQL 迁移。`observability/store.py` 通过 SQLite `mode=ro`、`PRAGMA query_only=ON` 和单一 `BEGIN` 快照读取现有事件、图进度、动作、模型、预算、队列、Worker 心跳、恢复、写入和证据可用性账本。分页和聚合有工作量上限；旧 schema 缺表、缺失存储或查询超限返回安全的 503，不在诊断请求中初始化／升级业务库、访问图库 saver、结算预算或清理隔离。

静态价目复用 model_settings_versions 与 run_config_snapshots 的原有配置 JSON，估算金额／币种复用 model_attempts 的记录 JSON。未配置价格时省略新增配置字段，保持历史摘要；未知用量、币种和费用不会补成零。`data/logs/` 的有界 JSONL 仅是安全进程元数据，不替代不可变业务事件、证明或 SSE 重放来源。接口、两个独立游标与指标时点见[观测开发说明](observability.md)。
