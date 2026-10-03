/** Public, nonsecret metadata returned by the existing identity service. */
export type IdentitySite = {
  site_id: string
  realm: 'public' | 'webarena'
  login_url: string
  verification_url: string
  adapter_id: string
}

export type PublicIdentity = {
  identity_ref: string
  site_id: string
  realm: 'public' | 'webarena'
  origin: string
  normalized_account: string
  state: 'VERIFIED' | 'NEEDS_LOGIN'
  state_version: number
  created_at: string
  updated_at: string
  requires_recheck: true
  requires_identity_check: true
  requires_business_check: true
}

export const LOGIN_LABELS = {
  OPENING: '正在打开登录窗口', AWAITING_USER: '等待在站点窗口登录', VERIFYING: '正在核对站点身份',
  VERIFIED: '本次身份已核实', NEEDS_LOGIN: '需要重新登录并核对', LOST: '登录窗口已丢失',
  CLOSED: '登录准备已关闭', FAILED: '登录准备失败',
} as const

export type LoginSession = {
  login_session_id: string
  site_id: string
  realm: 'public' | 'webarena'
  origin: string
  expected_account: string | null
  expected_identity_ref: string | null
  state: keyof typeof LOGIN_LABELS
  state_version: number
  identity_ref: string | null
  reason: string | null
  capture_blocked: true
  created_at: string
  updated_at: string
}

const REASONS: Record<string, string> = {
  wrong_account: '当前站点账号与预期账号不一致，请在站点窗口切换账号后再次确认。',
  account_mismatch: '当前站点账号与预期账号不一致，请在站点窗口切换账号后再次确认。',
  not_logged_in: '站点尚未登录，请在站点窗口完成登录。',
  not_authenticated: '站点尚未登录，请在站点窗口完成登录。',
  wrong_origin: '站点来源不匹配，身份未通过核验。',
  ambiguous_identity: '无法唯一确认站点身份，身份未通过核验。',
  verification_timeout: '身份核验超时，请先查询当前登录状态。',
  session_lost: '登录窗口已丢失，请建立新的登录准备。',
  session_closed: '登录窗口已关闭，历史核验记录仍保留。',
  manager_restarted: 'Worker 已重启，原登录窗口无法继续使用。',
  auth_unavailable: '认证快照暂不可用，需要重新登录核对。',
  verification_failed: '身份核验未通过，当前不能据此授权任务。',
  operation_cancelled: '身份核验已中断，请先查询当前登录状态。',
  launch_failed: '登录窗口未能启动，请确认 Worker 和浏览器可用。',
  browser_unavailable: '浏览器暂不可用，请确认 Worker 已启动。',
  navigation_failed: '登录站点无法打开，请检查连接后查询当前状态。',
  identity_conflict: '身份记录发生冲突，请重新查询并核对账号。',
  verification_changed: '两次身份信号不一致，身份未发布。',
  storage_unavailable: '认证快照无法保存，本次身份尚未就绪。',
  unverifiable: '站点身份信号无法核实，当前不能据此授权任务。',
  unknown: '身份核验未通过，请查询最新状态并重新核对。',
}

export function loginReason(reason: string | null): string | null {
  return reason === null ? null : REASONS[reason] ?? '身份核验未通过，请查询最新状态并重新核对。'
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function text(value: unknown, max = 300): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max
    && value === value.trim() && !/[\u0000-\u001f\u007f]/.test(value)
}

export function isLoginId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(value)
}

function version(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function realm(value: unknown): boolean {
  return value === 'public' || value === 'webarena'
}

function date(value: unknown): boolean {
  return text(value, 100) && Number.isFinite(Date.parse(value))
}

function metadataURL(value: unknown): value is string {
  if (!text(value, 2048)) return false
  try {
    const url = new URL(value)
    return (url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password && !url.hash
  } catch { return false }
}

export function isIdentitySites(value: unknown): value is IdentitySite[] {
  return Array.isArray(value) && value.length <= 1000 && value.every((site: unknown) => record(site)
    && text(site.site_id, 100) && realm(site.realm) && metadataURL(site.login_url)
    && metadataURL(site.verification_url) && text(site.adapter_id, 200))
}

export function isPublicIdentities(value: unknown): value is PublicIdentity[] {
  return Array.isArray(value) && value.length <= 1000 && value.every((identity: unknown) => record(identity)
    && isLoginId(identity.identity_ref) && text(identity.site_id, 100) && realm(identity.realm)
    && metadataURL(identity.origin) && text(identity.normalized_account, 256)
    && (identity.state === 'VERIFIED' || identity.state === 'NEEDS_LOGIN')
    && version(identity.state_version) && date(identity.created_at) && date(identity.updated_at)
    && identity.requires_recheck === true && identity.requires_identity_check === true
    && identity.requires_business_check === true)
}

export function isLoginSession(value: unknown): value is LoginSession {
  if (!record(value)) return false
  return isLoginId(value.login_session_id) && text(value.site_id, 100) && realm(value.realm)
    && metadataURL(value.origin) && version(value.state_version)
    && typeof value.state === 'string' && Object.hasOwn(LOGIN_LABELS, value.state)
    && (value.expected_account === null || text(value.expected_account, 256))
    && (value.expected_identity_ref === null || isLoginId(value.expected_identity_ref))
    && (value.identity_ref === null || isLoginId(value.identity_ref))
    && (value.reason === null || text(value.reason, 100)) && value.capture_blocked === true
    && date(value.created_at) && date(value.updated_at)
    && (value.state === 'VERIFIED' ? value.identity_ref !== null : value.identity_ref === null)
}
