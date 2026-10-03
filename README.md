# Browser Agent

本地浏览器 Agent 的执行基础。当前代码覆盖 **M1-01～M1-25**：项目骨架、核心持久化、状态／事件基础、任务 API、模型适配器、自然语言编译、配置／密钥存储、受管浏览器会话、登录身份确认、持久队列／统一资源租约、统一预算／独立截止、结构化浏览器动作网关、证据保存／脱敏／受控读取、运行时验证与结果聚合，自定义 LangGraph 执行循环，两类检查点与只读崩溃恢复，以及暂停／继续、取消、关联重跑、写入意图／UNKNOWN 查证协议、本地日志／诊断／指标及任务入口／模型配置／账号准备界面、执行工作台与实时进度、结果与证据页面，以及基础机制集成故障验收和第一个真实公开只读闭环。项目使用 React/TypeScript 前端、FastAPI API、独立 Python Worker、业务 SQLite 与 LangGraph 检查点 SQLite，并提供可重复启动和验证入口；各项最终验收状态见 [M1 清单](docs/m1/README.md)。

工作台提供任务入口、执行进度、结果与证据、模型配置与账号准备，并显示真实 API 健康状态。准备完成后需明确确认启动，执行区展示实际子目标、预算、队列、页面证据和异步控制回执，支持 SSE 重放及断线重同步；见[工作台说明](docs/development/workbench.md)。结果页展示实际字段值、四态验证、覆盖缺口、人工参与、历史运行和副作用；证据或读取失败不会显示完整成功，见[结果页说明](docs/development/results.md)。创建任务只保存准备状态与契约，不自动开始 Run；界面用法见[任务入口说明](docs/development/task-entry.md)。Worker 初始化检查点存储、会话管理器和持久调度代次，接收本机 API 转发的登录准备请求，并定期维护心跳及撤销过期执行资格。业务库已具备版本化迁移、状态机和持久事件，API 支持 SSE 重放及任务创建、查询、补参和幂等重试。默认 Worker 已装配图执行器，可领取可信内部代码入队、配置已冻结的只读 Run；只读崩溃恢复已接入。M1-18 已实现公开启动、暂停／继续、取消及关联重跑，并完成复验；[运行控制说明](docs/development/run-controls.md)及[运行控制验收](docs/m1/records/M1-18.md)记录实际进展。循环及适用范围见 [LangGraph 开发说明](docs/development/graph-loop.md)和[恢复说明](docs/development/recovery.md)。

## 第一次阅读

先按下方步骤安装和启动，再阅读 [目录与代码导读](docs/project-structure.md)。导读解释每个目录负责什么、前端如何访问 API，以及修改页面或接口时应从哪个文件开始。

- 想运行项目：从本页的“环境与安装”开始。
- 想理解代码：[目录与代码导读](docs/project-structure.md)。
- 想查看需求和进度：[文档导航](docs/README.md)。
- 想运行测试：[测试说明](tests/README.md)；日常统一入口为 `./scripts/check.sh`。

## 环境与安装

已验证环境：macOS 15.3.1 / Apple Silicon，Python **3.12.14**（实际链接 SQLite **3.53.1**），Node **24.19.0**，npm **10.8.2**。版本记录见 [构建清单](config/build-manifest.json) 和 [许可证清单](docs/m1/dependencies.md)。其他平台尚未验收。

从项目根目录执行：

```sh
./scripts/bootstrap.sh
```

脚本创建项目 `.venv`，按带 SHA-256 的 `requirements/requirements-dev.lock` 安装 Python 全部依赖，按 `frontend/package-lock.json` 执行 `npm ci`，将 Playwright 对应 Chromium 安装到 `.cache/ms-playwright`。它只修改项目目录，不修改系统 Python、Node 或全局 npm 包。首次安装需要连接 PyPI、npm registry 和 Playwright 官方下载源。

脚本先检查实际 Python 的 SQLite 修复版本、WAL 和 FTS5 查询能力。默认系统 Python 3.9 / SQLite 3.43 不可用于本项目。脚本优先寻找 Python 3.12 和 Node 24，也支持本机已有的 Codex runtime；无合适运行时或自动找到的 Python 不满足 SQLite 条件时，用绝对路径指定：

```sh
WEBAGENT_PYTHON=/absolute/path/to/python3.12 \
WEBAGENT_NODE=/absolute/path/to/node \
WEBAGENT_NPM_CLI=/absolute/path/to/npm/bin/npm-cli.js \
./scripts/bootstrap.sh
```

