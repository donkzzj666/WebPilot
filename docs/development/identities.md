# 登录准备与身份确认（M1-09）

登录在 Worker 管理的独立 Chromium 上下文中完成。用户在网站窗口输入凭据，再通过本机 API 请求确认；确认操作读取固定站点的已登录身份信号，核对预期账号，保存加密认证状态后才发布 `identity_ref`。当前任务执行仍关闭，模型不会参与登录。

## 文件与调用关系

```text
受保护的本机 HTTP API（identities/routes.py）
  → 有界、带令牌的私有 Unix socket（identities/rpc.py）
  → Worker 内 LoginService（identities/service.py）
  → 固定站点核验（identities/sites.py）＋受管浏览器（sessions/）
  → SQLite 原子发布（identities/store.py、0009_identities.sql）
```

API 不创建第二个浏览器管理器。Worker 持有既有数据目录独占锁后开放 RPC；相同数据目录的短 socket 放在当前用户私有临时目录中，目录 0700、socket 0600，复用本机 API 令牌。请求最多 16 KiB、最多 16 个在途连接，HTTP 请求体最多 8 KiB；重复 JSON 键、额外字段和未知操作被拒绝。Worker 不在线时返回 503；没有自动重试确认或创建操作。

## 接口

均需通过 M1-12 的本机 Bearer、Host／Origin 和 Fetch Metadata 校验；响应 `Cache-Control: no-store`。正常前端通过同源代理注入令牌，令牌不传给网页。以下请求正文只含元数据，**不要发送密码、验证码、Cookie、认证 JSON 或任意 URL／选择器**。

| 方法与路径 | 请求／结果 |
| --- | --- |
| `GET /v1/identities/sites` | Worker 配置的受支持站点 |
| `POST /v1/identities/login-sessions` | `site_id`、必填 `expected_account`、可选 `expected_identity_ref`；201 返回登录准备记录 |
| `GET /v1/identities/login-sessions/{id}` | 查询最新状态并同步窗口丢失状态 |
| `POST /v1/identities/login-sessions/{id}/confirm` | `expected_version`；重新核验网页，返回最新记录 |
| `POST /v1/identities/login-sessions/{id}/close` | `expected_version`；关闭准备上下文，保留核验历史 |
| `GET /v1/identities` | 已建立的身份元数据；不返回认证引用、密文位置或密钥 |

新登录示例：`{"site_id":"github","expected_account":"your-account"}`。恢复既有身份时额外提供 `expected_identity_ref`，站点、账号、realm 和 origin 必须全部匹配。首次登录必须明确预期用户名，避免把浏览器中的其他账号误绑定；GitHub 用户名规范化为小写比较。

返回同时包含冻结接口的 `login_session_id`／`context_id` 和内部记录名 `login_id`／`session_id`（分别同值），还有 `state`、`state_version`、`expected_account`、`identity_ref`、`reason` 和 `capture_blocked:true`。`expected_identity_ref` 只是本次期望恢复的历史身份；只有本次 `state=VERIFIED` 时 `identity_ref` 才非空。

201 表示登录请求已持久化，浏览器打开失败仍返回可查询的 `FAILED` 记录。确认接口 200 表示检查完成；未登录、错账号、无法确定、加密失败或超时返回 `NEEDS_LOGIN` 和静态原因，**200 不能解释为登录成功**。旧版本或并发确认返回 409，参数错误 422，身份范围不符 403，记录不存在 404，Worker／RPC 不可用 503。确认前重新查询版本；连接中断后先查询原记录，不能盲目重发。

## 手动体验

分别运行 `./scripts/dev.sh api` 和 `./scripts/dev.sh worker`，两个进程使用相同 `WEBAGENT_DATA_DIR`。旧进程需要重启才能载入新增接口。当前完整账号就绪界面留在 M1-20；开发者可在项目目录打开交互式 Python：

```sh
PYTHONPATH=backend .venv/bin/python
```

在交互环境执行下面代码，替换账号名。令牌只在本机 Python 内存中使用，不打印、不放入命令行参数：

