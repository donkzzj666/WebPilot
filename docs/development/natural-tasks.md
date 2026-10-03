# 自然语言任务编译（M1-06）

自然语言入口仍是 `POST /v1/tasks`，显式选择 `compiler_mode: "natural_language"`。省略此字段时保留 M1-04 的 `fixture` 模式，旧客户端和幂等回执继续有效。任务准备不启动浏览器，也不创建 Run；`READY` 表示契约通过准备阶段的程序校验。

## 一次完整提交

先通过设置页保存模型配置、密钥并确认数据发送说明。以下请求会调用所配置的模型一次；不需要将密钥放入任务正文。URL 是示例，用户应填入实际允许访问的来源；编译时不会访问该业务网站。

在项目根目录执行以下 Python 示例；`WEBAGENT_DATA_DIR` 和端口应与 API 进程一致。认证令牌只从私有文件读到请求头，不打印，也不传入模型：

```sh
PYTHONPATH=backend .venv/bin/python - <<'PYTHON'
import os
import httpx
from webagent.config import Settings
from webagent.security import load_or_create_token

headers = {"Authorization": "Bearer " + load_or_create_token(Settings.from_env().data_dir)}
body = {
    "compiler_mode": "natural_language",
    "instruction": "读取 ACME 的 2025 年年度财报，提取营收，币种美元。",
    "sources": [{
        "source_id": "acme-investor", "site_id": "acme",
        "origin": "https://investors.example.com", "path_prefix": "/reports",
    }],
    "start_urls": ["https://investors.example.com/reports/2025"],
}
url = f"http://127.0.0.1:{int(os.environ.get('WEBAGENT_API_PORT', '8000'))}"
with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=70) as client:
    response = client.post("/v1/tasks", json=body,
                           headers={"Idempotency-Key": "finance-acme-2025-1"})
    print(response.status_code, response.json())
PYTHON
```

模型可建议财务场景和 `entity_id=ACME`、`report_version=2025`、`period_type=annual`、`metrics=[revenue]`、`currency=USD`。程序只接受能在用户指令字面或有限枚举翻译中找到依据的建议。清楚但超出当前受控表达范围的指令也可能需要补参，不能靠猜测凑齐契约。

来源与起始 URL 始终由调用者显式声明。模型输出不能设置来源、写权限、账号、预算、验收规则或执行状态。URL 必须在声明的 origin 和路径范围内；路径编码、反斜杠、目录穿越和相似路径前缀不能绕过检查。来源列表不等于网站可用性证明，访问阶段的 DNS、重定向和实际连接策略见 [M1-12 本机访问与网络安全](network-security.md)。

响应沿用任务详情结构，并增加 `compiler`、`compilations`、`field_origins`、`provenance`、`clarification_questions`。不会把原始模型回答、推理文本或密钥写入调用日志。

## 缺参与补充

“读取 ACME 最近一期财报，提取营收，币种美元”不能确定报告版本；“ACME 或 BETA”不能确定对象。返回 `201`，任务状态为 `NEEDS_INPUT`，`missing_fields` 和 `clarification_questions` 给出需要补充的字段。具体字段以响应为准。

复用上例的认证客户端，把请求替换为下面的调用；`task_id`、版本和字段均使用实际响应值：

```python
response = client.post(f"/v1/tasks/{task_id}/clarifications", json={
    "contract_version": current_version,
    "values": {
        "parameters.report_version": "2025",
        "parameters.period_type": "annual",
    },
}, headers={"Idempotency-Key": "finance-acme-clarify-1"})
print(response.status_code, response.json())
```

只允许补充当前响应中请求的字段。补充采用严格的结构化值，不再次调用模型；每次有效补充产生一个新草稿版本，完整时产生同版本的不可变契约。旧版本请求返回 `409 CONTRACT_VERSION_CONFLICT`。原始指令和所有历史版本保留。

