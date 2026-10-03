# 受管浏览器上下文与会话生命周期（M1-08）

`webagent.sessions` 是 Worker 内部的会话模块。`ManagedBrowser` 默认启动有界面的、项目锁定版本的 Chromium，每个会话使用独立的非持久 `BrowserContext`。它不连接用户已经打开的 Chrome，也不加载日常浏览器 profile。

Worker 启动时取得当前数据目录的会话管理锁并检查旧会话记录，首次创建会话才真正启动 Chromium。正常退出先关闭会话代理及受管上下文，再关闭 Chromium／Playwright。当前 Worker 仍不领取或执行任务；[M1-12 网络边界](network-security.md)已接入管理器，登录界面、身份核实和动作网关由后续子任务接入。

启动私有 Playwright driver 前会关闭继承的协议调试输出与调试器变量，避免 IPC 中的认证状态进入普通日志。当前进程锁使用 POSIX `flock`，系统密钥后端为 macOS Keychain；本项在 macOS 上验收。

## 生命周期与归属

| 状态 | 含义 |
| --- | --- |
| OPENING | 已预留上下文名额，正在创建浏览器资源 |
| OPEN | 上下文及初始页面创建成功，不表示登录已确认 |
| CLOSING | 会话模块已经请求正常关闭 |
| CLOSED | 正常关闭完成，保留历史 |
| LOST | 用户关闭最后窗口、页面崩溃、浏览器断连或旧管理器异常退出，原上下文不可再使用 |

归属包含 `kind`（run／login／verification）、`owner_id`、`site_id`、`identity_ref` 和 `realm`（public／webarena）。取得或关闭会话必须给出完整相同的归属。run 类型还要求已存在且未结束的 Run；本模块不自动创建 Run。

同一数据目录只允许一个会话管理器持有进程锁，所有数据库写入使用短事务。SQLite 同时限制最多 4 个 OPENING／OPEN／CLOSING 名额；内存管理器还保留尚未确认释放的物理资源。满额后返回资源冲突，不能擅自关闭等待、暂停或人工接管中的窗口。全机调度、账号／仓库逻辑锁与排队属于 M1-10；BrowserContext 的本地隔离不能隔离服务器端同一账号的数据。

每次状态变更和会话事件同事务提交。管理器必须先取得独占锁才能把旧管理器留下的非终态会话标为 LOST，不根据一个旧 PID 猜测并杀进程，也不重新连接旧页面继续点击。

## 认证状态

`save_auth(session_id, owner)` 显式读取当前上下文的 Playwright `storage_state(indexed_db=True)`，在内存中交给 `AuthStateStore` 加密。正常关闭不会隐式保存或覆盖认证快照。

保存认证快照必须有明确的非空身份引用；匿名或尚未确认身份的上下文可正常创建和关闭，但不能保存认证。认证 JSON 大小上限为 8 MiB，超过上限会明确失败。

- 使用 AES-256-GCM，随机密钥与 nonce；每份快照使用新的随机引用，密文和引用不可覆盖。
- 密钥保存在独立的 Keychain 服务 `WebPilot.browser-auth-keys.v1`，与模型 API Key 分开。普通 SQLite 只保存引用、范围、密文摘要及时间，不含 Cookie、Token 或认证 JSON。
- 密文认证数据绑定快照引用、站点、身份引用和 public／webarena 环境，禁止跨范围加载。
- 文件保存在 `data/sessions/auth/`，目录为 0700、文件为 0600，并检查符号链接、硬链接、路径与文件类型；先写密文再发布引用，不产生明文临时文件。
- OS 凭据不可用、密文被修改、摘要不一致或范围不匹配时拒绝恢复，不回退为明文，也不假装已登录。发布数据库引用失败可能留下未引用的密文／密钥，当前保留供后续协调清理，不猜测式删除可能已经提交的快照。

认证文件的读取入口只位于会话模块；API、模型适配器、图状态和普通导出没有认证原文接口。这是应用模块边界和本机文件权限控制，不是对同一 OS 用户下任意恶意代码的进程沙箱保证。不要将用户认证目录提交到仓库或复制进普通验证工件。

OS 凭据和文件写入在线程中执行，异步超时不等于可以强行撤销底层调用；晚到结果不会发布为当前会话认证引用，可能留下未引用的密文与密钥。后续清理必须先核对数据库引用。

## 重建不等于恢复完整浏览器

创建新上下文或从已登记的 `auth_ref` 重建时，都会设置 `requires_identity_check=true` 和 `requires_business_check=true`。意外丢失还会写入 `recheck_required` 事件，供后续身份和执行模块接续；本阶段不提供可绕过核验的“设为已登录”或“恢复执行”接口。

Cookie、localStorage 和已保存的 IndexedDB 可作为认证恢复线索，DOM、标签页执行栈、在途请求和页面内存不在快照里。`sessionStorage` 不会被本模块保存或重注入；网站依赖它时可能需要重新登录。原上下文句柄一旦丢失即失效，不能因为加载了认证文件就复用旧观察或重放上次点击。

这与 Playwright 的 [BrowserContext 隔离](https://playwright.dev/python/docs/browser-contexts)、[认证状态与 sessionStorage 说明](https://playwright.dev/python/docs/auth) 一致。加密使用 cryptography 的 [AESGCM 实现](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM)，不自制加密算法。

## 内部接入方式

在通过网络策略、身份准备和动作网关的模块中使用下列接口；不要把原始 Playwright 对象暴露给模型或普通 HTTP 客户端。

```python
from webagent.sessions import SessionOwner
from webagent.sessions.manager import ManagedBrowser

manager = ManagedBrowser(settings)  # 默认有界面；start 只初始化管理能力
await manager.start()
try:
    owner = SessionOwner("login", "login-request-id", "declared-site")
    session = await manager.create(owner)
    context = await manager.context(session.session_id, owner)
    # 后续模块核验站点和身份；浏览器动作需经过 M1-12/M1-13 边界。
    await manager.close(session.session_id, owner)
finally:
    await manager.aclose()
```

只有显式调用 `save_auth` 时才读取和保存认证数据。快照引用被登记后，可通过 `create(owner, auth_ref=ref, replaces=old_session_id)` 创建关联的新一代会话；旧记录保持不可变，同一归属之外的替换会被拒绝。

## 验证

```sh
./scripts/check.sh
PYTHONPATH=backend PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" \
  .venv/bin/python scripts/verification/verify_browser_sessions.py \
  --output-dir /tmp/webpilot-browser-sessions
```

真实验收使用可见 Chromium、只提供合成页面的回环服务、独立 SQLite 和合成凭据。它覆盖 Cookie／localStorage 隔离、加密保存与重建、sessionStorage 缺失、窗口关闭、精确终止本次 Chromium、管理器异常退出、名额限制及旧句柄拒绝。不会访问真实网站、用户浏览器或已有用户凭据。结果见 [M1-08 验证记录](../m1/records/M1-08.md)。
