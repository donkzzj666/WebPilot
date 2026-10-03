# 测试说明

## 组件与边界测试：unit/

`test_foundation.py` 检查配置、SQLite 能力、存储归属、API 和 Worker；`test_network_audit.py` 检查网络审计分类不会漏掉真实外联。部分测试会启动独立 Worker，因此这里的 `unit/` 也包含轻量组件边界检查。

从根目录运行：

```sh
.venv/bin/python -m pytest
```

`pyproject.toml` 将默认收集范围限定为 `tests/unit/` 和 `tests/storage/`，避免混入历史评测夹具。

## 浏览器与多进程集成

入口位于 `scripts/verification/`，统一通过 `./scripts/check.sh` 运行。测试会使用真实 Chromium 和独立测试数据库，结果写入本轮独立证据目录，路径以命令输出为准。完整命令见[脚本说明](../scripts/README.md)。

## M0 合成评测夹具：fixtures/m0/

`arithmetic.py` 是独立的合成计分例子，`acceptance_test.py` 是原有 GitHub Actions 验收入口，与产品后端无关。CI 已更新为 `python3 tests/fixtures/m0/acceptance_test.py`；夹具实现及断言保持原样，既有 M0 分支不受目录整理影响。

## 持久化测试：storage/

迁移空库与历史数据升级、失败回滚、结构漂移、外键、不可覆盖历史、同一业务键竞争、WAL 读写并发和进程中断。并发用独立进程与独立连接验证，不用内存锁模拟生产一致性。单独运行 `.venv/bin/python -m pytest tests/storage`。

M1-03 的状态转移与真实 HTTP SSE 测试位于 `tests/storage/test_state_events.py` 和 `test_sse.py`。SSE 测试需要允许回环端口监听，使用临时数据目录并关闭测试进程；执行 `.venv/bin/python -m pytest tests/storage/test_state_events.py tests/storage/test_sse.py` 可单独验证。

M1-04 的 `unit/test_task_compiler.py` 验证编译和场景边界，`storage/test_task_api.py` 验证幂等、并发、进程中断和历史保留，`storage/test_task_api_migration.py` 验证 v3 升级。`scripts/verification/verify_task_api.py` 启动临时 API，通过真实 HTTP 验证创建、澄清、修订与重启重放。

M1-05 的 `unit/test_model_schema.py` 验证四类输出、动作与场景约束，`unit/test_model_transport.py` 验证协议、图像、错误和异步取消，`storage/test_model_adapter.py` 验证调用日志、修复上限、Run 绑定、计量、共享截止时间和 v4→v5 升级。`scripts/verification/verify_model_adapter.py` 通过真实回环 HTTP 补验挂起响应、持续空白及取消后的连接关闭，无需真实模型密钥。

M1-07 的 `unit/test_secret_store.py` 检查 OS 桥接、引用范围、失败分类和无明文回退；`storage/test_settings.py` 检查接口、版本竞争、旧 Run 绑定、密钥轮换、发布补偿和 v5 升级。`verify_secret_store.py` 实测独立临时 Keychain，`verify_settings.py` 实测本机 HTTP、Vite 和 Chromium 设置页。

M1-06 的 `unit/test_natural_compiler.py` 验证语义建议的依据、场景、来源与权限边界；`unit/test_task_extraction.py` 验证编译传输、格式与时限；`storage/test_natural_tasks.py` 验证缺参版本、幂等、并发、崩溃及配置快照。`verify_natural_tasks.py` 通过真实回环 HTTP 串起 API、SQLite 和可控模型服务，不读取真实凭据或调用付费模型。

M1-08 的 `unit/test_session_auth.py` 检查 AES 认证、文件安全与密钥故障，`unit/test_session_manager.py` 检查生命周期与取消竞态，`storage/test_browser_sessions.py` 检查所有权、状态事件、容量与升级。`verify_browser_sessions.py` 使用真实有界面 Chromium 检查隔离、关闭、杀进程和重建；`verify_auth_keychain.py` 在独立临时 Keychain 中验证浏览器专属密钥空间。