```python
import httpx
from webagent.config import Settings
from webagent.security import load_or_create_token
client = httpx.Client(base_url='http://127.0.0.1:8000', timeout=80,
    headers={'Authorization': 'Bearer ' + load_or_create_token(Settings.from_env().data_dir)})
response = client.post('/v1/identities/login-sessions',
    json={'site_id': 'github', 'expected_account': 'your-account'})
response.raise_for_status()
login = response.json()
print(login)
```

仅在新出现的站点窗口中完成登录。随后回到交互环境执行：

```python
path = '/v1/identities/login-sessions/' + login['login_session_id']
response = client.get(path)
response.raise_for_status()
login = response.json()
response = client.post(path + '/confirm', json={'expected_version': login['state_version']})
response.raise_for_status()
login = response.json()
print(login['state'], login['identity_ref'], login['reason'])
# 用最新版本主动关闭窗口；历史身份仍然保留。
response = client.post(path + '/close', json={'expected_version': login['state_version']})
response.raise_for_status()
client.close()
```

## 核验、存储与故障

状态顺序：`OPENING → AWAITING_USER → VERIFYING → VERIFIED`；未通过转为 `NEEDS_LOGIN`，可以用户修正后再次确认；窗口丢失或旧 Worker 代次转为 `LOST`，显式关闭为 `CLOSED`。关闭／丢失后的上下文不能续用，需创建新登录请求。

确认使用版本比较独占该记录。核验器重新 GET 固定身份页，要求正确 origin／路径、200 HTML、没有重定向或 Service Worker 响应，读取固定的已登录标记及用户名；通过 CDP 隔离环境与文档代次检测拒绝导航变化、重复或冲突身份信号。它不读取输入框值、完整 DOM、Cookie 或截图。

第一次核验匹配后，只在登录记录预留候选引用，不创建身份。会话模块将 storage state（含支持的 IndexedDB、无 sessionStorage）加密保存，随后再次新鲜核验账号及信号。两次一致、版本与上下文归属仍有效时，单个 SQLite 事务发布账号、密文元数据、不可变核验记录与登录状态事件。取消、并发关闭或提交失败不会发布半个身份；失败的外部写入可能留下未发布密文，当前不自动删除或复用它。

首次上下文始终保持匿名归属；恢复上下文只绑定此前已核实的身份。恢复认证文件不等于恢复完整浏览器内存，也不等于身份有效，必须重新核验。过期或错误身份使对应当前快照标为 `NEEDS_LOGIN`；旧恢复请求的失败不会覆盖更新的成功快照。身份列表始终返回 `requires_recheck`、`requires_identity_check` 和 `requires_business_check` 为 true，后续 Run 仍须独立核对身份与业务条件。

## 隐私与支持范围

`ManagedBrowser.context` 和通用 `save_auth` 拒绝所有登录上下文，包括已确认的上下文；只有身份服务能使用内部 `login_context` 做导航和固定信号核验。确认中的认证导出还要求持久状态为 VERIFYING、正确候选、版本、上下文和管理器代次。API／RPC 仅交换安全元数据，浏览器异常使用固定原因，不返回原始页面、输入值或异常正文；登录流程不调用模型，不启用截图、录屏或 tracing。

默认仅支持公开 `github.com` 的普通账号；不接受 API 自定义站点 URL，不支持 GitHub Enterprise／特殊托管用户名。官方依据是 [GitHub 账号设置入口](https://docs.github.com/en/account-and-profile/how-tos/account-management/changing-your-username) 与公开、无 Cookie 的 GitHub 页面和第一方脚本中的 `head meta[name=user-login]` 信号。站点改版、信号缺失或不能唯一核实时保守失败。

本次真实 Chromium 联调使用显式登记的本地合成站点，覆盖未登录、错账号、正确登录与过期恢复；没有读取用户已有浏览器配置或真实账号凭据，也没有宣称真实 GitHub 账号登录已验收。加密链路使用生产 AES 和内存测试密钥；原生 Keychain 由同次完整回归中的独立认证探针覆盖。真实 GitHub 的人工登录体验还需要用户自行验证，网络边界保持 M1-12 约束。
