# 状态机、持久事件与 SSE（M1-03）

先读 [数据库说明](database.md)。本模块管理一次 Run 的状态变化和事件记录；任务准备阶段的 `NEEDS_INPUT` 不属于 Run 状态。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| `backend/webagent/db/sql/0003_state_events.sql` | 完整转移表、版本约束、自动状态事件、重复事件保护 |
| `backend/webagent/state.py` | 可信服务调用的版本比较、状态转移、开始／结束／接管时间处理 |
| `backend/webagent/events.py` | 结构化业务事件追加、分页读取、游标校验 |
| `backend/webagent/sse.py` | `GET /v1/events` 长连接与重放 |
| `backend/webagent/errors.py`、`api.py` | 统一错误及 HTTP 映射 |
| `tests/storage/test_state_events.py`、`test_sse.py` | 转移矩阵、事务中断、多进程竞争、真实 HTTP 重连测试 |

本模块由迁移 **3** 引入，当前完整业务库版本为 **8**。API／Worker 启动或 `./scripts/dev.sh migrate` 都使用同一个入口。旧迁移文件保持原样；升级保留已有状态、版本及事件，不补造旧版没有记录的历史。以下状态／事件配对保证从 v3 生效开始成立。新建 Run 为 `QUEUED / 0`，这是初始记录，不生成虚构的状态变化事件。

## 完整状态转移表

没有列出的转移一律拒绝，包括原状态到自身。表中共 33 条允许转移。

| 原状态 | 允许到达的状态 |
| --- | --- |
| QUEUED | RUNNING、CANCELLED |
| RUNNING | VERIFYING、WAITING_CI、WAITING_SITE、WAITING_HANDOFF、PAUSED、RECONCILING、PARTIAL、FAILED、CANCELLED |
| VERIFYING | RUNNING、WAITING_HANDOFF、RECONCILING、SUCCEEDED、PARTIAL、FAILED、CANCELLED |
| WAITING_CI | RECONCILING、PARTIAL、FAILED、CANCELLED |
| WAITING_SITE | RECONCILING、CANCELLED |
| WAITING_HANDOFF | RECONCILING、PARTIAL、FAILED、CANCELLED |
| PAUSED | RECONCILING、CANCELLED |
| RECONCILING | RUNNING、VERIFYING、CANCELLED |
| SUCCEEDED、PARTIAL、FAILED、CANCELLED | 无；终态不可修改，重跑须新建 Run |

全部非终态允许取消。CI／人工接管等待超时允许收敛到 `PARTIAL` 或 `FAILED`；超时检测及根据证据选择结果由后续服务负责。异常恢复从运行／验证进入 `RECONCILING`；等待和暂停恢复也先进入该状态。

数据库保存唯一的运行时转移矩阵，Python 服务查询同一张表，测试依据 TRD 独立列出期望值核对。资源授权、证据达标、真实超时等业务前提必须由可信调度器／验证器判断；“允许转移”本身不代表这些前提已经满足。API 没有向客户端开放任意设置状态的接口。

M1-14 为已调度、网关或证据域 Run 增加成功前提：在当前状态事务里用同一数据库快照核验已发布工件及完整的脱敏观察闭环。缺失、损坏、过期或存储故障阻止 `SUCCEEDED`。这里只判断工件完整性，独立业务验证与最终聚合仍由 M1-15 负责。

## 一次状态更新怎样完成

已有合法 Run 时，可信调用方使用：

```python
from webagent.state import transition

event = transition(
    settings.business_db,
    run_id="run-1",
    expected_state_version=0,
    target="RUNNING",
)
# 此时状态与事件均已提交，event 包含 event_id 和 state_version=1。
```

服务以 `BEGIN IMMEDIATE` 开始短事务，读取当前版本，再检查转移并执行带版本条件的 `UPDATE`。数据库触发器强制版本恰好加一，并在同一条更新语句中插入 `state_changed`。事件写入、关联写入或提交失败时，整个事务回滚。进程在提交后、响应前中断时，重试旧版本会得到冲突，调用方应查询／重放已有结果，不能盲目重复外部动作。

需要同时改动额度、等待记录等本地数据时，在已有 `transaction(db)` 中调用 `transition_in_transaction()`；其返回值在提交前是临时结果，不能提前发送给客户端。浏览器、模型、网络和长文件操作放在事务之外。正常 SQL 状态更新也受触发器保护；迁移／管理员任意改结构不属于运行时安全边界。

版本不匹配抛出 `STATE_CONFLICT`，共用 API 错误适配器映射为 **409** 并附当前契约／状态版本；非法转移、未知状态、无效参数为 **422**，不存在的 Run 为 **404**，忙锁为 **503**。当前通过测试专用路由验证状态转移的 409 映射；任务准备 API 已在 M1-04 接入，执行控制接口仍属后续阶段。

进入 `WAITING_HANDOFF` 需要带时区的 `handoff_deadline`，退出时清除；`blocked_reason` 与转移一起记录，默认清空。首次运行设置 `started_at`，终态设置 `ended_at`，后续不会覆盖首次开始时间。

