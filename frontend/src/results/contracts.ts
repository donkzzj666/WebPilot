import type { DisplayEvidence, Evidence, Json, Results, Run } from './types'

export const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(value)
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const text = (value: unknown): value is string => typeof value === 'string' && value.length <= 65536
const date = (value: unknown): value is string => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value))
const nullable = (value: unknown, validate: (v: unknown) => boolean) => value === null || validate(value)
const hash = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 10000 && value.every(text)
const ids = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 10000 && value.every(id) && new Set(value).size === value.length
const verdict = (value: unknown) => ['PASS','FAIL','INSUFFICIENT','CONFLICT'].includes(value as string)
const states = ['QUEUED','RUNNING','VERIFYING','WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED','RECONCILING','SUCCEEDED','PARTIAL','FAILED','CANCELLED']
const effectStates = ['INTENT','CONFIRMED','NOT_APPLIED','UNKNOWN']
export function decimal(value: unknown): value is string { return typeof value === 'string' && /^[1-9][0-9]{0,18}$/.test(value) && BigInt(value) <= 9223372036854775807n }
function json(value: unknown, depth = 0, budget = { nodes: 0 }): value is Json {
  if (++budget.nodes > 20000 || depth > 24) return false
  if (value === null || typeof value === 'boolean' || text(value) || typeof value === 'number' && Number.isFinite(value)) return true
  if (Array.isArray(value)) return value.every((item) => json(item, depth + 1, budget))
  return record(value) && Object.entries(value).every(([key,item]) => key.length <= 4096 && json(item, depth + 1, budget))
}
function run(value: unknown, taskId: string): value is Run {
  return record(value) && id(value.run_id) && value.task_id === taskId && integer(value.contract_version) && value.contract_version > 0
    && nullable(value.parent_run_id,id) && value.parent_run_id !== value.run_id && states.includes(value.state as string) && integer(value.state_version)
    && integer(value.assistance_count) && date(value.created_at) && nullable(value.started_at,date) && nullable(value.ended_at,date) && typeof value.has_result === 'boolean'
}
const pointer = (value: unknown): value is string => typeof value === 'string' && value.length <= 4096 && (value === '' || value.startsWith('/')) && !/~(?![01])/.test(value)
function evidence(value: unknown, runId: string): value is Evidence {
  if (!record(value) || !id(value.evidence_id) || value.run_id !== runId || !nullable(value.artifact_kind,(v) => ['text','diff','screenshot','pdf','ci','har'].includes(v as string))
    || !['source_url','object_id','locator_or_page'].every((key) => nullable(value[key],text)) || !nullable(value.captured_at,date) || !nullable(value.sha256,hash)
    || !nullable(value.original_evidence_id,id) || !nullable(value.snapshot_id,id) || !['AVAILABLE','MISSING','CORRUPT','EXPIRED','UNAVAILABLE'].includes(value.availability as string)
    || !['AVAILABLE','MISSING','CORRUPT','EXPIRED','UNAVAILABLE','BLOCKED'].includes(value.display_status as string) || typeof value.displayable !== 'boolean'
    || !nullable(value.display_evidence_id,id) || !nullable(value.display_sha256,hash) || !nullable(value.display_size_bytes,integer) || !nullable(value.display_mime_type,text)) return false
  if (value.displayable && (value.availability !== 'AVAILABLE' || value.display_status !== 'AVAILABLE' || !id(value.display_evidence_id) || !hash(value.display_sha256)
    || !integer(value.display_size_bytes) || value.display_size_bytes < 1 || !['text/plain; charset=utf-8','image/png'].includes(value.display_mime_type as string))) return false
  return true
}
export function parseResults(value: unknown, taskId: string, runId: string|null = null): Results {
  if (!id(taskId) || !record(value) || value.task_id !== taskId || !nullable(value.current_run_id,id) || !Array.isArray(value.runs) || value.runs.length > 100
    || !value.runs.every((item) => run(item, taskId)) || new Set(value.runs.map((item) => item.run_id)).size !== value.runs.length
    || !nullable(value.next_cursor,decimal) || !(value.selected_run === null || run(value.selected_run,taskId))
    || !['AVAILABLE','NOT_READY','UNAVAILABLE'].includes(value.result_status as string) || !Array.isArray(value.field_checks) || value.field_checks.length > 10000
    || !nullable(value.verification_id,id) || ![null,'assisted','autonomous'].includes(value.assistance as null|string)
    || !Array.isArray(value.evidence) || value.evidence.length > 10000 || !Array.isArray(value.write_intents) || value.write_intents.length > 10000
    || !integer(value.pending_write_count) || typeof value.write_intents_truncated !== 'boolean' || typeof value.display_complete_success !== 'boolean'
    || !strings(value.display_blockers) || !date(value.as_of)) throw new Error('结果响应格式或任务关联无效。')
  const selected = value.selected_run
  if (selected === null) {
    if (runId !== null || value.current_run_id !== null || value.runs.length || value.result !== null || value.result_status !== 'NOT_READY'
      || value.field_checks.length || value.verification_id !== null || value.assistance !== null || value.evidence.length || value.write_intents.length
      || value.pending_write_count || value.write_intents_truncated || value.display_complete_success) throw new Error('空运行的结果关联无效。')
    return value as Results
  }
  if (selected.run_id !== (runId ?? value.current_run_id ?? value.runs[0]?.run_id) || value.assistance !== (selected.assistance_count > 0 ? 'assisted' : 'autonomous')
    || !value.evidence.every((item) => evidence(item, selected.run_id)) || new Set(value.evidence.map((item) => item.evidence_id)).size !== value.evidence.length) throw new Error('所选运行或证据关联无效。')
  const listed = value.runs.find((item) => item.run_id === selected.run_id)
  if (listed && (Object.keys(listed) as (keyof Run)[]).some((key) => listed[key] !== selected[key])) throw new Error('运行历史与所选运行不一致。')
  if (!value.write_intents.every((item) => record(item) && id(item.operation_id) && id(item.originating_run_id) && effectStates.includes(item.status as string)
    && ['receipt_available','critical_violation','recorded_in_selected_result'].every((key) => typeof item[key] === 'boolean'))
    || new Set(value.write_intents.map((item) => item.operation_id)).size !== value.write_intents.length) throw new Error('副作用记录无效。')
  if (value.result === null) {
    if (value.result_status === 'AVAILABLE' || value.field_checks.length || value.verification_id !== null || value.evidence.length || value.display_complete_success) throw new Error('缺少结果却声明已验证。')
    return value as Results
  }
  const result = value.result
  if (value.result_status !== 'AVAILABLE' || !selected.has_result || !record(result) || result.task_id !== taskId || result.run_id !== selected.run_id || result.contract_version !== selected.contract_version
    || !['finance','operations','research','monitoring'].includes(result.scenario as string) || !['SUCCEEDED','PARTIAL','FAILED','CANCELLED'].includes(result.outcome as string)
    || result.outcome !== selected.state || result.assistance_count !== selected.assistance_count || result.generated_by !== 'business_aggregator'
    || !record(result.items) || result.items.scenario !== result.scenario || !json(result.items) || !ids(result.evidence_ids) || !strings(result.unresolved)
    || !Array.isArray(result.checks) || result.checks.length > 1000 || !Array.isArray(result.side_effects) || result.side_effects.length > 10000 || !record(result.coverage)) throw new Error('持久结果与运行状态不一致。')
  const references = new Set(value.evidence.map((item) => item.evidence_id)), refs = (v: unknown) => ids(v) && v.every((ref) => references.has(ref))
  if (!refs(result.evidence_ids)) throw new Error('结果证据目录不完整。')
  if (!result.checks.every((item) => record(item) && id(item.criterion_id) && text(item.expected_rule) && json(item.actual) && verdict(item.verdict) && refs(item.evidence_ids)
    && (item.verdict !== 'PASS' || (item.evidence_ids as string[]).length > 0) && date(item.checked_at) && id(item.checker_version))
    || new Set(result.checks.map((item) => item.criterion_id)).size !== result.checks.length) throw new Error('检查结论无效或缺少对应证据。')
  if (!value.field_checks.every((item) => record(item) && pointer(item.result_path) && verdict(item.verdict) && refs(item.evidence_ids) && json(item.actual)
    && (item.verdict !== 'PASS' || (item.evidence_ids as string[]).length > 0)) || new Set(value.field_checks.map((item) => item.result_path)).size !== value.field_checks.length) throw new Error('字段级检查无效。')
  const coverage = result.coverage
  if (!strings(coverage.searched_sources) || !strings(coverage.queries) || !nullable(coverage.cutoff_at,date) || !integer(coverage.content_pages)
    || !strings(coverage.unread_candidates) || !strings(coverage.gaps) || typeof coverage.complete !== 'boolean'
    || coverage.complete && (coverage.gaps.length > 0 || coverage.unread_candidates.length > 0)) throw new Error('覆盖范围无效。')
  if (!result.side_effects.every((item) => record(item) && id(item.operation_id) && text(item.target) && ['branch','commit','pr','benchmark_write'].includes(item.effect_type as string)
    && effectStates.includes(item.status as string) && nullable(item.receipt,text) && refs(item.evidence_ids) && typeof item.critical_violation === 'boolean'
    && (item.status !== 'CONFIRMED' || typeof item.receipt === 'string' && (item.evidence_ids as string[]).length > 0))
    || new Set(result.side_effects.map((item) => item.operation_id)).size !== result.side_effects.length) throw new Error('持久副作用结论无效。')
  const parsed = value as Results
  if (parsed.display_complete_success && !canVerifySuccess(parsed)) throw new Error('完整成功声明与证据或未决项矛盾。')
  return parsed
}
export function canVerifySuccess(value: Results): boolean {
  const result = value.result
  return value.result_status === 'AVAILABLE' && result !== null && result.outcome === 'SUCCEEDED' && result.checks.length > 0 && result.checks.every((check) => check.verdict === 'PASS')
    && value.field_checks.length > 0 && value.field_checks.every((field) => field.verdict === 'PASS') && result.coverage.complete && result.coverage.gaps.length === 0 && result.coverage.unread_candidates.length === 0
    && result.unresolved.length === 0 && result.evidence_ids.length > 0 && value.evidence.every((item) => item.availability === 'AVAILABLE' && item.displayable && item.display_status === 'AVAILABLE')
    && value.display_blockers.length === 0 && value.pending_write_count === 0 && !value.write_intents_truncated
    && result.side_effects.every((item) => !['UNKNOWN','INTENT'].includes(item.status) && !item.critical_violation)
    && value.write_intents.every((item) => !['UNKNOWN','INTENT'].includes(item.status) && !item.critical_violation && item.recorded_in_selected_result && (item.status !== 'CONFIRMED' || item.receipt_available))
}
/** A history response is also a fresh evidence/write snapshot; never retain old success. */
export function mergeHistoryPage(previous: Results, next: Results, before: string, history: Run[]) {
  if (!decimal(before) || previous.task_id !== next.task_id || previous.current_run_id !== next.current_run_id
    || !previous.selected_run || !next.selected_run || previous.selected_run.run_id !== next.selected_run.run_id
    || previous.selected_run.state_version !== next.selected_run.state_version
    || next.next_cursor !== null && (!decimal(next.next_cursor) || BigInt(next.next_cursor) >= BigInt(before))) throw new Error('运行历史已变化，请刷新结果后继续。')
  const additions = new Map(next.runs.map((item) => [item.run_id,item])), known = new Set(history.map((item) => item.run_id))
  return { page:next, runs:[...history.map((item) => additions.get(item.run_id) ?? item),...next.runs.filter((item) => !known.has(item.run_id))] }
}
export function parseDisplayMetadata(value: unknown, expected: Evidence, runId: string): DisplayEvidence {
  if (!record(value) || !expected.displayable || expected.run_id !== runId || value.evidence_id !== expected.display_evidence_id || value.run_id !== runId
    || value.sha256 !== expected.display_sha256 || value.size_bytes !== expected.display_size_bytes || value.mime_type !== expected.display_mime_type
    || value.availability !== 'AVAILABLE' || value.redaction_status !== 'FILTERED' || !['text','diff','screenshot'].includes(value.artifact_kind as string)
    || value.mime_type !== (value.artifact_kind === 'screenshot' ? 'image/png' : 'text/plain; charset=utf-8') || !hash(value.sha256)
    || !integer(value.size_bytes) || value.size_bytes < 1 || value.size_bytes > 16 * 1024 * 1024
    || value.snapshot_id !== expected.snapshot_id || value.artifact_kind !== expected.artifact_kind || value.source_url !== expected.source_url || value.object_id !== expected.object_id
    || value.captured_at !== expected.captured_at || value.locator_or_page !== expected.locator_or_page
    || value.original_evidence_id !== (value.evidence_id === expected.evidence_id ? expected.original_evidence_id : expected.evidence_id)) throw new Error('证据展示副本与所选运行或目录不一致。')
  return value as DisplayEvidence
}
