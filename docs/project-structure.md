# 目录与代码导读

项目按职责分为前端、后端、依赖、测试、脚本、文档和运行数据。后端中的 API 和 Worker 是两个独立进程，执行、控制、恢复与写入查证各有独立模块；最终验收状态以 M1 清单为准。

## 目录地图

```text
webagent/
├── README.md                  # 先读这里：安装和启动
├── AGENTS.md                  # 协作约定与仓库地址
├── frontend/                  # 用户在浏览器里看到的页面
│   ├── src/
│   │   ├── main.tsx           # 挂载 React 应用
│   │   ├── App.tsx            # 本机健康、任务／配置／账号导航
│   │   ├── tasks/             # 输入、契约卡、补参、权限与持久列表
│   │   ├── ModelSettings.tsx  # 连接参数、静态价格、披露与密码输入
│   │   ├── IdentityReadiness.tsx # 登录准备与持久身份展示
│   │   ├── settings/          # 登录响应边界校验与契约测试
│   │   └── styles.css         # 页面样式
│   ├── vite.config.ts         # 开发服务与 /api 代理
│   └── package*.json          # 前端依赖及锁文件
├── backend/webagent/          # Python 应用包
│   ├── __main__.py            # api / worker / doctor 命令入口
│   ├── api.py                 # FastAPI 装配、健康接口和错误适配
│   ├── settings/              # 设置接口、版本快照、OS 密钥引用与就绪状态
│   ├── security/              # 本机令牌与 API 来源／身份校验
│   ├── network/               # 浏览器目的地策略、DNS／对端检查、认证代理
│   ├── identities/            # 登录准备、站点身份核验、账号引用和私有 RPC
│   ├── sessions/              # Chromium 上下文、归属、生命周期与认证密文
│   ├── budgets/               # 配额、不可变派发记账、单调累计与独立截止
│   ├── scheduler/             # SQLite 队列、执行资格、统一资源和 Worker 续约
│   ├── evidence/              # 原子工件、脱敏、不可变索引、故障门与 ID 读取
│   ├── verification/          # 原文解析、只读规则／语义检查、字段证据与结果聚合
│   ├── graph/                 # LangGraph循环、运行依赖、上下文和业务进度
│   ├── gateway/               # 固定浏览器动作、快照／权限核对与派发审计
│   ├── writes/                # 稳定写入意图、四态账本、原件查证与只读诊断
│   ├── models/                # 模型协议、输出校验、有限修复与调用账本
│   ├── tasks/                 # 请求 DTO、夹具编译、幂等事务、任务路由
│   ├── state.py               # 状态转移与乐观版本比较
│   ├── events.py              # 结构化事件追加与分页读取
│   ├── sse.py                 # 持久事件 SSE 重放
│   ├── errors.py              # 业务错误
│   ├── worker.py              # 独立 Worker 的启动与退出
│   ├── storage.py             # 业务库迁移入口
│   ├── db/                    # 连接、仓储、编号 SQL 迁移
│   ├── config.py              # 数据目录配置与 tracing 开关
│   └── runtime.py             # Python、SQLite、WAL、FTS5 检查
├── requirements/              # Python 依赖声明和哈希锁
├── tests/
│   ├── unit/                  # 日常组件与边界测试
│   ├── storage/               # 迁移、状态机、事务、并发与真实 SSE 重连
│   └── fixtures/m0/           # M0 合成夹具，供原有 GitHub CI 使用
├── scripts/
│   ├── bootstrap.sh           # 安装环境
│   ├── dev.sh                 # 启动一个组件
│   ├── check.sh               # 完整检查
│   ├── npm.sh                 # 用选定的 Node 执行 npm
│   ├── verification/          # 浏览器集成、启动与干净安装验证
│   └── dependencies/          # 依赖、浏览器与许可清单维护
├── config/                    # 构建版本和依赖身份记录
├── docs/                      # 文档导航、需求、阶段计划和验收记录
├── experiments/               # 本地历史实验，应用不加载
├── artifacts/verification/    # 测试报告、截图、合成数据库等证据
├── data/                      # 本机运行数据（自动生成，不提交）
├── pyproject.toml             # Python 项目元信息和 pytest 配置
├── .env.example               # 环境变量示例
├── .python-version            # 已验证的 Python 版本
└── .node-version              # 已验证的前端 Node 版本
```