M1-12 的 `unit/test_local_api_security.py` 覆盖私有令牌、全路由认证、Host／Origin 和请求元数据；`test_network_policy.py`、`test_network_proxy.py` 覆盖 DNS、地址分类、实际对端、精确基准端点、代理凭据与 HTTP 消息边界。API 业务测试用 `tests/api_support.py` 显式注入测试策略和认证，不按 TestClient 自动跳过安全层。

真实 API 攻击、生产代理与两种 Chromium 模式见 `verify_api_security.py`、`verify_network_boundary.py`。被阻断目标使用 TCP／UDP 接收计数，代理拒绝记录与页面结果交叉核对；`no-cors` 能解析代理的 403 响应，不应据此误判为目标已收到请求。工件摘要排除 `.security/`，禁止收集会话令牌。

M1-09 的 `verify_login_sessions.py --output-dir ...` 使用真实 HTTP、私有 Unix RPC、有界面 Chromium、生产代理和 AES，验证合成账号的正确／错误／过期登录；已纳入 `check.sh`。`test_identity_sites.py`、`test_login_service.py`、`test_login_rpc.py`、`test_identities.py`、`test_identity_api.py` 覆盖固定信号、并发确认、原子发布、权限和凭据拒收。

M1-10 的 `unit/test_scheduler_resources.py` 检查站点／仓库规范化和固定资源顺序；`unit/test_queue_worker.py` 检查执行器续约、失败、撤销及退出。`storage/test_scheduler.py` 检查持久领取、等待、资源扩展、旧 epoch、隔离与迁移，`storage/test_scheduler_sessions.py` 检查登录和 Run 共用名额、预留原子兑现、scope 与数据库外键，`storage/test_scheduler_api.py` 检查只读状态、权限和拒绝任意执行控制。`verify_scheduler.py` 通过竞争进程、真实 SQLite 和 Chromium 验证持久恢复；M1-16 的默认 Worker 执行边界见[图循环说明](../docs/development/graph-loop.md)。

M1-11 的 `storage/test_budgets.py` 覆盖配额及预算账本，`test_budget_scheduler.py` 覆盖同事务领取、等待、隔离和恢复，`test_budget_api.py` 覆盖只读及权限；`unit/test_budget_deadline.py` 验证独立取消与持久撤权顺序，`test_budget_integration.py` 验证真实模型／Worker 集成与晚回执。真实心跳间强杀及截止探针见 `verify_budgets.py`。

M1-13 的 `test_gateway_store.py` 验证事务日志与权限，`test_gateway_browser.py` 验证固定动作、定位和截图检查，`test_gateway_service.py` 验证完整派发顺序，`test_gateway_downloads.py` 验证一次性下载许可。`verify_gateway.py` 在自有 WebArena HTTP 夹具与真实 Chromium 上复验；写入只发生在合成夹具。

M1-14 的 `unit/test_evidence_redaction.py` 覆盖文本凭据、Unicode、已知密钥、冻结语义拒绝、编译输入与不透明截图；证据存储／API／模型测试覆盖原子文件、索引提交、不可变哈希、原件隔离、路径与权限、过期及磁盘满故障。`verify_evidence.py` 使用临时数据库、合成页面与真实回环 HTTP 验证，不读取用户凭据或连接付费模型。缺失、损坏或过期的工件会阻止证据参与成功；完整条件聚合由 M1-15 的验证服务接入。

M1-15 的 `unit/test_verification_rules.py` 和 `unit/test_verification_semantic.py` 覆盖四种结论、原始字段绑定、场景范围、独立上下文、无动作权限、输出校验、预算与过期资格。存储测试覆盖不可变检查／结果、资格与事件原子提交、并发终结及绕过拒绝。`verify_verification.py` 使用隔离 SQLite 和真实本机 HTTP 披露／模型协议；候选答案来自实际响应字节，负例通过改值、缺件和冲突构造，不加载评测真值。

