# 业务数据库与显式迁移（M1-02）

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
        └── 0002_resources_quotas.sql # 资源、预算与额度账本
```

从项目根目录显式执行：

```sh
./scripts/dev.sh migrate
# 可在隔离数据目录验证旧版本；不支持降级
WEBAGENT_DATA_DIR=/absolute/path/to/test-data ./scripts/dev.sh migrate --target 1
WEBAGENT_DATA_DIR=/absolute/path/to/test-data ./scripts/dev.sh migrate
```

API 和 Worker 启动时也调用同一编号迁移入口，确保空库、M1-01 空 WAL 库和已迁移库都可重复启动。这里的“显式迁移”指版本化 SQL 文件、版本日志与事务性升级；没有使用 ORM 的自动建表替代迁移。数据有价值时，应在升级前停止写入并通过 SQLite 备份机制制作备份，不能仅复制仍有活跃 WAL 的主文件。

输出含 `previous_version`、`schema_version`、`applied`。无待执行迁移时 `applied=0`；出错会非零退出。API 健康响应增加 `storage.schema_version`，但不表示任务执行能力已开放。

## 已实现的表

| 表 | 保存内容与主要约束 |
| --- | --- |
| `schema_migrations` | 连续版本、SQL 名称／SHA-256、结构摘要、UTC 应用时间 |
| `tasks` | 原始指令、准备状态、当前契约／运行引用、请求补充字段；当前引用必须属于本任务 |
| `contracts` | `(task_id, contract_version)`、规范化正文和哈希、格式版本及场景；禁止覆盖、更新、删除 |
| `runs` | 固定契约版本／哈希、父运行、状态版本、图版本、配置哈希、开始／结束／等待时间；`thread_id=run_id` |
| `task_events` | 数据库生成的递增 ID、任务／运行、事件类型、版本和 JSON payload；已提交事件不可改写 |
| `observations / steps` | 页面快照、原子尝试、动作序号、epoch、输入快照和结果；同运行序号唯一 |
| `evidence` | 工件相对路径、SHA-256、采集上下文、敏感级别和派生引用；工件索引不可改写 |
| `run_checkpoints` | 业务事件、契约、预算、子目标和页面引用；追加保存，不能冒充图库 checkpoint |
| `write_intents` | 跨重跑复用的业务操作键、来源运行、目标、预期变化、四态和回执；业务键唯一，固定绑定不可变 |
| `run_budgets` | 动作／页面／活跃时间／恢复累计值、心跳、扣额引用；每 Run 一条，累计值不能回退 |
| `resource_leases` | 资源键、持有者、worker、epoch、租约、控制权和逻辑占用；资源键唯一 |
| `resource_quarantines` | 资源与未决业务操作的关联；独立于短租约，删除租约不删除隔离记录 |
| `site_gates` | 站点开放／阻塞／冷却状态及下次可用时间 |
| `quota_buckets / quota_debits` | 上海日历日期桶、已用量、普通／监控扣额；每 Run 扣额唯一，账目不可覆盖 |

证据与步骤／观察／检查点，以及写入操作与尝试／证据之间，另用关联表存引用。组合外键防止将其他运行的证据或其他任务的运行误挂到当前记录；同一任务的新 Run 可以引用旧业务操作。

所有业务表使用 SQLite `STRICT`、外键、唯一键及适用的 `CHECK`。时间统一保存为 `YYYY-MM-DDTHH:MM:SS.ffffffZ`；`utc_text()` 将带时区时间转换为 UTC，拒绝无时区输入。额度桶的 `quota_date` 是业务日期，不是时间戳；上海切日与额度分配算法在 M1-11 实现。

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

M1-02 提供持久化结构和事务基础。它没有实现 M1-03 的合法状态转移／状态事件自动配对、M1-04 的任务 HTTP API／请求幂等、M1-10 的租约调度及 epoch 发放、M1-11 的额度分配、M1-14 的真实工件校验，也没有证明业务库与图库的崩溃恢复。额度桶总数与扣额、执行态与扣额等多表业务条件将由后续服务在同一短事务内实现。

实现依据：[SQLite 事务](https://www.sqlite.org/lang_transaction.html)、[连接级 PRAGMA](https://www.sqlite.org/pragma.html)、[Python sqlite3 事务与 executescript 行为](https://docs.python.org/3.12/library/sqlite3.html)。
