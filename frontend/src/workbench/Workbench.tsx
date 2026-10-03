import { useCallback, useEffect, useRef, useState } from 'react'
import { controlAllowed, currentOperation } from './contracts'
import { getInputs, getOperation, openStream, pendingControl, postControl, readEvidence, replayEvents, WorkbenchError } from './client'
import { EventTracker, parseFrame, SseParser } from './events'
import { actionLabel, duration, eventLabel, operationNotice, phaseLabel, reasonLabel, stateLabel } from './presentation'
import type { ControlAction, Operation, PendingControl, WorkbenchInputs } from './types'
import './workbench.css'

type Connection = 'loading'|'live'|'reconnecting'|'stale'
const connectionLabel = (value: Connection) => ({ loading:'正在同步', live:'已同步', reconnecting:'连接中断，正在重新同步', stale:'连接未同步，控制已禁用' }[value])
export default function Workbench({ taskId, onTaskChanged }: { taskId: string|null; onTaskChanged?: () => void }) {
  const [inputs, setInputs] = useState<WorkbenchInputs|null>(null)
  const [connection, setConnection] = useState<Connection>('loading')
  const [syncing, setSyncing] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [notice, setNotice] = useState('')
  const [receipt, setReceipt] = useState<Operation|null>(null)
  const [unknown, setUnknown] = useState<PendingControl|null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [streamGeneration, setStreamGeneration] = useState(0)
  const [image, setImage] = useState<string|null>(null)
  const [pageText, setPageText] = useState<{title:string;text:string}|null>(null)
  const [evidenceStatus, setEvidenceStatus] = useState('尚无页面证据')
  const inputsRef = useRef<WorkbenchInputs|null>(null), receiptRef = useRef<Operation|null>(null)
  const pendingRef = useRef<PendingControl|null>(null), busyRef = useRef(false)
  const lifetimeRef = useRef(new AbortController()), generationRef = useRef(0)
  const loadRef = useRef<Promise<WorkbenchInputs>|null>(null)
  const callbackRef = useRef(onTaskChanged); callbackRef.current = onTaskChanged
  const refresh = useCallback(async () => {
    if (!taskId) throw new Error('尚未选择任务。')
    if (loadRef.current) return loadRef.current
    const generation = generationRef.current, signal = lifetimeRef.current.signal
    setSyncing(true)
    const promise = (async () => {
      const fresh = await getInputs(taskId, signal)
      if (signal.aborted || generation !== generationRef.current) throw new Error('已切换任务。')
      const previous = inputsRef.current
      if (previous?.workspace.run?.run_id === fresh.workspace.run?.run_id && previous?.workspace.run && fresh.workspace.run
        && fresh.workspace.run.state_version < previous.workspace.run.state_version) throw new Error('运行版本退回，需重新同步。')
      const stamp = (value: WorkbenchInputs|null) => value ? `${value.workspace.task.state_version}:${value.workspace.task.current_contract_version}:${value.workspace.run?.run_id}:${value.workspace.run?.state_version}:${value.workspace.run ? value.workspace.run.settings_version : value.readiness.version}` : ''
      if (stamp(previous) !== stamp(fresh)) setConfirmed(false)
      inputsRef.current = fresh; setInputs(fresh)
      if (previous && (previous.workspace.task.state_version !== fresh.workspace.task.state_version || previous.workspace.task.current_run_id !== fresh.workspace.task.current_run_id
        || previous.workspace.run?.state_version !== fresh.workspace.run?.state_version)) callbackRef.current?.()
      if (!fresh.workspace.run) setConnection('live')
      const known = receiptRef.current
      if (known && known.task_id === taskId && known.run_id === fresh.workspace.run?.run_id) {
        const operation = await getOperation(known.operation_id, taskId, known.run_id, signal, known)
        if (!signal.aborted && generation === generationRef.current) { receiptRef.current = operation; setReceipt(operation) }
      }
      return fresh
    })()
    loadRef.current = promise
    try { return await promise } finally { if (generation === generationRef.current) { loadRef.current = null; setSyncing(false) } }
  }, [taskId])
  useEffect(() => {
    lifetimeRef.current.abort(); lifetimeRef.current = new AbortController(); generationRef.current++
    const signal = lifetimeRef.current.signal
    loadRef.current = null; inputsRef.current = null; receiptRef.current = null; pendingRef.current = null; busyRef.current = false
    setInputs(null); setReceipt(null); setUnknown(null); setConfirmed(false); setNotice(''); setSubmitting(false); setConnection('loading')
    if (!taskId) return () => lifetimeRef.current.abort()
    const load = () => refresh().catch(() => { if (!signal.aborted) { setConnection('stale'); setNotice('暂时无法读取最新状态，请重新同步。') } })
    void load()
    // Graph phases and elapsed budget do not always append business events.
    const poll = window.setInterval(() => { if (!busyRef.current && !signal.aborted) void load() }, 3000)
    return () => { lifetimeRef.current.abort(); window.clearInterval(poll) }
  }, [taskId, refresh])
  const selectedInputs = inputs?.workspace.task.task_id === taskId ? inputs : null
  const runId = selectedInputs?.workspace.run?.run_id ?? null
  useEffect(() => {
    if (!taskId || !runId) return
    const controller = new AbortController(), signal = AbortSignal.any([controller.signal, lifetimeRef.current.signal])
    let tracker = new EventTracker(inputsRef.current?.workspace.event_cursor ?? '0', inputsRef.current?.workspace.events ?? []), failures = 0
    const delay = (ms: number) => new Promise<void>((resolve) => {
      const finish = () => { window.clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
      const timer = window.setTimeout(finish, ms); signal.addEventListener('abort', finish, { once:true })
      if (signal.aborted) finish()
    })
    async function follow() {
      while (!signal.aborted) {
        let streamController: AbortController|null = null
        try {
          if (failures) {
            setConnection('reconnecting')
            await replayEvents(taskId!, runId!, tracker.cursor, signal)
            const fresh = await refresh()
            if (fresh.workspace.run?.run_id !== runId || signal.aborted) return
            tracker = new EventTracker(fresh.workspace.event_cursor, fresh.workspace.events)
          }
          streamController = new AbortController()
          const streamSignal = AbortSignal.any([signal, streamController.signal])
          const body = await openStream(taskId!, runId!, tracker.cursor, streamSignal)
          if (signal.aborted) return
          setConnection('live'); setNotice((prior) => prior === '实时连接中断，正在读取持久事件和最新状态。' ? '' : prior)
          const reader = body.getReader(), decoder = new TextDecoder('utf-8', { fatal:true }), parser = new SseParser()
          let lastActivity = Date.now(), refreshTimer: number|undefined
          const watchdog = window.setInterval(() => { if (Date.now() - lastActivity > 35000) streamController?.abort() }, 5000)
          try {
            while (!signal.aborted) {
              const part = await reader.read(); if (part.done) throw new Error('实时连接已关闭。')
              lastActivity = Date.now()
              const batch = parser.push(decoder.decode(part.value, { stream:true }))
              let changed = false
              for (const frame of batch.frames) {
                const verdict = tracker.accept(parseFrame(frame, taskId!, runId!))
                if (verdict === 'resync') throw new Error('事件顺序或内容需要重新同步。')
                if (verdict === 'new') changed = true
              }
              if (changed && refreshTimer === undefined) refreshTimer = window.setTimeout(() => {
                refreshTimer = undefined
                void refresh().catch(() => { if (!signal.aborted) { setConnection('stale'); streamController?.abort() } })
              }, 150)
            }
          } finally { window.clearInterval(watchdog); if (refreshTimer !== undefined) window.clearTimeout(refreshTimer); streamController.abort(); await reader.cancel().catch(() => {}); reader.releaseLock() }
        } catch {
          streamController?.abort()
          if (signal.aborted) return
          setConnection('reconnecting'); setConfirmed(false)
          setNotice('实时连接中断，正在读取持久事件和最新状态。')
          failures++
          await delay(Math.min(10000, 500 * 2 ** Math.min(failures - 1, 5)))
        }
      }
    }
    setConnection('loading'); void follow()
    return () => controller.abort()
  }, [taskId, runId, refresh, streamGeneration])
  const observation = selectedInputs?.workspace.observation
  const readable = observation?.evidence.filter((e) => ['screenshot','text'].includes(e.artifact_kind) && e.availability === 'AVAILABLE' && e.redaction_status === 'FILTERED')
  const evidenceId = (readable?.find((e) => e.evidence_id === observation?.screenshot_evidence_id) ?? readable?.find((e) => e.artifact_kind === 'screenshot') ?? readable?.find((e) => e.artifact_kind === 'text'))?.evidence_id ?? null, snapshotId = observation?.snapshot_id ?? null
  useEffect(() => {
    const controller = new AbortController(); let url: string|null = null
    setImage(null); setPageText(null)
    if (!evidenceId || !runId || !snapshotId) { setEvidenceStatus(inputsRef.current?.workspace.observation ? '最新页面没有可读的脱敏展示副本' : '尚无页面证据'); return () => controller.abort() }
    setEvidenceStatus('正在读取受控展示副本')
    void readEvidence(evidenceId, runId, snapshotId, controller.signal).then((result) => {
      if (controller.signal.aborted) return
      if (result.kind === 'screenshot') { url = URL.createObjectURL(result.blob); setImage(url) }
      else setPageText({ title:result.title, text:result.text })
      setEvidenceStatus('经服务校验的脱敏展示副本')
    }).catch(() => { if (!controller.signal.aborted) setEvidenceStatus('页面证据不可读，请重新同步；不代表页面或验证成功。') })
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [evidenceId, runId, snapshotId])
  const resync = async () => {
    setConfirmed(false); setConnection('loading'); setNotice('')
    try { await refresh(); setStreamGeneration((value) => value + 1) }
    catch { setConnection('stale'); setNotice('重新同步失败，请检查本机连接后重试。') }
  }
  async function submit(action: ControlAction, original?: PendingControl) {
    if (!taskId || busyRef.current || !inputsRef.current || inputsRef.current.workspace.task.task_id !== taskId) return
    const current = inputsRef.current, workspace = current.workspace, run = workspace.run
    if (!original && (connection !== 'live' || !confirmed || !controlAllowed(workspace, action, taskId) || action === 'start' && !current.readiness.ready)) return
    const body = action === 'start' ? { expected_state_version: workspace.task.state_version, contract_version: workspace.task.current_contract_version!, settings_version: current.readiness.version }
      : { expected_state_version: run!.state_version, contract_version: run!.contract_version, settings_version: run!.settings_version }
    const pending = original ?? pendingControl(taskId, action === 'start' ? taskId : run!.run_id, action, body, pendingRef.current)
    if (pending.taskId !== taskId) return
    busyRef.current = true; pendingRef.current = pending; setSubmitting(true); setConfirmed(false); setNotice('正在提交控制请求…')
    const generation = generationRef.current, signal = lifetimeRef.current.signal
    try {
      const accepted = await postControl(pending, signal)
      if (signal.aborted || generation !== generationRef.current) return
      receiptRef.current = accepted; setReceipt(accepted); setUnknown(null); pendingRef.current = null
      // Processing/completion copy follows the durable operation, never a stale toast.
      setNotice('')
      callbackRef.current?.()
      await refresh()
    } catch (error) {
      if (signal.aborted || generation !== generationRef.current) return
      const definite = error instanceof WorkbenchError && [400,401,403,404,409,422].includes(error.status)
      if (definite) {
        setUnknown(null); pendingRef.current = null
        setNotice(error instanceof Error ? error.message : '请求未受理，请重新同步。')
        await refresh().catch(() => setConnection('stale'))
      } else {
        setUnknown(pending); setNotice('提交结果尚未确认。原请求及其标识已保留；请查询原请求，不要另发控制请求。')
        setConnection('stale')
      }
    } finally { if (generation === generationRef.current) { busyRef.current = false; setSubmitting(false) } }
  }
  const workspace = selectedInputs?.workspace, run = workspace?.run
  const operation = workspace ? currentOperation(workspace, receipt) : undefined
  const banner = notice || operationNotice(operation)
  const canSubmit = !!selectedInputs && connection === 'live' && !syncing && !submitting && !unknown && confirmed
  const contract = selectedInputs?.detail.contract
  return <section id="execution-workbench" className="workbench" aria-labelledby="workbench-title">
    <header className="workbench-heading"><div><p className="section-kicker">当前任务执行</p><h2 id="workbench-title">执行工作台</h2></div>
      <span data-testid="workbench-connection" className={`workbench-connection ${connection}`}>{taskId ? connectionLabel(selectedInputs ? connection : 'loading') : '尚未选择任务'}</span></header>
    {!taskId ? <p className="workbench-empty">在任务入口选择一张任务卡，即可查看当前执行进度。</p> : <>
      <div className="workbench-toolbar"><code className="opaque-id">{taskId}</code><button type="button" disabled={submitting || syncing} onClick={() => void resync()}>重新同步</button></div>
      {banner && <p className="workbench-notice" role="status">{banner}</p>}
      {unknown && <button type="button" disabled={submitting} onClick={() => void submit(unknown.action, unknown)}>查询原请求</button>}
      {!workspace ? <p>正在读取持久状态…</p> : <>
        <div className="workbench-summary"><div><span>当前运行</span><strong data-testid="workbench-state" data-state={run?.state ?? 'PREPARED'}>{run ? stateLabel(run.state) : workspace.task.preparation_status === 'READY' ? '准备完成，尚未启动' : '等待补充输入'}</strong>
          {run && <small>契约 v{run.contract_version} · 配置 v{run.settings_version} · 状态 v{run.state_version}</small>}</div>
          <div data-testid="workbench-queue"><span>队列与受阻原因</span><strong>{workspace.queue ? ({ QUEUED:'排队', ACTIVE:'已取得资源', WAITING:'等待', RECOVERY:'恢复核对', FINISHED:'队列已结束' }[workspace.queue.status] ?? '待核对') : '尚未入队'}</strong>
            <small>{reasonLabel(run?.blocked_reason ?? workspace.queue?.reason ?? null)}</small></div></div>
        {run && <p className="opaque-id">Run：{run.run_id}</p>}
        {!run && contract && <div className="workbench-review"><h3>启动前核对</h3><p>{contract.objective}</p>
          <p>契约 v{contract.contract_version} · 模型配置 v{inputs!.readiness.version} · {inputs!.readiness.ready ? '配置已就绪' : '请先完成模型配置和数据发送说明'}</p>
          <ul>{contract.sources.map((source) => <li key={source.source_id}>{source.site_id}：{source.origin}{source.path_prefix}</li>)}</ul>
          <p>允许动作：{contract.action_policy.mode === 'read_only' ? '只读浏览器操作' : contract.action_policy.allowed_operations.map((action) => ({ edit_file:'编辑允许文件',create_branch:'创建指定分支',commit:'提交',create_pr:'创建 PR',update_pr:'更新 PR' }[action])).join('、')}</p>
          {contract.action_policy.mode === 'repository_write' && <p>写入仓库：{contract.action_policy.repository} · 分支：{contract.action_policy.branch} · 文件范围：{contract.action_policy.allowed_files.join('、')}</p>}
          <p>启动会创建新的运行并使用此时已持久化的配置快照；正文和截图按已确认说明发送给所选提供商。</p>
        </div>}
        <div className="workbench-controls"><label><input type="checkbox" data-testid="workbench-confirm" checked={confirmed} disabled={connection !== 'live' || syncing || submitting || !!unknown || workspace.controls.some((op) => op.status === 'PENDING')}
          onChange={(event) => setConfirmed(event.target.checked)} />{run ? '我已核对当前状态和预算，确认提交所选控制请求。' : '我已核对当前契约、来源范围和允许动作。'}</label>
          <div>{(['start','pause','resume','cancel'] as const).map((action) => <button key={action} type="button" data-testid={`workbench-${action}`} className={action === 'cancel' ? 'danger' : ''}
            disabled={!canSubmit || !controlAllowed(workspace, action, taskId) || action === 'start' && !selectedInputs!.readiness.ready} onClick={() => void submit(action)}>{actionLabel(action)}执行</button>)}</div>
          <small>暂停和取消在安全边界生效；取消不会自动撤回已经发生的外部写入。</small>
          {run && run.settings_version === 0 && <small>当前运行缺少持久配置快照，控制不可用；请核对运行来源。</small>}
        </div>
        <div data-testid="workbench-operation" data-status={operation?.status ?? 'NONE'} className="workbench-operation">
          {operation ? <><strong>{actionLabel(operation.action)}：{operation.status === 'PENDING' ? '已受理，处理中' : operation.status === 'APPLIED' ? '已应用' : '未应用'}</strong>
            <p>{operation.status === 'PENDING' ? '等待执行器到达安全边界。以最新 API 状态为准。' : operation.status === 'REJECTED' ? reasonLabel(operation.reason) : '操作处理已持久化。'}<span className="opaque-id"> {operation.operation_id}</span></p></> : '尚无控制请求'}
        </div>
        <div className="workbench-grid"><article data-testid="workbench-subgoal"><h3>子目标与验证进度</h3>
          <p>当前子目标：{workspace.criteria.find((criterion) => criterion.criterion_id === workspace.current_subgoal)?.expected_rule ?? (workspace.current_subgoal === 'aggregate' ? '汇总全部验证' : '等待首次观察')}</p>
          <ul className="workbench-criteria">{workspace.criteria.map((criterion) => <li key={criterion.criterion_id}><span>{criterion.verified ? '✓ 已验证' : '○ 待验证'}</span><p>{criterion.expected_rule}{criterion.critical && <small>关键条件</small>}</p></li>)}</ul>
          <p className="workbench-phase">最近持久化阶段：{phaseLabel(workspace.graph_progress.at(-1)?.phase ?? '')}{workspace.graph_progress.at(-1) && <small>（{workspace.graph_progress.at(-1)!.occurred_at}）</small>}</p></article>
          <article data-testid="workbench-budget"><h3>预算与截止</h3>{workspace.budget?.initialized ? <><p className={workspace.budget.exhausted ? 'budget-exhausted' : ''}>{workspace.budget.exhausted ? `预算已耗尽：${reasonLabel(workspace.budget.reason)}` : '预算可用'}</p>
            <dl><dt>剩余动作</dt><dd>{workspace.budget.remaining_actions}</dd><dt>剩余内容页</dt><dd>{workspace.budget.remaining_content_pages}</dd><dt>剩余执行时间</dt><dd>{duration(workspace.budget.remaining_active_ms)}</dd><dt>剩余检查等待</dt><dd>{duration(workspace.budget.remaining_ci_wait_ms)}</dd><dt>模型调用</dt><dd>{workspace.budget.model_calls_used}</dd><dt>已用动作</dt><dd>{workspace.budget.actions_used}</dd></dl>
            {(workspace.budget.handoff_deadline || run?.handoff_deadline) && <p>人工处理截止：{workspace.budget.handoff_deadline ?? run?.handoff_deadline}</p>}</> : <p>预算尚未初始化；启动后读取实际用量。</p>}</article>
        </div>
        <article data-testid="workbench-evidence" className="workbench-page"><h3>最新页面证据</h3><p>{evidenceStatus}</p>
          {workspace.observation && <><p>采集时页面快照：{workspace.observation.captured_at} · {workspace.observation.valid ? '观察仍有效' : '观察已失效，需重新读取'}</p><p className="opaque-id">观察：{workspace.observation.snapshot_id}</p></>}
          {image && <img src={image} alt="当前页面经校验的脱敏展示副本" />}
          {pageText && <div className="workbench-page-text"><strong>{pageText.title}</strong><pre>{pageText.text}</pre></div>}
        </article>
        <details className="workbench-events"><summary>持久进度事件（最近 {workspace.events.length} 条）</summary>
          <ol>{workspace.events.map((event) => <li key={event.event_id} data-testid="workbench-event" data-event-id={event.event_id}><span>{eventLabel(event.event_type)}</span><time>{event.occurred_at}</time><small>#{event.event_id}</small></li>)}</ol>
          {workspace.has_earlier_events && <p>更早事件仍在服务端保存，此处展示当前运行的最近事件。</p>}
        </details>
        <p className="workbench-asof">最新 API 快照：{workspace.as_of}。实时事件只触发重新读取，运行状态以上述快照为准。</p>
      </>}
    </>}
  </section>
}
