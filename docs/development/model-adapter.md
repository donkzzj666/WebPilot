# 模型适配器与输出校验

M1-05 提供可替换的异步模型调用接口、DeepSeek Chat 传输、严格输出校验和 SQLite 调用记录。M1-07 提供[配置快照和 OS 密钥存储](settings.md)，调用方通过 `provider_for_run(run_id)` 加载冻结配置与凭据；M1-06 已接入自然语言编译。M1-14 增加[证据资格与脱敏门禁](evidence.md)。M1-16 已把适配器接入默认 Worker 的受控图循环；普通启动不会调用模型。

## 按调用顺序读代码

| 文件 | 责任 |
| --- | --- |
| `backend/webagent/models/schema.py` | `ModelInput`、四种输出、十种动作以及场景结果的严格 DTO；校验声明的来源、快照、epoch 和证据引用 |
| `backend/webagent/models/transport.py` | `ModelConfig`、`ModelImage`、`DeepSeekTransport`；组装协议、取消 HTTP 请求、提取白名单元数据 |
| `backend/webagent/models/adapter.py` | `ModelProvider` 协议、`ModelAdapter.generate()`；共同截止时间、有限格式修复、返回候选响应 |
| `backend/webagent/models/journal.py` | 请求前预记账、请求后完成记录、原 Run 预算累加、状态变化时丢弃过期候选 |
| `backend/webagent/models/pricing.py` | 冻结 token 价目、内容版本和 Decimal 费用估算 |
| `backend/webagent/db/sql/0005_model_calls.sql` | 显式 v5 迁移，追加两张调用表及历史保护触发器 |

调用方传入完整且已持久化的 `TaskContract`、经过过滤的 `Observation`、`RunCheckpoint` 和明确选中的图像证据。`ModelAdapter(database, provider).generate(model_input, images=(), deadline=None)` 返回 `GenerationResult(output, call_id, records)`。`deadline` 是可选的 monotonic 绝对截止时间。

观察必须来自 `EvidenceService.publish_observation()` 的已登记副本。适配器核对不可变快照、证据归属及实际文件，在每次供应商请求前重新验证；自行填写 `FILTERED`、替换正文或图片字节会被拒绝。敏感的契约／检查点／动作语义和供应商审计元数据不能进入调用日志。

Run 必须处于 RUNNING、VERIFYING 或 RECONCILING，输入须匹配其契约摘要、模型配置摘要和预算记录。输入在首次 await 前重新验证并复制；修复请求使用同一输入。密钥通过传输构造函数显式注入，不读取现有环境密钥或操作系统凭据。配置摘要不包含密钥。可运行的完整合成调用见 `scripts/verification/verify_model_adapter.py`。

## 输出只提出候选

| `type` | 允许的内容 |
| --- | --- |
| `Action` | 单个已知动作及强类型参数 |
| `RequestEvidence` | 契约中的验收条件和来源 |
| `ProposeResult` | 场景结果、覆盖范围、证据和未解决项；没有成功状态字段 |
| `RequestInput` | 非空的待补充字段和原因 |

十种动作是 navigate、click、input、keypress、select、scroll、switch_tab、read_visible、screenshot、download_attachment。没有 JavaScript、Shell 或其他任意代码工具。重复 JSON 键、NaN／Infinity、Markdown 代码围栏、多响应数组、未知字段／类型、直接声明 SUCCEEDED 均被拒绝，不做文本截取或启发式纠正。

模型不能调用状态机或执行浏览器动作。校验声明不等于验证真实工件、最新租约或业务完成；后续执行网关和结果聚合器仍需检查这些条件。

## 协议、修复与取消

