# 预算、配额与截止控制（M1-11）

先读[调度说明](scheduler.md)，再按 `models.py → store.py → deadline.py → scheduler/worker.py` 阅读。调度器负责“谁可以执行”，预算模块负责“还能执行多少、何时必须停止”。SQLite 保存权威累计值；网络请求、模型和解析始终在短事务外运行。

## 配额与首次开始

`SchedulerStore.claim()` 在同一个事务中检查资源全集、调用 `BudgetStore.before_claim()`、扣每日额度、建立预算、切换 Run 状态并发出 Token。失败候选通过 savepoint 完整回滚，不占额度或部分资源。创建任务和读取 API 都不扣额；真正首次领取时才扣。

UTC 时间采用固定微秒格式，日额度按 `Asia/Shanghai` 切日。每个公开 Run 只有一条不可变 debit；暂停、恢复、重启或跨日继续同一 Run 不再扣。派生的新 Run 消耗新的普通额度。

| 额度 | 每个上海日上限 | 判定 |
| --- | --- | --- |
| 公开总额 | 50 | 同一个配置数据目录 |
| 普通 Run | 42 | 包括派生 Run、未知监控来源 |
| 社区监控 | 4 | 原始 monitoring Run，`parameters.source_kind=security_community` |
| CISA KEV 监控 | 4 | 原始 monitoring Run，`parameters.source_kind=cisa_kev` |
| WebArena | 不扣公开额度 | 仍受动作、时间、资源及网络约束 |

两个固定来源合计预留 8 次。历史监控扣额若缺少来源元数据，按未知来源保守占用两类余量，不通过缺字段补发额度。认定监控同时核对队列分类、冻结契约 scenario、来源枚举和 `parent_run_id`，不能靠普通任务传一个字符串借用预留。用完普通 42 次不会挤占这 8 次。

## 统一累计与动作记账

`budget_limits` 从冻结契约复制，只能收紧产品上限，数据库禁止修改；v2 的 `run_budgets` 继续记录累计值，v11 新表记录单调计时锚点和不可变派发尝试。

| 项目 | 默认上限或最短间隔 | 记账方式 |
| --- | --- | --- |
| 原子动作 | 150 | `action`、`recovery`、`ci_poll` 各计一次，失败不返还 |
| 内容页 | 25 | 内容打开设置 `content_page=True`；重新打开再次计数 |
| 活跃时间 | 1200 秒 | 单调累计，等待模型、解析、页面及站点冷却都计时 |
| 同阻碍恢复 | 3 | 规范站点＋稳定子目标＋固定 `ObstacleType` 的哈希键 |
| 导航／搜索节奏 | 至少 3 秒 | 同逻辑站点跨账号共享；GitHub 编辑器归为 GitHub |
| CI 等待 | 累计 1200 秒 | 多轮 WAITING_CI 共用原 Run 累计 |
| CI 兜底轮询 | 至少 30 秒 | 不允许快速重复观测；观测本身使用活跃预算 |
| 接管等待 | 首次窗口最多 86400 秒 | 重复进入只能保留或缩短首次截止 |
| 动作超时／模型格式修复 | 最多 30 秒／2 次 | 冻结配置供后续动作网关使用；模型适配器共用绝对截止 |

实际派发前调用 `consume(token, kind=..., attempt_id=...)`。先重新核对资格与预算，再原子提交累计和尝试记录，提交后才能执行外部动作。第 150／25／3 次允许，第 151／26／4 次拒绝并持久化停止原因，拒绝项不产生外部派发。普通 DOM 观察和截图另计数；需要浏览器交互的动作不能伪装为 observation。

`attempt_id` 唯一绑定该次元数据。相同 ID 重复调用不重复扣，但返回 `dispatch_allowed=False`，也不能再派发；真实重试使用新 ID。自由文本错误描述不参与恢复键，不能通过换描述重置次数。稳定子目标由受信编排指定。

## 状态与时间

| 状态／阶段 | 活跃计时 | 其他计时 |
| --- | --- | --- |
| 持有活跃槽、RUNNING／VERIFYING／恢复核对 | 是 | 模型调用持续在同一区间内 |
| WAITING_SITE | 是，已释放活跃槽 | 逻辑站点冷却仍保护其他账号 |
| PAUSED、普通排队、WAITING_HANDOFF | 否 | 接管首次截止仍持续 |
| WAITING_CI | 否 | CI 累计持续；恢复领取后观测记活跃时间 |
| resume 后等待重新领取 | 否 | 已离开持久 CI／站点等待阶段 |

