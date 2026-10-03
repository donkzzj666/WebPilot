# 结构化浏览器动作网关（M1-13）

网关是受信任 Worker 的内部组件。它把 M1-05 的严格 `Action` 协议与 M1-08 受管会话、M1-10 持久执行资格、M1-11 预算和 M1-12 网络边界连接起来。当前默认 Worker 仍不装配任务图执行器，健康页继续报告 `task_execution_enabled=false`。

## 目录与调用顺序

- `gateway/service.py`：观察、解析、权限核对、有界派发和十步等复合操作展开。
- `gateway/browser.py`：固定 Playwright 动作和开发者控制的隔离 DOM 探针。
- `gateway/permissions.py`：可信页面适配器授权；模型不能自己授予写入权限。
- `gateway/store.py`：观察绑定、预算与 INTENT 同事务、逐步结果及事件。
- `db/sql/0012_gateway.sql`：显式 v12 增量迁移；保留已有数据及 v1～v11 摘要。

```python
# 仅在可信 Worker 中：token 来自 SchedulerStore.claim，owner 属于该 Run。
session = await managed.create(owner, execution_token=token, gateway_downloads=True)
gateway = BrowserGateway.from_managed(managed, session, scheduler=scheduler)
await gateway.navigate(token, permitted_url, 'step-initial-navigation')
snapshot = await gateway.observe(token)
# action 使用 models.schema.Action 的固定结构，绑定该 snapshot / tab / frame。
record = await gateway.dispatch(token, action)
```

初始 `about:blank` 观察仅允许第一次授权 URL 导航。此导航也先保存 INTENT 并扣动作、内容页及站点节奏。`sequence(token, builders)` 对每项重新观察和派发，十个原子交互产生十条步骤、十次动作扣额；任何非 COMPLETED 结果都会停止后续步骤。观察、DOM 读取、截图另计数，不占 150 次交互上限。

## 固定动作与定位

允许 `navigate`、`click`、`input`、`keypress`、`select`、`scroll`、`switch_tab`、`read_visible`、`screenshot`、`download_attachment`。协议拒绝额外字段、任意 JavaScript、Shell、HTTP、SQL、CSS 选择器或本机路径。内部 DOM 脚本由代码固定，运行在独立 CDP world，页面不能通过伪造同名全局或修改主 world 的 DOM 方法替换探针。

定位优先语义 role／accessible_name／label，其次精确 DOM 属性 `id`／`data-testid`／`name`／`href`，最后截图坐标。可信适配器可显式排序候选，网关不会为模型猜测选择器。语义和 DOM 目标均要求唯一可见、可用，并在派发前再次核对节点身份与属性。每个请求和重定向由原网络边界及 CDP 守卫共同检查；只回答自有受管代理的精确认证挑战，不把代理凭据提供给网站。尚未装好守卫的弹窗首次导航会阻止，可信 Worker 可先创建空白受管标签，再经网关导航和切换。只读点击须指向当前真实 GET 链接、同一逻辑站点和契约允许来源；输入、按键、选择和写入按钮由可信页面适配器授权。

快照绑定 Run、epoch、状态版本、会话及 manager 代次、标签、帧、页面版本与视口。任何动作使用一次最新观察；派发后需重新观察。坐标还绑定该快照的截图引用、尺寸和像素 SHA-256，在准备及最终派发前各重拍一次并分别计入截图预算；画布／视频像素变化、遮挡、标签／帧变化和视口调整都不能沿用旧截图。

## 持久资格与写入授权

动作前读持久数据库，检查完整资源租约、Worker 代次、epoch、控制权、预算、受管会话及冻结任务契约；没有自动续签旧动作 token。浏览器准备完成后，在短事务内再检查相同资格与观察，保存动作 INTENT、必要写入意图、预算和 `action_recorded` 事件，然后才调用浏览器。浏览器最后还要核对当前页面与目标指纹。

同一 `step_id` 不会获得第二次派发许可，重复或冲突请求会返回既有结果或冲突。日志保存动作类型、参数哈希、目标哈希和结果哈希；不保存输入文本、页面正文或下载字节。动作扣额不因失败、超时或未知结果退回。

写动作默认拒绝。可信页面适配器必须从实际当前页面读取 repository、branch、base SHA、operation、files 和 identity，返回 `WriteAuthorization`，与冻结仓库策略逐项一致；SQLite 再检查已确认身份和仓库写租约。网络窗口只允许授权的精确 `(method, url)` 写端点，不能把任意 POST 当成已获准。

本项只在自有合成夹具验证写入。Playwright 调用返回不构成业务回执；所有外部写入仍记 UNKNOWN 并隔离相关资源。恢复与控制由 M1-17／M1-18 接入，业务结果查证协议由 M1-23 实现。普通会话访问会拒绝未决写入。唯一例外是内部一次性派发窗口：只允许当前已提交步骤对应的唯一 INTENT，并重查会话代次、完整执行 token、控制权、仓库资源、预算及无隔离；UNKNOWN、其他意图、旧资格和 RECONCILING 均不能使用该窗口，使用过的步骤不能再次进入。未决写入不能继续自动派发，也不能报告业务成功。

## 超时与附件

默认动作超时 30 秒；冻结契约可以收紧到 1～30 秒，准备、授权及派发共享该次动作的绝对截止。独立预算观察器不依赖浏览器协程是否返回：动作超时将已提交的派发意图记为 UNKNOWN，撤销执行资格后再有界取消；预算耗尽先通过统一调度截止使 Run 失败；晚到结果不进入成功结果缓存。调用方取消也先持久撤权。

下载仅在显式 `gateway_downloads=True` 的受管 Run 上下文启用，一次许可绑定当前页面、精确附件 URL 和执行 token；未授权或第二次下载会取消。`link_evidence_id` 是 Worker 内存中从当前快照生成的链接引用，必须匹配当前真实链接，不能传本机路径或任意 URL。附件读取默认最多 8 MiB，Playwright 临时文件在完成后删除，字节只短暂保留于 Worker 内存。

## 与后续模块的边界

`gateway_observations` 的正文为空、标题为固定占位，`redaction_status=BLOCKED`，用于动作绑定。M1-14 已接入[证据保存、过滤和受控读取](evidence.md)：真实来源的观察自动保存受限原件和独立脱敏视图，`model_observation()` 返回已登记的模型 DTO；`about:blank` 仅用于启动导航。截图／链接内部引用及 `local_result()` 缓存仍不能直接公开或作为过滤资格。动作回执也先保存工件再记完成；敏感动作和磁盘满会在派发前被阻止。成功聚合属于 M1-15，产品图循环属于 M1-16；没有公开浏览器派发 API。

## 验证入口

`./scripts/check.sh --headed` 包含组件测试和 `verify_gateway.py` 的自有 HTTP／Chromium 夹具。专项组件在 `tests/storage/test_gateway_store.py` 与 `tests/unit/test_gateway_*.py`；验收结果在 [M1-13 验证记录](../m1/records/M1-13.md)。全部验证使用隔离数据库及自有浏览器进程，不修改默认业务数据或真实账号。
