# 脚本说明

所有示例都从项目根目录运行。

| 日常入口 | 用途 |
| --- | --- |
| `./scripts/bootstrap.sh` | 检查运行时，安装锁定依赖和浏览器 |
| `./scripts/dev.sh frontend` | 启动前端；另开终端启动 API、Worker |
| `./scripts/dev.sh api` | 启动健康接口 |
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

# 三端启动、停止、重启与隔离验证
.venv/bin/python scripts/verification/verify_startup.py

# 在新目录按锁重装并运行完整检查，需要安装源网络
.venv/bin/python scripts/verification/verify_clean_install.py

# 检查依赖和浏览器身份是否漂移
.venv/bin/python scripts/dependencies/build_manifest.py --check
```

`verification/check_evidence.py` 在验证进程退出后检查报告内的工件哈希；`verification/network-audit.cjs` 是测试用的 Node 网络审计钩子。它们不由正常前端、API 或 Worker 启动流程加载。

`dependencies/build_manifest.py` 不带 `--check` 时会重写构建、浏览器和许可清单；只在有意维护依赖或目录布局后运行，不能靠重写清单掩盖意外漂移。
