# 构建与版本记录

这里记录“验证时使用了什么”，不是用户填写业务设置的位置。

| 文件 | 含义 |
| --- | --- |
| `build-manifest.json` | 当前依赖、运行时、锁文件路径与哈希 |
| `browser-lock.json` | Playwright、Chromium 版本与发行物摘要 |
| `m1-01-validated-versions.txt` | M1-01 初次验证的版本记录 |
| `m1-01-source-manifest.json` | M1-01 初次交付的历史源文件快照，使用整理前的路径 |
| `m1-02-source-manifest.json` | M1-02 核心持久化验收时的源文件快照 |

检查当前构建：`.venv/bin/python scripts/dependencies/build_manifest.py --check`。

历史源文件快照不代表当前工作区，也不随重构改写；对应代码见 [710ba08](https://github.com/donkzzj666/WebPilot/tree/710ba08080f75371796d9230c9c871b7fff47f01)。目录变化见[导读](../docs/project-structure.md)。本地运行配置示例在根目录 `.env.example`。

`m1-03-source-manifest.json` 保存 M1-03 验收的源文件快照，历史快照不重写。

`m1-04-source-manifest.json` 保存 M1-04 任务 API 验收时的源文件快照。

`m1-05-source-manifest.json` 保存 M1-05 模型适配器验收时的源文件快照。

`m1-07-source-manifest.json` 保存 M1-07 配置快照与 OS 密钥存储验收时的源文件快照。

`m1-06-source-manifest.json` 保存 M1-06 自然语言编译与缺参流程验收时的源文件快照；实际实施顺序晚于 M1-07，历史快照保持不变。

`m1-08-source-manifest.json` 记录受管浏览器会话验收版本。本项新增加密依赖，构建与许可清单已显式更新，历史 M1 源文件快照不改写。

`m1-12-source-manifest.json` 记录本地 API 防护与浏览器网络边界验收版本。本项沿用既有依赖锁，不新增第三方包；历史快照保持原样，私有令牌不进入清单。

`m1-09-source-manifest.json` 记录登录准备与身份确认验收版本（实施晚于 M1-12）。本项追加 schema v9，没有新增第三方依赖；认证密文、私有令牌和真实凭据不进入源文件快照。

`m1-10-source-manifest.json` 记录持久队列与统一资源租约验收版本。本项追加 schema v10，沿用依赖锁；历史 SQL、源文件快照及现有业务数据保留，凭据与认证密文不进入清单。

`m1-11-source-manifest.json` 记录统一预算、配额和独立截止的验收版本。本项追加 schema v11，原 v1～v10 SQL 和历史证据保持原样；没有新增第三方依赖。实际派发尝试、单调时间锚点和两类监控来源继续使用业务库权威，工件不包含私有密钥。

`m1-13-source-manifest.json` 记录结构化浏览器动作网关的验收版本，追加 schema v12。固定动作、快照和权限检查沿用现有依赖；历史 SQL／源码快照保持不变。原始页面、下载内容、代理凭据和私有认证数据不进入清单。

`m1-14-source-manifest.json` 保存证据工件、脱敏和受控读取的源码快照。本项追加 schema v13，没有新增第三方依赖；历史 v1～v12 SQL、历史源码快照保持不变，受限原件和私有数据不进入源码或导出清单。

`m1-15-source-manifest.json` 保存运行时验证与结果聚合的历史源码快照。schema v14 追加不可变检查和聚合结果，没有新增第三方依赖，历史 v1～v13 SQL 与历史快照保持原样。合成 HTTP 探针不读取评测真值或用户账户。

M1-16 源码快照为 `m1-16-source-manifest.json`；完整回归和全新安装均已通过，证据见 [M1-16 验收记录](../docs/m1/records/M1-16.md)。本项沿用锁定的 LangGraph／AsyncSqliteSaver 依赖，追加 schema v15 的业务图进度，保留 v1～v14 SQL 与历史源文件清单。运行状态只持久化业务引用；模型密钥、完整回复、浏览器对象和执行 Token 不收入源文件或图状态。结构、验证入口与恢复边界见[图循环说明](../docs/development/graph-loop.md)。

M1-17 源码快照为 `m1-17-source-manifest.json`，保留 M1-16 快照和 v1～v15 SQL；新增 schema v16 的恢复检查与受控恢复导航记录。两库职责及恢复顺序见[恢复说明](../docs/development/recovery.md)，最终验证证据见[M1-17 验收记录](../docs/m1/records/M1-17.md)。

M1-23 源码快照为 `m1-23-source-manifest.json`，追加 schema v18 的写入意图与查证协议；沿用原依赖锁，v1～v17 SQL、M1-17／M1-18 快照和默认用户数据保持原样。实际验证结果见[M1-23 验收记录](../docs/m1/records/M1-23.md)；受限查证原件及凭据不收入源码清单。

M1-19 的静态 token 价目通过模型设置的 `pricing` 字段保存，内容摘要生成 `price_version`，随既有 Run 配置快照冻结。没有预设供应商价格，无需新增配置文件或 SQL 迁移；格式见[设置说明](../docs/development/settings.md#静态-token-价目)。源码与验收导航见 [M1-19 记录](../docs/m1/records/M1-19.md)；历史源码清单保留。

M1-20 将静态 token 单价、受支持模型连接、数据发送确认和账号准备接入本机页面；这些业务配置仍由版本化设置 API 保存，不能在本目录写入密钥。任务列表使用既有 SQLite 结构，无新增迁移或依赖。界面、配置及公网／评测身份范围见[任务入口说明](../docs/development/task-entry.md)，源码冻结与最终证据见[M1-20 记录](../docs/m1/records/M1-20.md)。

M1-21 工作台沿用 schema v18 和现有依赖，通过认证只读快照展示 Run 冻结的设置、预算和队列；控制仍使用既有版本与幂等协议。页面证据只能读取现有过滤衍生物，不能通过配置关闭脱敏或开放私有路径。见[工作台说明](../docs/development/workbench.md)。

M1-22 结果页继续使用 schema v18 和原有锁定依赖；只读投影不迁移或初始化数据库。证据可用性须检查当前实际文件，历史结果不能关闭认证、哈希或脱敏要求。见[结果页说明](../docs/development/results.md)。

M1-24 沿用 schema v18 和现有依赖锁，故障注入仅由验收脚本在自有子进程中安装；正常 API／Worker 配置没有可开启故障的入口。最终源码清单为 `m1-24-source-manifest.json`，历史源码清单与失败报告保留。见[故障验收说明](../docs/development/fault-acceptance.md)。
