# 模型设置、配置快照与密钥存储

M1-07 新增工作台模型设置区、配置就绪查询和版本化保存接口。SQLite 只保存模型参数、摘要和不透明的凭据引用；实际 API Key 保存在 macOS Keychain。密钥不会回填到页面，也不写入普通配置、模型提示或应用日志。

## 在工作台配置

按项目首页启动 API 与前端，使用相同数据目录及端口配置，打开 `http://127.0.0.1:5173` 的“模型设置”。页面使用 `apiFetch` 经受限 Vite 代理自动认证；用户无需复制令牌，令牌不进入页面或浏览器存储。当前支持已选定的 DeepSeek / deepseek-flash，可调整输出 token 上限及连接、读取、总超时。

保存前需要确认数据说明：执行模型任务时，供应商可能接收任务指令与必要参数、经过过滤的相关网页正文、明确选中的截图。页面在密码输入框前展示这项说明；接口同样要求明确确认。

首次可以只保存参数，此时显示“密钥缺失”。输入 API Key 后保存会新建 OS 凭据引用。再次保存时，密钥留空表示沿用当前版本的凭据；输入新密钥表示轮换，旧凭据仍供历史 Run 使用。页面每次提交尝试后清空密钥，不使用 localStorage、sessionStorage 或查询参数保存密钥。

“本机模型配置就绪”仅表示配置完整且当前凭据可从 OS 读取。保存和查询均不访问供应商，因此不能证明 Key 有效、余额充足或供应商可用。任务执行能力仍按后续 M1 任务接入。

## 接口

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/v1/settings` | 返回一个已提交版本及其就绪状态，不返回密钥或凭据引用 |
| PUT | `/v1/settings/model` | 新建不可变版本，使用 `expected_version` 比较当前版本 |

GET、PUT 都要求本机 Bearer 认证；未认证返回 `401 UNAUTHENTICATED`，Host／Origin 或浏览器来源越界返回 `403 FORBIDDEN`。

初次 GET 的 `version=0`、`model=null`、`credential_status=not_configured`。已保存但缺少密钥为 `missing`；其他状态明确区分 `locked`、`access_denied`、`unavailable`、`unsupported`、`invalid`。就绪时为 `available`，`provider_verified` 仍为 false。

PUT 正文结构（下面只保存参数，不写入密钥）：

```json
{
  "expected_version": 0,
  "model": {
    "provider": "deepseek",
    "model_id": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
    "prompt_version": "m1-05-model-v1",
    "max_tokens": 1024,
    "connect_seconds": 10,
    "read_seconds": 30,
    "total_seconds": 60,
    "price_version": null
  },
  "accept_data_sharing": true
}
```

写入新密钥时由本机页面添加 `api_key` 字段。省略表示沿用；显式 null、空字符串、空白或非法类型均拒绝。服务地址只允许官方 DeepSeek HTTPS 地址（及其 `/v1` 形式），不接受携带用户名、密码、查询参数或其他主机的地址。

参数错误、正文超过 64 KiB 或非 JSON 请求返回 `INVALID_PARAMETER / 422`，旧配置版本返回 `STATE_CONFLICT / 409`，细节标记 `VERSION_CONFLICT`。凭据设施或配置存储故障返回 `SERVICE_UNAVAILABLE / 503`，具体安全原因放入 `details`，保持既定 API 错误格式。配置使用乐观版本比较，不保存包含密钥的请求收据或密钥摘要；相同旧版本重试返回 409，客户端应 GET 核对当前版本后明确重新提交。保存结果未知时页面提示重新载入，不自动重试。

响应含非秘密的模型参数、模型摘要、运行配置摘要、数据披露版本及就绪原因。普通响应和错误响应都标记 `Cache-Control: no-store`。所有 API 共用本机访问中间件：精确匹配配置中的 Host／Origin，拒绝远程／null Origin、重复 Origin 和跨站浏览器请求；不是任意回环端口都可访问。详情见 [本机访问与网络安全](network-security.md)。

从命令行只读查询时，可在项目根目录执行（数据目录、端口需与 API 一致）：

```sh
PYTHONPATH=backend .venv/bin/python - <<'PYTHON'
import os
import httpx
from webagent.config import Settings
from webagent.security import load_or_create_token

headers = {"Authorization": "Bearer " + load_or_create_token(Settings.from_env().data_dir)}
url = f"http://127.0.0.1:{int(os.environ.get('WEBAGENT_API_PORT', '8000'))}"
with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=10) as client:
    response = client.get("/v1/settings")
    print(response.status_code, response.json())
