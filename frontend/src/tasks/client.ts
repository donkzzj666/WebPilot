import { apiFetch } from '../api'
import { isRecord, parseIdentities, parseTaskDetail, parseTaskList } from './types'
import type { JsonValue } from './types'

export class TaskApiError extends Error {
  readonly status: number
  readonly code: string
  constructor(status: number, code: string, message: string) { super(message); this.status = status; this.code = code }
}
async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  let response: Response
  const deadline = AbortSignal.timeout(init.method === 'POST' ? 120000 : 5000)
  try { response = await apiFetch(path, { ...init, signal: init.signal ? AbortSignal.any([init.signal, deadline]) : deadline,
    headers: { Accept: 'application/json', ...init.headers } }) }
  catch { throw new TaskApiError(0, 'NETWORK', init.method === 'POST'
    ? '连接中断；提交是否完成尚未确认。保持原输入再次提交可查询同一请求。' : '读取服务超时或连接中断，请重新读取。') }
  let payload: unknown
  try { payload = await response.json() } catch { throw new TaskApiError(response.status, 'INVALID_RESPONSE', '服务未返回有效 JSON，请重新读取。') }
  if (!response.ok) {
    const code = isRecord(payload) && typeof payload.code === 'string' ? payload.code : 'REQUEST_FAILED'
    // Present fixed local messages. Provider/unknown response bodies are never echoed to the UI.
    const messages: Record<string, string> = {
      CONFIG_NOT_READY: '模型配置尚未就绪，请前往配置页完成设置后再提交。',
      CREDENTIAL_UNAVAILABLE: '本机模型凭据暂不可用，请在配置页检查。',
      CONTRACT_VERSION_CONFLICT: '任务版本已变化，请核对最新内容并明确确认后再提交。',
      IDEMPOTENCY_CONFLICT: '该请求标识对应的内容已不同，请核对最新任务后重新提交。',
      STATE_CONFLICT: '任务当前状态不允许修改，请核对最新任务。',
      INVALID_PARAMETER: '输入未通过校验。请检查字段格式、来源范围和权限；原输入已保留。',
      NOT_FOUND: '该任务不存在，请刷新任务列表。',
      FORBIDDEN: '请求被拒绝，请检查本机连接及账号权限。',
    }
    const compilationPending = code === 'STATE_CONFLICT' && isRecord(payload) && Array.isArray(payload.details)
      && payload.details.some((item: unknown) => isRecord(item) && item.field === 'compilation')
    throw new TaskApiError(response.status, code, compilationPending
      ? '任务编译正在进行或结果尚未确认。原请求标识仍保留；再次提交相同内容只读取该请求，不会再次调用模型。' : messages[code]
      ?? (response.status === 422 ? messages.INVALID_PARAMETER : response.status === 409 ? messages.STATE_CONFLICT
        : response.status === 401 ? '本机 API 认证失败，请检查启动配置。' : '任务服务暂不可用，请稍后重试。'))
  }
  return payload
}
export async function getTasks(before?: string, signal?: AbortSignal) {
  return parseTaskList(await request(`/api/v1/tasks?limit=20${before ? `&before=${encodeURIComponent(before)}` : ''}`, { signal }))
}
export async function getTask(taskId: string, signal?: AbortSignal) {
  const detail = parseTaskDetail(await request(`/api/v1/tasks/${encodeURIComponent(taskId)}`, { signal }))
  if (detail.task.task_id !== taskId) throw new TaskApiError(200, 'INVALID_RESPONSE', '任务响应与所选对象不一致，请重新读取。')
  return detail
}
export async function getIdentities(signal?: AbortSignal) {
  return parseIdentities(await request('/api/v1/identities', { signal }))
}
export async function getModelReadiness(signal?: AbortSignal): Promise<boolean> {
  const value = await request('/api/v1/settings', { signal })
  if (!isRecord(value) || !isRecord(value.readiness) || !isRecord(value.disclosure)
    || typeof value.readiness.ready !== 'boolean' || value.readiness.provider_verified !== false
    || typeof value.task_execution_enabled !== 'boolean' || typeof value.disclosure.accepted !== 'boolean'
    || !Number.isSafeInteger(value.version) || (value.version as number) < 0
    || !(value.model === null || isRecord(value.model))) throw new Error('模型就绪响应格式无效，请在配置页重新读取。')
  return value.readiness.ready && value.task_execution_enabled && value.disclosure.accepted && value.model !== null
}
export async function postTask(path: string, payload: JsonValue, key: string, signal?: AbortSignal) {
  const route = /^\/api\/v1\/tasks\/([^/]+)\/(revisions|clarifications)$/.exec(path)
  if (path !== '/api/v1/tasks' && route === null) throw new Error('任务提交路径无效。')
  const detail = parseTaskDetail(await request(path, { method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(payload) }))
  if (route && detail.task.task_id !== decodeURIComponent(route[1])) {
    throw new TaskApiError(200, 'INVALID_RESPONSE', '提交回执与目标任务不一致，请先重新读取目标任务。')
  }
  return detail
}

export type PendingSubmission = { fingerprint: string; key: string }
/** Only in component memory: retry the identical request; changed content gets a new key. */
export function submissionKey(prior: PendingSubmission | null, path: string, payload: JsonValue): PendingSubmission {
  const fingerprint = path + '\n' + JSON.stringify(payload)
  return prior?.fingerprint === fingerprint ? prior : { fingerprint, key: crypto.randomUUID() }
}
