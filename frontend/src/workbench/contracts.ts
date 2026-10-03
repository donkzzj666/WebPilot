import type { ControlAction, ControlBody, Operation, Readiness, Workspace } from './types'
import { compareIds, decimal, parseEvent } from './events'
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/.test(value)
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 10000
const nullableText = (value: unknown) => value === null || text(value)
const date = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value))
export const runStates = ['QUEUED','RUNNING','VERIFYING','WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED','RECONCILING','SUCCEEDED','PARTIAL','FAILED','CANCELLED']
export const terminal = (state: string) => ['SUCCEEDED','PARTIAL','FAILED','CANCELLED'].includes(state)
export function parseOperation(value: unknown, taskId: string, expected?: { operationId?: string; runId?: string; action?: ControlAction; body?: ControlBody }): Operation {
  if (!record(value) || !id(value.operation_id) || value.task_id !== taskId || !id(value.run_id)
    || !['start','retry','pause','resume','cancel'].includes(value.action as string) || !['PENDING','APPLIED','REJECTED'].includes(value.status as string)
    || !['requested_state_version','accepted_run_state_version','settings_version'].every((key) => integer(value[key]))
    || !integer(value.contract_version) || value.contract_version === 0 || !nullableText(value.reason) || !date(value.created_at)
    || !(value.completed_at === null || date(value.completed_at)) || !(value.state === null || runStates.includes(value.state as string))
    || !(value.state_version === null || integer(value.state_version))
    || value.status === 'PENDING' && (value.completed_at !== null || value.state !== null || value.state_version !== null)
    || value.status !== 'PENDING' && value.completed_at === null
    || expected?.operationId && expected.operationId !== value.operation_id
    || expected?.runId && expected.runId !== value.run_id || expected?.action && expected.action !== value.action
    || expected?.body && (expected.body.expected_state_version !== value.requested_state_version || expected.body.contract_version !== value.contract_version || expected.body.settings_version !== value.settings_version)) {
    throw new Error('操作回执格式、版本或目标不一致。')
  }
  return value as Operation
}
export function parseReadiness(value: unknown): Readiness {
  if (!record(value) || !integer(value.version) || !record(value.readiness) || typeof value.readiness.ready !== 'boolean'
    || value.readiness.provider_verified !== false || typeof value.task_execution_enabled !== 'boolean' || !record(value.disclosure)
    || typeof value.disclosure.accepted !== 'boolean' || !(value.model === null || record(value.model))) throw new Error('模型配置版本响应无效。')
  return { version: value.version, ready: value.readiness.ready && value.task_execution_enabled && value.disclosure.accepted && value.model !== null }
}
export function parseWorkspace(value: unknown, taskId: string): Workspace {
  if (!record(value) || !record(value.task) || value.task.task_id !== taskId || !['READY','NEEDS_INPUT'].includes(value.task.preparation_status as string)
    || !integer(value.task.state_version) || !(value.task.current_contract_version === null || integer(value.task.current_contract_version) && value.task.current_contract_version > 0)
    || !(value.task.current_run_id === null || id(value.task.current_run_id)) || typeof value.is_current_run !== 'boolean'
    || !(value.criteria_contract_version === null || integer(value.criteria_contract_version) && value.criteria_contract_version > 0)
    || !Array.isArray(value.criteria) || value.criteria.length > 1000 || !value.criteria.every((c) => record(c) && text(c.criterion_id) && c.criterion_id.length <= 200 && c.criterion_id.trim().length > 0
      && text(c.expected_rule) && ['rule','semantic','independent_test'].includes(c.check_method as string) && typeof c.critical === 'boolean' && typeof c.verified === 'boolean')
    || !nullableText(value.current_subgoal) || !Array.isArray(value.graph_progress) || value.graph_progress.length > 20
    || !value.graph_progress.every((p) => record(p) && decimal(p.progress_id) && decimal(p.business_event_id) && text(p.phase) && integer(p.state_version) && integer(p.iteration) && nullableText(p.diagnostic) && date(p.occurred_at))
    || !Array.isArray(value.controls) || value.controls.length > 20 || !Array.isArray(value.events) || value.events.length > 100
    || !decimal(value.event_cursor) || !decimal(value.event_high_water) || value.event_cursor !== value.event_high_water
    || !decimal(value.global_event_high_water) || compareIds(value.event_high_water, value.global_event_high_water) > 0
    || typeof value.has_earlier_events !== 'boolean' || !date(value.as_of)) throw new Error('工作台响应格式或目标不一致。')
  if (value.run === null) {
    if (value.task.current_run_id !== null || value.controls.length || value.events.length || value.observation !== null || value.budget !== null || value.queue !== null) throw new Error('运行关联不一致。')
  } else if (!record(value.run) || !id(value.run.run_id) || value.run.task_id !== taskId || value.run.run_id !== value.task.current_run_id || value.is_current_run !== true
    || !integer(value.run.contract_version) || value.run.contract_version === 0 || value.run.contract_version !== value.criteria_contract_version
    || !integer(value.run.settings_version) || !integer(value.run.state_version) || !runStates.includes(value.run.state as string)
    || !nullableText(value.run.blocked_reason) || !date(value.run.created_at) || !['started_at','ended_at','handoff_deadline'].every((key) => value.run !== null && record(value.run) && (value.run[key] === null || date(value.run[key])))) throw new Error('当前运行响应关联或版本不一致。')
  const runId = value.run === null ? null : (value.run as Record<string, unknown>).run_id as string
  const criterionIds = value.criteria.map((c) => (c as Record<string, unknown>).criterion_id as string)
  if (new Set(criterionIds).size !== criterionIds.length) throw new Error('验证条件编号重复。')
  let verified: string[] = []
  if (value.checkpoint !== null) {
    if (!record(value.checkpoint) || !id(value.checkpoint.checkpoint_id) || !integer(value.checkpoint.action_sequence) || !date(value.checkpoint.saved_at)
      || !['verified_item_ids','pending_item_ids'].every((key) => record(value.checkpoint) && Array.isArray(value.checkpoint[key])
        && (value.checkpoint[key] as unknown[]).every((v) => typeof v === 'string' && criterionIds.includes(v))
        && new Set(value.checkpoint[key] as string[]).size === (value.checkpoint[key] as string[]).length)) throw new Error('检查点条件关联无效。')
    verified = value.checkpoint.verified_item_ids as string[]
  } else if (value.current_subgoal !== null) throw new Error('子目标缺少持久检查点。')
  if (value.current_subgoal !== null && value.current_subgoal !== 'aggregate' && !criterionIds.includes(value.current_subgoal as string)
    || value.criteria.some((c) => record(c) && c.verified !== verified.includes(c.criterion_id as string))) throw new Error('验证进度与持久检查点不一致。')
  if (value.observation !== null && (!record(value.observation) || !id(value.observation.snapshot_id) || !date(value.observation.captured_at)
    || typeof value.observation.valid !== 'boolean' || !integer(value.observation.state_version) || !text(value.observation.page_version)
    || !(value.observation.screenshot_evidence_id === null || id(value.observation.screenshot_evidence_id)) || !Array.isArray(value.observation.evidence) || value.observation.evidence.length > 100
    || !value.observation.evidence.every((e) => record(e) && id(e.evidence_id) && ['screenshot','text','pdf','ci','har','diff'].includes(e.artifact_kind as string)
      && (e.availability === null || ['AVAILABLE','MISSING','CORRUPT','EXPIRED'].includes(e.availability as string))
      && (e.redaction_status === null || ['FILTERED','BLOCKED'].includes(e.redaction_status as string)) && nullableText(e.mime_type) && date(e.captured_at)))) throw new Error('页面证据元数据无效。')
  if (value.budget !== null && (!record(value.budget) || value.budget.run_id !== runId || typeof value.budget.initialized !== 'boolean' || typeof value.budget.exhausted !== 'boolean'
    || !nullableText(value.budget.reason) || Object.entries(value.budget).some(([key, amount]) => (key.endsWith('_used') || key.endsWith('_ms') || key.startsWith('remaining_')) && !integer(amount)))) throw new Error('预算响应格式无效。')
  if (record(value.budget) && value.budget.initialized === true && !['actions_used','content_pages_used','observations_used','screenshots_used','model_calls_used','active_ms','ci_wait_ms','remaining_actions','remaining_content_pages','remaining_active_ms','remaining_ci_wait_ms'].every((key) => record(value.budget) && integer(value.budget[key]))) throw new Error('已初始化预算缺少实际用量。')
  if (value.queue !== null && (!record(value.queue) || !['QUEUED','ACTIVE','WAITING','RECOVERY','FINISHED'].includes(value.queue.status as string)
    || !['ordinary','monitoring','webarena'].includes(value.queue.queue_class as string) || !nullableText(value.queue.reason) || !date(value.queue.available_at) || !date(value.queue.updated_at))) throw new Error('队列响应格式无效。')
  if (runId !== null) {
    value.controls.forEach((operation) => parseOperation(operation, taskId, { runId }))
    let cursor = '0'
    for (const event of value.events) { const parsed = parseEvent(event, taskId, runId); if (compareIds(parsed.event_id, cursor) <= 0 || compareIds(parsed.event_id, value.event_cursor) > 0) throw new Error('工作台事件顺序无效。'); cursor = parsed.event_id }
  }
  return value as Workspace
}
export function controlAllowed(workspace: Workspace, action: ControlAction, selectedTaskId = workspace.task.task_id): boolean {
  if (workspace.task.task_id !== selectedTaskId) return false
  if (workspace.controls.some((operation) => operation.status === 'PENDING')) return false
  if (action === 'start') return workspace.task.preparation_status === 'READY' && workspace.run === null && workspace.task.current_contract_version !== null
  const run = workspace.run
  if (!run || !workspace.is_current_run || run.settings_version < 1 || terminal(run.state)) return false
  if (action === 'pause') return ['RUNNING','VERIFYING','RECONCILING'].includes(run.state)
  if (action === 'resume') return ['PAUSED','RECONCILING'].includes(run.state) && ['WAITING','RECOVERY'].includes(workspace.queue?.status ?? '') && workspace.budget?.exhausted !== true
  return true
}
/** A newer request from another local client must remain visible as processing. */
export function currentOperation(workspace: Workspace, known: Operation|null): Operation|undefined {
  return workspace.controls.find((operation) => operation.status === 'PENDING') ?? workspace.controls.at(-1)
    ?? (known?.task_id === workspace.task.task_id && known.run_id === workspace.run?.run_id ? known : undefined)
}