## 业务事件

事件信封固定包含 `event_id / task_id / run_id / event_type / state_version / occurred_at / payload`。时间为 UTC。状态事件由数据库自动生成；其他事件用 `append_event()` 在调用方的短事务中追加：

- `ActionEvent`：动作类型、步骤 ID、尝试状态、证据 ID。
- `WaitingEvent`：等待 ID、原因、可选截止时间。
- `ResultEvent`：结果引用、与当前终态一致的 outcome。

这些模型拒绝额外字段和原始图／模型消息。引用对应的实体存在性及证据授权由后续业务服务校验，调用者须提供已脱敏的业务元数据。`append_event()` 同样比较期望状态版本。状态事件不能手工经此入口追加。

`AUTOINCREMENT` 让**已提交事件**的 ID 在整个业务库中严格递增，跨 Run 共用序列；编号不保证连续。回滚的临时编号可能复用，所以禁止对外发布未提交的 ID。事件表禁止更新／删除，当前不做清理、截断或内存事件缓存。

## SSE 读取和重连

所有 SSE 请求也必须通过本机认证，响应标记 `Cache-Control: no-store, no-transform`。先启动 API，然后从项目根目录执行；若使用自定义数据目录或端口，终端应导出与 API 一致的环境变量：

```sh
PYTHONPATH=backend .venv/bin/python - <<'PYTHON'
import os
import httpx
from webagent.config import Settings
from webagent.security import load_or_create_token

headers = {"Authorization": "Bearer " + load_or_create_token(Settings.from_env().data_dir)}
last_event_id = None  # 重连时改为真正处理过的事件 ID；不要猜测。
if last_event_id is not None:
    headers["Last-Event-ID"] = str(last_event_id)
url = f"http://127.0.0.1:{int(os.environ.get('WEBAGENT_API_PORT', '8000'))}"
with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=None) as client:
    with client.stream("GET", "/v1/events") as response:
        response.raise_for_status()
        for line in response.iter_lines():
            print(line, flush=True)
PYTHON
```

令牌从权限受检的私有文件读取，仅进入请求头；不要打印 `headers`。可给 `client.stream()` 添加 `params={"task_id": "实际任务ID", "run_id": "实际运行ID"}` 过滤事件。按 Ctrl-C 停止监听。

浏览器代码使用现有 `frontend/src/api.ts`，由 Vite 在服务端注入认证：

```ts
import { apiFetch } from './api'

// lastProcessedId 来自真正处理过的 SSE 帧；重连时继续传入。
const response = await apiFetch('/api/v1/events', {
  headers: lastProcessedId ? { 'Last-Event-ID': lastProcessedId } : {},
  signal: controller.signal,
})
if (!response.ok || !response.body) throw new Error('事件连接失败')
// 后续按 SSE 帧解析 response.body，处理 event、id、data，并保存已处理 ID。
```

这是连接片段，完整前端事件消费仍待接入。不要改用裸 `EventSource`：它无法添加代理要求的 `X-WebPilot-Client` 请求头，也不应将令牌放进 URL。访问规则见 [本机访问与网络安全](network-security.md)。

新数据库没有事件时连接保持等待，不会创建演示任务。运行 `pytest` 的 SSE 测试会自动创建隔离的合成任务、启动临时 API，验证后关闭，不写用户的 `data/`。

- 不带游标或空游标，从头读取。`Last-Event-ID=N` 只读取 `event_id>N`；非数字、负数、超出 SQLite 整数范围或超过当前全库最大 ID，返回 422。
- 可按 `task_id`、`run_id` 过滤，两者同时出现取交集；不存在／不匹配的对象产生空流。游标始终是全库 ID，过滤后编号跳跃正常；改变过滤条件需重新选择游标。
- 每个事件输出 `id:`、`event:`、`data:` 和空行；`data` 是单行 UTF-8 JSON，不包含原始 `payload_json` 字段。流式客户端应按 `event:` 字段分发 `state_changed` 等命名事件。
- 首帧设置 `retry: 1000`，每 15 秒无数据时发送无 ID 的心跳注释。每批最多 100 条，空闲每 0.2 秒查询一次；SQLite 读取在线程池中进行，不占用异步事件循环。
- 每批读完即关闭连接，向慢客户端发送时不持有数据库事务；没有无限增长的内存事件队列。断开连接会取消生成器。读到末尾后继续等待新提交的事件，终态不会关闭全局订阅。
- 按“至少一次”处理重连：客户端保存已处理 ID 并据此去重。服务重启后仍从业务库重放。流开始后的数据库故障会断开流，客户端使用最后处理的 ID 重连；不会在 SSE 中伪造成功或推进游标。

帧格式及 `Last-Event-ID` 重连语义遵循 [HTML SSE 规范](https://html.spec.whatwg.org/multipage/server-sent-events.html)，触发器使用 [SQLite 触发器语义](https://www.sqlite.org/lang_createtrigger.html)。正式前端事件消费、调度和两库恢复仍属于后续 M1 子任务；任务准备 API 已实现。
