# Browser Agent

本地浏览器 Agent 的执行基础。当前交付 **M1-01 项目骨架**：React/TypeScript 前端、FastAPI API、独立 Python Worker、业务 SQLite 与 LangGraph 检查点 SQLite，以及可重复启动和验证入口。

工作台显示真实 API 健康状态。Worker 当前初始化检查点存储后等待退出信号，尚不领取或执行任务。任务创建、状态机、队列、受管浏览器网关及真实业务闭环按 [M1 清单](docs/m1/README.md) 后续任务实现。

## 环境与安装

已验证环境：macOS 15.3.1 / Apple Silicon，Python **3.12.14**（实际链接 SQLite **3.53.1**），Node **24.19.0**，npm **10.8.2**。版本记录见 [构建清单](config/build-manifest.json) 和 [许可证清单](docs/m1/dependencies.md)。其他平台尚未验收。

从项目根目录执行：

```sh
./scripts/bootstrap.sh
```

脚本创建项目 `.venv`，按带 SHA-256 的 `requirements-dev.lock` 安装 Python 全部依赖，按 `frontend/package-lock.json` 执行 `npm ci`，将 Playwright 对应 Chromium 安装到 `.cache/ms-playwright`。它只修改项目目录，不修改系统 Python、Node 或全局 npm 包。首次安装需要连接 PyPI、npm registry 和 Playwright 官方下载源。

脚本先检查实际 Python 的 SQLite 修复版本、WAL 和 FTS5 查询能力。默认系统 Python 3.9 / SQLite 3.43 不可用于本项目。脚本优先寻找 Python 3.12 和 Node 24，也支持本机已有的 Codex runtime；无合适运行时或自动找到的 Python 不满足 SQLite 条件时，用绝对路径指定：

```sh
WEBAGENT_PYTHON=/absolute/path/to/python3.12 \
WEBAGENT_NODE=/absolute/path/to/node \
WEBAGENT_NPM_CLI=/absolute/path/to/npm/bin/npm-cli.js \
./scripts/bootstrap.sh
```

SQLite 允许 3.51.3 及以后，或官方修复的 3.44.6+（3.44 分支）、3.50.7+（3.50 分支）；不能单看系统 `sqlite3` 命令的版本。[官方 WAL 修复说明](https://www.sqlite.org/wal.html)

`requirements.lock` 是运行依赖；`requirements-dev.lock` 加入验证和锁定工具。两者均固定传递依赖及哈希。依赖升级应先重做受影响集成，再更新锁与清单。

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

打开 [本机工作台](http://127.0.0.1:5173)。API 默认监听 `127.0.0.1:8000`，`GET /health` 返回 API 自身状态；前端通过 Vite 的 `/api/health` 代理访问。Worker 在终端输出 `worker_ready` 后处于 idle，不依赖 API 进程存活。

三个进程都可用 `Ctrl-C` 或 `SIGTERM` 独立停止。再次执行同一入口使用原数据目录；浏览器仅由显式集成验证脚本启动，普通骨架启动不打开浏览器、不调用模型。

```sh
# 检查 Python 实际链接的 SQLite、WAL、FTS5
./scripts/dev.sh doctor

# Worker 初始化检查后立即退出
./scripts/dev.sh worker --once
```

可通过环境变量指定 `WEBAGENT_API_PORT`、`WEBAGENT_UI_PORT`（1024–65535）。前端代理与 API 必须使用相同 API 端口。`WEBAGENT_DATA_DIR` 指向绝对的本机目录，API 与 Worker 应使用同一目录；不指定时为项目 `data/`。入口只读取导出的环境变量，不自动加载 `.env`，示例见 [.env.example](.env.example)。不要把该开发骨架暴露到公网；完整本地 API 防护和网络权限仍属 M1-12。

## 验证与证据

```sh
./scripts/check.sh
```

该入口检查运行时、依赖一致性、组件测试、前端类型和构建，运行真实 LangGraph/AsyncSqliteSaver/Chromium 本机集成，并启动和停止三端做隔离与重复启动验证。需要允许本机回环监听和启动 Chromium；不需要模型密钥或业务账号。证据每次写入全新的 `artifacts/verification/M1-01/` 子目录，失败记录也保留。

单独验证或显示测试浏览器：

```sh
.venv/bin/python scripts/verify_m1_01.py --headed
.venv/bin/python scripts/verify_startup.py
.venv/bin/python scripts/build_manifest.py --check
# 在全新目录中按锁重建环境并复验（需要安装源网络）
.venv/bin/python scripts/verify_clean_install.py
```

异步集成仅使用固定本机合成页面，图设置 `durability="sync"`，在独立业务库写测试观察，在图库保存检查点，关闭后重读检查点。它不实现 M1-16 产品执行循环，不代替 M1-17 的崩溃恢复。

所有入口关闭继承的 LangSmith/LangChain tracing。集成测试记录 Python socket、Playwright Node driver 和 Chromium NetLog；测试浏览器使用只提供固定页面、不向上游转发的本机代理，并将解析限制到回环地址。Chromium 后台服务请求会被记录并拒绝；IPv6 可达性套接字检查单独列示，不能声称系统层零数据包。此测试隔离不等于 M1-12 产品网络边界。详细通过项、原始失败与限制见 [M1-01 验证记录](docs/m1/records/M1-01.md)。

## 目录与责任

| 路径 | 用途 |
|---|---|
| `frontend/` | React/TypeScript 启动状态页与 Vite 配置 |
| `backend/webagent/` | API、独立 Worker、运行时检查与存储初始化 |
| `data/business.sqlite3` | 业务库位置；本项仅初始化 WAL，业务实体和迁移留 M1-02 |
| `data/graph.sqlite3` | Worker 的 AsyncSqliteSaver 所有，不混用业务检查点 |
| `scripts/` | 安装、独立启动、集成验证、构建清单入口 |
| `tests/` | SQLite 修复门槛、存储边界、进程及网络审计分类测试 |
| `config/` | 可复核的依赖／运行时／浏览器版本清单 |
| `artifacts/verification/M1-01/` | 本项测试输出；测试库不作为正式业务数据 |
| `docs/m0/`、`experiments/` | 已有契约、准备材料和历史实验；不由运行服务加载 |

本项不连接付费模型、不访问正式任务或独立答案、不写入真实业务网站。FR-01 的完整图适配器替换、新进程恢复等验收仍留对应后续任务。