M1-16 的 `unit/test_graph_context.py` 检查精确脱敏视图、上下文裁剪、工件链和禁止敏感图状态；`test_graph_executor.py` 检查冻结配置、可信依赖注入、模型未配置健康及前置等待；`test_graph_runtime.py` 检查异步节点与持久路由。`storage/test_graph_store.py` 检查检查点／进度幂等、资格、最新验证摘要、真实事件绑定与不可变历史；`test_graph_source.py` 检查完整可见 JSON 的原始位置解析；`test_graph_api.py` 检查认证后的只读进度和有界分页。

`verify_graph.py` 实测受管 Chromium、网关与原始证据、可替换的 HTTP 模型协议、补证／预算终止、持久等待和两个实际进程间的恢复。它不使用评测真值，不把网页加载或图 END 当成功，FR-01 安全等待恢复不替代 M1-17 的 FR-02 崩溃提交窗口测试。结构与限制见[图循环说明](../docs/development/graph-loop.md)。

M1-17 的 `test_graph_recovery.py`、`test_graph_crash_runtime.py` 及恢复浏览器测试检查两库引用、真实对象版本、已验收条件、只读不确定动作查证、终态图修复与当前代号。`verify_recovery.py` 进行真实进程崩溃／保存故障验收；详见[恢复说明](../docs/development/recovery.md)。

M1-18 的 `test_run_controls.py`、`test_run_control_api.py` 检查控制受理／完成、版本、幂等、不可变历史及新 Run 关联；`test_control_budget_admission.py` 检查请求受理后阻止新派发，已有尝试仍保留扣额；`test_run_controls_runtime.py` 检查真实 SQLite、图中断、模型／动作／观察和 Worker 协调。真实 HTTP／浏览器／进程崩溃联调由 `verify_controls.py` 覆盖，实际通过范围以 [M1-18 记录](../docs/m1/records/M1-18.md) 为准。

M1-23 的写入协议测试按边界分布：

- `unit/test_write_protocol_models.py` 检查稳定业务键、规范化目标与有界查证事实；`storage/test_write_protocol.py` 检查四态意图、并发登记、原始证明、不可变历史、迁移及单次消费的重试许可。
- `storage/test_write_gateway_service.py` 与 `unit/test_write_gateway_browser.py` 检查可信授权、真实证据发布、当前页面复核、缺失／损坏证明、站点节流与取消；查证只允许观察和 GET／HEAD，不能重放已保存的浏览器动作。
- `storage/test_write_reconciliation_queue.py` 检查关联新 Run 的只读查证资格、资源隔离、人控、旧 epoch 和在途尝试；`test_write_protocol_api.py` 检查认证、有界分页与私有事实不外泄，诊断 API 不能安装适配器或清除 UNKNOWN。
- `unit/test_write_graph_integration.py` 使用真实 StateGraph、异步 SQLite 检查点与业务账本，检查写入后先查证再进入下一节点、UNKNOWN 持久暂停、NOT_APPLIED 经新决策才尝试，以及提交后取消、无效证明、当前结果聚合和历史证明拒绝。
- `unit/test_writes_probe.py` 检查独立测试站的稳定键、提交绑定、故障标记、账本保留与逐次预算断言。

真实浏览器与跨进程验收使用 `scripts/verification/verify_writes.py`：七种场景覆盖意图提交后中断、外部已完成但回执未存、结果未知、对象改变、版本改变、查证期间改变和提交后取消。外部测试站的独立账本在 Worker 被真实 SIGKILL 后保留；新进程通过页面原件查证，HTTP 接收记录交叉检查是否发生重复提交。节点重入和关联 Run 不更换业务操作键；每次重新派发必须有新的步骤、预算扣额及当前有效的 NOT_APPLIED 证明。

证明的字节、哈希、身份、目标、版本、会话和 epoch 都参与门禁，不能仅凭 Playwright 返回或一个回执字符串认定成功。查询和等待不重置预算或截止，撤权与取消不表示外部写入已撤销。测试只修改自有合成站点和临时数据，不访问用户浏览器、真实业务账号、付费模型或评测真值。运行方式见[脚本说明](../scripts/README.md)，实际通过范围以本轮报告为准。

