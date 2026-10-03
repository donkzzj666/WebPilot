# 脚本说明

所有示例都从项目根目录运行。

| 日常入口 | 用途 |
| --- | --- |
| `./scripts/bootstrap.sh` | 检查运行时，安装锁定依赖和浏览器 |
| `./scripts/dev.sh frontend` | 启动前端；另开终端启动 API、Worker |
| `./scripts/dev.sh api` | 启动健康接口、任务 API 与持久事件 SSE |
| `./scripts/dev.sh worker` | 启动独立 Worker |
| `./scripts/dev.sh migrate` | 显式升级业务数据库并报告版本 |
| `./scripts/dev.sh doctor` | 检查 SQLite、WAL、FTS5 |
| `./scripts/check.sh` | 依赖检查、组件测试、前端构建、浏览器集成和三端启动验证 |
| `./scripts/npm.sh --prefix frontend run build` | 使用项目选择的 Node/npm 构建前端 |

## 内部工具

初次使用只需要上面的入口。以下脚本供单独排查或维护使用：

```sh
# 真实浏览器与 LangGraph 异步集成；--headed 可显示浏览器
.venv/bin/python scripts/verification/verify_m1_01.py --headed

# 真实 HTTP 任务 API 与幂等、历史和重启重放
.venv/bin/python scripts/verification/verify_task_api.py

# 模型适配器：真实本机 HTTP、格式修复、限流及取消（无需密钥）
.venv/bin/python scripts/verification/verify_model_adapter.py

# 产品图循环：真实Chromium、本机模型HTTP、补证及新进程等待恢复
PYTHONPATH=backend PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright \
  .venv/bin/python scripts/verification/verify_graph.py \
  --output-dir /tmp/webpilot-graph-verification

# 写入意图与 UNKNOWN：自有测试站、真实 Chromium 和跨进程崩溃查证
PYTHONPATH=backend PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright \
  .venv/bin/python scripts/verification/verify_writes.py \
  --output-dir /tmp/webpilot-writes-verification

# 原生 Keychain：独立临时凭据库与合成密钥
.venv/bin/python scripts/verification/verify_secret_store.py

# 设置页、真实本机 HTTP 与版本冲突（内存凭据夹具）
.venv/bin/python scripts/verification/verify_settings.py

# 任务入口、自然编译、缺参与配置冲突、真实合成登录
.venv/bin/python scripts/verification/verify_task_entry.py --headed

# 三端启动、停止、重启与隔离验证
.venv/bin/python scripts/verification/verify_startup.py

# 在新目录按锁重装并运行完整检查，需要安装源网络
.venv/bin/python scripts/verification/verify_clean_install.py

# 检查依赖和浏览器身份是否漂移
.venv/bin/python scripts/dependencies/build_manifest.py --check
```

`verification/check_evidence.py` 在验证进程退出后检查报告内的工件哈希；`verification/network-audit.cjs` 是测试用的 Node 网络审计钩子。它们不由正常前端、API 或 Worker 启动流程加载。

`dependencies/build_manifest.py` 不带 `--check` 时会重写构建、浏览器和许可清单；只在有意维护依赖或目录布局后运行，不能靠重写清单掩盖意外漂移。

自然语言任务验收入口：`PYTHONPATH=backend .venv/bin/python scripts/verification/verify_natural_tasks.py --output-dir /tmp/webpilot-natural-verification`。它使用隔离的 API、SQLite 与回环模型服务；`./scripts/check.sh` 已包含该探针，完整证据目录以本轮命令输出为准。

`verify_browser_sessions.py --output-dir ...` 默认使用有界面 Chromium，验证上下文隔离、认证重建与窗口／进程丢失。`verify_auth_keychain.py --output-dir ...` 验证独立临时 Keychain 与真实 AES 密文，两者都已纳入 `check.sh`，因此完整检查会短暂显示合成测试窗口；不使用日常浏览器或已有凭据。

`verify_api_security.py --output-dir ...` 实测外部网页攻击和正常工作台访问；`verify_network_boundary.py --output-dir ...` 实测生产代理、DNS 重校验、TCP／UDP 探针、Service Worker 与原生实时通信。后者加 `--headless` 验证 headless-shell；完整检查会运行两种浏览器模式，明确覆盖它们不同的原生策略参数。

