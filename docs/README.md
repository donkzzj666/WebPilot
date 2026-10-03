# 文档导航

初学者建议按“启动 → 目录 → 页面与接口 → 测试 → 需求”的顺序阅读。

## 开发入门

1. [项目首页与启动方法](../README.md)：环境、安装、三个进程的启动命令。
2. [目录与代码导读](project-structure.md)：目录职责、代码阅读顺序和常见修改位置。
3. [脚本说明](../scripts/README.md)：日常入口和维护工具。
4. [测试说明](../tests/README.md)：组件测试、真实集成和 M0 夹具的区别。
5. [依赖管理](../requirements/README.md)与[构建记录](../config/README.md)。

6. [业务数据库与迁移](development/database.md)：核心实体、历史保护、升级和短事务。

## 需求与计划

| 文档 | 回答的问题 |
| --- | --- |
| [总任务计划](browser-agent-task-plan-v0.1.md) | 项目分哪些阶段、每项怎样验收？ |
| [M1 执行清单](m1/README.md) | 当前下一步做什么？哪些任务已经完成？ |
| [M1-01 验证记录](m1/records/M1-01.md) | 项目骨架做到了什么、证据在哪里？ |
| [状态与事件开发说明](development/state-events.md) | 状态怎样提交、SSE 怎样重放？ |
| [任务 API 开发说明](development/task-api.md) | 怎样创建、补参、修订和幂等重试？ |
| [任务入口与配置界面](development/task-entry.md) | 怎样在 UI 建立契约、集中补参、处理冲突并核对正确账号？ |
| [M1-20 验证记录](m1/records/M1-20.md) | 真实页面、自然编译、持久状态和合成错账号怎样验收？ |
| [执行工作台与实时进度](development/workbench.md) | 怎样核对实际子目标、预算、页面证据、控制回执和断线恢复？ |
| [结果与证据页面](development/results.md) | 怎样查看字段证据、历史失败、未覆盖范围和未知副作用？ |
| [M1-22 验证记录](m1/records/M1-22.md) | 实际字段值、损坏证据、历史分页和未知副作用怎样验收？ |
| [M1-21 验证记录](m1/records/M1-21.md) | 实际执行和重复／乱序／断线等故障怎样验收？ |
| [模型适配器说明](development/model-adapter.md) | 输出怎样校验、格式怎样修复、调用怎样计量与取消？ |
| [配置与密钥说明](development/settings.md) | 怎样保存模型设置、判断就绪和固定运行快照？ |
| [自然语言任务编译](development/natural-tasks.md) | 如何提交指令、处理缺参与区分网页内容？ |
| [M1-06 验证记录](m1/records/M1-06.md) | 编译、补参、幂等与来源隔离是否通过验收？ |
| [浏览器会话说明](development/browser-sessions.md) | 上下文如何隔离、关闭及重建？认证文件如何保护？ |
| [M1-08 验证记录](m1/records/M1-08.md) | 有界面 Chromium 与加密认证是否通过验收？ |
| [登录与身份确认](development/identities.md) | 如何打开登录窗口、确认正确账号和恢复过期身份？ |
| [M1-09 验证记录](m1/records/M1-09.md) | 登录、加密保存、API 与 Worker 之间的闭环是否通过？ |
| [预算开发说明](development/budgets.md) | 为什么恢复后时间不会清零，额度何时扣？ |
| [M1-11 验证记录](m1/records/M1-11.md) | 配额、时间和独立截止是否通过？ |
| [M1-10 验证记录](m1/records/M1-10.md) | 持久队列、资源竞争、Worker 崩溃与上下文容量是否通过？ |
| [持久队列与统一资源租约](development/scheduler.md) | 如何领取任务、共享浏览器名额、续约和拒绝旧执行器？ |
| [本地 API 与网络边界](development/network-security.md) | 网页怎样被阻止调用本机 API？浏览器出口怎样检查实际目标？ |
| [M1-12 验证记录](m1/records/M1-12.md) | 来源、DNS、实时通信及各类绕过路径是否通过验收？ |
| [证据开发说明](development/evidence.md) | 原件与展示副本怎样保存、脱敏、校验及受控读取？ |
| [M1-14 验证记录](m1/records/M1-14.md) | 原子保存、工件故障和敏感内容阻断的验收进度是什么？ |
| [运行时验证与结果聚合](development/verification.md) | 字段证据、独立语义检查和成功／部分／失败如何判定？ |
| [M1-15 验证记录](m1/records/M1-15.md) | 缺证据、冲突、越权和未决写入是否阻止虚报成功？ |
| [LangGraph 执行循环](development/graph-loop.md) | 四种输出怎样分派、补证如何扣预算、检查点与新进程恢复有哪些边界？ |
| [写入意图与 UNKNOWN 查证](development/write-intents.md) | 丢失回执后怎样查证，何时可以复用或重试？ |
| [日志、指标与进程健康](development/observability.md) | 如何关联业务事件与图执行，区分未知费用、排队、恢复和 Worker 健康？ |
| [M1-19 验证记录](m1/records/M1-19.md) | 成功／失败流程、敏感日志和实际出站如何核验？ |
| [M1-07 验证记录](m1/records/M1-07.md) | 配置轮换、Keychain 与设置页是否通过验收？ |
| [M1-05 验证记录](m1/records/M1-05.md) | 受控模型输出与真实本机 HTTP 故障测试是否通过？ |
| [M1-04 验证记录](m1/records/M1-04.md) | 任务 API 与持久幂等是否通过？ |
| [M1-03 验证记录](m1/records/M1-03.md) | 转移、冲突、中断和重连是否通过？ |
| [M1-02 验证记录](m1/records/M1-02.md) | 核心实体、显式迁移与并发是否通过？ |
| [依赖与许可清单](m1/dependencies.md) | 当前安装了哪些依赖、版本和许可是什么？ |

本地还保留 `browser-agent-prd-v0.4.md`（产品需求）、`browser-agent-trd-v0.2.md`（技术方案）、`browser-agent-validation-v0.4.md`（验证方案）和 `browser-agent-framework-evaluation-v0.1.md`（框架评估）。这些是整理前已存在的本地资料，当前并非都已纳入 Git；如果克隆仓库后没有看到它们，可先按上面的已交付文档阅读。

## 历史资料

`m0/` 保存前期契约、来源核查、夹具准备及评测材料；`../experiments/` 保存框架实验。它们不由应用运行时加载，也不是初次启动的前置阅读。独立评测答案仍留在原有本地目录。

- [结构化浏览器动作网关](development/browser-gateway.md)：固定动作、快照、权限与派发日志。

- [两类检查点与只读崩溃恢复](development/recovery.md)：按业务账本重建图，先查证当前租约、身份和对象版本。

- [运行控制](development/run-controls.md)：异步受理、完成回执、暂停／继续、取消与关联重跑；状态见 [M1-18 记录](m1/records/M1-18.md)。

- [基础机制集成故障验收](development/fault-acceptance.md)：同一 Run 的 API／Worker 崩溃提交边界、恢复对账、故障矩阵及后续待验范围。
- [M1-24 验收记录](m1/records/M1-24.md)：本轮源码冻结、全新安装、故障结果及历史资料保护。
- [第一个公开只读闭环](development/readonly-loop.md)：固定 arXiv 版本原文解析、独立字段验证和显式真实供应商入口。
- [M1-25 验收记录](m1/records/M1-25.md)：当前开发子任务范围、真实运行与人工原页面对照进度。
- [M1-22 复验修复补记](m1/records/M1-22-revalidation-20261003.md)：历史写入授权按来源 Run 冻结契约判定。
