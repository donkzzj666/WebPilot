# 目录与代码导读

项目按职责分为前端、后端、依赖、测试、脚本、文档和运行数据。当前是 M1-01 骨架，后端中的 API 和 Worker 是两个独立进程。

## 目录地图

```text
webagent/
├── README.md                  # 先读这里：安装和启动
├── AGENTS.md                  # 协作约定与仓库地址
├── frontend/                  # 用户在浏览器里看到的页面
│   ├── src/
│   │   ├── main.tsx           # 挂载 React 应用
│   │   ├── App.tsx            # 工作台与 API 健康状态
│   │   └── styles.css         # 页面样式
│   ├── vite.config.ts         # 开发服务与 /api 代理
│   └── package*.json          # 前端依赖及锁文件
├── backend/webagent/          # Python 应用包
│   ├── __main__.py            # api / worker / doctor 命令入口
│   ├── api.py                 # FastAPI 健康接口
│   ├── worker.py              # 独立 Worker 的启动与退出
│   ├── storage.py             # 业务库初始化
│   ├── config.py              # 数据目录配置与 tracing 开关
│   └── runtime.py             # Python、SQLite、WAL、FTS5 检查
├── requirements/              # Python 依赖声明和哈希锁
├── tests/
│   ├── unit/                  # 日常组件与边界测试
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
                                              └─ 初始化业务 SQLite

独立 Python Worker → 初始化双库 → 持有 LangGraph 检查点库 → idle
```

默认双库在 `data/business.sqlite3` 和 `data/graph.sqlite3`。当前前端只读取 API 健康状态，API 尚不向 Worker 下发任务；不要将页面可访问等同于已完成浏览器任务执行功能。

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
| 本地数据目录、环境配置 | `backend/webagent/config.py`、`.env.example` |
| Worker 启动或退出行为 | `backend/webagent/worker.py` |
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