M1-09 的 `verify_login_sessions.py --output-dir ...` 使用真实 HTTP、私有 Unix RPC、有界面 Chromium、生产代理和 AES，验证合成账号的正确／错误／过期登录；已纳入 `check.sh`。`test_identity_sites.py`、`test_login_service.py`、`test_login_rpc.py`、`test_identities.py`、`test_identity_api.py` 覆盖固定信号、并发确认、原子发布、权限和凭据拒收。

M1-10 的 `verify_scheduler.py --output-dir ...` 使用竞争 Worker 进程、临时 SQLite、受管 Chromium 和合成本机页面，验证单实例、队列领取、资源竞争、等待、过期资格与上下文预留；已纳入 `check.sh`。M1-16 的正常 `dev.sh worker` 已注册图执行器，可领取可信内部代码配置并入队的只读 Run；内部接口和公开启动边界见[图循环说明](../docs/development/graph-loop.md)。

M1-11 的 `verify_budgets.py --output-dir ...` 使用临时数据库、真实 Worker 子进程强杀、挂起协程与虚拟时钟验证配额、累计和独立截止，已纳入 `check.sh`；不会操作默认业务库或账号。[预算说明](../docs/development/budgets.md)列出调用次序和作用域。

M1-13：`PYTHONPATH=backend PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright .venv/bin/python scripts/verification/verify_gateway.py --output-dir /tmp/webpilot-gateway-verification`。固定动作夹具探针已纳入 `check.sh`，无需真实账号或模型密钥。

M1-14：`PYTHONPATH=backend PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright .venv/bin/python scripts/verification/verify_evidence.py --output-dir /tmp/webpilot-evidence-verification`。该探针已纳入 `check.sh`，覆盖合成敏感正文／截图、原子保存故障、孤儿、真实 HTTP 读取与重启后的存储故障门；私有原件不列入导出工件。M1-14 交付时统一入口检查 17 份集成报告。实现与限制见[证据说明](../docs/development/evidence.md)。

M1-15：`PYTHONPATH=backend .venv/bin/python scripts/verification/verify_verification.py --output-dir /tmp/webpilot-verification-probe`。探针获取自有 HTTP 服务动态生成的合成原始字节，覆盖正确字段、假数值、缺证据、冲突、历史未决写入、越权、部分交付、人工标记、独立语义模型协议、认证结果读取与终态门禁。该探针已纳入 `check.sh`；历史验收见 M1-15 记录，技术说明见[运行时验证说明](../docs/development/verification.md)。

M1-16：`verify_graph.py` 使用隔离业务库／图库、自有动态 JSON 页面、实际本机模型 HTTP 协议和真实 Chromium，验证四种模型输出、错误页面／缺字段补证、预算终止和两个真实进程间的持久等待恢复。该探针已纳入 `check.sh`，统一入口检查 20 份集成报告；通过范围以本轮报告为准。图中每次异步执行显式使用 `durability="sync"`，M1-17 的 `verify_recovery.py` 增加真实进程终止、保存／磁盘故障、版本／事件失配及受控只读查证。详见[恢复说明](../docs/development/recovery.md)。详见[图循环说明](../docs/development/graph-loop.md)。

M1-18 的 `verification/verify_controls.py` 使用隔离 API、Worker、真实 Chromium、两类检查点及自有模型 HTTP 端点验证控制请求、在途动作和等待崩溃窗口。`check.sh` 遇到首个失败即退出；本轮剩余任务按清单顺序逐项验证，失败后中断后续任务。

M1-23 的 `verification/verify_writes.py` 使用自有 HTTP 测试站、独立外部对象账本、隔离业务库／图库和真实 Chromium。七种场景分别为意图已落库但未提交、提交完成但回执未存、结果仍未知、目标对象改变、前置版本改变、查证期间页面改变、提交后取消。探针在意图提交或实际外部写入窗口强杀独立子进程，再由新进程只读查证；也检查图节点重入与关联新 Run 沿用业务键，不重复创建外部变更。

