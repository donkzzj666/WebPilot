export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue }
export type Scenario = 'finance' | 'operations' | 'research' | 'monitoring'
export type SourceScope = { source_id: string; site_id: string; origin: string; path_prefix: string }
export type ReadOnlyPolicy = { mode: 'read_only' }
export type WriteOperation = 'edit_file' | 'create_branch' | 'commit' | 'create_pr' | 'update_pr'
export type RepositoryWritePolicy = {
  mode: 'repository_write'; repository: string; base_branch: string; branch: string; base_sha: string
  task_kind: 'ordinary_repair' | 'workflow_repair'; allowed_files: string[]; workflow_exception_files: string[]
  protected_patterns: string[]; required_checks: string[]; independent_rules_ref: string
  allowed_operations: WriteOperation[]
}
export type ActionPolicy = ReadOnlyPolicy | RepositoryWritePolicy
export type TimeScope = { start: string | null; end: string | null; basis: string }
export type CreateTaskRequest = {
  instruction: string; compiler_mode: 'natural_language'; scenario?: Scenario
  sources: SourceScope[]; start_urls: string[]; parameters: Record<string, JsonValue>
  action_policy: ActionPolicy; identity_ref: string | null
}
export type TaskSummary = {
  task_id: string; original_instruction: string; preparation_status: 'NEEDS_INPUT' | 'READY'
  current_contract_version: number | null; current_run_id: string | null; state_version: number
  requested_fields: string[]; created_at: string; current_run_state?: string | null
  historical_run_ids?: string[]; revision?: number | null; updated_at?: string
}
export type TaskList = { tasks: TaskSummary[]; next_cursor: string | null }
export type TaskContract = {
  schema_version: 'm0-contract-v1'; task_id: string; contract_version: number; scenario: Scenario
  objective: string; original_instruction: string
  targets: { object_id: string; kind: string; canonical_name: string }[]
  sources: SourceScope[]; start_urls: string[]; parameters: Record<string, JsonValue>
  time_scope: TimeScope; output_schema: { field_id: string; required: boolean; description: string }[]
  acceptance_criteria: { criterion_id: string; expected_rule: string; check_method: string; critical: boolean }[]
  action_policy: ActionPolicy; identity_ref: string | null
}
export type RunSummary = { run_id: string; state: string; state_version: number; contract_version: number }
export type TaskDetail = {
  task: TaskSummary; contract_version: number; contract: TaskContract | null
  draft: Record<string, JsonValue> | null; missing_fields: string[]; current_run: RunSummary | null
  historical_runs: RunSummary[]; contract_history: TaskContract[]
  clarification_questions?: { field: string; message: string }[]
  field_origins?: Record<string, string>
}
export type PublicIdentity = {
  identity_ref: string; site_id: string; realm: 'public' | 'webarena'; origin: string
  normalized_account: string; state: 'VERIFIED' | 'NEEDS_LOGIN'; requires_identity_check: boolean
  requires_business_check: boolean; requires_recheck: true; state_version: number; created_at: string; updated_at: string
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(text)
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) > 0
const version = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0
const runState = (value: unknown): value is string => typeof value === 'string' && ['QUEUED', 'RUNNING', 'VERIFYING', 'WAITING_CI', 'WAITING_SITE', 'WAITING_HANDOFF', 'PAUSED', 'RECONCILING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'].includes(value)
const nullableText = (value: unknown) => value === null || text(value)
const scenario = (value: unknown): value is Scenario => ['finance', 'operations', 'research', 'monitoring'].includes(value as string)
function json(value: unknown): value is JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  return Array.isArray(value) ? value.every(json) : isRecord(value) && Object.values(value).every(json)
}
function sources(value: unknown): value is SourceScope[] {
  return Array.isArray(value) && value.length > 0 && value.length <= 20 && value.every((source) => isRecord(source)
    && ['source_id', 'site_id'].every((key) => text(source[key]) && (source[key] as string).length <= 200)
    && validOrigin(source.origin) && validPath(source.path_prefix))
    && new Set(value.map((source) => source.source_id)).size === value.length
}
/** Authorization sources remain meaningful when a draft is still missing start_urls. */
export function parseSourceScopes(value: unknown): SourceScope[] {
  if (!sources(value)) throw new Error('来源范围响应格式无效，请重新读取。')
  return value.map((source) => ({ source_id: source.source_id, site_id: source.site_id, origin: source.origin, path_prefix: source.path_prefix }))
}
export function isPolicy(value: unknown): value is ActionPolicy {
  if (!isRecord(value)) return false
  if (value.mode === 'read_only') return true
  return value.mode === 'repository_write'
    && ['repository', 'base_branch', 'branch', 'base_sha', 'independent_rules_ref'].every((key) => text(value[key]))
    && ['ordinary_repair', 'workflow_repair'].includes(value.task_kind as string)
    && ['allowed_files', 'workflow_exception_files', 'protected_patterns', 'required_checks', 'allowed_operations']
      .every((key) => strings(value[key]))
    && (value.allowed_files as string[]).length > 0 && (value.required_checks as string[]).length > 0
    && (value.allowed_operations as string[]).length > 0
    && (value.allowed_operations as string[]).every((item) => ['edit_file', 'create_branch', 'commit', 'create_pr', 'update_pr'].includes(item))
}
export function isSummary(value: unknown): value is TaskSummary {
  return isRecord(value) && text(value.task_id) && text(value.original_instruction)
    && ['NEEDS_INPUT', 'READY'].includes(value.preparation_status as string)
    && (value.current_contract_version === null || integer(value.current_contract_version))
    && nullableText(value.current_run_id) && version(value.state_version)
    && strings(value.requested_fields) && text(value.created_at)
    && (value.current_run_state === undefined || value.current_run_state === null || runState(value.current_run_state))
}
export function isContract(value: unknown): value is TaskContract {
  if (!isRecord(value)) return false
  return value.schema_version === 'm0-contract-v1' && text(value.task_id) && integer(value.contract_version)
    && scenario(value.scenario) && text(value.objective) && text(value.original_instruction)
    && Array.isArray(value.targets) && value.targets.length > 0 && value.targets.every((target) => isRecord(target)
      && ['object_id', 'kind', 'canonical_name'].every((key) => text(target[key])))
    && sources(value.sources) && strings(value.start_urls) && value.start_urls.length > 0
    && isRecord(value.parameters) && json(value.parameters)
    && isRecord(value.time_scope) && nullableText(value.time_scope.start) && nullableText(value.time_scope.end) && text(value.time_scope.basis)
    && Array.isArray(value.output_schema) && value.output_schema.length > 0 && value.output_schema.every((item) => isRecord(item)
      && text(item.field_id) && text(item.description) && typeof item.required === 'boolean')
    && Array.isArray(value.acceptance_criteria) && value.acceptance_criteria.length > 0 && value.acceptance_criteria.every((item) => isRecord(item)
      && text(item.criterion_id) && text(item.expected_rule) && text(item.check_method) && typeof item.critical === 'boolean')
    && isPolicy(value.action_policy) && nullableText(value.identity_ref)
}
function run(value: unknown): value is RunSummary {
  return isRecord(value) && text(value.run_id) && runState(value.state) && version(value.state_version) && integer(value.contract_version)
}
export function parseTaskDetail(value: unknown): TaskDetail {
  if (!isRecord(value) || !isSummary(value.task) || !integer(value.contract_version)
    || !(value.contract === null || isContract(value.contract)) || !strings(value.missing_fields)
    || !(value.draft === null || isRecord(value.draft) && json(value.draft))
    || !(value.current_run === null || run(value.current_run))
    || !Array.isArray(value.historical_runs) || !value.historical_runs.every(run)
    || !Array.isArray(value.contract_history) || !value.contract_history.every(isContract)
    || (value.clarification_questions !== undefined && (!Array.isArray(value.clarification_questions)
      || !value.clarification_questions.every((item) => isRecord(item) && text(item.field) && text(item.message))))
    || (value.field_origins !== undefined && (!isRecord(value.field_origins) || !Object.values(value.field_origins).every(text)))) {
    throw new Error('任务响应格式无效，请重新读取。')
  }
  const detail = value as TaskDetail
  if ((detail.contract !== null && detail.contract.task_id !== detail.task.task_id)
    || detail.contract?.contract_version !== detail.task.current_contract_version && detail.contract !== null
    || detail.contract !== null && detail.contract.contract_version > detail.contract_version
    || detail.task.requested_fields.join('\0') !== detail.missing_fields.join('\0')
    || detail.current_run !== null && detail.current_run.run_id !== detail.task.current_run_id
    || detail.current_run === null && detail.task.current_run_id !== null
    || detail.contract_history.some((contract) => contract.task_id !== detail.task.task_id)
    || detail.task.preparation_status === 'READY' && (detail.contract === null || detail.missing_fields.length !== 0)) {
    throw new Error('任务响应版本或关联不一致，请重新读取。')
  }
  return detail
}
export function parseTaskList(value: unknown): TaskList {
  if (!isRecord(value) || !Array.isArray(value.tasks) || !value.tasks.every(isSummary)
    || !(value.next_cursor === null || typeof value.next_cursor === 'string' && /^[1-9]\d{0,18}$/.test(value.next_cursor) && BigInt(value.next_cursor) <= 9223372036854775807n)) {
    throw new Error('任务列表响应格式无效，请重新读取。')
  }
  return value as TaskList
}
export function parseIdentities(value: unknown): PublicIdentity[] {
  if (!Array.isArray(value) || !value.every((identity) => isRecord(identity)
    && ['identity_ref', 'site_id', 'origin', 'normalized_account'].every((key) => text(identity[key]))
    && ['public', 'webarena'].includes(identity.realm as string)
    && ['VERIFIED', 'NEEDS_LOGIN'].includes(identity.state as string)
    && identity.requires_recheck === true && identity.requires_identity_check === true && identity.requires_business_check === true
    && version(identity.state_version) && text(identity.created_at) && text(identity.updated_at)
    && Number.isFinite(Date.parse(identity.created_at)) && Number.isFinite(Date.parse(identity.updated_at))
    && !/[\u0000-\u001f\u007f]/.test(identity.normalized_account as string)
    && (identity.normalized_account as string).trim() === identity.normalized_account
    && validOrigin(identity.origin))) throw new Error('账号元数据响应格式无效，请重新读取。')
  return value as PublicIdentity[]
}
function validOrigin(value: unknown): boolean {
  if (typeof value !== 'string' || /[\s\\\u0000-\u001f\u007f]/.test(value)) return false
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && ['/', ''].includes(url.pathname)
      && !value.split('/')[2]?.includes('%')
      && !url.search && !url.hash && url.port !== '0'
  } catch { return false }
}
function validPath(value: unknown): boolean {
  if (typeof value !== 'string' || !value.startsWith('/') || /[\\?#\s\u0000-\u001f\u007f]/.test(value)
    || /%(?![0-9a-fA-F]{2})|%(?:2f|5c|25)/i.test(value)) return false
  try {
    const decoded = decodeURIComponent(value)
    return !/[\u0000-\u001f\u007f]/.test(decoded) && !decoded.includes('//')
      && !decoded.split('/').some((segment) => segment === '.' || segment === '..')
  } catch { return false }
}