M1-19 的 `test_observability_logging.py`、`test_observability_graph.py` 检查有界并发日志、敏感字段拒收、真实持久引用、故障不改业务结果及只读连接。`test_observability_http.py` 检查请求 ID、幂等重放与安全标准日志；`test_observability.py`、`test_observability_api.py` 检查一致快照、分页、未知值、精确费用、队列／恢复来源、租约健康与认证。价格快照及实际模型／语义调用费用另由 pricing 和 semantic 测试验证。真实浏览器／模型 HTTP／进程验收使用 `verify_observability.py`；辅助 guards 检查探针判定逻辑，不能替代真实流程证据。

M1-20 的 `storage/test_task_catalog.py` 检查有界只读任务列表、倒序游标、并发插入、参数拒绝和本机认证；`unit/test_task_entry_probe.py` 检查探针的安全摘要、敏感标记扫描、错误脱敏和 owned 模型 HTTP。前端 `settings/identity-contracts.test.ts`、`tasks/contracts.test.ts`、`tasks/client.test.ts` 通过 Node 原生 TypeScript 执行，检查响应校验、身份 realm／来源匹配、显式权限、对象 ID 绑定及组件内幂等请求，不增加测试依赖。

`verify_task_entry.py` 实际从 UI 提交明确自然任务、集中补参和完整修订，检查真实 409／422、非秘密草稿保留、重载后的 SQLite 状态、模型数据发送说明、价格和凭据锁定。它另通过真实 LoginService／私有 RPC／ManagedBrowser 在 owned 合成登录站验证错账号不发布身份、正确账号历史记录及公网界面不能选择 `webarena` 身份。配置和登录准备不是执行成功；探针没有 QueueWorker、Run 或真实写入。内存凭据／加密密钥替代仅限测试，不提供生产明文回退。该探针纳入统一检查；实际验收范围见[M1-20 记录](../docs/m1/records/M1-20.md)。

M1-21 的 `storage/test_workspace.py` 验证单次只读快照、任务／Run 绑定、冻结条件、预算、队列、证据投影及 64 位事件分页；`frontend/src/workbench/` 的 Node 测试验证流解析、重复／乱序、版本与回执目标绑定。`verify_workbench.py` 才是实际 UI 与 Worker 执行验收；辅助探针测试不能替代真实流程。传输故障和站点门控夹具均在报告明确标注，动作预算通过实际执行耗尽；证据限过滤衍生物，不导出内部推理或凭据。见[M1-21 记录](../docs/m1/records/M1-21.md)。

M1-22 的 `storage/test_results.py` 验证结果完整性、任务／运行／契约绑定、历史分页、只读性、失效证据和未决写入；`frontend/src/results/` 测试响应绑定、成功前提、受控内容哈希及中性 PNG。`unit/test_results_probe.py` 仅测试探针辅助逻辑，实际页面验收使用 `verify_results.py`。见[结果页说明](../docs/development/results.md)。

M1-24 的 `unit/test_fault_acceptance_probe.py` 检查固定故障选择、状态与事件对账、提交前后故障、首错停止及报告哈希边界；它不替代真实进程验收。`verify_fault_acceptance.py` 在隔离数据域执行同一 Run 的实际 API／Worker 故障及界面对账，并复跑既有故障探针。历史写入授权的新增回归及修复见[M1-22 复验补记](../docs/m1/records/M1-22-revalidation-20261003.md)，故障范围见[说明](../docs/development/fault-acceptance.md)。

M1-25 的 `unit/test_arxiv_direct_parser.py` 检查严格原件格式、版本、UTC 日期、作者、歧义和明确截断标记；`storage/test_arxiv_direct_source.py` 检查实际观察、完成导航和内容页扣额对来源完整性的约束。`unit/test_readonly_loop_probe.py` 检查隔离目录、任务契约、真实计量、首错保留及进程退出门槛。组件通过不能替代公开网站和真实模型运行；`verify_readonly_loop.py` 是单独显式入口，自动通过后仍等待人类原页面复核。见[只读闭环说明](../docs/development/readonly-loop.md)。
