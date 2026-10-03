import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { getIdentities, getModelReadiness, getTask, getTasks, postTask, submissionKey, TaskApiError } from './client'
import type { PendingSubmission } from './client'
import { ARRAY_FIELDS, clarificationValue, createRequest, EMPTY_SOURCE, EMPTY_WRITE, ENUM_FIELDS,
  matchingIdentity, sourceValues, writePolicy } from './inputs'
import type { SourceInput, WriteInput } from './inputs'
import { dateLabel, FIELD_LABELS, fieldLabel, formatValue, OPERATIONS, SCENARIOS, STATE_LABELS, utcInput } from './presentation'
import { isPolicy, isRecord, parseSourceScopes } from './types'
import type { ActionPolicy, JsonValue, PublicIdentity, Scenario, SourceScope, TaskDetail, TaskSummary, WriteOperation } from './types'
import './task-entry.css'

type Notice = { kind: 'success' | 'error' | 'conflict'; message: string; configuration?: boolean }
type Props = { onOpenSettings?: () => void; configurationRevision?: number; executionRevision?: number; onTaskSelected?: (taskId: string | null) => void }

function SourceEditor({ rows, onChange, prefix = '', disabled = false }: {
  rows: SourceInput[]; onChange: (rows: SourceInput[]) => void; prefix?: string; disabled?: boolean
}) {
  const fields = [ ['sourceId', 'source-id', '来源标识', '例如 financial-reports'],
    ['siteId', 'site-id', '站点标识', '例如 company-site'], ['origin', 'source-origin', '来源地址（协议与域名）', 'https://example.com'],
    ['path', 'source-path', '允许访问的路径前缀', '/reports'], ['startUrl', 'start-url', '起始 URL', 'https://example.com/reports/2025'] ] as const
  return <div className="task-sources">{rows.map((row, index) => <fieldset key={index} disabled={disabled} className="task-source-row">
    <legend>来源 {index + 1}</legend>
    <div className="task-field-grid">{fields.map(([key, id, label, placeholder]) => {
      const fieldId = `${prefix}${id}${index ? `-${index + 1}` : ''}`
      return <label key={key} htmlFor={fieldId} className={key === 'startUrl' ? 'task-full-field' : ''}>{label}
        <input id={fieldId} type={key === 'origin' || key === 'startUrl' ? 'url' : 'text'}
          value={row[key]} placeholder={placeholder} autoComplete="off" maxLength={key === 'sourceId' || key === 'siteId' ? 200 : 4000}
          required onChange={(event) => onChange(rows.map((item, i) => i === index ? { ...item, [key]: event.target.value } : item))} />
      </label>
    })}</div>
    {rows.length > 1 && <button type="button" className="task-text-button" onClick={() => onChange(rows.filter((_, i) => i !== index))}>移除来源 {index + 1}</button>}
  </fieldset>)}<button type="button" className="task-secondary" disabled={disabled || rows.length >= 20}
    onClick={() => onChange([...rows, { ...EMPTY_SOURCE }])}>＋ 添加来源</button></div>
}

function WriteEditor({ value, onChange, prefix = '', disabled = false }: {
  value: WriteInput; onChange: (value: WriteInput) => void; prefix?: string; disabled?: boolean
}) {
  const inputs = [ ['repository', 'write-repository', '仓库', '所有者/仓库名'],
    ['baseBranch', 'write-base-branch', '基线分支', '例如 main'], ['branch', 'write-branch', '工作分支', '例如 repair/fix'],
    ['baseSha', 'write-base-sha', '基线提交 SHA', '40 位小写十六进制'], ['rules', 'write-rules', '独立检查规则引用', '已确认的规则标识'],
    ['failureRun', 'write-failure-run', '失败流水线标识（若未确定可稍后补充）', '失败检查的运行标识'] ] as const
  const lists = [ ['files', 'write-allowed-files', '允许修改的文件（每行一个明确路径）'],
    ['checks', 'write-required-checks', '必须通过的检查（每行一个）'], ['protectedPatterns', 'write-protected-patterns', '禁止修改的文件模式（每行一个）'] ] as const
  return <fieldset disabled={disabled} className="task-write-fields"><legend>仓库写入范围</legend>
    <p className="task-hint">写入只用于代码修复。请明确文件和动作；授权不包括合并、推送或删除。</p>
    <div className="task-field-grid">{inputs.map(([key, id, label, placeholder]) => <label key={key} htmlFor={prefix + id}>{label}
      <input id={prefix + id} value={value[key]} placeholder={placeholder} autoComplete="off" required={key !== 'failureRun'}
        onChange={(event) => onChange({ ...value, [key]: event.target.value })} />
    </label>)}
      <label htmlFor={prefix + 'write-task-kind'}>修复类型<select id={prefix + 'write-task-kind'} value={value.taskKind}
        onChange={(event) => onChange({ ...value, taskKind: event.target.value as WriteInput['taskKind'], workflowFiles: '' })}>
        <option value="ordinary_repair">普通代码修复</option><option value="workflow_repair">工作流修复（需明确例外文件）</option>
      </select></label>
      {lists.map(([key, id, label]) => <label key={key} htmlFor={prefix + id} className="task-full-field">{label}
        <textarea id={prefix + id} rows={3} required value={value[key]} onChange={(event) => onChange({ ...value, [key]: event.target.value })} />
      </label>)}
      {value.taskKind === 'workflow_repair' && <label className="task-full-field" htmlFor={prefix + 'write-workflow-files'}>明确允许的工作流例外文件（每行一个）
        <textarea id={prefix + 'write-workflow-files'} rows={2} value={value.workflowFiles}
          onChange={(event) => onChange({ ...value, workflowFiles: event.target.value })} />
      </label>}
    </div>
    <fieldset className="task-operation-list"><legend>允许动作（逐项选择）</legend>
      {Object.entries(OPERATIONS).map(([operation, label]) => <label key={operation} htmlFor={prefix + 'write-op-' + operation} className="task-check">
        <input type="checkbox" id={prefix + 'write-op-' + operation} checked={value.operations.includes(operation as WriteOperation)}
          onChange={(event) => onChange({ ...value, operations: event.target.checked
            ? [...value.operations, operation as WriteOperation] : value.operations.filter((item) => item !== operation) })} />{label}
      </label>)}
    </fieldset>
  </fieldset>
}