SQLite 允许 3.51.3 及以后，或官方修复的 3.44.6+（3.44 分支）、3.50.7+（3.50 分支）；不能单看系统 `sqlite3` 命令的版本。[官方 WAL 修复说明](https://www.sqlite.org/wal.html)

`requirements/requirements.lock` 是运行依赖；`requirements/requirements-dev.lock` 加入验证和锁定工具。两者均固定传递依赖及哈希。依赖升级应先重做受影响集成，再更新锁与清单。

## 分别启动三个进程

在三个终端中分别执行，每个终端都位于项目根目录：

```sh
./scripts/dev.sh api
```

```sh
./scripts/dev.sh worker
```

```sh
./scripts/dev.sh frontend
```

打开 [本机工作台](http://127.0.0.1:5173)。API 默认监听 `127.0.0.1:8000`，包括 `GET /health` 在内的所有 API 均要求本机 Bearer 令牌。前端通过 `apiFetch` 和 Vite 的 `/api/` 受限代理自动接入，用户无需复制令牌；令牌只由服务端读取，不进入网页、浏览器存储或 URL。Worker 在终端输出 `worker_ready` 后维护持久队列，不依赖 API 进程存活；模型未配置时仍可启动，`model_ready` 单独反映设置就绪情况。

API 和 Worker 启动时会检查并应用业务库的编号迁移；也可先执行 `./scripts/dev.sh migrate`。迁移机制、表结构和事务用法见 [数据库开发说明](docs/development/database.md)。

三个进程都可用 `Ctrl-C` 或 `SIGTERM` 独立停止。再次执行同一入口使用原数据目录；受管浏览器按需启动，普通 Worker 初始化不打开浏览器、不调用模型，领取符合条件的 Run 后才装配执行依赖。

```sh
# 检查 Python 实际链接的 SQLite、WAL、FTS5
./scripts/dev.sh doctor

# Worker 初始化检查后立即退出
./scripts/dev.sh worker --once
```

可通过环境变量指定 `WEBAGENT_API_PORT`、`WEBAGENT_UI_PORT`（1024–65535）。前端代理与 API 必须使用相同 API 端口，三个进程应导出一致的端口设置。`WEBAGENT_DATA_DIR` 指向绝对的本机目录，API、前端与 Worker 应使用同一目录；不指定时为项目 `data/`。入口只读取导出的环境变量，不自动加载 `.env`，示例见 [.env.example](.env.example)。入口固定使用回环地址，前端启动入口不接受额外 Vite 命令行参数。请使用 `127.0.0.1` 与配置中的精确端口访问；错误 Host／Origin、跨站网页请求会被拒绝。实现范围与验收限制见 [本机访问与网络安全](docs/development/network-security.md)。

状态转移、事件协议和 SSE 重连示例见[状态与事件开发说明](docs/development/state-events.md)。当前最新业务数据库版本为 **18**。任务 API 示例和准备版本说明见[任务 API 开发说明](docs/development/task-api.md)；默认使用确定性夹具编译器，也可显式启用自然语言编译；创建任务不会自动执行。

模型接口、严格输出、格式修复与调用计量见[模型适配器说明](docs/development/model-adapter.md)。适配器已可被后续服务调用，普通启动仍不调用模型。

工作台模型设置区支持后端允许的模型／服务地址、参数、静态 token 单价、数据发送确认、就绪原因和 macOS Keychain 密钥存储。设置操作不会请求模型，详见[配置与密钥说明](docs/development/settings.md)。已有 Run 使用固定版本，修改设置不会覆盖旧运行。

认证后的 `/v1/diagnostics/runs/{run_id}` 和 `/v1/metrics` 提供只读账本诊断与指标，`/v1/health/worker` 单独检查持久 Worker 心跳；`/health` 只说明 API 与本机存储可用，不代表任务成功。日志、独立分页游标、冻结价目和未知用量的说明见[观测开发说明](docs/development/observability.md)。M1-19 与 M1-20 沿用 schema v18 和现有依赖；任务卡、缺参、冲突核对及账号界面见[任务入口说明](docs/development/task-entry.md)。

## 验证与证据

```sh
./scripts/check.sh
```

该入口检查运行时、依赖一致性、组件测试、前端类型和构建，运行有界面浏览器生命周期、认证密文／原生 Keychain、自然语言编译／缺参 HTTP 验收、设置页／HTTP、独立临时 Keychain 和模型适配器的本机 HTTP 故障验收、真实 LangGraph/AsyncSqliteSaver/Chromium 本机集成，并启动和停止三端做隔离与重复启动验证。另有双库检查点／真实进程崩溃／只读查证恢复，以及图执行循环／补证／预算终止／新进程等待恢复、运行时字段验证／独立语义／终态聚合、证据工件故障、持久队列／资源竞争、本机 API 攻击和浏览器网络边界探针，具体通过范围以报告为准。需要允许本机回环监听和启动 Chromium；不需要模型密钥或业务账号。证据每次写入全新的 `artifacts/verification/M1-25/` 子目录，失败记录也保留；观测、心跳、合成价目和安全日志探针的范围见[观测开发说明](docs/development/observability.md)。

单独验证或显示测试浏览器：

```sh
.venv/bin/python scripts/verification/verify_m1_01.py --headed
.venv/bin/python scripts/verification/verify_startup.py
.venv/bin/python scripts/dependencies/build_manifest.py --check
# 任务界面、缺参冲突与真实合成登录（无需业务账号）
.venv/bin/python scripts/verification/verify_task_entry.py --headed
# 同一 Run 的 API／Worker 提交崩溃、恢复及界面对账
.venv/bin/python scripts/verification/verify_fault_acceptance.py --output-dir /tmp/webpilot-fault-acceptance
# 在全新目录中按锁重建环境并复验（需要安装源网络）
.venv/bin/python scripts/verification/verify_clean_install.py
```

历史基础异步集成仍使用固定本机合成页面验证 saver。M1-16 另由 `verify_graph.py` 验证真实产品图、替换模型供应商及两个新进程间的安全等待恢复，使用独立图库并显式设置 `durability="sync"`；M1-17 的 `verify_recovery.py` 继续覆盖真实进程崩溃、双库保存窗口、身份／对象变化及只读恢复，详见[恢复说明](docs/development/recovery.md)。

所有入口关闭继承的 LangSmith/LangChain tracing。集成测试记录 Python socket、Playwright Node driver 和 Chromium NetLog；测试浏览器使用只提供固定页面、不向上游转发的本机代理，并将解析限制到回环地址。Chromium 后台服务请求会被记录并拒绝；IPv6 可达性套接字检查单独列示，不能声称系统层零数据包。这段描述针对历史基础集成夹具；当前产品网络策略、原生浏览器通道与验证范围见 [本机访问与网络安全](docs/development/network-security.md)，不以旧夹具证据替代当前验收。历史隔离范围见 [M1-01 验证记录](docs/m1/records/M1-01.md)；历史核心持久化证据见 [M1-02 验证记录](docs/m1/records/M1-02.md)，状态／事件历史验收见 [M1-03 记录](docs/m1/records/M1-03.md)，任务 API 历史验收见 [M1-04 记录](docs/m1/records/M1-04.md)，模型适配器验收见 [M1-05 记录](docs/m1/records/M1-05.md)，配置／密钥验收见 [M1-07 记录](docs/m1/records/M1-07.md)，自然语言编译验收见 [M1-06 记录](docs/m1/records/M1-06.md)，受管会话历史验收见 [M1-08 记录](docs/m1/records/M1-08.md)。

## 目录与责任

| 路径 | 用途 |
|---|---|
| `frontend/` | React/TypeScript 任务入口、模型／账号配置与 Vite 代理 |
| `backend/webagent/` | API、独立 Worker、运行时检查与存储初始化 |
| `requirements/` | Python 依赖声明与固定版本／哈希锁 |
| `data/business.sqlite3` | 业务库位置；由编号 SQL 迁移管理核心实体 |
| `data/.security/` | 私有本机 API 令牌目录；不提交到 Git，不收入验证证据 |
| `data/evidence/` | 本机受限原件与展示副本、不可变内容哈希；不能通过路径公开读取 |
| `data/graph.sqlite3` | Worker 的 AsyncSqliteSaver 所有，不混用业务检查点 |
| `scripts/` | 常用安装／启动／检查入口；内部按 `verification/`、`dependencies/` 分类 |
| `tests/unit/`、`tests/storage/` | SQLite 修复门槛、存储边界、迁移、并发、进程及网络审计分类测试 |
| `tests/fixtures/m0/` | M0 合成计分夹具与 GitHub CI 验收，不是产品业务代码 |
| `config/` | 可复核的依赖／运行时／浏览器版本清单 |
| `backend/webagent/evidence/` | 原子工件、脱敏策略、持久故障门与 ID 读取接口 |
| `backend/webagent/budgets/` | 统一累计、上海日配额与独立截止控制 |
| `backend/webagent/graph/` | 自定义执行循环、可信依赖装配、检查点／上下文、字段绑定和只读进度 |
| `backend/webagent/writes/` | 稳定写入意图、受限只读查证、证明和逐次派发关联 |
| `backend/webagent/observability/` | 安全元数据日志、认证只读诊断／指标与 Worker 心跳健康 |
| `data/logs/` | 有界 API／Worker JSONL 诊断；不是业务事件或成功回执 |
| `artifacts/verification/M1-23/` | 写入协议历史测试输出；各次报告继续保留 |
| `artifacts/verification/M1-19/` | 本地观测历史证据，保持原样 |
| `artifacts/verification/M1-20/` | 任务界面及本轮完整验证每次独立输出，实际结论以报告为准 |
| `docs/m0/`、`experiments/` | 已有契约、准备材料和历史实验；不由运行服务加载 |

统一验证使用合成服务，不连接付费模型、不访问正式任务或独立答案、不写入真实业务网站。FR-01 的异步图、模型适配器替换和安全等待恢复探针见 [LangGraph 开发说明](docs/development/graph-loop.md)；FR-02 的只读崩溃与双库保存窗口见[恢复说明](docs/development/recovery.md)，FR-03 的写入丢回执、稳定业务键与 UNKNOWN 查证见[写入协议说明](docs/development/write-intents.md)。

自然语言任务使用 `compiler_mode: "natural_language"`，通过模型建议和程序校验生成任务卡，缺参后可版本化补充。示例与信任边界见[自然语言任务编译](docs/development/natural-tasks.md)。任务表单通过真实自然语言编译接入，允许来源、动作和实际契约均可查看；执行区要求核对契约后明确启动，创建任务不等于开始执行；用法见[工作台说明](docs/development/workbench.md)。

受管浏览器使用独立上下文与加密认证快照；会话丢失或重建后必须重查身份和业务状态。接口与边界见[浏览器会话说明](docs/development/browser-sessions.md)。

登录准备的 API 用法、受支持站点和隐私边界见[登录与身份说明](docs/development/identities.md)。密码和验证码只在受管站点窗口中输入；账号准备区可创建、查询、确认和关闭登录准备，显示错账号与历史身份；每个 Run 仍须重新核对身份和业务条件。

持久队列与统一租约的内部接口、两项活跃／四个上下文的共享容量、等待和恢复边界见[调度开发说明](docs/development/scheduler.md)。只读 `GET /v1/scheduler` 可检查队列及租约。Worker 的 `task_execution_enabled=true` 表示内部执行器已注册，API 健康仍按公开产品入口报告能力；创建任务不会自动入队执行。

结构化动作的定位、快照绑定、派发日志和超时保护见[网关开发说明](docs/development/browser-gateway.md)。M1-16 图循环通过受信任 Worker 内部调用网关、证据服务与 M1-15 验证器。认证后的 `GET /v1/runs/{run_id}/progress` 只读取结构化进度，不提供派发权限。

证据使用受限原件和不可变展示副本，详情见[证据开发说明](docs/development/evidence.md)。读取 API 只接受证据 ID；原始截图、PDF、HAR 默认不发送模型或公开导出。可选全灰截图副本保留尺寸但没有视觉上下文，不能作为坐标点击图像或视觉业务证明。正式验收证据应使用内部 `retain=True` 保留；普通运行工件默认 30 天后标记过期。

M1-23 用稳定业务键关联同一任务的写入，派发前记录意图与预算；已发生则查证复用，确定未发生且前提一致才允许新尝试，UNKNOWN 保持隔离并阻塞成功。认证后的 `GET /v1/runs/{run_id}/write-intents` 提供分页状态诊断。协议已接入可信图网关；默认 Worker 尚未安装真实网站写入适配器，自有测试站验收与真实 GitHub 业务接入的边界见[协议说明](docs/development/write-intents.md)和[验收记录](docs/m1/records/M1-23.md)。

结果页与本轮完整验证每次独立保存于 `artifacts/verification/M1-25/`，实际结论以报告为准。

基础故障验收已纳入统一检查，13 组／427 项及全新安装通过，涵盖真实提交强杀、预算保留、UNKNOWN、网络和证据故障；见[开发说明](docs/development/fault-acceptance.md)与[验收记录](docs/m1/records/M1-24.md)。历史写入授权修复见[M1-22 补记](docs/m1/records/M1-22-revalidation-20261003.md)。当前 M1 为 25/25 VERIFIED，全部子任务已勾选完成。

M1-25 新增固定版本 arXiv 原文解析器及显式真实只读验收入口，范围为 C1-01 调试输入派生的一篇指定论文元数据。真实 UI／DeepSeek／Worker／LangGraph 闭环、独立核验及用户原页面对照全部通过，完成标记已勾选；不计入正式评测成绩。`check.sh` 继续使用自有页面和本机模型 HTTP，不调用真实供应商。运行步骤、数据边界与限制见[只读闭环说明](docs/development/readonly-loop.md)，最终证据见[M1-25 记录](docs/m1/records/M1-25.md)。