安装后还会出现 `.venv/`（Python 环境）、`.runtime/`（选定的工具入口）、`.cache/`（下载缓存）、`frontend/node_modules/`（前端依赖）和 `frontend/dist/`（构建产物）。这些目录由工具生成，已通过 `.gitignore` 排除；日常写代码主要关注 `frontend/src/` 和 `backend/webagent/`。

## 三个进程怎样配合

```text
浏览器 → Vite 前端（5173）→ /api/health 代理 → FastAPI（8000）/health
                                              └─ 迁移并初始化业务 SQLite

独立 Python Worker → 迁移业务库、初始化图库 → 登录 RPC 与受管 Chromium
                         └─ 注册调度代次、维护心跳、领取可信入队Run → 自定义LangGraph
                                                                          ├─ 模型适配器
                                                                          ├─ 动作网关／证据
                                                                          └─ 独立验证／业务聚合
```

默认双库在 `data/business.sqlite3` 和 `data/graph.sqlite3`。当前前端显示 API 健康，提供任务输入、持久任务卡、缺参／修订、模型配置与账号准备；登录请求由 API 转发到 Worker，调度状态和图进度通过认证后的只读接口查询。Worker 已装配图执行器；任务卡创建不会自动执行，公开启动／暂停／继续／取消／重跑由 `controls/` 接入。默认 Worker 执行只读任务；写任务需要可信内部代码明确装配授权与查证适配器，普通 API 不能安装适配器或传入执行代码。

## 从哪里开始读代码

1. [dev.sh](../scripts/dev.sh)：看三个进程分别如何启动。
2. [App.tsx](../frontend/src/App.tsx) → [vite.config.ts](../frontend/vite.config.ts) → [api.py](../backend/webagent/api.py)：跟着一次健康查询理解前后端关系。
3. [config.py](../backend/webagent/config.py)和[storage.py](../backend/webagent/storage.py)：理解配置与业务数据库位置。
4. [worker.py](../backend/webagent/worker.py)：理解 Worker 的独立生命周期和检查点存储。
5. [test_foundation.py](../tests/unit/test_foundation.py)：查看组件行为和边界的可执行例子。

| 想修改什么 | 主要位置 |
| --- | --- |
| 页面文字、布局、状态显示 | `frontend/src/App.tsx`、`styles.css` |
| API 返回内容 | `backend/webagent/api.py` |
| 本地 API 鉴权、前端代理接入 | `backend/webagent/security/`、`frontend/src/api.ts`、`frontend/vite.config.ts` |
| 浏览器网络配置与实际出口 | `backend/webagent/network/`、`sessions/manager.py`；详见[网络边界说明](development/network-security.md) |
| 任务 UI、契约卡、权限与冲突确认 | `frontend/src/tasks/`；详见[任务入口说明](development/task-entry.md) |
| 结果、字段证据、历史运行与副作用 | `frontend/src/results/`、`backend/webagent/results/`；详见[结果页说明](development/results.md) |
| 实时工作台、控制回执、预算与页面证据 | `frontend/src/workbench/`、`backend/webagent/workspace/`；详见[工作台说明](development/workbench.md) |
| 任务创建、只读分页、补参、修订、幂等 | `backend/webagent/tasks/`；详见[任务 API 说明](development/task-api.md) |
| 模型／账号配置页面 | `ModelSettings.tsx`、`IdentityReadiness.tsx`、`frontend/src/settings/` |
| 模型设置、凭据和 Run 配置快照 | `backend/webagent/settings/`；详见[配置与密钥说明](development/settings.md) |
| 模型输出、调用、超时与用量 | `backend/webagent/models/`；详见[模型适配器说明](development/model-adapter.md) |
| 状态转移与事件重放 | `state.py`、`events.py`、`sse.py`；详见[状态与事件说明](development/state-events.md) |
| 证据保存、脱敏、读取与存储故障 | `backend/webagent/evidence/`；详见[证据说明](development/evidence.md) |
| 业务字段核验、独立语义和成功判定 | `backend/webagent/verification/`；详见[验证与聚合说明](development/verification.md) |
| 观察／决策／动作／补证循环、图上下文和进度 | `backend/webagent/graph/`；详见[LangGraph 循环说明](development/graph-loop.md) |
| 写入意图、未知结果查证与跨 Run 关联 | `backend/webagent/writes/`，配合 `gateway/`、`graph/` 和 `scheduler/` |
| 本地数据目录、环境配置 | `backend/webagent/config.py`、`.env.example` |
| Worker 启动或退出行为 | `backend/webagent/worker.py` |
| 持久队列、资源冲突、执行资格与续约 | `backend/webagent/scheduler/`；详见[调度说明](development/scheduler.md) |
| Python 依赖 | `requirements/*.in`；验证后重建对应锁与清单 |
| 前端依赖 | `frontend/package.json` 和 `package-lock.json` |
| 功能测试 | `tests/unit/`；涉及多进程或浏览器时使用 `scripts/verification/` |