PYTHON
```

此示例仅输出响应，不能打印请求头。保存配置时同样使用该认证客户端；填写实际供应商密钥请使用工作台密码框。

## 版本与 Run 的关系

v6 迁移追加 `model_settings_versions` 与 `run_config_snapshots`。前者保存连续编号的不可变配置，当前版本是已提交的最大版本；后者把 Run 固定到该版本及两个摘要。更新、删除、替换快照或运行绑定都会被数据库触发器拒绝。

模型摘要与 M1-05 `ModelConfig.config_sha256` 一致，覆盖请求参数且不含密钥。运行配置快照还包含设置版本、随机 UUID 凭据引用、观察过滤策略和输出协议版本，因此仅轮换密钥也会产生新的运行配置摘要。凭据引用不是 API Key 的哈希，不能据此验证或推测 Key。

后续调度器使用 `create_configured_run()` 创建 Run。该内部服务先读取凭据检查就绪，再以同一短事务创建 Run、不可变配置绑定及原 Run 的预算记录，并关联任务；如果配置已变化则冲突返回。它不会执行任务，也不替代未来的调度扣额。

`load_run_config()` 和 `provider_for_run()` 按 Run 的绑定加载原参数与原凭据，绝不回退到最新设置。已有 Run 在参数修改或密钥轮换后保持原配置；旧凭据缺失时明确失败。M1-07 之前没有配置绑定的历史 Run 保留原记录，返回 `CONFIG_SNAPSHOT_MISSING`，不伪造可追溯快照。通过工厂取得的 `DeepSeekTransport` 使用完毕需要 `await provider.aclose()`。

## 静态 token 价目

M1-19 在现有 `model` 参数中增加可选 `pricing`。配置仍通过 `PUT /v1/settings/model` 发布新版本；M1-20 已在[任务入口与配置界面](task-entry.md)提供价目编辑及持久版本展示。下面是可加入 `model` 对象的合成示例，数值仅用于演示，不代表供应商现行价格：

```json
{
  "pricing": {
    "basis": "reported_input_output_tokens",
    "currency": "USD",
    "input_per_million": "0.5",
    "output_per_million": "1.5",
    "cache_hit_input_per_million": null
  }
}
```

币种只允许 USD／CNY，单价为非负十进制字符串；规范化后价目内容的 SHA-256 自动生成 `price_version=price-<sha256>`。可省略请求中的 `price_version`，提供时必须与价目一致；公开设置接口拒绝只有价格版本、没有价目的配置。未配置 `pricing` 时序列化省略该新字段，保留历史配置 JSON 与摘要。

价目与模型参数一同进入设置摘要和 Run 冻结快照。修改设置只影响之后的 Run，旧 Run 继续使用原价目；不下载价格目录，不按新价格改写历史记录。省略 `pricing` 保存一个新版本表示该版本不配置费用估算，因此需要保留价目时应在模型参数中一并提交。凭据留空沿用规则独立于价目，不会因价格修改而泄漏密钥。

估算只使用供应商报告的完整输入／输出 token；指定 cache-hit 单价时还需有效命中用量，未知值不能补成零。金额为 Decimal 十进制字符串并记录币种。算法和收费范围见[模型适配器](model-adapter.md#静态价格与费用估算)，已知／部分／未知用量与费用的统计见[观测说明](observability.md)。这些非秘密配置及金额复用现有 JSON 列，业务库仍为 schema v18，没有新增迁移或依赖。

## OS 凭据边界与失败处理

`settings/secrets.py` 提供可替换的 `SecretStore` 协议，目前生产实现为 macOS 原生 Security／CoreFoundation 框架，经 Python ctypes 调用。无需新增 Python 依赖；不使用含密钥命令行参数的 `security` 命令，不读取环境变量密钥，也不提供明文文件回退。

每次操作只查询默认 Keychain 中固定服务 `WebPilot.model-credentials.v1` 和精确 UUID v4 账户。创建使用 add-only 语义，不覆盖已有引用。系统调用禁用认证弹窗；锁定或访问被拒绝时返回明确原因，用户可在系统中处理后重新读取。其他操作系统显示 unsupported，尚未实现 Windows／Linux 凭据后端。

OS 凭据与 SQLite 无法成为同一个原子事务。保存流程先完成新凭据写入，再用 SQLite 事务比较版本并发布快照。数据库失败或版本竞争失败时，只删除本次新建且已确认未被任何配置引用的凭据；不删除旧凭据。若提交已经成功，即使后续发生异常，也保留已被引用的 Key。无法确认提交状态或清理失败时保留凭据并返回安全诊断，避免破坏已提交快照。

进程在 OS 写入后、数据库发布前崩溃，或 OS 写入结果不确定时，可能留下孤立凭据。当前不进行猜测式自动清理；旧版本凭据的保留与后续显式清理策略需要一并考虑仍被引用的 Run。原生 Keychain 调用同步且不弹窗，但不能声称内核 IPC 可强制取消；API 在线程池中执行这些调用，不阻塞异步模型请求的事件循环。

## 目录与验证

| 文件 | 用途 |
| --- | --- |
| `backend/webagent/settings/models.py` | 严格请求、数据披露、运行配置 DTO |
| `backend/webagent/settings/secrets.py` | OS 密钥存储协议与 macOS 实现 |
| `backend/webagent/settings/service.py` | 版本发布、就绪查询、补偿清理、Run 快照加载 |
| `backend/webagent/settings/routes.py` | 本机接口、严格 JSON、正文限制、安全错误 |
| `frontend/src/ModelSettings.tsx` | 数据说明、密码输入、就绪提示与冲突处理 |
| `backend/webagent/db/sql/0006_settings_snapshots.sql` | 配置与运行绑定迁移 |

```sh
.venv/bin/python -m pytest tests/unit/test_secret_store.py tests/storage/test_settings.py
.venv/bin/python scripts/verification/verify_secret_store.py
.venv/bin/python scripts/verification/verify_settings.py
./scripts/check.sh
```

OS 验证使用独立临时 Keychain 和随机合成密钥，完成后删除，不读取用户已有凭据值，不改变默认 Keychain 或搜索列表。界面／HTTP 验证使用独立 SQLite 和明确注入的内存凭据夹具，模拟锁定等故障，不启用生产服务的明文回退。详见 [M1-07 验证记录](../m1/records/M1-07.md)。

M1-06 的 `provider_for_compilation()` 为准备请求读取一次固定版本的配置和凭据，并由独立 `task_compilations` 日志绑定；不需要先创建 Run。任务仍有 STARTED 编译时，内部 Run 创建入口拒绝执行旧目标。详情见[自然语言任务编译](natural-tasks.md)。
