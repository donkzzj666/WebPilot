import { apiFetch } from '../api'
import { getTask } from '../tasks/client'
import { compareIds, decimal, parseEvent } from './events'
import { parseOperation, parseReadiness, parseWorkspace } from './contracts'
import type { ControlAction, ControlBody, PendingControl, WorkbenchInputs } from './types'
const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
export class WorkbenchError extends Error {
  readonly status: number
  readonly code: string
  constructor(status: number, code: string, message: string) { super(message); this.status = status; this.code = code }
}
async function request(path: string, init: RequestInit = {}, expectedStatus?: number): Promise<unknown> {
  let response: Response
  try { response = await apiFetch(path, { ...init, signal: init.signal ? AbortSignal.any([init.signal, AbortSignal.timeout(10000)]) : AbortSignal.timeout(10000),
    headers: { Accept: 'application/json', ...init.headers } }) }
  catch { throw new WorkbenchError(0, 'NETWORK', '连接中断或读取超时；请重新同步。') }
  let value: unknown
  try {
    if (!response.body) throw new Error()
    const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal:true }); let body = '', bytes = 0
    try { while (true) { const part = await reader.read(); if (part.done) break; bytes += part.value.byteLength; if (bytes > 2_000_000) { await reader.cancel(); throw new Error() }; body += decoder.decode(part.value, { stream:true }) }; body += decoder.decode() }
    finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
    value = JSON.parse(body)
  }
  catch { throw new WorkbenchError(response.status, 'INVALID_RESPONSE', '服务响应无效；请重新同步。') }
  if (!response.ok) {
    const code = record(value) && typeof value.code === 'string' ? value.code : 'REQUEST_FAILED'
    throw new WorkbenchError(response.status, code, response.status === 409 ? '任务、配置或运行状态已变化。请重新同步，核对后再次明确确认。'
      : response.status === 422 ? '控制请求未通过校验。请重新同步后检查当前版本。'
        : response.status === 404 ? '所选任务或运行不存在。请刷新任务入口。'
          : response.status === 401 || response.status === 403 ? '本机连接认证失败，请检查启动配置。' : '执行服务暂不可用；请重新同步。')
  }
  if (expectedStatus !== undefined && response.status !== expectedStatus) throw new WorkbenchError(response.status, 'INVALID_RESPONSE', '操作未返回预期受理回执；请查询原请求。')
  return value
}
export async function getInputs(taskId: string, signal: AbortSignal): Promise<WorkbenchInputs> {
  const [raw, detail, settings] = await Promise.all([
    request(`/api/v1/tasks/${encodeURIComponent(taskId)}/workspace`, { signal }), getTask(taskId, signal), request('/api/v1/settings', { signal }),
  ])
  const workspace = parseWorkspace(raw, taskId), readiness = parseReadiness(settings)
  if (workspace.task.state_version !== detail.task.state_version || workspace.task.current_contract_version !== detail.task.current_contract_version
    || workspace.task.current_run_id !== detail.task.current_run_id || workspace.task.preparation_status !== detail.task.preparation_status) throw new WorkbenchError(200, 'INCONSISTENT_READ', '读取期间任务发生变化，请重新同步。')
  return { workspace, detail, readiness }
}
export function pendingControl(taskId: string, target: string, action: ControlAction, body: ControlBody, prior: PendingControl|null = null): PendingControl {
  const fingerprint = JSON.stringify({ taskId, target, action, body })
  if (prior && prior.fingerprint === fingerprint) return prior
  return { taskId, target, action, body: { ...body }, fingerprint, key: crypto.randomUUID() }
}
export async function postControl(pending: PendingControl, signal: AbortSignal) {
  const route = pending.action === 'start' ? `tasks/${encodeURIComponent(pending.target)}/start` : `runs/${encodeURIComponent(pending.target)}/${pending.action}`
  const value = await request(`/api/v1/${route}`, { method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pending.key }, body: JSON.stringify(pending.body) }, 202)
  if (!record(value)) throw new WorkbenchError(202, 'INVALID_RESPONSE', '操作回执无效；请查询原请求。')
  try { const operation = parseOperation(value.operation, pending.taskId, { action: pending.action, runId: pending.action === 'start' ? undefined : pending.target, body: pending.body }); if (operation.status !== 'PENDING') throw new Error(); return operation }
  catch { throw new WorkbenchError(202, 'INVALID_RESPONSE', '操作回执与所选目标或版本不一致；请查询原请求。') }
}
export async function getOperation(operationId: string, taskId: string, runId: string, signal: AbortSignal, prior?: import('./types').Operation) {
  const value = await request(`/api/v1/operations/${encodeURIComponent(operationId)}`, { signal })
  if (!record(value)) throw new WorkbenchError(200, 'INVALID_RESPONSE', '操作查询响应无效。')
  return parseOperation(value.operation, taskId, { operationId, runId, action: prior?.action === 'retry' ? undefined : prior?.action,
    body: prior ? { expected_state_version:prior.requested_state_version, contract_version:prior.contract_version, settings_version:prior.settings_version } : undefined })
}
export async function replayEvents(taskId: string, runId: string, after: string, signal: AbortSignal) {
  let cursor = after
  // The following coherent snapshot covers history beyond this bounded replay.
  for (let page = 0; page < 10; page++) {
    const value = await request(`/api/v1/runs/${encodeURIComponent(runId)}/events?after=${encodeURIComponent(cursor)}&limit=100`, { signal })
    if (!record(value) || value.task_id !== taskId || value.run_id !== runId || !Array.isArray(value.events) || value.events.length > 100
      || !decimal(value.cursor) || !decimal(value.high_water) || !decimal(value.global_high_water) || typeof value.has_more !== 'boolean'
      || compareIds(value.cursor, cursor) < 0 || compareIds(value.cursor, value.high_water) > 0 || compareIds(value.high_water, value.global_high_water) > 0) throw new WorkbenchError(200, 'INVALID_RESPONSE', '重放响应格式或目标不一致。')
    let previous = cursor
    const events = value.events.map((event) => { const parsed = parseEvent(event, taskId, runId); if (compareIds(parsed.event_id, previous) <= 0) throw new Error('重放事件顺序无效。'); previous = parsed.event_id; return parsed })
    if (previous !== value.cursor || value.has_more && events.length === 0) throw new Error('重放游标不一致。')
    cursor = value.cursor
    if (!value.has_more) return
  }
}
export async function openStream(taskId: string, runId: string, cursor: string, signal: AbortSignal) {
  if (!decimal(cursor)) throw new Error('订阅游标无效。')
  const opening = new AbortController(), timer = setTimeout(() => opening.abort(), 10000)
  let response: Response
  try { response = await apiFetch(`/api/v1/events?task_id=${encodeURIComponent(taskId)}&run_id=${encodeURIComponent(runId)}`, {
    signal:AbortSignal.any([signal, opening.signal]), headers: { Accept: 'text/event-stream', 'Last-Event-ID': cursor },
  }) } finally { clearTimeout(timer) }
  if (!response.ok || !response.headers.get('Content-Type')?.startsWith('text/event-stream') || !response.body) { await response.body?.cancel(); throw new Error('实时连接不可用。') }
  return response.body
}
export async function readEvidence(evidenceId: string, runId: string, snapshotId: string, signal: AbortSignal) {
  const metadata = await request(`/api/v1/evidence/${encodeURIComponent(evidenceId)}`, { signal })
  if (!record(metadata) || metadata.evidence_id !== evidenceId || metadata.run_id !== runId || metadata.snapshot_id !== snapshotId
    || metadata.availability !== 'AVAILABLE' || metadata.redaction_status !== 'FILTERED' || !['screenshot','text'].includes(metadata.artifact_kind as string)
    || metadata.mime_type !== (metadata.artifact_kind === 'screenshot' ? 'image/png' : 'text/plain; charset=utf-8')
    || !Number.isSafeInteger(metadata.size_bytes) || (metadata.size_bytes as number) < 1 || (metadata.size_bytes as number) > 8_388_608) throw new Error('页面展示副本不可用。')
  const expectedMime = metadata.artifact_kind === 'screenshot' ? 'image/png' : 'text/plain'
  const response = await apiFetch(`/api/v1/evidence/${encodeURIComponent(evidenceId)}/content`, { signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]), headers: { Accept: expectedMime } })
  if (!response.ok || !response.headers.get('Content-Type')?.startsWith(expectedMime) || !response.body) { await response.body?.cancel(); throw new Error('页面展示副本不可读。') }
  const reader = response.body.getReader(), chunks: Uint8Array<ArrayBuffer>[] = []; let total = 0
  try { while (true) { const { value, done } = await reader.read(); if (done) break; total += value.byteLength; if (total > 8_388_608) { await reader.cancel(); throw new Error('展示副本超过限制。') }; chunks.push(new Uint8Array(value)) } }
  finally { reader.releaseLock() }
  if (total !== metadata.size_bytes) throw new Error('展示副本长度不一致。')
  const blob = new Blob(chunks, { type: metadata.mime_type as string })
  if (metadata.artifact_kind === 'screenshot') return { kind:'screenshot' as const, blob }
  if (total > 65536) throw new Error('文字展示副本超过限制。')
  const page: unknown = JSON.parse(new TextDecoder('utf-8', { fatal:true }).decode(await blob.arrayBuffer()))
  if (!record(page) || typeof page.title !== 'string' || typeof page.text !== 'string' || page.title.length + page.text.length > 30000) throw new Error('页面文字展示副本格式无效。')
  return { kind:'text' as const, title:page.title, text:page.text }
}
