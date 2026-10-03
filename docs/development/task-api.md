# 任务创建、查询与幂等 API（M1-04）

本模块把显式输入保存为任务草稿，补齐字段后生成不可变契约。默认保留 **fixture 确定性编译器**，显式使用 `compiler_mode: "natural_language"` 可启用 [M1-06 自然语言编译](natural-tasks.md)。本文的请求样例是离线合成夹具；自然模式由用户明确声明来源，并通过模型建议和程序校验生成任务卡。两种模式均不自动创建／执行 Run。

## 目录与数据流

| 位置 | 职责 |
| --- | --- |
| `backend/webagent/tasks/models.py` | 请求、场景参数、权限与完整 TaskContract 的严格校验 |
| `backend/webagent/tasks/compiler.py` | 缺失字段识别、确定性夹具契约；提供来源／固定规则的测试配置 provenance |
| `backend/webagent/tasks/service.py` | 版本比较、补参／修订、事务性幂等响应和一致性查询 |
| `backend/webagent/tasks/routes.py` | HTTP 路由与幂等键校验 |
| `backend/webagent/db/sql/0004_task_api.sql` | 草稿修订与幂等响应表，由 v4 引入；当前完整业务库为 v8 |
| `tests/unit/test_task_compiler.py` | 四类场景的完整／缺失／错误输入与权限边界 |
| `tests/storage/test_task_api.py` | API、版本历史、并发、进程中断和响应重放 |
| `scripts/verification/verify_task_api.py` | 真实回环 HTTP 端到端验收 |

请求 → 严格 DTO 校验 → 幂等记录查询 → 版本／未结束运行检查 → 编译 → 任务、修订、契约、幂等响应同事务提交。查询在同一只读事务内读取任务及关联记录，避免把两个时刻的版本拼在一起。

## 接口

所有接口（含 GET）均要求 `Authorization: Bearer …`，未认证返回 `401 UNAUTHENTICATED`，Host／Origin 或浏览器来源越界返回 `403 FORBIDDEN`。本机页面使用 `apiFetch` 经 Vite 代理自动认证，不读取或保存令牌。具体规则见 [本机访问与网络安全](network-security.md)。

| 方法与路径 | 行为／成功状态码 |
| --- | --- |
| `POST /v1/tasks` | 创建完整或待补参任务，201 |
| `GET /v1/tasks/{task_id}` | 当前草稿／契约、缺失字段、当前及全部历史运行、契约与修订历史，200 |
| `GET /v1/tasks/{task_id}/runs` | `{task_id,runs}`，包含失败、取消及成功记录，200 |
| `GET /v1/tasks/{task_id}/contracts` | `{task_id,contracts}`，按版本排列的不可变完整契约，200 |
| `POST /v1/tasks/{task_id}/clarifications` | 仅填写已请求的字段；生成新准备版本，200 |
| `POST /v1/tasks/{task_id}/revisions` | 显式替换全部输入，产生关联新版本，200 |

`/revisions` 是对冻结最小接口的补充，用来明确区分“补缺失字段”和“改变目标／范围／授权”。创建、补参、修订均要求 `Idempotency-Key`。请求和所有嵌套 DTO 拒绝未定义控制字段。错误格式沿用 M0 的 `request_id/status/code/message/details/retryable/current_contract_version/current_state_version`，`X-Request-ID` 与正文一致，不返回原始内部错误。

## 用隔离目录试用

从项目根目录，在第一个终端创建隔离目录并启动独立 API：

```sh
export WEBAGENT_DATA_DIR="$(mktemp -d /tmp/webagent-m1-04.XXXXXX)"
export WEBAGENT_API_PORT=8001
printf '%s\n' "$WEBAGENT_DATA_DIR"  # 只显示目录，供第二个终端使用。
./scripts/dev.sh api
```

第二个终端先把 `WEBAGENT_DATA_DIR` 设置为上面显示的绝对路径，随后创建完整财务夹具任务。令牌只在 Python 内存中读取到请求头，不进入命令行参数或输出：

```sh
export WEBAGENT_DATA_DIR=/absolute/path/from/first/terminal
export WEBAGENT_API_PORT=8001
PYTHONPATH=backend .venv/bin/python - <<'PYTHON'
import os
import httpx
from webagent.config import Settings
from webagent.security import load_or_create_token

headers = {"Authorization": "Bearer " + load_or_create_token(Settings.from_env().data_dir)}
body = {
    "instruction": "读取本地财报夹具",
    "source_ids": ["local-fixture"],
    "scenario": "finance",
    "parameters": {
        "entity_id": "fixture-company", "report_version": "2025",
        "period_type": "annual", "metrics": ["revenue"], "currency": "USD",
    },
}
url = f"http://127.0.0.1:{int(os.environ.get('WEBAGENT_API_PORT', '8000'))}"
with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=10) as client:
    response = client.post("/v1/tasks", json=body, headers={"Idempotency-Key": "demo-finance-1"})
    print(response.status_code, response.json())
    if response.status_code == 201:
        detail = client.get(response.headers["Location"])
        print(detail.status_code, detail.json())
PYTHON
```

返回 `201`、`task.preparation_status=READY`、`contract_version=1` 和完整契约；`current_run=null`，代表准备完成，没有宣称执行成功。`Location` 可用于 GET 查询。

