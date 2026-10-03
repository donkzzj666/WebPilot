import { isRecord } from './types'
import type { ActionPolicy, CreateTaskRequest, JsonValue, PublicIdentity, RepositoryWritePolicy, Scenario, SourceScope, WriteOperation } from './types'
import { splitLines, utcInput } from './presentation'

export type SourceInput = { sourceId: string; siteId: string; origin: string; path: string; startUrl: string }
export type WriteInput = {
  repository: string; baseBranch: string; branch: string; baseSha: string; failureRun: string
  taskKind: 'ordinary_repair' | 'workflow_repair'; files: string; workflowFiles: string
  protectedPatterns: string; checks: string; rules: string; operations: WriteOperation[]
}
export const EMPTY_SOURCE: SourceInput = { sourceId: '', siteId: '', origin: '', path: '/', startUrl: '' }
export const EMPTY_WRITE: WriteInput = { repository: '', baseBranch: '', branch: '', baseSha: '', failureRun: '',
  taskKind: 'ordinary_repair', files: '', workflowFiles: '', protectedPatterns: 'tests/*\nacceptance/*\nevaluation/*\n.github/*',
  checks: '', rules: '', operations: [] }

function required(value: string, label: string, max?: number) {
  const trimmed = value.trim()
  if (!trimmed || max && trimmed.length > max) throw new Error(`请填写${label}${max ? `（不超过 ${max} 个字符）` : ''}。`)
  return trimmed
}
function http(value: string, label: string): URL {
  if (/[\s\\\u0000-\u001f\u007f]/.test(value)) throw new Error(`${label}不能含空白、控制字符或反斜线。`)
  let parsed: URL
  try { parsed = new URL(value) } catch { throw new Error(`请填写有效的${label}。`) }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || !parsed.hostname
    || parsed.port === '0' || value.split('/')[2]?.includes('%')) throw new Error(`${label}必须是不含账号或密码的 HTTP(S) 地址。`)
  return parsed
}
export function sourceValues(rows: SourceInput[]): { sources: SourceScope[]; start_urls: string[] } {
  if (rows.length < 1 || rows.length > 20) throw new Error('请列出 1 至 20 个已授权来源。')
  const sources = rows.map((row) => {
    const origin = required(row.origin, '来源地址')
    const parsed = http(origin, '来源地址')
    if (!['', '/'].includes(parsed.pathname) || parsed.search || parsed.hash) throw new Error('来源地址只填写协议与域名；页面路径填写到起始 URL。')
    const path = required(row.path, '允许路径')
    if (!path.startsWith('/') || /[\\?#\s\u0000-\u001f\u007f]/.test(path)) throw new Error('允许路径须以 / 开始，不能包含查询参数或空白。')
    return { source_id: required(row.sourceId, '来源标识', 200), site_id: required(row.siteId, '站点标识', 200), origin: parsed.origin, path_prefix: path }
  })
  const urls = rows.map((row) => { const url = required(row.startUrl, '起始 URL'); http(url, '起始 URL'); return url })
  if (new Set(sources.map((source) => source.source_id)).size !== sources.length) throw new Error('每个来源标识须唯一。')
  if (new Set(urls).size !== urls.length) throw new Error('起始 URL 不可重复。')
  return { sources, start_urls: urls }
}
export function matchingIdentity(identity: PublicIdentity, sources: SourceScope[]): boolean {
  return identity.state === 'VERIFIED' && identity.realm === 'public' && sources.length > 0
    && sources.every((source) => {
      try { return identity.site_id === source.site_id && new URL(identity.origin).origin === new URL(source.origin).origin }
      catch { return false }
    })
}
export function writePolicy(value: WriteInput, authorized: boolean): RepositoryWritePolicy {
  if (!authorized) throw new Error('请核对并勾选写入授权；自然语言本身不会授予写入权限。')
  const files = splitLines(value.files), checks = splitLines(value.checks), patterns = splitLines(value.protectedPatterns)
  if (!files.length || !checks.length || !patterns.length || !value.operations.length) throw new Error('请明确文件白名单、保护规则、必需检查及允许动作。')
  return { mode: 'repository_write', repository: required(value.repository, '仓库'),
    base_branch: required(value.baseBranch, '基线分支'), branch: required(value.branch, '工作分支'),
    base_sha: required(value.baseSha, '基线提交 SHA'), task_kind: value.taskKind,
    allowed_files: files, workflow_exception_files: value.taskKind === 'workflow_repair' ? splitLines(value.workflowFiles) : [],
    protected_patterns: patterns, required_checks: checks, independent_rules_ref: required(value.rules, '独立检查规则引用'),
    allowed_operations: [...value.operations] }
}
export type CreateInput = {
  instruction: string; scenario: Scenario | ''; rows: SourceInput[]; authorized: boolean
  permission: ActionPolicy['mode']; write: WriteInput; writeAuthorized: boolean
  identityRef: string; identities: PublicIdentity[]
}
export function createRequest(input: CreateInput): CreateTaskRequest {
  if (!input.authorized) throw new Error('请先确认已获授权访问所列来源与路径。')
  const scoped = sourceValues(input.rows)
  const identity = input.identityRef ? input.identities.find((item) => item.identity_ref === input.identityRef) : null
  if (input.identityRef && (!identity || !matchingIdentity(identity, scoped.sources))) throw new Error('所选账号未就绪或与来源站点不一致，请重新选择匹配账号。')
  const policy: ActionPolicy = input.permission === 'read_only' ? { mode: 'read_only' } : writePolicy(input.write, input.writeAuthorized)
  if (policy.mode === 'repository_write' && !identity) throw new Error('仓库写入需要明确选择匹配且已验证的执行账号。')
  const parameters: Record<string, JsonValue> = policy.mode === 'read_only' ? {} : {
    operation_kind: 'code_repair', repository: policy.repository, base_sha: policy.base_sha, branch: policy.branch,
    required_checks: policy.required_checks, independent_rules_ref: policy.independent_rules_ref,
    ...(input.write.failureRun.trim() ? { failure_run_id: input.write.failureRun.trim() } : {}),
  }
  return { instruction: required(input.instruction, '任务描述'), compiler_mode: 'natural_language', ...scoped,
    ...(policy.mode === 'repository_write' ? { scenario: 'operations' } : input.scenario ? { scenario: input.scenario } : {}),
    parameters, action_policy: policy, identity_ref: identity?.identity_ref ?? null }
}

export const ENUM_FIELDS: Record<string, { value: string; label: string }[]> = {
  scenario: [ { value: 'finance', label: '财务信息' }, { value: 'operations', label: '运维与代码' }, { value: 'research', label: '科研检索' }, { value: 'monitoring', label: '来源监控' } ],
  'parameters.operation_kind': [ { value: 'grafana_read', label: '只读仪表盘' }, { value: 'code_repair', label: '代码修复（需独立写入授权）' } ],
  'parameters.period_type': [ { value: 'annual', label: '年度' }, { value: 'quarterly', label: '季度' }, { value: 'year_to_date', label: '年初至今' }, { value: 'point_in_time', label: '时点' } ],
  'parameters.source_kind': [ { value: 'security_community', label: '安全社区' }, { value: 'cisa_kev', label: 'CISA KEV' } ],
  'parameters.baseline': [ { value: 'true', label: '建立首次基线' }, { value: 'false', label: '增量监控' } ],
}
export const ARRAY_FIELDS = new Set(['parameters.metrics', 'parameters.queries', 'parameters.topic_criteria', 'parameters.panel_ids', 'parameters.required_checks', 'start_urls'])
const INTEGER_FIELDS = new Set(['parameters.max_items', 'parameters.max_list_items', 'parameters.max_details'])
const TIME_FIELDS = new Set(['parameters.cutoff_at', 'parameters.scheduled_at'])
export function clarificationValue(field: string, value: string): JsonValue {
  if (ARRAY_FIELDS.has(field)) { const values = splitLines(value); if (!values.length) throw new Error('列表至少需要一项。'); return values }
  if (INTEGER_FIELDS.has(field)) {
    if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error('数量上限须为正整数。')
    return Number(value)
  }
  if (TIME_FIELDS.has(field)) return utcInput(value)
  if (field === 'parameters.baseline') {
    if (!['true', 'false'].includes(value)) throw new Error('请选择是否建立首次基线。')
    return value === 'true'
  }
  if (field === 'parameters.confirmed_boundary' && value === '__none__') return null
  if (field === 'parameters.variables') {
    let parsed: unknown
    try { parsed = JSON.parse(value) } catch { throw new Error('变量须为 JSON 对象，例如 {"region":"cn"}；无变量请填写 {}。') }
    if (!isRecord(parsed) || !Object.values(parsed).every((item) => typeof item === 'string')) throw new Error('变量须为名称与文本值组成的 JSON 对象。')
    return parsed as Record<string, string>
  }
  const result = required(value, '缺失信息')
  if (ENUM_FIELDS[field] && !ENUM_FIELDS[field].some((option) => option.value === result)) throw new Error('请选择有效的字段选项。')
  return result
}
