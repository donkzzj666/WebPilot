# 本地日志、诊断与指标（M1-19）

M1-19 把已提交的业务事实投影为认证诊断、指标和 Worker 健康接口，并增加本机结构化元数据日志。观测接口不会调用模型、打开浏览器、读取密钥或证据原件，也不会结算预算、修复租约、清除 UNKNOWN 或改变任务状态。业务库仍为 **schema v18**；本阶段没有新增 SQL 迁移、依赖或 M1-20 工作台页面。

## 按责任读代码

| 文件 | 责任 |
| --- | --- |
| `backend/webagent/observability/store.py` | 单一 SQLite 只读快照、白名单字段、分页和账本聚合 |
| `backend/webagent/observability/routes.py` | 三个认证只读接口与参数限制 |
| `backend/webagent/observability/http.py` | HTTP 请求关联 ID；只绑定服务返回的持久引用 |
| `backend/webagent/observability/logging.py` | 有界 JSONL 日志、安全错误枚举、真实图／事件／检查点关联 |
| `backend/webagent/observability/standard.py` | 第三方 Python logging 的安全元数据处理器 |
| `backend/webagent/models/pricing.py` | 静态 token 价目、内容版本与精确费用估算 |

业务事件与 SSE 仍以 `task_events` 为准。JSONL 日志用于排查进程行为；它不是业务回执、成功证明或可重放的状态来源。

## 认证接口与请求关联

