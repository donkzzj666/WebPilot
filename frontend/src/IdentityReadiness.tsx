import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { apiFetch } from './api'
import {
  isIdentitySites, isLoginId, isLoginSession, isPublicIdentities, LOGIN_LABELS, loginReason,
} from './settings/identity-contracts'
import type { IdentitySite, LoginSession, PublicIdentity } from './settings/identity-contracts'
import './identity-readiness.css'

type Notice = { kind: 'error' | 'success' | 'conflict'; message: string }
type Operation = 'creating' | 'querying' | 'confirming' | 'closing'
const CONFIRMABLE = new Set<LoginSession['state']>(['AWAITING_USER', 'NEEDS_LOGIN'])
const CLOSED = new Set<LoginSession['state']>(['CLOSED', 'LOST', 'FAILED'])

class IdentityHTTPError extends Error {
  status: number
  constructor(status: number) { super('identity-request'); this.status = status }
}

function requestError(error: unknown, mutation: boolean): Notice {
  if (error instanceof IdentityHTTPError) {
    if (error.status === 409) return { kind: 'conflict', message: '登录状态或资源已变化，本次没有继续操作。请查询登录准备后再核对。' }
    if (error.status === 422) return { kind: 'error', message: '登录参数无效，请检查受支持站点、预期账号和登录准备 ID。输入已保留。' }
    if (error.status === 404) return { kind: 'error', message: '登录准备记录不存在，请核对 ID 后重新查询。' }
    if (error.status === 403) return { kind: 'error', message: '所选历史身份与站点或预期账号不匹配，本次没有继续操作。' }
    if (error.status === 503) return { kind: 'error', message: '登录服务暂不可用，请确认 Worker 已启动后查询。' }
    return { kind: 'error', message: `登录接口返回 HTTP ${error.status}，请先查询当前登录状态。` }
  }
  return { kind: 'error', message: mutation
    ? '操作结果无法确认，请先查询原登录准备；不要重复打开或确认。'
    : '暂时无法读取登录状态，请确认 API 和 Worker 已启动后重新查询。' }
}

function initialSessionId(): string {
  const value = new URL(window.location.href).searchParams.get('login_session')
  return isLoginId(value) ? value : ''
}

function rememberSession(id: string): void {
  const url = new URL(window.location.href)
  url.searchParams.set('login_session', id)
  window.history.replaceState(window.history.state, '', url)
}