## 本次整理与历史记录

常用入口 `./scripts/bootstrap.sh`、`./scripts/dev.sh`、`./scripts/check.sh` 没有改变。直接调用内部工具时使用下列新路径：

| 原位置 | 当前位置 |
| --- | --- |
| 根目录 `requirements*.in`、`requirements*.lock` | `requirements/` 下同名文件 |
| `scripts/verify_*.py`、`check_evidence.py`、`network-audit.cjs` | `scripts/verification/` 下同名文件 |
| `scripts/build_manifest.py` | `scripts/dependencies/build_manifest.py` |
| `tests/test_*.py` | `tests/unit/` 下同名文件 |
| 根目录 `arithmetic.py`、`acceptance_test.py` | `tests/fixtures/m0/` 下同名文件 |
| `docs/m0-fixture-README.md` | `tests/fixtures/m0/README.md` |

原始 M1-01 证据、报告里的旧命令和源文件 SHA-256 快照保留原样，它们描述的是整理前的版本。历史代码可在 [M1-01 合并提交](https://github.com/donkzzj666/WebPilot/tree/710ba08080f75371796d9230c9c871b7fff47f01) 查看。当前命令以项目首页和脚本说明为准。

目录整理后的[干净安装复验](../artifacts/verification/M1-01/clean-install-20260929T060351.192585Z/report.json)已通过：从锁重新安装、22 项组件测试、前端类型检查与构建、真实 Chromium／LangGraph 集成、8 项三端生命周期检查；[复制后哈希复核](../artifacts/verification/M1-01/clean-install-20260929T060351.192585Z.post-exit-hashes.json)确认 27 个证据文件一致。Python 依赖文件及 M0 夹具内容未变，原始证据未改写。GitHub 工作流已修改夹具路径；远程 CI 检查 M0 夹具，完整 M1 验证由上述本地复验覆盖。

业务持久化的详细表说明与事务示例见[数据库开发说明](development/database.md)。

`backend/webagent/sessions/` 专门管理浏览器生命周期：`models.py` 定义非敏感归属数据，`store.py` 保存生命周期，`manager.py` 拥有 Playwright 对象，`auth.py` 负责认证加密。API 进程不拥有浏览器对象，认证原文不放入图状态或普通任务接口。

`identities/` 的阅读顺序是 `routes.py`（HTTP 元数据）→ `rpc.py`（API 与 Worker 通信）→ `service.py`（登录流程）→ `sites.py`（固定站点核查）→ `store.py`（SQLite 原子发布）。API 转发登录请求，Worker 拥有 Chromium；这条链路不创建任务 Run，详情见[身份说明](development/identities.md)。

`scheduler/` 的阅读顺序是 `models.py`（资源规范化与 Token）→ `store.py`（短事务）→ `worker.py`（异步执行器与心跳）→ `routes.py`（只读状态）。浏览器和队列共享四个上下文名额；同一配置数据目录的运行同时共享两个活跃槽，详情见[调度说明](development/scheduler.md)。

`backend/webagent/gateway/` 把动作协议、浏览器驱动、权限核对与 SQLite 日志连接起来：`service.py` 控制顺序，`browser.py` 只执行固定动作，`permissions.py` 核对可信页面适配器授权，`store.py` 提交意图和结果。详见[网关开发说明](development/browser-gateway.md)。

`writes/` 的阅读顺序是 `models.py`（业务目标、稳定操作键与查证事实）→ `store.py`（四态意图、尝试和不可变证明记录）→ `service.py`（可信适配器、只读 GET／观察与原始证据）→ `routes.py`（认证后的有界诊断）。一个业务操作跨图节点重入和关联 Run 沿用同一编号，每次真实派发则创建新的步骤并扣预算。已发生的变更经当前证明复用；只有明确未发生、身份／目标／前置版本一致且证明仍有效时，才能允许新的尝试。无法判断时保留 `UNKNOWN` 和资源隔离，不能据此声明成功。

写入证明必须绑定当前受管会话与实际观察，原始工件缺失或哈希改变会阻止复用和重试。查证入口只能读取，站点节流等待、查询、观察与截止继续使用本次 Run 的预算；epoch 撤销及取消不能回滚已经提交的网站请求。真实写入故障探针位于 `scripts/verification/verify_writes.py`，只访问自有合成测试站；它验证七种场景及真实进程崩溃，范围和运行方式见[脚本说明](../scripts/README.md)与[测试说明](../tests/README.md)。

`evidence/` 的阅读顺序是 `models.py`（有界元数据）→ `files.py`（受限原子文件）→ `store.py`（不可变索引、可用状态与孤儿）→ `redaction.py`（文本过滤和不透明图像阻断）→ `service.py`（网关／模型绑定）→ `routes.py`（认证后的 ID 读取）。运行文件由程序生成在 `data/evidence/`，用户或模型不能提交任意本机路径；[证据说明](development/evidence.md)列出保留策略与适用边界。

`verification/` 的阅读顺序是 `models.py`（检查与结果协议）→ `rules.py`（纯规则和字段证据）→ `semantic.py`（独立语义上下文）→ `service.py`（真实工件读取与持久聚合）→ `routes.py`（认证后的结果读取）。模型候选没有修改规则或提交成功的权力；schema v14 保存检查记录和不可变结果。

`graph/` 的阅读顺序是 `models.py`（只含引用的图状态）→ `store.py/context.py`（真实业务进度与当前模型输入）→ `source.py`（原件字段位置）→ `runtime.py`（节点和路由）→ `executor.py`（可信依赖装配）→ `routes.py`（只读进度）。schema v15 追加图进度；模型回复、浏览器对象、Token 和秘密不进入图库。`recovery.py/recovery_state.py` 核对两库引用、身份和实际对象版本，追加 schema v16 恢复记录。当前通用字段解析支持完整可见 JSON；公开执行控制属于 M1-18。详见[图循环说明](development/graph-loop.md)和[恢复说明](development/recovery.md)。

运行控制位于 `backend/webagent/controls/`：`models.py` 定义版本化请求，`store.py` 负责受理和边界完成，`routes.py` 提供认证 API。浏览器／模型派发、图中断及队列协调仍由各自模块执行，见[运行控制说明](development/run-controls.md)。

## 日志、指标与健康（M1-19）

`backend/webagent/observability/` 把既有账本投影为认证只读诊断、指标和 Worker 租约健康，并将经过核验的图引用写入有界本地 JSONL。`models/pricing.py` 使用冻结的静态价目估算已报告的 token 用量；缺价格／缺用量保持未知。HTTP 的持久业务请求 ID 与每次尝试的 transport ID 分开关联，日志不会复制请求或框架原文。

`scripts/verification/verify_observability.py` 使用隔离的实际 API／Worker、HTTP 模型和 Chromium 检查成功与失败流程。验收见 [M1-19 记录](m1/records/M1-19.md)，字段和限制见 [开发说明](development/observability.md)。schema 仍为 v18，本项未增加迁移或依赖。

## 任务入口与配置（M1-20）

`frontend/src/tasks/` 按“请求 → 边界校验 → 输入转换 → 展示”阅读：`client.ts` 调用既有认证 API 并绑定预期对象 ID，`types.ts` 校验响应，`inputs.ts` 处理来源、权限和字段类型，`presentation.ts` 提供中文标签，`TaskEntry.tsx` 连接持久列表、契约、集中补参与显式修订。`backend/webagent/tasks/catalog.py` 使用既有表提供有界只读分页，游标以十进制字符串跨过 JavaScript 的整数精度限制；不新增 SQL 迁移。

`ModelSettings.tsx` 显示提供商数据发送范围、支持的连接与静态价格，保存尝试会清空组件内 API Key；`IdentityReadiness.tsx` 只处理站点、预期账号、版本和持久身份元数据，密码／验证码仍在 Worker 管理的站点窗口中输入。创建契约不启动 Run，历史身份核实不代表本次业务成功，公网任务也不能使用评测 realm 身份。实际浏览器验收位于 `verify_task_entry.py`，范围见[任务入口说明](development/task-entry.md)和[M1-20 记录](m1/records/M1-20.md)。

## 集成故障验收（M1-24）

`scripts/verification/verify_fault_acceptance.py` 从真实 API 创建 Run，在 Worker／API 的持久提交边界强杀进程，然后核对同一 Run 的业务库、图库、动作、预算、证据、工作台和结果页。`tests/unit/test_fault_acceptance_probe.py` 检查验收器本身的停止与对账边界；真实恢复依据本轮集成证据。输出的新目录包含公开摘要和私有诊断，历史资料不能覆盖。见[故障验收说明](development/fault-acceptance.md)。
