import { useEffect, useRef, useState } from 'react'
import { canVerifySuccess, id, mergeHistoryPage } from './contracts'
import { getResults, readEvidence } from './client'
import { checkExplanation, resultFieldValue } from './fields'
import type { DisplayContent, Evidence, Json, Results, Run, Verdict } from './types'
import './results.css'

const stateNames: Record<string,string> = { SUCCEEDED:'验证成功', PARTIAL:'部分完成', FAILED:'运行失败', CANCELLED:'已取消', QUEUED:'等待执行', RUNNING:'运行中', VERIFYING:'验证中', WAITING_CI:'等待检查', WAITING_SITE:'等待站点', WAITING_HANDOFF:'等待人工处理', PAUSED:'已暂停', RECONCILING:'查证中' }
const verdictNames: Record<Verdict,string> = { PASS:'通过', FAIL:'不通过', INSUFFICIENT:'证据不足', CONFLICT:'存在冲突' }
const effectNames: Record<string,string> = { INTENT:'写入意图已记录，尚未确认', UNKNOWN:'写入结果未知，仍需查证', CONFIRMED:'已确认发生', NOT_APPLIED:'已确认未发生' }
const availabilityNames: Record<string,string> = { AVAILABLE:'可读取', MISSING:'证据缺失', EXPIRED:'证据已过期', CORRUPT:'证据已损坏', UNAVAILABLE:'暂不可核查', BLOCKED:'无可展示的脱敏副本' }
const blockerLabel = (code: string) => ({ result_integrity_unavailable:'持久结果未通过完整性核查。', result_not_available:'此运行尚无可读取的聚合结果。', business_outcome_not_succeeded:'本次运行的聚合结论未达到成功。', coverage_incomplete:'声明范围尚未全部覆盖。', unresolved_items:'仍有未决项需要处理。', criteria_not_passed:'部分验证条件尚未通过。', fields_not_passed:'部分字段尚未通过证据核查。', evidence_not_readable:'所需证据缺失、过期、损坏或无法安全展示。', historical_side_effect_unresolved:'所选运行仍有未确认或违规的外部副作用。', write_not_in_selected_result:'部分写入记录尚未纳入所选运行的结果。', task_writes_pending:'该任务仍有写入结果等待查证。', critical_side_effect:'写入记录存在关键违规。', write_policy_unavailable:'写入权限依据暂不可核查，不能判断是否符合原契约。', write_history_truncated:'写入记录超过展示上限，无法核查全部记录。' }[code] ?? '结果中存在尚未核实的限制，请刷新并检查对应记录。')
const dateLabel = (value: string|null) => value === null ? '尚未记录' : new Date(value).toLocaleString('zh-CN',{hour12:false})
const format = (value: Json): string => value === null ? '未提供' : typeof value === 'string' ? value : typeof value === 'object' ? JSON.stringify(value,null,2) : String(value)
const errorText = (error: unknown) => error instanceof Error ? error.message : '结果读取失败，请刷新重试。'
function updateRunUrl(runId: string|null) { const url = new URL(window.location.href); if (runId) url.searchParams.set('result_run',runId); else url.searchParams.delete('result_run'); window.history.replaceState(null,'',url) }
function VerdictBadge({ value }: { value: Verdict }) { return <span className={`results-verdict ${value.toLowerCase()}`}>{value} · {verdictNames[value]}</span> }
function RunCard({ run, current, selected, choose }: { run: Run; current: string|null; selected: string|null; choose: (id:string)=>void }) {
  return <li data-testid={`result-run-${run.run_id}`} className={selected === run.run_id ? 'selected' : ''}>
    <div className="results-history-line"><strong>{stateNames[run.state] ?? run.state}</strong><span>{run.run_id === current ? '当前运行' : '历史运行'}</span></div>
    <code>{run.run_id}</code>
    <p>契约 v{run.contract_version} · {run.assistance_count > 0 ? `人工参与 ${run.assistance_count} 次` : '无人工参与记录'}</p>
    <p>{run.parent_run_id ? <>重跑来源：<code>{run.parent_run_id}</code></> : '首次执行或独立运行'}</p>
    <time dateTime={run.created_at}>创建于 {dateLabel(run.created_at)}</time>
    <button type="button" aria-label={`查看运行 ${run.run_id}`} aria-pressed={selected === run.run_id} onClick={() => choose(run.run_id)}>{selected === run.run_id ? '正在查看此运行' : '查看此运行'}</button>
  </li>
}
export default function ResultsPage({ taskId, revision = 0 }: { taskId: string|null; revision?: number }) {
  const [selection,setSelection] = useState<{ taskId:string; runId:string|null }|null>(null)
  const [page,setPage] = useState<Results|null>(null), [history,setHistory] = useState<Run[]>([]), [cursor,setCursor] = useState<string|null>(null)
  const [loading,setLoading] = useState(false), [fresh,setFresh] = useState(false), [error,setError] = useState(''), [epoch,setEpoch] = useState(0)
  const [paging,setPaging] = useState(false), [pageError,setPageError] = useState('')
  const [evidenceId,setEvidenceId] = useState<string|null>(null), [content,setContent] = useState<DisplayContent|null>(null), [imageUrl,setImageUrl] = useState<string|null>(null)
  const [evidenceLoading,setEvidenceLoading] = useState(false), [evidenceError,setEvidenceError] = useState(''), [failedEvidence,setFailedEvidence] = useState<string[]>([])
  const previousTask = useRef<string|null>(null), lifetime = useRef(new AbortController()), generation = useRef(0)
  const selectionIsCurrent = selection !== null && selection.taskId === taskId
  useEffect(() => {
    lifetime.current.abort(); lifetime.current = new AbortController(); generation.current++
    setPage(null); setHistory([]); setCursor(null); setError(''); setFresh(false); setEvidenceId(null); setContent(null); setFailedEvidence([])
    if (!taskId) { setSelection(null); return }
    const changed = previousTask.current !== null && previousTask.current !== taskId
    previousTask.current = taskId
    if (changed) updateRunUrl(null)
    const url = new URL(window.location.href), requested = !changed && url.searchParams.get('task') === taskId ? url.searchParams.get('result_run') : null
    setSelection({taskId,runId:requested})
    return () => lifetime.current.abort()
  },[taskId])
  useEffect(() => {
    const pop = () => {
      if (!taskId) return
      const url = new URL(window.location.href)
      if (url.searchParams.get('task') === taskId) { setSelection({taskId,runId:url.searchParams.get('result_run')}); setEpoch((value) => value + 1) }
    }
    window.addEventListener('popstate',pop); return () => window.removeEventListener('popstate',pop)
  },[taskId])
  useEffect(() => {
    if (!taskId || !selectionIsCurrent || !selection) return
    const controller = new AbortController(), signal = AbortSignal.any([controller.signal,lifetime.current.signal]), stamp = ++generation.current
    setLoading(true); setFresh(false); setError(''); setPageError(''); setEvidenceId(null); setContent(null); setFailedEvidence([]); setEvidenceError(''); setPaging(false)
    void getResults(taskId,selection.runId,signal).then((value) => {
      if (signal.aborted || stamp !== generation.current) return
      setPage(value); setHistory(value.runs); setCursor(value.next_cursor); setFresh(true)
    }).catch((reason) => { if (!signal.aborted && stamp === generation.current) { setPage(null); setHistory([]); setCursor(null); setError(errorText(reason)) } })
      .finally(() => { if (!signal.aborted && stamp === generation.current) setLoading(false) })
    return () => controller.abort()
  },[taskId,selection,selectionIsCurrent,revision,epoch])
  const selected = page?.task_id === taskId && selectionIsCurrent && (selection?.runId === null || selection?.runId === page.selected_run?.run_id) ? page : null
  const run = selected?.selected_run, result = selected?.result
  const selectedEvidence = selected?.evidence.find((item) => item.evidence_id === evidenceId) ?? null
  useEffect(() => {
    const controller = new AbortController(), signal = AbortSignal.any([controller.signal,lifetime.current.signal]); let localUrl: string|null = null
    setContent(null); setImageUrl(null); setEvidenceError(''); setEvidenceLoading(false)
    if (!selectedEvidence || !run || !fresh) return () => controller.abort()
    setEvidenceLoading(true)
    void readEvidence(selectedEvidence,run.run_id,signal).then((value) => {
      if (signal.aborted) return
      setContent(value)
      if (value.kind === 'screenshot') { localUrl = URL.createObjectURL(value.blob); setImageUrl(localUrl) }
    }).catch((reason) => {
      if (!signal.aborted) { setEvidenceError(errorText(reason)); setFailedEvidence((values) => values.includes(selectedEvidence.evidence_id) ? values : [...values,selectedEvidence.evidence_id]) }
    }).finally(() => { if (!signal.aborted) setEvidenceLoading(false) })
    return () => { controller.abort(); if (localUrl) URL.revokeObjectURL(localUrl) }
  },[selectedEvidence,run,fresh])
  function chooseRun(runId: string) {
    if (!taskId || !id(runId)) return
    setFresh(false); setEvidenceId(null); setFailedEvidence([]); updateRunUrl(runId); setSelection({taskId,runId})
  }
  async function moreHistory() {
    if (!taskId || !selected || !cursor || paging || !fresh) return
    const stamp = generation.current, signal = lifetime.current.signal, expectedRun = selected.selected_run?.run_id ?? null
    setPaging(true); setPageError(''); setFresh(false); setEvidenceId(null)
    try {
      const more = await getResults(taskId,expectedRun,signal,cursor)
      if (signal.aborted || stamp !== generation.current) return
      const merged = mergeHistoryPage(selected,more,cursor,history)
      setPage(merged.page); setHistory(merged.runs); setCursor(more.next_cursor); setFresh(true)
    } catch (reason) { if (!signal.aborted && stamp === generation.current) setPageError(errorText(reason)) }
    finally { if (!signal.aborted && stamp === generation.current) setPaging(false) }
  }
  const complete = selected && fresh && selected.display_complete_success && canVerifySuccess(selected) && failedEvidence.length === 0
  function evidenceButtons(refs: string[]) {
    return refs.length ? <div className="results-evidence-links">{refs.map((ref) => {
      const item = selected?.evidence.find((entry) => entry.evidence_id === ref)
      return <button type="button" key={ref} aria-label={`查看证据 ${ref}`} disabled={!fresh || !item?.displayable} onClick={() => setEvidenceId(ref)}>{ref}{!item?.displayable ? ' · 不可读取' : ''}</button>
    })}</div> : <span className="results-muted">未记录对应证据</span>
  }
  function evidenceCard(item: Evidence) {
    return <li key={item.evidence_id} data-testid={`result-evidence-${item.evidence_id}`}>
      <div className="results-history-line"><strong>{item.artifact_kind ?? '工件'}</strong><span className={!item.displayable ? 'results-warning' : ''}>{availabilityNames[item.display_status] ?? '暂不可展示'}</span></div>
      <code>{item.evidence_id}</code>
      {item.source_url && <p>来源：<span>{item.source_url}</span></p>}
      {item.locator_or_page && <p>定位：{item.locator_or_page}</p>}
      <p>采集于 {dateLabel(item.captured_at)}</p>
      {item.sha256 && <details><summary>校验摘要</summary><code>SHA-256 {item.sha256}</code></details>}
      {item.display_evidence_id !== item.evidence_id && item.display_evidence_id && <p>脱敏副本：<code>{item.display_evidence_id}</code></p>}
      {evidenceButtons([item.evidence_id])}
    </li>
  }
  return <section id="task-results" className="results-page" data-testid="results-page" data-complete-success={!!complete} aria-labelledby="results-heading">
    <div className="results-heading"><div><span className="results-kicker">RUN RESULTS</span><h2 id="results-heading">结果与证据</h2></div><button type="button" disabled={!taskId || loading} onClick={() => { setFresh(false); setEpoch((value) => value + 1) }}>刷新结果</button></div>
    <p className="results-intro">逐项核对结果、验证条件和证据，并保留每次运行的结论。</p>
    {!taskId && <p className="results-empty">请先在任务入口选择任务。</p>}
    {taskId && loading && <p role="status" className="results-empty">正在读取所选运行的持久结果…</p>}
    {error && <p role="alert" className="results-warning-box" data-testid="results-error">{error}</p>}
    {selected && !run && !loading && <p className="results-empty">此任务尚无运行结果。可在运行工作台确认后开始执行。</p>}
    {selected && run && <>
      <div className="results-run-heading"><p data-testid="results-selected-run">所选运行 <code>{run.run_id}</code></p><span>{run.run_id === selected.current_run_id ? '当前运行' : '历史运行'}</span></div>
      <div className={`results-outcome ${complete ? 'complete' : ''}`} data-testid="results-outcome" aria-live="polite">
        <div><span>当前可核查性</span><strong>{!fresh ? '正在重新核查' : complete ? '完整成功 · 当前证据可核查' : selected.result_status === 'NOT_READY' ? '尚未形成聚合结果' : selected.result_status === 'UNAVAILABLE' ? '持久结果不可读取' : result?.outcome === 'SUCCEEDED' ? '证据或未决项阻止完整成功' : '本次运行未完整成功'}</strong></div>
        <div><span>持久运行状态 · {run.state}</span><strong>{stateNames[run.state] ?? run.state}</strong><small>{result ? `历史聚合结论：${result.outcome}` : '尚无可读取的聚合结论'}</small></div>
        <div data-testid="results-assistance"><span>人工参与</span><strong>{run.assistance_count > 0 ? `有人工参与 · ${run.assistance_count} 次` : '无人工参与记录'}</strong><small>{selected.assistance === 'assisted' ? 'assisted' : 'autonomous'} · 契约 v{run.contract_version}</small></div>
      </div>
      {(run.state === 'FAILED' || run.state === 'CANCELLED' || selected.write_intents.length > 0 || (result?.side_effects.length ?? 0) > 0) && <p className="results-warning-box">失败或取消不代表已发生的外部写入已回滚。请以副作用记录和独立查证结果为准。</p>}
      {(selected.display_blockers.length > 0 || failedEvidence.length > 0) && <div className="results-warning-box" data-testid="results-blockers"><strong>当前核查受阻</strong><ul>{selected.display_blockers.map((item,index) => <li key={index}>{blockerLabel(item)}</li>)}{failedEvidence.map((item) => <li key={item}>证据 {item} 在本页读取失败；刷新后重新核查。</li>)}</ul></div>}
      {result && <>
        <article className="results-section" data-testid="results-fields"><h3>字段结果与对应证据</h3><p className="results-muted">字段结论来自此运行保存的验证记录。打开对应工件可核查原文与定位。</p>
          {selected.field_checks.length ? <ol className="results-fields">{selected.field_checks.map((field) => {
            const resolved = resultFieldValue(result,field.result_path)
            return <li key={field.result_path} data-result-path={field.result_path}><div className="results-field-heading"><code>{field.result_path || '/'}</code><VerdictBadge value={field.verdict}/></div>
              {resolved.found ? <pre data-testid="result-field-value">{resolved.value === null ? '空值（null）' : format(resolved.value)}</pre> : <p className="results-field-missing" data-testid="result-field-value">结果中未保存此字段的值。</p>}
              <p className="results-muted">{checkExplanation(field.actual)}</p>{evidenceButtons(field.evidence_ids)}
              <details className="results-field-details"><summary>核查详情</summary><pre>{format(field.actual)}</pre></details>
            </li>
          })}</ol> : <p className="results-empty">此运行未保存字段级检查。</p>}
          <details className="results-raw"><summary>查看完整结构化结果</summary><pre>{JSON.stringify(result.items,null,2)}</pre></details>
        </article>
        <article className="results-section" data-testid="results-checks"><h3>验证条件</h3><div className="results-checks">{result.checks.map((check) => <div key={check.criterion_id}><div className="results-field-heading"><strong>{check.criterion_id}</strong><VerdictBadge value={check.verdict}/></div><p>{check.expected_rule}</p><pre>{format(check.actual)}</pre>{evidenceButtons(check.evidence_ids)}<small>检查于 {dateLabel(check.checked_at)} · {check.checker_version}</small></div>)}</div>{!result.checks.length && <p className="results-empty">没有验证条件记录。</p>}</article>
        <article className="results-section" data-testid="results-unresolved"><h3>覆盖范围与未决项</h3><p className={result.coverage.complete ? '' : 'results-warning'}>{result.coverage.complete ? '已完成声明范围的覆盖核查' : '覆盖尚不完整'} · 已读取 {result.coverage.content_pages} 页</p>
          <dl><dt>已检索来源</dt><dd>{result.coverage.searched_sources.join('、') || '未记录'}</dd><dt>检索条件</dt><dd>{result.coverage.queries.join('；') || '未记录'}</dd><dt>截止时间</dt><dd>{dateLabel(result.coverage.cutoff_at)}</dd></dl>
          <div className="results-coverage-grid">{[['未覆盖范围',result.coverage.gaps],['未读候选',result.coverage.unread_candidates],['未决项',result.unresolved]].map(([title,items]) => <div key={title as string}><h4>{title as string}</h4>{(items as string[]).length ? <ul>{(items as string[]).map((item,index) => <li key={index}>{item}</li>)}</ul> : <p className="results-muted">未记录</p>}</div>)}</div>
        </article>
      </>}
      <article className="results-section" data-testid="results-side-effects"><h3>副作用与写入查证</h3><p className="results-muted">UNKNOWN 表示结果未知；INTENT 仅代表写入意图已记录，两者都不能据此判断已发生或未发生。</p>
        {result?.side_effects.map((effect) => <div className="results-effect" key={effect.operation_id}><div className="results-field-heading"><strong>{effect.effect_type} · {effectNames[effect.status]}</strong><code>{effect.status}</code></div><code>{effect.operation_id}</code><p>目标：{effect.target}</p>{effect.critical_violation && <p className="results-warning">此操作存在关键违规。</p>}{effect.receipt && <p>回执：{effect.receipt}</p>}{evidenceButtons(effect.evidence_ids)}</div>)}
        {selected.write_intents.map((write) => <div key={write.operation_id} className="results-effect"><strong>{effectNames[write.status]} <code>{write.status}</code></strong><p>操作 <code>{write.operation_id}</code></p><p>发起运行：<code>{write.originating_run_id}</code></p><p>{write.receipt_available ? '已保存回执' : '尚无可用回执'} · {write.recorded_in_selected_result ? '已纳入所选运行的聚合结果' : '尚未纳入所选运行的聚合结果'}</p>{write.critical_violation && <p className="results-warning">存在关键违规。</p>}</div>)}
        {!result?.side_effects.length && !selected.write_intents.length && <p className="results-empty">没有已记录的副作用或写入意图。</p>}
        {selected.pending_write_count > 0 && <p className="results-warning">仍有 {selected.pending_write_count} 项写入待查证。</p>}{selected.write_intents_truncated && <p className="results-warning">写入记录超过展示上限，当前列表不完整。</p>}
      </article>
      <article className="results-section" data-testid="results-evidence"><h3>证据工件</h3><p className="results-muted">仅展示经校验的脱敏文本、差异或中性图片副本。来源与定位以文字保留。</p><ul className="results-evidence-list">{selected.evidence.map(evidenceCard)}</ul>{!selected.evidence.length && <p className="results-empty">此运行尚无可展示的结果证据。</p>}
        {selectedEvidence && <div className="results-reader" data-testid="result-evidence-content"><div className="results-field-heading"><strong>证据展示副本</strong><button type="button" onClick={() => setEvidenceId(null)}>关闭证据</button></div><code>{selectedEvidence.evidence_id}</code>
          {evidenceLoading && <p role="status">正在校验并读取受控证据…</p>}{evidenceError && <p role="alert" className="results-warning-box">{evidenceError} 此工件不可作为本页完整成功的依据。</p>}
          {content?.kind === 'screenshot' && imageUrl && <><img src={imageUrl} alt="经完整性校验的中性证据副本" onError={() => { setContent(null); setEvidenceError('图片展示失败。'); setFailedEvidence((items) => [...new Set([...items,selectedEvidence.evidence_id])]) }}/><p className="results-muted">中性副本隐藏页面像素；可结合文字证据核查内容。</p></>}
          {content && content.kind !== 'screenshot' && <pre className={content.kind === 'diff' ? 'results-diff' : ''}>{content.text}</pre>}
        </div>}
      </article>
      <p className="results-asof">核查时间：{dateLabel(selected.as_of)} · 开始：{dateLabel(run.started_at)} · 结束：{dateLabel(run.ended_at)}。页面按此时的持久数据展示，刷新可重新核查证据可用性。</p>
    </>}
    {selected && history.length > 0 && <article className="results-section" data-testid="results-history"><h3>运行历史</h3><p className="results-muted">每次运行独立保留。后续成功不会覆盖旧运行的失败、取消或人工参与记录。</p><ul className="results-history">{history.map((item) => <RunCard key={item.run_id} run={item} current={selected.current_run_id} selected={run?.run_id ?? null} choose={chooseRun}/>)}</ul>{cursor && <button type="button" disabled={paging || !fresh} onClick={() => void moreHistory()}>{paging ? '正在加载历史…' : '加载更早运行'}</button>}{pageError && <p role="alert" className="results-warning-box">{pageError}</p>}</article>}
  </section>
}