采用已冻结的 DeepSeek Chat 协议：`deepseek-flash`、`/chat/completions`、JSON Object 输出、关闭 thinking、`stream=false`。运行依赖锁已有 `httpx==0.28.1`，本阶段将它声明为直接依赖，不增加 SDK 或变更锁定版本。JSON 模式和图像格式参考 [DeepSeek JSON 文档](https://api-docs.deepseek.com/guides/json_mode/)及[视觉文档](https://api-docs.deepseek.com/guides/vision/)。

默认 connect 10 秒、read 30 秒、整轮 60 秒；实际整轮期限取配置、调用方期限、Run 剩余活动时间三者的最小值。格式修复最多两次，且尊重契约的 0／1／2 次上限；所有尝试共享同一个期限，持续空白字节不延长期限。只有无效模型输出可以修复；HTTP 故障、限流、认证失败和超时均不自动重试，不切换供应商。

传输只连接官方 HTTPS 地址；验收使用显式开启的 `127.0.0.1` 测试地址。不跟随重定向，不继承环境代理。图像须与已登记证据及实际字节完全一致，最多 4 张、每张 5 MiB。当前策略只允许经过全灰证明的 PNG；原始截图及不透明图片默认阻断，全灰副本不能支持坐标动作或证明页面事实。不读取任意路径或抓取图像 URL。响应体最多 1 MiB。

外部取消会传播 `CancelledError` 并关闭正在读取的 HTTP 响应。正常可写的数据库中保留 CANCELLED 记录；如果终结记录遇到数据库忙锁，保留 STARTED 作为“结果未知”，不能据此自动重发。数据库事务没有网络 await，忙锁等待上限 100 毫秒。

## 日志与预算

`model_generations` 固定调用组、Run、配置摘要、提示版本、修复上限和起始状态版本。`model_attempts` 为每次实际尝试保存独立请求 ID，状态从 STARTED 只可完成一次为 VALID、INVALID、ERROR 或 CANCELLED；同一个 Run 不允许两个未完成请求。调用组不可变，尝试记录不能删除、替换或重复完成。

每次尝试在网络访问前与 `run_budgets.model_calls_used += 1` 同事务写入。耗时、修复用量都归原 Run；完成时更新 `active_ms`，已有活动计时间隔只结算一次，避免与模型耗时重复相加。等待期间 Run 状态版本改变会丢弃候选并返回 409。

`ModelCallRecord` 保存本地／供应商请求 ID、供应商／模型、配置摘要、提示版本、输入／输出 token、图像单位、白名单供应商元数据、耗时、累计修复次数、错误分类和可选价格版本。未返回的用量和无法估算的成本保持 `null`，不虚构零值或价格。原始提示、模型正文、推理正文、图像字节、凭据和供应商错误正文不入调用表。

### 静态价格与费用估算

M1-19 的可选 `ModelConfig.pricing` 定义 USD／CNY 的输入、输出及可选 cache-hit token 每百万单价，使用非负十进制字符串。规范价目内容的 SHA-256 生成 `price-<sha256>`；不能用任意版本标签替代价格。价目进入原有配置摘要并随 Run 冻结，不查询在线价格，也不重新定价历史调用。未配置时省略新 `pricing` 字段，保留历史配置 JSON 和摘要。

适配器和语义验证器仅在供应商报告完整输入／输出 token 时，用 Decimal 计算 `estimated_cost`；配置 cache-hit 单价时还要求有效的命中用量，缺失或矛盾则保持未知。该价目仅覆盖报告的 token 总量，不猜测图像单位或其他收费项。已估算记录同时保存 `price_version`、十进制金额和 `cost_currency`；旧记录没有币种时不推断 USD。没有价目、用量未知、STARTED 或失败尝试都保留各自未知字段，不补成零。

设置 JSON 示例见[静态价目配置](settings.md#静态-token-价目)。认证诊断与指标按价格版本、币种区分已知／未知合计，详情见[观测说明](observability.md)；本地日志仅记录白名单关联元数据，不输出原始调用内容。

| 供应商情况 | 错误分类 | 应用错误 |
| --- | --- | --- |
| 超时／整轮期限耗尽 | timeout | TIMEOUT / 504 |
| 429 | rate_limit | MODEL_RATE_LIMIT / 429，保留受限的 Retry-After |
| 401 | invalid_credentials | UPSTREAM_ERROR / 502 |
| 格式修复耗尽 | invalid_output | UPSTREAM_ERROR / 502 |
| 其他 HTTP／传输错误 | provider_error | UPSTREAM_ERROR / 502 |

这些错误不会切换 Run 到网站挑战或反爬状态。400／422、402、503 等保留安全子类；认证失败不冒充本地 API 的 401。进程崩溃后 STARTED 的恢复决策仍由后续恢复服务实现。

## 验证

```sh
.venv/bin/python -m pytest tests/unit/test_model_schema.py tests/unit/test_model_transport.py tests/storage/test_model_adapter.py
.venv/bin/python scripts/verification/verify_model_adapter.py
./scripts/check.sh
```

受控测试覆盖协议、四类输出、预算、取消、故障和历史升级；真实 HTTP 验收只监听本机回环，不发起付费模型调用。结果见 [M1-05 验证记录](../m1/records/M1-05.md)。真实供应商连通性和模型业务质量不由本次合成测试证明。

M1-11 已接通[统一预算](budgets.md)：进入持久队列的 Run 调用 `generate(..., execution_token=token)`，预留与结束均检查当前 owner／epoch／状态版本。修复共享初始绝对截止，每次计一个 model call，provider duration 不再叠加到活跃计时。旧资格、暂停或预算截止后的候选持久标为 CANCELLED；适配器超时若先于独立看门狗发现预算截止，会交给 Worker 同一停止路径。未进入队列的历史／合成调用保留原有兼容接口。