function AccountSelect({ id, value, onChange, identities, sources, disabled }: {
  id: string; value: string; onChange: (value: string) => void; identities: PublicIdentity[]; sources: SourceScope[]; disabled: boolean
}) {
  return <label htmlFor={id}>执行账号<select id={id} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
    <option value="">不指定账号（公开只读页面）</option>
    {identities.map((identity) => <option key={identity.identity_ref} value={identity.identity_ref} disabled={!matchingIdentity(identity, sources)}>
      {identity.normalized_account} · {identity.site_id} · {matchingIdentity(identity, sources) ? '已验证，运行时仍须核对' : identity.state !== 'VERIFIED' ? '需要重新登录' : '与当前来源不匹配'}
    </option>)}
  </select><span className="task-field-note">账号不会自动选择或替换。密码和验证码请在受管站点窗口输入。</span></label>
}

function PolicyView({ policy }: { policy: ActionPolicy }) {
  return <div className={`task-policy ${policy.mode === 'read_only' ? 'task-policy-read' : 'task-policy-write'}`} data-testid="task-policy">
    <strong>{policy.mode === 'read_only' ? '只读权限' : '明确限定的仓库写入'}</strong>
    {policy.mode === 'read_only' ? <p>允许观察、读取和在来源范围内浏览；不授权提交、修改、发布或删除。</p>
      : <><p>{policy.repository} · {policy.base_branch} → {policy.branch}</p>
        <dl><dt>允许动作</dt><dd>{policy.allowed_operations.map((operation) => OPERATIONS[operation]).join('、')}</dd>
          <dt>文件白名单</dt><dd>{policy.allowed_files.join('、')}</dd><dt>保护规则</dt><dd>{policy.protected_patterns.join('、')}</dd>
          <dt>必需检查</dt><dd>{policy.required_checks.join('、')}</dd><dt>基线提交</dt><dd className="task-mono">{policy.base_sha}</dd>
          <dt>独立规则</dt><dd>{policy.independent_rules_ref}</dd>
          {policy.workflow_exception_files.length > 0 && <><dt>工作流例外</dt><dd>{policy.workflow_exception_files.join('、')}</dd></>}
        </dl></>}
  </div>
}

function ParameterInput({ field, value, onChange, disabled }: { field: string; value: string; onChange: (value: string) => void; disabled: boolean }) {
  const id = 'clarify-' + field.replaceAll('.', '-')
  return <label htmlFor={id}>{fieldLabel(field)}
    {ENUM_FIELDS[field] ? <select id={id} required disabled={disabled} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">请选择</option>{ENUM_FIELDS[field].map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select> : ARRAY_FIELDS.has(field) || field === 'parameters.variables' ? <textarea id={id} disabled={disabled} required rows={3} value={value}
      placeholder={field === 'parameters.variables' ? '{"region":"cn"}；无变量填写 {}' : '每行一项'} onChange={(event) => onChange(event.target.value)} />
      : <input id={id} disabled={disabled} required value={value} autoComplete="off" placeholder={field.endsWith('_at') ? '2026-10-02T00:00:00Z' : ''}
        onChange={(event) => onChange(event.target.value)} />}
    {field === 'parameters.confirmed_boundary' && <span className="task-field-note">首次基线没有边界时，明确填写 __none__。</span>}
  </label>
}

