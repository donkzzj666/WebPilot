# 运行时验证与结果聚合

M1-15 在模型候选结果和业务终态之间增加验证边界。固定契约决定要检查什么，原始证据决定字段是否有支持，结果聚合器决定是否成功。模型的成功声明、图运行结束和页面加载完成都不产生业务成功。

## 职责边界

| 组件 | 输入与责任 |
| --- | --- |
| 规则验证器 | 只读冻结契约、候选业务字段和已验证证据快照；核对身份、版本、范围、数值、字段和回执 |
| 语义验证器 | 独立调用上下文；辅助判断主题相关性和摘要是否受到原文支持 |
| 证据读取器 | 从已登记工件读取真实字节，核验归属、可用性、长度与哈希；不接受候选结果自带的证据正文 |
| 结果聚合器 | 重读持久事实和检查记录，计算成功、部分或失败，提交不可变结果和业务事件 |

运行时实现位于 `backend/webagent/verification/`：`models.py` 定义检查及结果 DTO，`rules.py` 是纯规则检查，`semantic.py` 管理独立模型上下文和调用记账，`service.py` 负责可信读取与聚合，`routes.py` 只提供认证后的结果读取。

验证核心不接收浏览器、动作工具、执行器历史、数据库连接或写权限。语义模型输入只包含固定条件、待核实事实和脱敏证据；不接收执行器的自评、成功声明或完整对话。语义辅助与程序检查分别保留结论，语义 PASS 不能覆盖已发现的关键规则错误。

## 检查与逐字段证据

检查沿用 `m0-contract-v1`：`criterion_id`、`expected_rule`、`actual`、`verdict`、`evidence_ids`、`checked_at` 和 `checker_version`。结论只有 `PASS`、`FAIL`、`INSUFFICIENT`、`CONFLICT`。置信度不能替代通过条件。

每条检查绑定冻结 criterion，不能改写规则或增加一个宽松规则取代原条件。通过必须引用证据。字段关联使用有界 JSON Pointer 标识候选字段及证据中的对应位置；缺少位置、值不相符或来源冲突分别保留不足、失败或冲突结论。原始字节来自 M1-14 工件域，不能用 metadata 中的摘要代替正文。

`FieldBinding.result_path` 从 `proposal.items` 开始，例如 `/values/0/raw_value`；`evidence_path` 从原始工件的解析根开始，例如 `/report/revenue`。研究和监测还可用 `/coverage/...` 绑定来源、查询、页数及缺口，候选自己填写 `complete=true` 不证明实际覆盖。字段关联只声明定位，不携带实际值或通过结论。监测还要求 `/context/source_kind`、`/context/confirmed_boundary`、`/context/list_items`、`/context/detail_pages` 绑定原件的 `/verification_context/同名字段`，读取计数必须是原件中的有界非负整数。

对于不能确定解析的非结构化正文或不透明图像，系统应请求补证或保留不足，不把无法核实的声明默认为通过。原始截图和全灰展示副本不因存在文件就证明业务字段真实。未知规则同样不能自动通过。

## 聚合原则

成功必须同时满足：全部关键条件通过；全部必要输出与证据完整；证据属于当前 Run；没有越权、关键副作用及未决写入。聚合器从持久写意图获取副作用，包括同 Task 前次 Run 仍未查明的操作，不能只采用模型主动列出的操作 ID。

有可信交付但仍有缺口时，结果明确标为部分完成；没有可信交付或有关键违规时不能成功。非关键缺失在检查及未解决事项中明确列出。人工参与数量取自 Run，成功结果保留该数量，以区分自主完成与接管后完成。

财务字段核对主体、报告版本、期间、币种、指标和十进制数值。代码维护结果要求授权仓库、分支、文件范围、真实 PR、完整必需检查、独立检查和一致的交付 SHA。Grafana 保留时间窗口、变量和面板范围。研究结果保留来源、版本、截止时间、固定查询和摘要证据。监测结果保留计划槽、源标识、连续边界和缺口。

验证记录与结果须绑定契约摘要、候选摘要、Run 版本和证据摘要；最终提交前重新检查，避免验证后暂停、取消、工件损坏或写入状态变化仍提交成功。结果、业务终态与 `result_ready` 事件同事务提交；重复终结返回同一事实，不重复生成成功事件。

schema v14 追加 `run_verifications` 与 `run_results`，通过触发器保护不可变历史和契约／版本绑定。完整可执行契约通过状态服务或直接 SQL 进入成功／部分终态时，都必须存在匹配的聚合结果。老迁移测试中缺少验收契约的结构样本保留原状态矩阵用途，不是应用任务的成功入口。

## 服务调用与读取

```python
service = VerificationService(data_dir)
verification_token = service.begin(
    run_id, expected_state_version=running_version, execution_token=token
)
checked = await service.verify(
    run_id, proposal, bindings, expected_state_version=running_version + 1,
    execution_token=verification_token, provider=verification_provider
)
result = service.finalize(
    checked['verification_id'], expected_state_version=running_version + 1,
    execution_token=verification_token
)
```

已调度 Run 必须持有当前 Token；`begin()` 进入 VERIFYING 后返回同一运行的新状态版本资格，旧 Token 不能继续使用。规则检查无网络动作，语义请求使用独立的 `m1-15-verifier-v1` 提示词，只接受检查回复，仍消耗原 Run 的模型次数和活动时间。格式修复、超时与取消不会创建新的 Run 规避预算。

`GET /v1/runs/{run_id}/result` 返回 `result`、`field_checks`、`verification_id` 和 `assistance`（`autonomous` 或 `assisted`）。它继承本机 Bearer、精确 Host／Origin 校验及 no-store 响应，只接受 Run ID。没有公开提交检查结论、指定 outcome 或直接终结任务的 HTTP 接口。

当前策略保守地将非关键未解决条件也显式保留为部分完成；不会隐藏缺项。没有语义供应商、规则不支持、格式无法解析或证据不充分时，保留 `INSUFFICIENT`。原始自由文本只能通过明确定位或已支持解析关联，任意财务公式不会被当作代码执行。财务期间未冻结时明确返回不足，不推测起止日期；数值长度上限 1000 字符，计算使用独立 Decimal 精度上下文。重复键、非有限数值和损坏的结构化 JSON 不能作为已解析事实。

CONFIRMED 副作用除了持久状态，还需要当前 Run 原始回执明确关联 `operation_id`、`target`、`receipt` 和 `identity_ref`；只有无关工件、历史 Run 回执或无法解析的回执时，保留状态及不足项，不能成功。语义未通过的研究成果不会被算作可信局部交付。

## 验证方式与适用边界

独立探针位于 `scripts/verification/verify_verification.py`。它在临时数据目录启动自有本机 HTTP 服务，读取动态生成的合成结构化披露，将收到的真实字节保存为工件，再验证正确及被篡改的候选。探针不访问用户网站、真实模型账户或评测真值目录。

M1-15 不实现 M1-16 的 LangGraph 执行闭环，也不把本轮合成测试当作正式业务站点验收。原始工件及语义模型调用继续遵守 M1-14 脱敏策略和统一预算。实现与验收状态见 [M1-15 记录](../m1/records/M1-15.md)。