重复同一命令会得到相同响应；保留幂等键而改变 `instruction` 会得到 `409 IDEMPOTENCY_CONFLICT`。换用新键才表示新的创建意图。调用结束后对启动终端按 Ctrl+C 即可关闭测试 API。

创建缺参任务也可只提供 `{ "instruction": "读取本地财报夹具" }`，使用另一个幂等键。它返回 `NEEDS_INPUT` 和 `missing_fields`，不存在不完整的 TaskContract。

## 补参、版本与关键变更

响应有两个不同的版本概念：

- 顶层 `contract_version` 是**当前准备版本游标**，从 1 开始，每次成功补参／修订增加 1。把它原样放进下一次变更请求。
- `task.current_contract_version` 指向最近一次完整契约；未生成契约时为 null。完整契约沿用当次准备版本，所以完整契约编号允许跳号。

例如初始草稿版本 1 返回 `source_ids`、`scenario` 缺失，先提交：

```json
{
  "contract_version": 1,
  "values": {
    "source_ids": ["local-fixture"],
    "scenario": "finance"
  }
}
```

向 `/v1/tasks/{task_id}/clarifications` 发送此 JSON，并附认证头和新的幂等键。返回版本 2 和 `parameters.entity_id` 等缺失字段；再使用这些**精确字段路径**填值，即可在版本 3 生成契约。可分多次补充，不必一次填完。错误类型／值返回 422；过期版本返回 `409 CONTRACT_VERSION_CONFLICT`，响应附当前准备版本。

`values` 不能修改已知字段、增加任意控制字段或用一个 `parameters` 对象覆盖整个范围。目标、来源、参数或授权变化应向 `/revisions` 提交**完整的新创建正文，再加当前 `contract_version`**；省略的字段按新草稿默认值处理，不继承旧写权限。

任务的 `original_instruction` 永远保留最初指令；新版本的 `objective` 可以取新的明确指令。补参契约累积所有贡献请求的 provenance 和 SHA-256；完整替换重新建立来源链，旧契约及修订记录不变。

只要该 Task 仍有任一非终态 Run（包含 QUEUED、暂停、等待，且不只检查 current_run），变更返回 `409 STATE_CONFLICT`，要求先安全结束旧运行。本项不会假装已取消在途动作。历史失败 Run 仍绑定旧契约；新版本不抹去失败，也不原地重跑。新修订将旧 current_run 转入历史。

若新修订尚未完整，Task 为 NEEDS_INPUT，顶层游标是新准备版本，而 `contract` 仍可能是上一个完整契约。调用方应同时检查 preparation_status 和 missing_fields，不能因旧契约存在就执行新草稿。

## 幂等和持久化

`Idempotency-Key` 必须为 1–200 个可见 ASCII 字符，拒绝缺失或重复的同名请求头。正文可带 `idempotency_key` 以兼容冻结 DTO，存在时必须与请求头一致；传输键不进入正文摘要。

键按 HTTP 方法与目标路径划分范围：相同键可用于不同任务的修订，但同一创建路径使用同一键始终指向同一次创建。请求先填充 DTO 默认值，再以 UTF-8、键排序、紧凑 JSON 计算 SHA-256；对象键顺序无关，数组顺序保留。非有限数值被拒绝。

`api_idempotency` 保存键、请求摘要、任务 ID、首次状态码及完整响应正文。`task_revisions` 保存父版本、操作类型、完整草稿、本次提交、摘要、缺失字段和 provenance。两者均禁止更新、删除和替换，目前不设置过期时间。

幂等查询先于版本和运行状态校验。因此任务后来被修订，重试原请求仍返回原来的对象、状态码和正文；需要最新状态时使用 GET。并发相同请求由 SQLite 短写事务串行化，只有一份任务／响应落库，不依赖进程内锁。普通错误不占用幂等键；任务、契约或幂等记录任何一项写入失败，全部回滚。提交后响应丢失可使用原键安全重试。

## 场景范围与升级

夹具编译支持 `finance / operations / research / monitoring`；operations 分 `code_repair` 和 `grafana_read`。场景参数严格对应冻结契约。只接受 `source_ids=["local-fixture"]`，其声明范围是 `http://127.0.0.1:8765`；本项不要求此站点实际启动，因为不会访问它。

编译器的目标、输出字段和验收规则来自固定测试配置，并写入 `explicit_test_configuration` provenance。代码修复需要显式仓库写策略和身份引用，且仓库、分支、基准 SHA、必需检查相互一致；其他场景只能只读。身份引用不等于已完成网页登录核验；访问受 [M1-12 网络策略](network-security.md) 约束，身份就绪及执行条件仍由后续业务服务核验。

升级至 v4 保留既有任务、契约、Run 和事件。M1-02／03 通过底层辅助函数创建的历史任务可能没有准备修订：查询仍能展示它们，变更返回 409，避免编造过去的草稿／授权记录。

准备阶段的审计保存在 `task_revisions`，不向要求 run_id 的既有 SSE 通道伪造 Run 事件。[配置快照与密钥存储](settings.md)已在 M1-07 交付；[自然语言编译](natural-tasks.md)已在 M1-06 交付；任务启动／重跑、暂停／取消和任务 UI 属于后续 M1 子任务。
