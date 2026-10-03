# 本地 API 防护与浏览器网络边界（M1-12）

本项提供两道独立防线：本地 API 校验调用者，受管浏览器通过认证代理检查实际网络目的地。任务仍不会自动执行；来源授权、登录身份、动作权限和调度分别由后续模块使用这些能力。

## 本机工作台如何访问 API

API 和前端固定监听 `127.0.0.1`，端口从环境变量读取。请使用 `http://127.0.0.1:5173` 打开工作台；任意主机名、`localhost` 别名或覆盖监听地址的启动参数不会自动获得授权。

API 首次启动时，在数据目录的 `.security/local-api-token` 创建 384 位随机令牌，目录权限 0700、文件权限 0600。前端开发服务器读取同一文件；所有 API 请求，包括健康查询、任务读取和 SSE，均需要 Bearer 认证。这个令牌不是模型密钥，也不放入 Cookie、URL、浏览器存储或网页源码。

```text
工作台 apiFetch('/api/...')
  → Vite 检查 Host / Origin / Fetch Metadata / X-WebPilot-Client
  → Vite 服务端注入 Bearer，转发到固定的 127.0.0.1 API
  → API 再次检查 Bearer / Host / Origin / Fetch Metadata
  → 执行业务接口
```

浏览器请求必须是同源脚本请求，带 `X-WebPilot-Client: 1` 和 `Sec-Fetch-Dest: empty`。跨站表单、图片、iframe、预检和 `no-cors` 请求不能使用代理。API 不开放跨域 CORS；前端通过同源 `/api/` 访问，无需跨域许可。所有 API 响应禁止缓存，SSE 保留 `no-transform`。API WebSocket 握手直接拒绝。

启动前端、API、Worker 时应使用相同的 `WEBAGENT_DATA_DIR` 和端口配置。没有有效令牌返回 401；来源或主机越界返回 403。直接在地址栏打开 API 得到 401 是正常防护行为。已有接口样例必须带认证，不能把测试辅助函数的显式模拟策略用于生产服务。

这道边界防止网页跨站调用，不对同一 OS 用户下能读取私有文件的任意本地程序作隔离保证。令牌目录已从 Git 和验证工件中排除；不要将它复制到截图、工件或日志。

## 浏览器出口检查

每个受管 BrowserContext 使用独立的回环代理和随机代理凭据，凭据只在 Worker 与 Playwright 之间传递。浏览器后台使用空白名单的默认拒绝代理。调用方不能用 `launch_options` 覆盖代理、DNS、用户配置目录、扩展或网络功能开关。

| 策略 | 允许范围 |
| --- | --- |
| `public` | HTTP／HTTPS 公网单播地址；HTTPS CONNECT 限 443。拒绝本机、私有、链路本地、元数据、保留、多播、IPv4 映射 IPv6 等地址 |
| `webarena` | 管理者显式登记的完整“协议、主机、业务端口”组合，默认登记为空。不能按整个主机开放 |
| 控制／管理端口 | 优先拒绝，即使被误写到 WebArena 允许表中也不能访问；管理端口对所有主机拒绝以覆盖 DNS 别名 |

请求通过以下步骤才能向业务服务器发送数据：

1. 严格解析 URL／CONNECT authority、Host、协议与端口；拒绝混淆及含凭据的地址。
2. 每次新连接重新解析域名，并检查所有 A／AAAA 答案。公网答案中只要混入私网地址，就整体拒绝。
3. 选择已经检查的数字 IP 建立 TCP 连接，不让连接库再次解析域名。
4. 在发送请求或确认 CONNECT 之前，核对 socket 实际对端的 IP 和端口。

重定向、iframe、弹窗、页面请求和 Worker 请求都必须经过相同出口，不能依赖只检查第一条导航 URL。连接到代理自身会被拒绝；代理关闭后不会回退为直连。代理日志只记录协议、主机、端口及固定结果，不记录 URL 路径／查询、Cookie、请求正文或凭据。

## 其他浏览器通道