每次动作、模型预留和 Worker 心跳都结算原锚点，再更新锚点，纳秒余数避免频繁心跳丢失毫秒。模型调用数另外累计，provider duration 只供调用审计，不再加到统一活跃时间。暂停后的晚回执也不会额外加时。

正常运行使用单调时钟；重启恢复取可比单调间隔与 UTC 间隔的保守较大值，包含最后未闭区间。同一系统启动域跨进程可比；不能读取启动域时使用进程域与 UTC 保守恢复。换域并发生 UTC 回拨时耗尽该区间剩余预算，避免重启赠送时间。接管首次锚点也包含单调时间，墙钟回拨不能延长窗口。

历史 v10 的已存在活跃锚点首次受理时保守计入。若已耗尽或时间回拨导致无法安全判定，拒绝领取，等待恢复编排显式处理；不会清零后重跑。

## 独立截止与恢复边界

`DeadlineController` 在执行器之外运行：预检、定期读取持久预算、完成前再检查。到期先调用 `expire_budget()`，在事务中设 Run FAILED、撤销 epoch、释放安全资源，再有界取消正在等待的协程。额度拒绝也走同一停止路径。执行器不配合取消或账本故障时，Worker 停止继续派发；不能把晚结果认定为成功。

未确认写意图 INTENT 在截止时转 UNKNOWN，保留账号／仓库／环境的隔离和逻辑占用。人工控制不因 Run 到期或短租约而消失；存活 Chromium 继续占四个上下文名额，必须由会话管理器明确关闭。这里只提供保守终止基础，外部写入查证留给后续协议。当前截止统一 FAILED 并保留已有结果；有证据可交付的 PARTIAL 聚合由 M1-15 接入，不能直接绕过停止标记声明成功或部分成功。

正常核对造成 Run 版本变化时，运行时的 `runtime_budget()`／`runtime_heartbeat()` 在同一短事务重新绑定资格并结算／续约，避免两次读取间状态切换导致误停。它们与只读 `refresh_qualification()` 只供受信运行时使用，限定同一个 owner、generation、epoch、完整资源和控制权。旧动作 Token 继续严格拒绝；撤销或换代不能刷新。模型预留和结束均核资格，旧 epoch 回执持久标记 CANCELLED，不能发布候选。

`wait_site()` 持久保存共享 Retry-After、释放槽并继续活跃计时；等待超过剩余预算时立即终止本 Run，站点冷却保留。本站点有多个 realm 时显式使用 `public:github` 等范围，不能含糊选择。

## 只读接口与验证

- `GET /v1/budgets`：当前上海日的公开／普通／两类监控余量和数据目录作用域。
- `GET /v1/budgets/runs/{run_id}`：累计、剩余、停止原因与模式。还未首次领取时 `initialized=false`；不存在返回 404。

接口沿用本机令牌、Origin／Host 防护和 `no-store`，读取不结算、不扣额、不启动任务。投影时间取当前单调时钟；真正持久结算由 Worker 执行。

```sh
.venv/bin/python -m pytest tests/storage/test_budgets.py tests/storage/test_budget_scheduler.py tests/storage/test_budget_api.py tests/unit/test_budget_deadline.py tests/unit/test_budget_integration.py
PYTHONPATH=backend .venv/bin/python scripts/verification/verify_budgets.py --output-dir /tmp/webpilot-budget-verification
./scripts/check.sh --headed
```

默认 Worker 保持业务执行禁用，注册、续约、维护等待截止。受信执行器和预算接口已经可组合；产品图、逐动作网关、CI 业务轮询及接管 UI 按 M1-13／M1-16 等后续任务接入。配额与资源约束作用于同一个 `WEBAGENT_DATA_DIR`，不跨不同数据域汇总。完整证据见 [M1-11 验证记录](../m1/records/M1-11.md)。

M1-13 的 `consume_in_transaction()` 供动作日志在调用方同一事务组合预算和 INTENT。它返回 `(status, error)`：预算拒绝必须先提交累计时间与停止标记，再抛出错误；其他存储故障回滚整项预留。对坐标最终截图和动作的联合预留，第二次检查跨截止时回滚部分扣额，再持久结算截止，避免出现没有步骤的动作扣额。
