import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from './api'
import ModelSettings from './ModelSettings'
import IdentityReadiness from './IdentityReadiness'
import TaskEntry from './tasks/TaskEntry'
import Workbench from './workbench/Workbench'
import ResultsPage from './results/ResultsPage'

type HealthState =
  | { state: 'checking' }
  | { state: 'ready'; checkedAt: string }
  | { state: 'unavailable'; message: string }

function isLocalHealth(value: unknown): boolean {
  if (typeof value !== 'object' || value === null) return false
  const health = value as Record<string, unknown>
  return health.status === 'ok' && health.service === 'api' && health.stage === 'M1-25'
    && health.task_execution_enabled === true && health.tasks_success_implied === false
}

export default function App() {
  const [health, setHealth] = useState<HealthState>({ state: 'checking' })
  const [refresh, setRefresh] = useState(0)
  const [configurationRevision, setConfigurationRevision] = useState(0)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [executionRevision, setExecutionRevision] = useState(0)
  const executionChanged = useCallback(() => setExecutionRevision((value) => value + 1), [])
  const configurationChanged = useCallback(() => setConfigurationRevision((value) => value + 1), [])
  const openSettings = useCallback(() => document.getElementById('model-configuration')?.scrollIntoView({ behavior: 'smooth' }), [])

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    setHealth({ state: 'checking' })
    async function checkHealth() {
      try {
        const response = await apiFetch('/api/health', { signal: controller.signal, headers: { Accept: 'application/json' } })
        if (!response.ok) throw new Error('unavailable')
        const payload: unknown = await response.json()
        if (!isLocalHealth(payload)) throw new Error('version-mismatch')
        if (!disposed) setHealth({ state: 'ready', checkedAt: new Date().toLocaleTimeString('zh-CN', { hour12: false }) })
      } catch (error) {
        if (!disposed) setHealth({ state: 'unavailable', message: controller.signal.aborted
          ? '连接超时，请确认 API 已启动。'
          : error instanceof Error && error.message === 'version-mismatch'
            ? '服务版本与工作台不匹配，请重新启动 API。'
            : '暂时无法连接 API，请确认 API 已启动。' })
      } finally { window.clearTimeout(timeout) }
    }
    void checkHealth()
    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [refresh])

  const label = health.state === 'ready' ? 'API 已连接' : health.state === 'checking' ? '正在检查 API' : 'API 未就绪'
  return <div className="workspace">
    <header className="topbar">
      <a className="brand" href="/" aria-label="WebPilot 首页"><span className="brand-mark" aria-hidden="true">w.</span><span>WebPilot</span></a>
      <nav aria-label="工作台导航"><a href="#task-entry">任务入口</a><a href="#execution-workbench">执行进度</a><a href="#task-results">结果与证据</a><a href="#model-configuration">模型配置</a><a href="#account-preparation">账号准备</a></nav>
      <span className="local-label"><span aria-hidden="true" />本机工作台</span>
    </header>
    <main>
      <div className="intro"><span className="eyebrow">浏览器任务</span><h1>任务入口</h1><p>描述目标，确认来源和允许动作，建立可核对的任务卡。</p></div>
      <section className={`service-status ${health.state}`} aria-label="服务连接">
        <div className="service-copy" aria-live="polite" aria-atomic="true"><span className="service-dot" aria-hidden="true" /><div><h2>{label}</h2><p>{health.state === 'ready' ? `最近检查 ${health.checkedAt}。连接状态不表示任务完成。` : health.state === 'unavailable' ? health.message : '正在读取本机服务的健康状态。'}</p></div></div>
        <button type="button" onClick={() => setRefresh((value) => value + 1)} disabled={health.state === 'checking'}>{health.state === 'checking' ? '检查中…' : '重新检查连接'}</button>
      </section>
      <div id="task-entry" className="anchor-section"><TaskEntry onOpenSettings={openSettings} configurationRevision={configurationRevision} executionRevision={executionRevision} onTaskSelected={setSelectedTaskId} /></div>
      <Workbench taskId={selectedTaskId} onTaskChanged={executionChanged} />
      <ResultsPage taskId={selectedTaskId} revision={executionRevision} />
      <section id="model-configuration" className="anchor-section" aria-labelledby="configuration-heading">
        <div className="section-intro"><span className="eyebrow">任务准备</span><h2 id="configuration-heading">配置与账号</h2><p>确认提供商数据范围，并为需要登录的站点准备正确账号。</p></div>
        <ModelSettings onSettingsChange={configurationChanged} />
        <div id="account-preparation" className="anchor-section"><IdentityReadiness onIdentityChange={configurationChanged} /></div>
      </section>
    </main>
    <footer><span>WebPilot · 本机工作台</span><span>任务与契约由本机服务持久保存</span></footer>
  </div>
}
