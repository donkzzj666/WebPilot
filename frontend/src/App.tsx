import { useEffect, useState } from 'react'

type HealthState =
  | { state: 'checking' }
  | { state: 'ready'; checkedAt: string; sqliteVersion: string }
  | { state: 'unavailable'; message: string }

function isSkeletonHealth(value: unknown): value is {
  status: 'ok'
  service: 'api'
  stage: 'M1-02'
  task_execution_enabled: false
  sqlite_version: string
} {
  if (typeof value !== 'object' || value === null) return false
  const health = value as Record<string, unknown>
  return (
    health.status === 'ok' &&
    health.service === 'api' &&
    health.stage === 'M1-02' &&
    health.task_execution_enabled === false &&
    typeof health.sqlite_version === 'string'
  )
}

export default function App() {
  const [health, setHealth] = useState<HealthState>({ state: 'checking' })
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    setHealth({ state: 'checking' })

    async function checkHealth() {
      try {
        const response = await fetch('/api/health', {
          signal: controller.signal,
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) throw new Error(`API 返回 HTTP ${response.status}。`)
        const payload: unknown = await response.json()
        if (!isSkeletonHealth(payload)) {
          throw new Error('API 响应与 M1-02 启动契约不一致。')
        }
        if (!disposed) {
          setHealth({
            state: 'ready',
            checkedAt: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
            sqliteVersion: payload.sqlite_version,
          })
        }
      } catch (error) {
        if (!disposed) {
          setHealth({
            state: 'unavailable',
            message: controller.signal.aborted
              ? '连接超时，请确认 API 已启动。'
              : error instanceof SyntaxError
                ? 'API 未返回有效的 JSON 健康信息。'
                : error instanceof Error && error.message.startsWith('API ')
                  ? error.message
                  : '暂时无法连接 API，请确认 API 已启动。',
          })
        }
      } finally {
        window.clearTimeout(timeout)
      }
    }

    void checkHealth()
    return () => {
      disposed = true
      controller.abort()
      window.clearTimeout(timeout)
    }
  }, [refresh])

  const apiLabel =
    health.state === 'ready'
      ? 'API 已连接'
      : health.state === 'checking'
        ? '正在检查 API'
        : 'API 未就绪'

  return (
    <div className="workspace">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Browser Agent 首页">
          <span className="brand-mark" aria-hidden="true">b.</span>
          <span>Browser Agent</span>
        </a>
        <span className="local-label"><span aria-hidden="true" />本机工作台</span>
      </header>

      <main>
        <div className="intro">
          <span className="eyebrow">M1-02 / 执行基础</span>
          <h1>工作台启动检查</h1>
          <p>从可重复启动开始，逐步建立浏览器任务的执行基础。</p>
        </div>

        <section className="startup-grid" aria-label="启动状态">
          <article className="overview-card">
            <div className="status-badge"><span aria-hidden="true" />前端骨架已启动</div>
            <h2>基础就位，<br />准备连接执行服务。</h2>
            <p>当前页面用于核对前端和 API 的启动状态。任务创建、浏览器操作与结果验证将在后续子任务中接入。</p>
            <div className="component-row" aria-label="系统组件">
              <span>React 工作台</span><span className="connector" aria-hidden="true">→</span>
              <span>FastAPI 服务</span><span className="connector" aria-hidden="true">→</span>
              <span>独立 Worker</span>
            </div>
          </article>

          <article className="health-card">
            <div className="card-heading"><h2>服务连接</h2><span>LOCAL</span></div>
            <div className={`health-result ${health.state}`} aria-live="polite" aria-atomic="true">
              <div className="health-icon" aria-hidden="true">{health.state === 'ready' ? '✓' : health.state === 'checking' ? '…' : '!'}</div>
              <h3>{apiLabel}</h3>
              <p>{health.state === 'ready'
                ? `最近检查 ${health.checkedAt} · SQLite ${health.sqliteVersion}`
                : health.state === 'unavailable'
                  ? health.message
                  : '正在读取本机服务的健康状态。'}</p>
            </div>
            <button type="button" onClick={() => setRefresh((value) => value + 1)} disabled={health.state === 'checking'}>
              {health.state === 'checking' ? '检查中…' : '重新检查连接'}
            </button>
            <p className="health-note">健康检查只确认 API 可用，不代表 Worker、外部网站或任务运行成功。</p>
          </article>
        </section>

        <section className="next-card" aria-labelledby="next-heading">
          <div><span className="section-number">NEXT</span><h2 id="next-heading">后续能力尚未实现</h2></div>
          <p>任务创建与查询、持久队列、浏览器操作及执行结果将在各自子任务通过验收后开放。</p>
          <span className="pending-tag">任务执行未开放</span>
        </section>
      </main>

      <footer><span>Browser Agent · 本地开发环境</span><span>M1-02 核心持久化</span></footer>
    </div>
  )
}