该探针已纳入 `check.sh`。单独运行时使用新的 `--output-dir`，已有目录不会被覆盖；报告保存场景矩阵、进程事实、HTTP 请求和工件哈希。可以用 `--case physical-applied` 等固定场景名定位问题。查证证明来自实际页面的原始工件，缺失、损坏或对象／身份／版本不一致时不能授权重试；每次真实派发单独扣预算，GET 查证也受预算、站点节流和独立截止约束。取消保留已发生的外部内容，`UNKNOWN` 不能被普通继续或重跑清除。测试不使用用户账号、默认服务、真实 GitHub 写入、付费模型或评测真值，实际通过范围以对应报告为准。

M1-19 的 `verification/verify_observability.py --output-dir <新目录>` 验证两个实际 Run、独立 API／Worker、真实图检查点、配置与未配置价格、两层 HTTP 请求 ID、敏感 canary 日志扫描及 Python／Node／Chromium／代理的实际出站。额外账本夹具只验证恢复／UNKNOWN／隔离指标，不当作业务运行或物理写入。该探针已纳入 `check.sh`，统一入口在新的 M1-19 子目录检查 23 份报告；任一失败即退出。[开发说明](../docs/development/observability.md)列出健康状态和观测范围。

M1-20 的 `verification/verify_task_entry.py --output-dir <新目录> [--headed]` 使用真实 Vite／Chromium／FastAPI／SQLite、自然语言编译器、owned 模型 HTTP 与实际 LoginService／RPC／ManagedBrowser。内存 OS 凭据和认证加密密钥是明确的测试夹具；错账号、契约持久状态、409／422 和重载通过真实服务验证。它不启动 QueueWorker，不创建任务 Run、不执行真实写入，也不把合成 `webarena` 身份当成公网授权。请求正文、HAR、trace、登录 DOM／截图不导出。

本轮 `check.sh` 加入该探针和 Node 原生 TypeScript 契约／请求测试，该阶段统一入口在全新 `artifacts/verification/M1-20/` 子目录检查 24 份集成报告，遇到首个失败即退出。报告退出后另检查哈希，历史 M1-19／M1-23 证据不重写。界面和分页规则见[任务入口说明](../docs/development/task-entry.md)，最终通过范围见[M1-20 记录](../docs/m1/records/M1-20.md)。

M1-21 的 `verification/verify_workbench.py --output-dir <新目录>` 使用隔离 UI、API、Worker、合成模型 HTTP 与受管浏览器验证启动、实际进度、控制回执和重载；重复／乱序／断线等传输故障注入独立标注，不替代业务落库。统一 `check.sh` 当前在全新 `artifacts/verification/M1-24/` 目录检查 27 份顶层集成报告，遇到首个失败退出。见[工作台说明](../docs/development/workbench.md)和[M1-21 记录](../docs/m1/records/M1-21.md)。

M1-22 的 `verification/verify_results.py --output-dir <新目录>` 使用独立数据、自有 HTTP 原文、真实证据存储与验证聚合，再由实际 API／Vite／Chromium 验证结果页。准备、人工次数及副作用账本夹具明确标记，不声称完成公开业务闭环。见[结果页说明](../docs/development/results.md)。

M1-24 的 `verification/verify_fault_acceptance.py --output-dir <新目录>` 在同一实际 Run 上强杀暂停提交后的 Worker 和取消受理提交前／后的 API，再由新进程对账预算、动作、证据及工作台／结果页。它按固定子组复跑故障探针，首个失败即停止；子报告与数据库留在模式 0700 的 `.private/`／`.owned/`，公开汇总不导出秘密或原件。统一入口加入本项，当前报告写入 `artifacts/verification/M1-24/`。范围及待验部分见[故障验收说明](../docs/development/fault-acceptance.md)。

M1-25 的 `verification/verify_readonly_loop.py --data-dir <已配置且无任务的隔离目录> --output <全新目录> --headed` 通过 UI 创建和启动固定 arXiv 版本元数据任务，使用正常 Worker、真实供应商和公开网页；它可能产生模型费用，未纳入日常 `check.sh`。自动报告不会替人类勾选原页面对照。当前统一检查将 27 份本机集成报告写入全新的 `artifacts/verification/M1-25/` 子目录；真实闭环另有独立输出。见[运行说明](../docs/development/readonly-loop.md)及[M1-25 记录](../docs/m1/records/M1-25.md)。