- 创建任何页面前安装上下文路由和 WebSocket 路由，阻止本地 `file://` 请求，关闭通常的 WebSocket 连接；新窗口首请求也受上下文规则约束。
- `service_workers='block'` 阻止正常的 Service Worker 注册。它不是浏览器引擎完全关闭的保证；验收特意用原生执行环境绕过这层包装，确认实际 Worker 请求仍由代理拒绝越界目的地。
- 固定 `--proxy-bypass-list=<-loopback>`，撤销 Chromium 对回环目标的隐式代理绕过；浏览器端 DNS 不自行解析外部地址，DNS 检查由代理完成。
- 禁用 QUIC，限制 WebRTC 非代理 UDP，并在页面层关闭 WebRTC／WebTransport 接口作为补充。Chrome 与 headless-shell 使用不同的 WebRTC 策略参数，启动时同时设置两个已验证参数。验收在不含页面包装的独立原生 JS 环境中检查 STUN、TURN TCP 和 WebTransport，确认禁止目标没有收到 TCP 连接或 UDP 包。

`about:blank`、内联 `data:`／`blob:` 等不发网络请求的页面不等于网络出口。后续动作网关仍需限制显式导航和任意脚本能力；不得向模型暴露原始 Playwright／CDP 对象。这里也不声称提供抵御浏览器漏洞或恶意本机程序的 OS 沙箱。

## 登记基准业务地址

配置只由启动 Worker 的可信本机环境提供，不从网页文字、模型输出或普通任务参数读取。例如：

```sh
# 示例，必须改成已部署并核实的基准业务地址；不能直接当真实资源使用。
export WEBAGENT_WEBARENA_ORIGINS='["http://benchmark.example:7770"]'
export WEBAGENT_NETWORK_DENIED_ORIGINS='["http://benchmark.example:9001"]'
export WEBAGENT_NETWORK_ADMIN_PORTS='[9002]'
./scripts/dev.sh worker
```

配置不接受通配主机、路径、查询、userinfo、非法端口或其他协议。默认控制端口包含 8000、5173、4173、9222、9333，并合并实际 API／UI／preview 端口。已登记的管理端点端口也加入优先拒绝集合；同主机业务和管理端口必须分别配置。

当前 M0 的真实 WebArena 部署仍未就绪，允许表默认为空；本项不会自动把 M0 候选来源或测试夹具当生产授权。公网网络可达也不代表任务获准读取或修改该来源。

## 支持范围与验证入口

HTTP 代理只接受严格的 Content-Length 请求体（最大 8 MiB），拒绝 Transfer-Encoding、Expect、Upgrade、重复头等不支持或有歧义的请求；每条 HTTP 连接只转发一次请求。HTTPS CONNECT 校验目标后透传 TLS，不中间人解密，不关闭网站证书验证。流式上传、实时媒体或依赖被禁通道的网站可能不可用，不能降级直连来绕过限制。

```sh
./scripts/check.sh
.venv/bin/python scripts/verification/verify_api_security.py --output-dir /tmp/webpilot-api-security
.venv/bin/python scripts/verification/verify_network_boundary.py --output-dir /tmp/webpilot-network
.venv/bin/python scripts/verification/verify_network_boundary.py --headless --output-dir /tmp/webpilot-network-headless
```

所有验收使用新数据目录、合成凭据和本机 HTTP／TLS／TCP／UDP 夹具；不会访问真实业务网站或读取已有密钥。结果及局限见 [M1-12 验证记录](../m1/records/M1-12.md)。

官方依据：[Playwright 网络和 Service Worker](https://playwright.dev/python/docs/network)、[上下文路由](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route)、[Chromium 代理与回环绕过](https://chromium.googlesource.com/chromium/src/+show/main/net/docs/proxy.md)、[Chrome 命令行到首选项映射](https://raw.githubusercontent.com/chromium/chromium/main/chrome/browser/prefs/chrome_command_line_pref_store.cc)、[RFC 9110 CONNECT](https://www.rfc-editor.org/rfc/rfc9110.html#name-connect)、[RFC 9112 请求体边界](https://www.rfc-editor.org/rfc/rfc9112.html#name-message-body-length)。