function initialTaskId(): string | null {
  const value = new URL(window.location.href).searchParams.get('task')
  return value && value.length <= 200 ? value : null
}
function selectUrl(taskId: string | null) {
  const url = new URL(window.location.href)
  if (taskId) url.searchParams.set('task', taskId); else url.searchParams.delete('task')
  window.history.replaceState(null, '', url.pathname + url.search + url.hash)
}
function errorNotice(error: unknown): Notice {
  return { kind: error instanceof TaskApiError && error.status === 409 ? 'conflict' : 'error',
    message: error instanceof Error ? error.message : '任务操作未完成，请重新读取。',
    configuration: error instanceof TaskApiError && ['CONFIG_NOT_READY', 'CREDENTIAL_UNAVAILABLE', 'MODEL_NOT_CONFIGURED'].includes(error.code) }
}
function sourceInputs(draft: Record<string, JsonValue> | null): SourceInput[] {
  if (!draft || !Array.isArray(draft.sources)) return [{ ...EMPTY_SOURCE }]
  const urls = Array.isArray(draft.start_urls) ? draft.start_urls : []
  const rows = draft.sources.filter((value): value is Record<string, JsonValue> => isRecord(value)).map((source, index) => ({ sourceId: String(source.source_id ?? ''), siteId: String(source.site_id ?? ''),
    origin: String(source.origin ?? ''), path: String(source.path_prefix ?? '/'), startUrl: typeof urls[index] === 'string' ? urls[index] as string : '' }))
  return rows.length ? rows : [{ ...EMPTY_SOURCE }]
}

