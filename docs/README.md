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
| [M1-02 验证记录](m1/records/M1-02.md) | 核心实体、显式迁移与并发是否通过？ |
| [依赖与许可清单](m1/dependencies.md) | 当前安装了哪些依赖、版本和许可是什么？ |

本地还保留 `browser-agent-prd-v0.4.md`（产品需求）、`browser-agent-trd-v0.2.md`（技术方案）、`browser-agent-validation-v0.4.md`（验证方案）和 `browser-agent-framework-evaluation-v0.1.md`（框架评估）。这些是整理前已存在的本地资料，当前并非都已纳入 Git；如果克隆仓库后没有看到它们，可先按上面的已交付文档阅读。

## 历史资料

`m0/` 保存前期契约、来源核查、夹具准备及评测材料；`../experiments/` 保存框架实验。它们不由应用运行时加载，也不是初次启动的前置阅读。独立评测答案仍留在原有本地目录。