要修改已经明确的目标、来源或写入范围，请用 `POST /v1/tasks/TASK_ID/revisions`，提交 `contract_version` 和完整的新请求。替换不会继承遗漏的权限字段。存在非终态 Run 或正在编译的替换请求时，拒绝并发变更。

## 参数和信任边界

| 场景 | 必要参数与额外条件 |
| --- | --- |
| 财务 | 对象、报告版本、期间口径、指标列表、币种；“最新”等相对期间需澄清 |
| 代码修复 | 仓库、基础 SHA、工作分支、失败运行、检查项、独立规则；另需显式仓库写入策略和账号引用 |
| Grafana | 仪表盘、面板、变量、时区；另需明确起止 UTC 时间的 `time_scope` |
| 科研 | 查询、主题条件、UTC 截止时间、数量上限 |
| 监控 | 来源 ID、来源类别、基线标志、计划时间、已确认边界；读取数量受固定预算限制 |

显式 `parameters` 优先于模型建议，但同样接受严格类型、场景和契约校验。所有场景默认只读；只有代码修复允许显式仓库写策略，且参数必须与授权的仓库、分支、SHA 和检查项一致。写授权字段缺失时进入 `NEEDS_INPUT`，不会从“帮我修好”或网页文本推导权限。

网页摘录放在 `web_context: ["..."]` 中。它可以作为不可信背景传给模型，不能用来补齐用户尚未确认的对象、期间、权限等关键字段。指令中显式标记的网页段落、引用和代码块同样不作为参数依据。审计中用户／API 输入与 `web_content` 分开标记，后者的 `authorizes_execution` 始终为 `false`。没有格式标记的任意文本无法被程序可靠判断是谁写的，调用者必须保留输入来源标签。

## 配置、幂等和调用审计

编译使用请求开始时选定的不可变设置版本。之后更新模型参数或轮换密钥，不会改变已预留调用的快照。凭据只在需要调用时从 OS 存储读取，不进入业务数据库。

新增显式迁移 `0007_task_compilations.sql`。`task_compilations` 以请求范围和幂等键唯一预留一次调用，记录设置版本、配置摘要、独立提示词版本 `m1-06-compiler-v1`、用量、耗时和安全错误分类。它与执行期的 `model_attempts` 分开，不伪造 Run 或消耗某个 Run 的预算。

- 先提交 `STARTED` 预留，再释放数据库锁并调用模型；调用后在短事务中提交任务版本、调用结论和成功回执。
- 每个请求只调用一次；格式错误不自动修复或重试，准备调用总时限不超过 60 秒。供应商未返回的 token 数量和价格保持未知，不猜测费用。
- 成功和模型失败均保留可重放响应；同键同正文不会再次调用，不同正文返回 `409 IDEMPOTENCY_CONFLICT`。补参无需再次调用模型。
- 同键尚在 `STARTED` 时返回 `409 STATE_CONFLICT`。进程意外退出后，结果可能未知，因此保留该记录并阻止盲目重试；当前没有自动接管或删除机制。
- 编译失败后若用户明确希望重新尝试，需使用新幂等键。模型可能已经产生费用，HTTP 失败不代表没有调用。

任务 UI、队列、账号就绪检查、执行图与实际浏览器动作由后续 M1 子任务接入。这里的程序规则定义契约需要什么证据；真正的成功判定仍需执行阶段的独立证据与验收器。

全部任务读写入口均经 M1-12 的 Bearer 认证与精确 Host／Origin 检查，未认证请求为 401，来源越界为 403。本机前端使用 `apiFetch` 经受限 Vite 代理自动接入，网页不读取令牌。访问与网络验证范围见 [本机访问与网络安全](network-security.md)。

## 验证入口

```sh
./scripts/check.sh
PYTHONPATH=backend .venv/bin/python scripts/verification/verify_natural_tasks.py --output-dir /tmp/webpilot-natural-verification
```

组件测试和真实回环 HTTP 探针使用隔离 SQLite、合成凭据及可控模型响应，不访问用户已有密钥，不调用付费供应商。验收记录见 [M1-06](../m1/records/M1-06.md)。