export default function TaskEntry({ onOpenSettings, configurationRevision = 0, executionRevision = 0, onTaskSelected }: Props) {
  const [instruction, setInstruction] = useState('')
  const [scenario, setScenario] = useState<Scenario | ''>('')
  const [rows, setRows] = useState<SourceInput[]>([{ ...EMPTY_SOURCE }])
  const [authorized, setAuthorized] = useState(false)
  const [permission, setPermission] = useState<ActionPolicy['mode']>('read_only')
  const [write, setWrite] = useState<WriteInput>({ ...EMPTY_WRITE, operations: [] })
  const [writeAuthorized, setWriteAuthorized] = useState(false)
  const [identityRef, setIdentityRef] = useState('')
  const [identities, setIdentities] = useState<PublicIdentity[]>([])
  const [identityError, setIdentityError] = useState(false)
  const [modelReady, setModelReady] = useState<boolean | null>(null)
  const [modelRefresh, setModelRefresh] = useState(0)
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [listLoading, setListLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(initialTaskId)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [detailRefresh, setDetailRefresh] = useState(0)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [editTask, setEditTask] = useState<{ id: string; version: number } | null>(null)
  const [revisionConfirmed, setRevisionConfirmed] = useState(false)
  const [clarifications, setClarifications] = useState<Record<string, string>>({})
  const [clarifyConfirmed, setClarifyConfirmed] = useState(false)
  const [clarifyRows, setClarifyRows] = useState<SourceInput[]>([{ ...EMPTY_SOURCE }])
  const [clarifySourceConfirmed, setClarifySourceConfirmed] = useState(false)
  const [clarifyWrite, setClarifyWrite] = useState<WriteInput>({ ...EMPTY_WRITE, operations: [] })
  const [clarifyWriteConfirmed, setClarifyWriteConfirmed] = useState(false)
  const [clarifyIdentity, setClarifyIdentity] = useState('')
  const [timeScope, setTimeScope] = useState({ start: '', end: '', basis: '' })
  const [conflict, setConflict] = useState(false)
  const createPending = useRef<PendingSubmission | null>(null)
  const clarifyPending = useRef<PendingSubmission | null>(null)
  const alive = useRef(true)
  const saveController = useRef<AbortController | null>(null)
  useEffect(() => { alive.current = true; return () => { alive.current = false; saveController.current?.abort() } }, [])
  useEffect(() => { onTaskSelected?.(selectedId) }, [selectedId, onTaskSelected])

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    setListLoading(true); setListError(null)
    void getTasks(undefined, controller.signal).then((result) => {
      if (disposed) return
      setTasks(result.tasks); setCursor(result.next_cursor)
      setSelectedId((current) => current ?? result.tasks[0]?.task_id ?? null)
    }).catch((error: unknown) => { if (!disposed) setListError(error instanceof Error ? error.message : '任务列表读取失败。') })
      .finally(() => { if (!disposed) setListLoading(false) })
    return () => { disposed = true; controller.abort() }
  }, [refresh, executionRevision])

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    void getIdentities(controller.signal).then((result) => { if (!disposed) { setIdentities(result); setIdentityError(false) } })
      .catch(() => { if (!disposed) { setIdentities([]); setIdentityError(true) } })
    return () => { disposed = true; controller.abort() }
  }, [configurationRevision, refresh])
  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    setModelReady(null)
    void getModelReadiness(controller.signal).then((ready) => { if (!disposed) setModelReady(ready) })
      .catch(() => { if (!disposed) setModelReady(false) })
    return () => { disposed = true; controller.abort() }
  }, [configurationRevision, modelRefresh])

  useEffect(() => {
    if (!selectedId) { setDetail(null); return }
    const controller = new AbortController()
    let disposed = false
    setDetailLoading(true); setDetailError(null)
    selectUrl(selectedId)
    void getTask(selectedId, controller.signal).then((result) => { if (!disposed) setDetail(result) })
      .catch((error: unknown) => { if (!disposed) setDetailError(error instanceof Error ? error.message : '任务读取失败。') })
      .finally(() => { if (!disposed) setDetailLoading(false) })
    return () => { disposed = true; controller.abort() }
  }, [selectedId, detailRefresh, executionRevision])

  function chooseTask(taskId: string) {
    if (saving) return
    setEditTask(null); createPending.current = null; setRevisionConfirmed(false)
    setNotice(editTask ? { kind: 'conflict', message: '已退出原任务修订。表单内容保留为新任务草稿，来源和写入授权已清除；请重新核对。' } : null)
    setAuthorized(false); setWriteAuthorized(false); setPermission('read_only'); setIdentityRef('')
    setSelectedId(taskId); setDetail(null); setConflict(false); setClarifications({}); setClarifyConfirmed(false)
    setClarifyIdentity(''); setClarifyRows([{ ...EMPTY_SOURCE }]); setClarifySourceConfirmed(false)
    setClarifyWrite({ ...EMPTY_WRITE, operations: [] }); setClarifyWriteConfirmed(false); setTimeScope({ start: '', end: '', basis: '' })
    clarifyPending.current = null
  }
  function safeSources(values: SourceInput[]) {
    try { return sourceValues(values).sources } catch { return [] }
  }
  function savedSources(values: unknown) {
    try { return parseSourceScopes(values) } catch { return [] }
  }
  const currentSources = safeSources(rows)
  const draftSources = savedSources(detail?.draft?.sources)
  const clarifySources = detail?.missing_fields.includes('sources') ? safeSources(clarifyRows) : draftSources
  const busy = saving || detailLoading

  async function synchronizeConflict(taskId: string) {
    setConflict(true); setClarifyConfirmed(false); setRevisionConfirmed(false)
    try { const latest = await getTask(taskId); if (alive.current) { setDetail(latest); setDetailError(null) } }
    catch { if (alive.current) setDetailError('最新任务读取失败，暂不能确认；请先重新读取。') }
  }

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving || conflict || modelReady !== true) return
    setNotice(null)
    let body: JsonValue
    try {
      if (editTask && !revisionConfirmed) throw new Error('请核对来源和权限，并明确确认本次完整修订。')
      const request = createRequest({ instruction, scenario, rows, authorized, permission, write, writeAuthorized, identityRef, identities })
      body = (editTask ? { ...request, contract_version: editTask.version } : request) as unknown as JsonValue
    } catch (error) { setNotice(errorNotice(error)); return }
    const path = editTask ? `/api/v1/tasks/${encodeURIComponent(editTask.id)}/revisions` : '/api/v1/tasks'
    createPending.current = submissionKey(createPending.current, path, body)
    const controller = new AbortController(); saveController.current = controller; setSaving(true)
    try {
      const receipt = await postTask(path, body, createPending.current.key, controller.signal)
      const latest = await getTask(receipt.task.task_id, controller.signal)
      if (!alive.current) return
      setSelectedId(latest.task.task_id); selectUrl(latest.task.task_id); setDetail(latest); setDetailError(null); setEditTask(null)
      setClarifications({}); setClarifyConfirmed(false); setConflict(false); setRefresh((value) => value + 1)
      createPending.current = null; setAuthorized(false); setWriteAuthorized(false); setRevisionConfirmed(false)
      setNotice({ kind: 'success', message: latest.task.preparation_status === 'READY'
        ? '任务和契约已持久保存。请核对允许来源与动作；创建任务不会自动开始运行。' : '任务草稿已持久保存。请在下方集中补充必需信息。' })
    } catch (error) {
      if (!alive.current) return
      setNotice(errorNotice(error))
      if (editTask && error instanceof TaskApiError && error.status === 409 && error.code !== 'CONFIG_NOT_READY') await synchronizeConflict(editTask.id)
    } finally { if (alive.current) setSaving(false); saveController.current = null }
  }

  async function submitClarifications(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!detail || saving || conflict) return
    setNotice(null)
    let values: Record<string, JsonValue>
    try {
      if (!clarifyConfirmed) throw new Error('请核对所填信息，并确认只补充本次列出的必需字段。')
      values = {}
      for (const field of detail.missing_fields) {
        if (!Object.hasOwn(FIELD_LABELS, field)) throw new Error('此任务包含当前界面不支持的字段；请使用对应 API 明确提交。')
        if (field === 'sources') {
          if (!clarifySourceConfirmed) throw new Error('请明确确认新增来源的访问授权。')
          values.sources = sourceValues(clarifyRows).sources as unknown as JsonValue
        } else if (field === 'action_policy') values.action_policy = writePolicy(clarifyWrite, clarifyWriteConfirmed) as unknown as JsonValue
        else if (field === 'identity_ref') {
          const identity = identities.find((item) => item.identity_ref === clarifyIdentity)
          if (!identity || !matchingIdentity(identity, clarifySources)) throw new Error('请明确选择与授权来源匹配且已验证的账号。')
          values.identity_ref = identity.identity_ref
        } else if (field === 'time_scope') {
          if (!timeScope.basis.trim()) throw new Error('请填写时间窗口的依据。')
          values.time_scope = { start: utcInput(timeScope.start), end: utcInput(timeScope.end), basis: timeScope.basis.trim() }
        } else values[field] = clarificationValue(field, clarifications[field] ?? '')
      }
    } catch (error) { setNotice(errorNotice(error)); return }
    const path = `/api/v1/tasks/${encodeURIComponent(detail.task.task_id)}/clarifications`
    const body: JsonValue = { contract_version: detail.contract_version, values }
    clarifyPending.current = submissionKey(clarifyPending.current, path, body)
    const controller = new AbortController(); saveController.current = controller; setSaving(true)
    try {
      const receipt = await postTask(path, body, clarifyPending.current.key, controller.signal)
      const latest = await getTask(receipt.task.task_id, controller.signal)
      if (!alive.current) return
      setDetail(latest); setDetailError(null); setClarifyConfirmed(false); setClarifySourceConfirmed(false); setClarifyWriteConfirmed(false)
      setRefresh((value) => value + 1); clarifyPending.current = null
      setNotice({ kind: 'success', message: latest.task.preparation_status === 'READY'
        ? '补充信息已持久保存，实际契约已生成。请核对以下范围与允许动作。' : '补充信息已保存；请继续补充下一组明确的必需字段。' })
    } catch (error) {
      if (!alive.current) return
      setNotice(errorNotice(error))
      if (error instanceof TaskApiError && error.status === 409) await synchronizeConflict(detail.task.task_id)
    } finally { if (alive.current) setSaving(false); saveController.current = null }
  }

  function beginRevision() {
    if (!detail || busy || detail.task.task_id !== selectedId) return
    setEditTask({ id: detail.task.task_id, version: detail.contract_version })
    setInstruction(typeof detail.draft?.instruction === 'string' ? detail.draft.instruction : detail.task.original_instruction)
    setRows(sourceInputs(detail.draft)); setScenario(Object.hasOwn(SCENARIOS, String(detail.draft?.scenario)) ? detail.draft?.scenario as Scenario : '')
    setPermission('read_only'); setIdentityRef(''); setWrite({ ...EMPTY_WRITE, operations: [] }); setAuthorized(false); setWriteAuthorized(false)
    setRevisionConfirmed(false); setConflict(false); createPending.current = null
    setNotice({ kind: 'conflict', message: '正在编写完整修订。请重新核对来源和权限；权限从只读开始，账号须再次显式选择。' })
    document.getElementById('task-instruction')?.focus()
  }

  async function loadMore() {
    if (!cursor || listLoading) return
    setListLoading(true)
    try {
      const result = await getTasks(cursor)
      if (!alive.current) return
      setTasks((prior) => [...prior, ...result.tasks.filter((task) => !prior.some((item) => item.task_id === task.task_id))]); setCursor(result.next_cursor)
    } catch (error) { if (alive.current) setListError(error instanceof Error ? error.message : '读取下一页失败。') }
    finally { if (alive.current) setListLoading(false) }
  }

  return <section className="task-entry" aria-labelledby="task-entry-heading">
    <div className="task-section-heading"><div><span className="task-eyebrow">TASK ENTRY</span><h2 id="task-entry-heading">把目标变成明确的任务</h2>
      <p>描述目标，确认可访问的来源，再核对系统保存的契约与权限。</p></div><span className="task-small-tag">默认只读</span></div>
    {notice && <div className={`task-notice ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'} aria-live="polite">
      <p>{notice.message}</p>{notice.configuration && onOpenSettings && <button type="button" className="task-text-button" onClick={onOpenSettings}>前往模型配置</button>}
    </div>}
    {conflict && <div className="task-notice conflict" role="alert"><p>原输入已保留。请先查看下方最新任务，确认版本、缺失字段和来源权限，再继续提交。</p>
      <button type="button" disabled={busy || !!detailError || !detail} className="task-secondary" onClick={() => {
        setConflict(false); setClarifyConfirmed(false); setRevisionConfirmed(false)
        if (editTask && detail?.task.task_id === editTask.id) setEditTask({ ...editTask, version: detail.contract_version })
        createPending.current = null; clarifyPending.current = null
        setNotice({ kind: 'conflict', message: '已采用最新任务版本。请再次核对所填信息并明确确认提交。' })
      }}>我已核对最新任务</button></div>}
    <div className="task-entry-layout"><form className="task-compose" onSubmit={(event) => { void submitTask(event) }}>
      <div className="task-card-heading"><h3>{editTask ? '修改任务' : '新建任务'}</h3>{editTask ? <button type="button" className="task-text-button" disabled={saving}
        onClick={() => { setEditTask(null); setRevisionConfirmed(false); createPending.current = null; setNotice(null); setConflict(false) }}>取消修订</button>
        : <button type="button" className="task-text-button" disabled={saving} onClick={() => {
          setInstruction(''); setScenario(''); setRows([{ ...EMPTY_SOURCE }]); setAuthorized(false); setPermission('read_only')
          setIdentityRef(''); setWrite({ ...EMPTY_WRITE, operations: [] }); setWriteAuthorized(false); createPending.current = null; setNotice(null)
        }}>新建任务</button>}</div>
      {editTask && <p className="task-edit-target" data-testid="task-revision-target">正在修订 <code>{editTask.id}</code> · 当前修订 {editTask.version}</p>}
      {modelReady !== true && <div className="task-configuration-note" role="status"><p>{modelReady === null ? '正在读取模型就绪状态…' : '模型配置尚未就绪，完成配置后才能编译任务。'}</p>
        {onOpenSettings && <button type="button" className="task-text-button" onClick={onOpenSettings}>前往模型配置</button>}
      </div>}
      <button type="button" className="task-text-button task-readiness-refresh" disabled={saving || modelReady === null} onClick={() => setModelRefresh((value) => value + 1)}>刷新模型就绪状态</button>
      <label htmlFor="task-instruction">你希望完成什么？<textarea id="task-instruction" rows={5} required value={instruction} disabled={saving}
        placeholder="例如：读取 ACME 的 2025 年报，提取营收和净利润，使用人民币口径。请写明对象、时间和需要的结果。"
        onChange={(event) => { setInstruction(event.target.value); setRevisionConfirmed(false); setClarifyConfirmed(false) }} /></label>
      <label htmlFor="task-scenario">任务场景<select id="task-scenario" disabled={saving || permission === 'repository_write'} value={permission === 'repository_write' ? 'operations' : scenario}
        onChange={(event) => { setScenario(event.target.value as Scenario | ''); setRevisionConfirmed(false); setClarifyConfirmed(false) }}><option value="">从任务描述判断；无法确定时询问</option>
        {Object.entries(SCENARIOS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <div className="task-subheading"><h4>已授权的来源</h4><p>来源地址限定站点，路径前缀限定访问范围。请逐项列出；网页内容不能增加来源或权限。</p></div>
      <SourceEditor rows={rows} onChange={(values) => { setRows(values); setAuthorized(false); setIdentityRef(''); setRevisionConfirmed(false); setClarifyConfirmed(false) }} disabled={saving} />
      <label className="task-check task-authorization" htmlFor="source-authorization"><input id="source-authorization" type="checkbox" required disabled={saving}
        checked={authorized} onChange={(event) => setAuthorized(event.target.checked)} />我确认已获授权访问所列来源与路径</label>
      <div className="task-subheading"><h4>允许动作与账号</h4><p>任务文字中的“修改”不会自动授予写入权限。</p></div>
      <label htmlFor="task-permission">权限方式<select id="task-permission" disabled={saving} value={permission}
        onChange={(event) => { setPermission(event.target.value as ActionPolicy['mode']); setWriteAuthorized(false); setRevisionConfirmed(false); setClarifyConfirmed(false); setIdentityRef('') }}>
        <option value="read_only">只读浏览与读取（默认）</option><option value="repository_write">仓库代码修复：明确授权写入</option>
      </select></label>
      {permission === 'read_only' ? <PolicyView policy={{ mode: 'read_only' }} />
        : <><WriteEditor value={write} disabled={saving} onChange={(value) => { setWrite(value); setWriteAuthorized(false); setRevisionConfirmed(false); setClarifyConfirmed(false) }} />
          <label className="task-check task-authorization" htmlFor="write-authorization"><input id="write-authorization" type="checkbox" required checked={writeAuthorized} disabled={saving}
            onChange={(event) => setWriteAuthorized(event.target.checked)} />我明确授权以上仓库、分支、文件白名单和勾选的动作</label></>}
      <AccountSelect id="task-identity" value={identityRef} onChange={(value) => { setIdentityRef(value); setRevisionConfirmed(false); setClarifyConfirmed(false) }} identities={identities} sources={currentSources} disabled={saving} />
      {identityError && <p className="task-warning" role="status">账号服务暂不可用。公开只读任务可不指定账号；需要账号的任务请先到配置页检查。</p>}
      {editTask && <label className="task-check task-authorization" htmlFor="task-revision-confirmation"><input id="task-revision-confirmation" type="checkbox" required disabled={saving}
        checked={revisionConfirmed} onChange={(event) => setRevisionConfirmed(event.target.checked)} />我已核对完整修订、来源与允许动作</label>}
      <button type="submit" className="task-primary" disabled={saving || conflict || modelReady !== true}>{saving ? '正在保存，请稍候…' : editTask ? '确认提交任务修订' : '提交任务'}</button>
      <p className="task-hint">任务描述会交给已配置的模型提供商编译。这里只创建并保存任务，开始运行将在执行工作台中提供。</p>
    </form>
    <aside className="task-catalog" aria-labelledby="task-catalog-heading"><div className="task-card-heading"><h3 id="task-catalog-heading">已保存的任务</h3>
      <button type="button" className="task-text-button" disabled={saving || listLoading} onClick={() => setRefresh((value) => value + 1)}>刷新列表</button></div>
      <p className="task-hint">从本机持久记录读取；重载页面后仍可查看。</p>
      {listError && <p className="task-warning" role="alert">{listError}</p>}
      {listLoading && tasks.length === 0 && <p className="task-empty" role="status">正在读取任务…</p>}
      {!listLoading && !listError && tasks.length === 0 && <div className="task-empty"><span aria-hidden="true">＋</span><p>还没有已保存的任务</p><small>提交左侧表单后，任务会出现在这里。</small></div>}
      <div className="task-list" data-testid="task-list">{tasks.map((task) => <button type="button" key={task.task_id} disabled={saving}
        className={`task-list-card ${selectedId === task.task_id ? 'selected' : ''}`} aria-pressed={selectedId === task.task_id} onClick={() => chooseTask(task.task_id)}>
        <span className={`task-preparation ${task.preparation_status.toLowerCase()}`}>{STATE_LABELS[task.preparation_status]}</span>
        <strong>{task.original_instruction}</strong><span className="task-list-date">{dateLabel(task.created_at)}</span>
        <span className="task-list-meta">{task.requested_fields.length ? `${task.requested_fields.length} 项信息待补充` : '查看契约与允许动作'}{task.current_run_state ? ` · ${STATE_LABELS[task.current_run_state] ?? task.current_run_state}` : ''}</span>
      </button>)}</div>
      {cursor && <button type="button" className="task-secondary task-load-more" disabled={listLoading || saving} onClick={() => { void loadMore() }}>{listLoading ? '读取中…' : '读取更早的任务'}</button>}
    </aside></div>

    <section className="task-detail" data-testid="task-detail" aria-labelledby="task-detail-heading"><div className="task-card-heading"><h3 id="task-detail-heading">任务详情</h3>
      <div className="task-detail-actions"><button type="button" className="task-text-button" disabled={!selectedId || busy} onClick={() => setDetailRefresh((value) => value + 1)}>重新读取详情</button>
        <button type="button" className="task-secondary" disabled={!detail || detail.task.task_id !== selectedId || busy || conflict} onClick={beginRevision}>修改任务</button></div></div>
      {detailLoading && <p className="task-empty" role="status">正在读取所选任务…</p>}
      {detailError && <p className="task-warning" role="alert">{detailError}</p>}
      {!selectedId && <p className="task-empty">选择已保存的任务，查看实际契约与缺失信息。</p>}
      {detail && detail.task.task_id === selectedId && <><div className="task-detail-intro"><div><span className={`task-preparation ${detail.task.preparation_status.toLowerCase()}`} data-testid="task-status">
        {STATE_LABELS[detail.task.preparation_status]}</span><h4>{detail.task.original_instruction}</h4><p>修订 {detail.contract_version} · 保存于 {dateLabel(detail.task.created_at)}</p></div>
        <p className="task-reference">任务标识 <code>{detail.task.task_id}</code></p></div>
        {detail.current_run && <p className="task-saved-run">最近运行：{STATE_LABELS[detail.current_run.state] ?? detail.current_run.state}。任务准备状态与运行结论分别保存。</p>}
        {!detail.current_run && <p className="task-saved-run">{detail.task.preparation_status === 'READY' ? '任务卡已准备，尚无执行运行。' : '任务草稿尚需补充信息，尚无执行运行。'}</p>}
        {detail.missing_fields.length > 0 && <form className="task-clarifications" data-testid="task-clarifications" onSubmit={(event) => { void submitClarifications(event) }}>
          <div className="task-subheading"><h4>请集中补充 {detail.missing_fields.length} 项信息</h4><p>只提交系统列出的缺失字段，来源、账号和写入权限均需明确确认。</p></div>
          {detail.clarification_questions && <ul className="task-question-list">{detail.clarification_questions.map((question) => <li key={question.field}>{question.message}</li>)}</ul>}
          <div className="task-field-grid">{detail.missing_fields.map((field) => field === 'sources' ? <div key={field} className="task-full-field">
            <SourceEditor prefix="clarify-" rows={clarifyRows} disabled={busy} onChange={(values) => { setClarifyRows(values); setClarifySourceConfirmed(false); setClarifyIdentity(''); setClarifyConfirmed(false) }} />
            <label className="task-check task-authorization" htmlFor="clarify-source-authorization"><input type="checkbox" id="clarify-source-authorization" required disabled={busy}
              checked={clarifySourceConfirmed} onChange={(event) => setClarifySourceConfirmed(event.target.checked)} />我确认已获授权访问新增来源与路径</label>
          </div> : field === 'action_policy' ? <div key={field} className="task-full-field"><WriteEditor prefix="clarify-" value={clarifyWrite} disabled={busy}
            onChange={(value) => { setClarifyWrite(value); setClarifyWriteConfirmed(false); setClarifyConfirmed(false) }} />
            <label className="task-check task-authorization" htmlFor="clarify-write-authorization"><input type="checkbox" id="clarify-write-authorization" required disabled={busy}
              checked={clarifyWriteConfirmed} onChange={(event) => setClarifyWriteConfirmed(event.target.checked)} />我明确授权以上写入范围与勾选动作</label>
          </div> : field === 'identity_ref' ? <AccountSelect key={field} id="clarify-identity_ref" value={clarifyIdentity} onChange={(value) => { setClarifyIdentity(value); setClarifyConfirmed(false) }}
            identities={identities} sources={clarifySources} disabled={busy} /> : field === 'time_scope' ? <div key={field} className="task-full-field task-time-scope">
            {([['start', 'UTC 开始时间'], ['end', 'UTC 结束时间'], ['basis', '时间窗口依据']] as const).map(([key, label]) => <label key={key} htmlFor={`clarify-time-${key}`}>{label}
              <input id={`clarify-time-${key}`} required disabled={busy} value={timeScope[key]} placeholder={key === 'basis' ? '例如用户指定的查询窗口' : '2026-10-02T00:00:00Z'}
                onChange={(event) => { setTimeScope({ ...timeScope, [key]: event.target.value }); setClarifyConfirmed(false) }} /></label>)}
          </div> : Object.hasOwn(FIELD_LABELS, field) ? <ParameterInput key={field} field={field} value={clarifications[field] ?? ''} disabled={busy}
            onChange={(value) => { setClarifications({ ...clarifications, [field]: value }); setClarifyConfirmed(false) }} />
            : <p key={field} className="task-warning task-full-field">当前界面暂不支持此缺失字段，请通过对应 API 明确提交。</p>)}</div>
          <label className="task-check task-authorization" htmlFor="clarify-confirmation"><input type="checkbox" id="clarify-confirmation" required disabled={busy || conflict}
            checked={clarifyConfirmed} onChange={(event) => setClarifyConfirmed(event.target.checked)} />我已核对信息，仅补充本次列出的必需字段</label>
          <button type="submit" className="task-primary" disabled={busy || conflict || detail.missing_fields.some((field) => !Object.hasOwn(FIELD_LABELS, field))}>确认补充信息</button>
        </form>}
        {detail.contract ? <section className="task-contract" data-testid="task-contract"><div className="task-subheading"><h4>
          {detail.contract.contract_version === detail.contract_version ? '实际保存的契约' : `最近保存的契约（版本 ${detail.contract.contract_version}，当前修订尚未就绪）`}</h4>
          <p>契约已保存不代表任务运行成功。以下内容来自服务的持久记录。</p></div>
          <div className="task-contract-overview"><span>{SCENARIOS[detail.contract.scenario]}</span><span>{detail.contract.targets.map((target) => target.canonical_name).join('、')}</span></div>
          <h5>来源范围与起始页面</h5><ul className="task-contract-sources">{detail.contract.sources.map((source) => <li key={source.source_id}>
            <strong>{source.source_id}</strong><span>{source.origin}{source.path_prefix}</span><small>站点：{source.site_id}</small></li>)}</ul>
          <ul className="task-start-urls">{detail.contract.start_urls.map((url) => <li key={url}>{url}</li>)}</ul>
          <PolicyView policy={detail.contract.action_policy} />
          <p className="task-hint">执行账号：{detail.contract.identity_ref ? identities.find((identity) => identity.identity_ref === detail.contract?.identity_ref)?.normalized_account ?? '已绑定账号引用；当前元数据不可用，请在配置页核对' : '未指定（公开只读）'}</p>
          <h5>明确的业务参数</h5><dl className="task-parameter-list">{Object.entries(detail.contract.parameters).filter(([key]) => key !== 'scenario').map(([key, value]) => <div key={key}>
            <dt>{fieldLabel('parameters.' + key)}</dt><dd>{formatValue(value)}</dd></div>)}</dl>
          <p className="task-hint">时间口径：{detail.contract.time_scope.basis}；{detail.contract.time_scope.start ? dateLabel(detail.contract.time_scope.start) : '未限定开始时间'} → {detail.contract.time_scope.end ? dateLabel(detail.contract.time_scope.end) : '未限定结束时间'}</p>
          <h5>预期输出与验收条件</h5><ul className="task-output-list">{detail.contract.output_schema.map((field) => <li key={field.field_id}>{field.description}{field.required ? '（必需）' : '（可选）'}</li>)}</ul>
          <ul className="task-criterion-list">{detail.contract.acceptance_criteria.map((criterion) => <li key={criterion.criterion_id}>{criterion.expected_rule}{criterion.critical ? ' · 必须满足' : ''}</li>)}</ul>
        </section> : <div className="task-draft-summary"><p>草稿尚未生成完整契约；当前授权范围仍须核对。</p>
          {detail.draft && isPolicy(detail.draft.action_policy) && <PolicyView policy={detail.draft.action_policy} />}</div>}
      </>}
    </section>
  </section>
}