async function readSession(id: string, signal: AbortSignal): Promise<LoginSession> {
  const response = await apiFetch(`/api/v1/identities/login-sessions/${encodeURIComponent(id)}`, {
    signal, headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw new IdentityHTTPError(response.status)
  const payload: unknown = await response.json()
  if (!isLoginSession(payload) || payload.login_session_id !== id) throw new Error('invalid-login')
  return payload
}

export type IdentityReadinessProps = { onIdentityChange?: () => void }

export default function IdentityReadiness({ onIdentityChange }: IdentityReadinessProps = {}) {
  const [sites, setSites] = useState<IdentitySite[]>([])
  const [identities, setIdentities] = useState<PublicIdentity[]>([])
  const [loading, setLoading] = useState(true)
  const [metadataError, setMetadataError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [siteId, setSiteId] = useState('')
  const [expectedAccount, setExpectedAccount] = useState('')
  const [restoreIdentity, setRestoreIdentity] = useState('')
  const [lookup, setLookup] = useState(initialSessionId)
  const [login, setLogin] = useState<LoginSession | null>(null)
  const [pending, setPending] = useState<Operation | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [needsQuery, setNeedsQuery] = useState(false)
  const [uncertainCreate, setUncertainCreate] = useState(false)
  const active = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const identityChanged = useRef(onIdentityChange)
  identityChanged.current = onIdentityChange
  const metadataSignature = useRef<string | null>(null)
  const notifyMetadataRefresh = useRef(false)
  const busy = pending !== null

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 10000)
    setLoading(true)
    setMetadataError(null)
    async function loadMetadata() {
      try {
        const responses = await Promise.all([
          apiFetch('/api/v1/identities/sites', { signal: controller.signal, headers: { Accept: 'application/json' } }),
          apiFetch('/api/v1/identities', { signal: controller.signal, headers: { Accept: 'application/json' } }),
        ])
        if (responses.some((response) => !response.ok)) throw new Error('metadata-unavailable')
        const [sitePayload, identityPayload]: unknown[] = await Promise.all(responses.map((response) => response.json()))
        if (!isIdentitySites(sitePayload) || !isPublicIdentities(identityPayload)) throw new Error('invalid-metadata')
        if (!disposed) {
          setSites(sitePayload)
          setIdentities(identityPayload)
          setSiteId((previous) => sitePayload.some((site) => site.site_id === previous) ? previous : sitePayload[0]?.site_id ?? '')
          // Compare only the public identity fields used by the UI. Initial
          // load does not notify the parent or cause a second load cascade.
          const signature = JSON.stringify(identityPayload.map((identity) => ({
            id: identity.identity_ref, account: identity.normalized_account, site: identity.site_id,
            realm: identity.realm, origin: identity.origin, state: identity.state,
            version: identity.state_version, updated: identity.updated_at,
          })).sort((left, right) => left.id.localeCompare(right.id)))
          if (notifyMetadataRefresh.current && metadataSignature.current !== null
            && signature !== metadataSignature.current) identityChanged.current?.()
          metadataSignature.current = signature
          notifyMetadataRefresh.current = false
        }
      } catch {
        if (!disposed) {
          setSites([])
          setIdentities([])
          setMetadataError('暂时无法读取账号状态，请确认 API 和 Worker 已启动。尚未确认任何账号就绪。')
        }
      } finally {
        // A failed metadata batch may leave response bodies unread or a peer
        // fetch pending. This controller belongs only to these read requests.
        controller.abort()
        window.clearTimeout(timeout)
        if (!disposed) setLoading(false)
      }
    }
    void loadMetadata()
    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [revision])

  useEffect(() => {
    mounted.current = true
    const id = initialSessionId()
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 10000)
    let disposed = false
    if (id) {
      setPending('querying')
      void readSession(id, controller.signal).then((value) => {
        if (!disposed) { setLogin(value); setLookup(id) }
      }).catch((error: unknown) => {
        if (!disposed) { setNotice(requestError(error, false)); setNeedsQuery(true) }
      }).finally(() => {
        window.clearTimeout(timeout)
        if (!disposed) setPending(null)
      })
    } else window.clearTimeout(timeout)
    return () => {
      disposed = true; mounted.current = false; controller.abort()
      active.current?.abort(); window.clearTimeout(timeout)
    }
  }, [])

  function installSession(value: LoginSession) {
    setLogin(value)
    setLookup(value.login_session_id)
    setNeedsQuery(false)
    setUncertainCreate(false)
    rememberSession(value.login_session_id)
  }

  async function operate(operation: Operation) {
    // The ref closes the gap before React renders a disabled button.
    if (active.current || busy) return
    if (operation === 'creating' && (!siteId || !expectedAccount.trim()
      || expectedAccount !== expectedAccount.trim() || expectedAccount.length > 200
      || /[\u0000-\u001f\u007f]/.test(expectedAccount))) {
      setNotice({ kind: 'error', message: '请先选择站点并填写该站点的预期账号，不要输入密码或验证码。' }); return
    }
    if (operation !== 'creating' && !isLoginId(lookup)) {
      setNotice({ kind: 'error', message: '请填写有效的登录准备 ID，不能使用网址或凭据。' }); return
    }
    if ((operation === 'confirming' || operation === 'closing')
      && (!login || lookup !== login.login_session_id || needsQuery)) {
      setNotice({ kind: 'error', message: '请先查询登录准备，核对站点、预期账号和状态后再操作。' }); return
    }
    const controller = new AbortController()
    active.current = controller
    setPending(operation)
    setNotice(null)
    const timeout = window.setTimeout(() => controller.abort(), operation === 'querying' ? 10000 : 90000)
    let mutationSent = false
    try {
      if (operation === 'querying') {
        const value = await readSession(lookup, controller.signal)
        if (mounted.current) installSession(value)
        return
      }
      let path = '/api/v1/identities/login-sessions'
      let body: unknown = {
        site_id: siteId, expected_account: expectedAccount,
        ...(restoreIdentity ? { expected_identity_ref: restoreIdentity } : {}),
      }
      if (operation !== 'creating' && login) {
        // Read the actual record before any confirm/close command. A change
        // requires a new human click; never retry a state transition silently.
        const current = await readSession(login.login_session_id, controller.signal)
        if (!mounted.current) return
        if (current.state_version !== login.state_version || current.state !== login.state) {
          installSession(current)
          setNotice({ kind: 'conflict', message: '登录状态已更新，本次没有继续操作。请核对最新状态后再确认。' })
          return
        }
        if (operation === 'confirming' && !CONFIRMABLE.has(current.state)) {
          installSession(current)
          setNotice({ kind: 'error', message: '当前登录状态不能确认，请先核对状态或建立新的登录准备。' }); return
        }
        if (operation === 'closing' && CLOSED.has(current.state)) {
          installSession(current)
          setNotice({ kind: 'error', message: '该登录窗口已结束，无需重复关闭。' }); return
        }
        path += `/${encodeURIComponent(current.login_session_id)}/${operation === 'confirming' ? 'confirm' : 'close'}`
        body = { expected_version: current.state_version }
      }
      mutationSent = true
      const response = await apiFetch(path, {
        method: 'POST', signal: controller.signal,
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
      if (!response.ok) throw new IdentityHTTPError(response.status)
      const value: unknown = await response.json()
      if (!isLoginSession(value)
        || (operation !== 'creating' && value.login_session_id !== login?.login_session_id)) throw new Error('invalid-login')
      if (!mounted.current) return
      installSession(value)
      notifyMetadataRefresh.current = false
      setRevision((previous) => previous + 1)
      identityChanged.current?.()
      if (operation === 'creating') setNotice({ kind: value.state === 'AWAITING_USER' ? 'success' : 'error', message:
        value.state === 'AWAITING_USER' ? '登录准备已建立。请在受管站点窗口完成登录，再回到这里确认页面身份。' : '登录准备已记录，请查看实际状态；窗口打开失败不代表已登录。' })
      else if (operation === 'closing') setNotice({ kind: 'success', message: '已读取关闭后的登录状态，历史身份核验记录仍保留。' })
      else setNotice(value.state === 'VERIFIED'
        ? { kind: 'success', message: '本次站点身份已核实。每个 Run 仍须重新检查身份及业务条件。' }
        : { kind: 'error', message: '本次身份尚未核实，请查看原因。接口受理或 HTTP 200 不代表账号就绪。' })
    } catch (error: unknown) {
      if (mounted.current) {
        setNotice(requestError(error, mutationSent))
        // Definite input errors retain the draft; uncertain mutations and
        // conflicts require an explicit read before a further command.
        const uncertain = !(error instanceof IdentityHTTPError) || error.status >= 500
        if (uncertain || (error instanceof IdentityHTTPError && error.status === 409)) {
          setNeedsQuery(true)
          if (operation === 'creating' && mutationSent && uncertain) setUncertainCreate(true)
        }
      }
    } finally {
      window.clearTimeout(timeout)
      if (active.current === controller) active.current = null
      if (mounted.current) setPending(null)
    }
  }

  function create(event: FormEvent<HTMLFormElement>) { event.preventDefault(); void operate('creating') }
  const currentWindowOpen = login !== null && !CLOSED.has(login.state)
  const confirmedRecord = login !== null && lookup === login.login_session_id && !needsQuery
  const restoreChoices = identities.filter((identity) => identity.site_id === siteId)
  const selectedSite = sites.find((site) => site.site_id === siteId)

  return <section className="identity-readiness" data-testid="identity-readiness" aria-labelledby="identity-readiness-heading">
    <div className="identity-heading"><div><span className="section-number">ACCOUNT</span><h2 id="identity-readiness-heading">账号就绪</h2></div>
      <button type="button" id="identity-refresh" disabled={loading || busy} onClick={() => { notifyMetadataRefresh.current = true; setRevision((previous) => previous + 1) }}>
        {loading ? '正在读取账号…' : '刷新账号状态'}
      </button></div>
    <p className="identity-intro">账号状态来自 Worker 的持久身份记录。历史核验通过表示曾核实过；每个 Run 仍会检查当前身份和业务条件。</p>
    {metadataError && <p className="identity-notice error" role="alert">{metadataError}</p>}
    {!loading && !metadataError && identities.length === 0 && <p className="identity-empty" data-testid="identity-empty">尚未准备任何账号。需要登录的任务请先建立登录准备。</p>}
    {identities.length > 0 && <ul className="identity-records" aria-label="已建立的身份记录">{identities.map((identity) => <li key={identity.identity_ref} data-testid={`identity-record-${identity.identity_ref}`}>
      <div><strong>{identity.normalized_account}</strong><span className={`identity-state ${identity.state === 'VERIFIED' ? 'verified' : 'needs-login'}`}>
        {identity.state === 'VERIFIED' ? '历史身份已核实 · 本次 Run 待核验' : '认证可能过期或失效 · 需要重新登录'}
      </span></div>
      <p>{identity.site_id} · {identity.realm === 'public' ? '公开站点' : '评测站点'} · {identity.origin}</p>
      <p>身份引用：<code>{identity.identity_ref}</code> · 状态版本 {identity.state_version}</p>
      <p>最近更新：<time dateTime={identity.updated_at}>{new Date(identity.updated_at).toLocaleString('zh-CN', { hour12: false })}</time></p>
    </li>)}</ul>}

    <div className="identity-login">
      <h3>登录准备</h3>
      <p className="identity-note">只在 Worker 打开的站点窗口中输入密码和验证码。这里仅提交站点、预期账号和版本；登录过程不向模型发送页面、截图或凭据。</p>
      <form onSubmit={create} autoComplete="off"><fieldset disabled={busy || loading || metadataError !== null || sites.length === 0}>
        <legend className="identity-visually-hidden">创建登录准备</legend>
        <div className="identity-fields">
          <label htmlFor="identity-site">站点<select id="identity-site" value={siteId} onChange={(event) => { setSiteId(event.target.value); setRestoreIdentity('') }}>
            {sites.length === 0 && <option value="">暂无可用站点</option>}{sites.map((site) => <option key={site.site_id} value={site.site_id}>{site.site_id} · {site.realm === 'public' ? '公开站点' : '评测站点'}</option>)}
          </select></label>
          <label htmlFor="identity-expected-account">预期账号<input id="identity-expected-account" type="text" maxLength={200} required spellCheck={false}
            placeholder="填写该站点的账号名" value={expectedAccount} onChange={(event) => setExpectedAccount(event.target.value)} /></label>
        </div>
        {selectedSite && <p className="identity-note">登录站点：{selectedSite.login_url}。请核对 Worker 窗口中的站点与预期账号。</p>}
        {restoreChoices.length > 0 && <label className="identity-restore" htmlFor="identity-restore">恢复历史身份（可选）
          <select id="identity-restore" value={restoreIdentity} onChange={(event) => setRestoreIdentity(event.target.value)}><option value="">新建登录准备</option>
            {restoreChoices.map((identity) => <option key={identity.identity_ref} value={identity.identity_ref}>{identity.normalized_account} · {identity.state === 'VERIFIED' ? '曾核实' : '需重登录'}</option>)}
          </select><span>恢复时预期账号必须与历史身份一致，认证恢复后仍需重新核验。</span></label>}
        <button id="identity-create" type="submit" className="identity-primary" disabled={currentWindowOpen || uncertainCreate || !expectedAccount.trim()}>
          {pending === 'creating' ? '正在建立登录准备…' : '打开登录准备窗口'}
        </button>
        {currentWindowOpen && <p className="identity-note">当前登录准备尚未结束，请先查询并关闭当前窗口再建立新的准备。</p>}
        {uncertainCreate && <p className="identity-note">打开结果尚未确认。若已有登录准备 ID，请查询原记录，避免重复创建窗口。</p>}
      </fieldset></form>

      <form className="identity-lookup" onSubmit={(event) => { event.preventDefault(); void operate('querying') }} autoComplete="off">
        <label htmlFor="identity-login-session-id">登录准备 ID<input id="identity-login-session-id" type="text" maxLength={200} required spellCheck={false}
          placeholder="创建后自动填写，也可查询已有 ID" disabled={busy} value={lookup}
          onChange={(event) => { setLookup(event.target.value); setNeedsQuery(true) }} /></label>
        <button id="identity-query" type="submit" disabled={busy || !lookup}>{pending === 'querying' ? '正在查询…' : '查询登录准备'}</button>
      </form>
      {login && <div className="identity-session" aria-live="polite">
        <div className="identity-session-heading"><strong data-testid="identity-login-state">{LOGIN_LABELS[login.state]}</strong><span>状态版本 {login.state_version}</span></div>
        <p>站点：{login.site_id} · {login.origin}</p><p>本次预期账号：<strong data-testid="identity-login-account">{login.expected_account ?? '未指定'}</strong></p>
        <p>登录准备：<code>{login.login_session_id}</code></p>
        {loginReason(login.reason) && <p className="identity-reason" data-testid="identity-login-reason">{loginReason(login.reason)}</p>}
        {login.identity_ref && <p>本次已核实身份引用：<code>{login.identity_ref}</code></p>}
        {needsQuery && <p className="identity-reason">以上为上次已读取的记录。请查询当前状态后再操作。</p>}
        <div className="identity-actions"><button id="identity-confirm" type="button" className="identity-primary"
          disabled={busy || !confirmedRecord || !CONFIRMABLE.has(login.state)} onClick={() => { void operate('confirming') }}>
          {pending === 'confirming' ? '正在核对身份…' : '确认页面身份'}</button>
          <button id="identity-close" type="button" disabled={busy || !confirmedRecord || CLOSED.has(login.state)} onClick={() => { void operate('closing') }}>
            {pending === 'closing' ? '正在关闭…' : '关闭登录准备窗口'}</button></div>
      </div>}
      {notice && <p className={`identity-notice ${notice.kind}`} role={notice.kind === 'success' ? 'status' : 'alert'}>{notice.message}</p>}
    </div>
  </section>
}