以下接口都经过现有本机 Bearer、Host／Origin 和浏览器来源门禁，包括两种健康接口。凭据不能放入 URL、浏览器存储或日志。可复用[设置文档中的认证客户端](settings.md#接口)进行本机查询。

| 方法 | 路径 | 返回内容 |
| --- | --- | --- |
| GET | `/v1/diagnostics/runs/{run_id}?after=0&model_after=0&limit=100` | 一个 Run 的持久事件页、模型尝试页、预算与图关联 |
| GET | `/v1/metrics` | 当前持久账本的状态计数、用量和费用聚合 |
| GET | `/v1/health/worker` | 最新持久 Worker 登记与心跳租约 |
| GET | `/health` | API 自身及本机业务存储的可读状态 |

前三个接口响应使用 `Cache-Control: no-store`。诊断只接受 `after`、`model_after`、`limit`；指标和 Worker 健康不接受查询参数。重复、未知、负数、非规范整数参数被拒绝。游标范围为 `0 <= cursor < 2**63 - 1`，页大小范围为 `1..1000`，默认 100。不存在的 Run 返回 404；存储缺失、旧 schema 缺少必要账本或查询超过资源限制时返回安全的 503，不创建或升级数据库。

HTTP 使用两层关联 ID：`X-Request-ID` 保留持久幂等响应的业务请求 ID；`X-Transport-Request-ID` 是每次 HTTP 请求新生成的 UUID v4。创建任务重放时，原响应正文和业务 ID 保持一致，两次 HTTP 的 transport ID 不同。普通响应和安全错误使用本次生成的业务 ID；客户端传入的 `X-Request-ID` 不被当作可信日志字段。模型尝试自己的 `request_id` 属于调用账本，不能当作 HTTP transport ID。

## 诊断页与两个游标

响应的 `schema_version` 为 `m1-19-diagnostics-v1`，`source` 为 `persistent_business_ledgers`。`run` 包含 task／run／thread ID、契约和图版本、状态版本及时间；`budget` 标记 `timing=persisted_only`，表示已结算的账本值。

`events` 按真实 `task_events.event_id` 排序。每项仅返回事件类型、状态版本、时间，以及可核实的 step／operation／图节点／业务检查点引用。动作记录同时区分 `attempt_status` 与 `current_step_status`，不会把步骤目前状态当作过去事件的结果。`graph` 引用通过已提交 `business_event_id` 关联，不能由框架消息填入。图链接总数受限，省略时 `graph_links_truncated=true`。

`model_attempts` 按现有 `model_attempts.rowid` 分页，`model_cursor` 只是稳定的预留顺序游标，不表示与事件的因果关系。调用可以在 STARTED 行创建后完成；已经越过该行的游标不会再次收到完成更新，需要重新读取该范围。此接口不是模型完成事件流。

下一页分别使用 `next_event_cursor` 和 `next_model_cursor`。`events_truncated`、`models_truncated` 表示对应序列还有后续项；两个序列可以各返回最多 `limit` 条，不能只更新其中一个游标。`model_summary` 聚合该 Run 的完整调用账本，不只统计这一页。每次响应有一个 `as_of` 时点并使用一个 SQLite `BEGIN` 快照；不同分页响应可能看到后续提交。

诊断不返回提示词、模型正文、页面内容、URL、Cookie、Token、Key、原始错误正文或写入收据。错误和原因转换为固定枚举，无法识别的值为 `other`；不安全的标识符转为稳定的 `redacted-<摘要>`，不会原样输出。

## 指标来源与时点

`/v1/metrics` 的 `schema_version` 为 `m1-19-metrics-v1`。`as_of` 是本次读取开始时的 UTC 时钟，其他值来自同一个 SQLite 读快照。指标使用 SQL 计数与分批流式聚合，不加载所有原始记录；查询工作量和价格组数有界，超限返回 503，不能用截断统计冒充完整总量。

| 字段 | 权威来源与含义 |
| --- | --- |
| `run_state_counts`、`scenarios` | runs 与冻结 contracts 的实际状态／场景计数 |
| `budget` | run_budgets 的 actions、content_pages、active_ms、ci_wait_ms、model_calls、observations、screenshots；只含已持久结算值 |
| `actions` | gateway_attempts 与 steps 关联的动作类型及当前步骤状态计数 |
| `queue.status_counts`、`queue.reason_counts` | scheduler_queue 的持久状态与固定原因 |
| `recovery.phase_counts`、`reason_counts` | graph_recoveries 的查证回执阶段和原因，`phase_counts_are_receipts=true` |
| `recovery.budget_attempt_count` | budget_attempts 中 `kind=recovery` 的记录数 |
| `recovery.budget_counter_total` | run_budgets 中恢复计数的总和；不把 BEGIN／BLOCKED／COMPLETE 相加当作恢复次数 |
| `writes.status_counts`、`quarantines` | write_intents 状态与 resource_quarantines 数量 |
| `evidence.capture_counts`、`availability_counts` | evidence 捕获状态与 evidence_availability 的持久状态；不读取实际原件 |
| `model` | 全部 model_attempts，包括未完成 STARTED 的用量／费用聚合 |

排队时长标记 `timing=wall_clock_snapshot`：

- `queue.current_pending_wait_age_ms.QUEUED/RECOVERY` 从当前队列行的 `updated_at` 到 `as_of` 计算，表示当前调度区间的年龄，不是任务全程等待时长。
- WAITING_CI／WAITING_SITE／WAITING_HANDOFF／PAUSED 放在独立的 `wait_state_revision_age_ms` 桶，不混入排队等待。
- `completed_enqueue_to_first_claim_ms` 从 scheduler_events 的第一次 enqueued 到第一次 claimed 计算已完成的首次领取耗时；未领取就结束的 Run 没有该值。

时长桶返回 `count`、`known_count`、`unknown_count`、`known_total_ms`、`max_ms`。时间无效或逆序记为未知，不补成零。恢复计数也区分 `known_budget_counter_total` 和 `unknown_counter_groups`；存在未知组时完整 `budget_counter_total` 为 null。

`unavailable_metrics` 明确列出尚未实现的 business_quality_rate、false_success_rate、unauthorized_write_rate、login_success_rate、handoff_success_rate、flow_hit_rate、flow_invalidations、monitoring_start_deviation、monitoring_gaps、benchmark_recovery_failures，原因均为 `not_implemented`。本机状态计数和探针结果不构成真实业务正确率。

## 用量、费用与未知值

价目由调用方配置，随设置版本和 Run 快照冻结；不抓取在线价格，不用当前价目重算旧记录。具体配置见[设置说明](settings.md#静态-token-价目)，算法见[模型适配器说明](model-adapter.md#静态价格与费用估算)。

单次尝试返回 `usage`、`usage_known`、`usage_partial`、`usage_complete`、`price_version`、`estimated_cost`、`cost_currency`、`cost_known`。仅输入或输出其中之一已知时为 partial，两者均未知时仍为 unknown；image_units 单独已知不能证明 token 用量完整。

聚合分别返回 `unknown_usage_attempts` 和 `partial_usage_attempts`。每个用量字段提供 `known_total`、`known_attempts`、`complete`：没有任何已知值时总量为 null；有已知值时返回已知部分并用 complete 标明是否完整。零只表示供应商确实报告零。

`costs_by_price_version` 按 `(price_version, cost_currency)` 分组，用 Decimal 精确相加并返回十进制字符串。各组提供 known／unknown attempts、estimated_cost、complete；未知费用不补成零，不混合 USD 与 CNY，也不猜测历史记录缺失的币种。STARTED、失败或未配置价目的尝试仍进入统计，因此“有用量但无价格”和“用量未知”可以区分。

## 两种健康状态

`GET /health` 只检查 API 进程及其业务库的只读连接、tasks 表与已启动 schema 版本。Worker 未启动或任务失败时，API 仍可返回 200。`health_scope=local_api_and_storage`、`tasks_success_implied=false`；`diagnostic_logging` 单独报告 ready／unavailable，不能由日志故障替换业务结论。

`GET /v1/health/worker` 只读取最新 scheduler_workers 代次，`source=persisted_scheduler_heartbeat`。ACTIVE 且 `heartbeat_at <= as_of < expires_at` 时 `ready=true`、HTTP 200；not_started、stopped、stale 均返回 HTTP 503。正文提供 Worker ID、generation、state、heartbeat_at、expires_at、heartbeat_age_ms、as_of，并固定 `tasks_success_implied=false`。这表示持久登记租约仍有效，不证明正在处理的任务成功，也不探测供应商或网站。

## 本机结构化日志

日志写入数据目录的 `logs/api.jsonl` 和 `logs/worker.jsonl`。允许字段是程序所有的 ID、版本、节点名、事件序号、耗时、HTTP 状态与固定错误枚举；不会记录请求路径、请求头、正文、异常文本或 traceback。图诊断先核对业务事件、Run／thread、进度和检查点的实际关联，不把任意框架 payload 复制到业务事件或日志。

默认单文件最多 1 MiB，保留 3 个轮转备份；单条最多 4096 字节，使用跨进程 flock 和有界锁等待。文件权限为 0600；目录要求属于当前用户且不可由组／其他用户写入，拒绝符号链接及不安全文件。日志无法写入或字段不合规时丢弃诊断，不改任务状态或响应；因此日志数量不能当作完整业务事件数量。

API／Worker CLI 启动时安装安全标准 logging handler：WARNING 及以上仅转为固定 `service_failed`／error_class 元数据，绝不调用 getMessage、格式化参数、异常文本或 traceback。队列最多 128 项，满时丢弃，后台线程写入。Uvicorn access log 关闭且不安装默认 log_config，避免另一路输出 URL 或第三方原始消息。嵌入式调用 `create_app()` 时应由宿主安装同等日志边界。

入口在导入执行框架前覆盖继承的 LangSmith／LangChain tracing 标志为 false，API／Worker 启动再次应用。默认不启用外部 trace；本地检查点和上述诊断不会上传到 trace 服务。

## 验证入口与证明范围

```sh
PYTHONPATH=backend .venv/bin/python -m pytest -q \
  tests/unit/test_observability_logging.py \
  tests/unit/test_observability_http.py \
  tests/unit/test_observability_graph.py \
  tests/unit/test_model_pricing.py \
  tests/storage/test_pricing_settings.py \
  tests/storage/test_observability.py \
  tests/storage/test_observability_api.py
PYTHONPATH=backend .venv/bin/python scripts/verification/verify_observability.py \
  --output-dir "/tmp/webpilot-observability-$(date -u +%Y%m%dT%H%M%SZ)"
```

真实探针使用独立临时业务库、图库、API／Worker 子进程、本机 HTTP 模型服务和 Chromium 自有页面，核对成功与失败 Run 的事件、步骤、节点、检查点、用量和合成价目费用。另用明确登记的账本夹具验证 recovery／UNKNOWN／隔离计数；这些夹具不能被描述为只读图产生了真实网站写入。敏感 canary、框架 payload 和所有者进程清理独立验证。探针不使用真实账号或付费模型，不改默认服务与数据目录。

网络证据范围是自有 Python socket、Playwright Node driver、Chromium NetLog 与受管代理；不能声称系统级零数据包。继承 tracing 标志故意开启后再由生产入口关闭，既核对允许的本机模型／页面流量，也核对没有外部框架 trace。`--output-dir` 必须显式指定一个尚不存在的新目录；上例使用临时目录，正式留证可使用全新的 `artifacts/verification/M1-19/` 子目录。实际成功／失败与通过范围以最终报告及 [M1 清单](../m1/README.md)为准，本说明不预先宣告验收通过。
