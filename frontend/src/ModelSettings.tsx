import { apiFetch } from './api'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import './model-settings.css'

type ModelConfig = {
  provider: 'deepseek'
  model_id: string
  base_url: string
  prompt_version: string
  max_tokens: number
  connect_seconds: number
  read_seconds: number
  total_seconds: number
  price_version: string | null
  pricing?: TokenPricing
}

type TokenPricing = {
  basis: 'reported_input_output_tokens'
  currency: 'USD' | 'CNY'
  input_per_million: string
  output_per_million: string
  cache_hit_input_per_million: string | null
}

type CredentialStatus =
  | 'available' | 'missing' | 'locked' | 'access_denied' | 'unavailable'
  | 'unsupported' | 'invalid' | 'not_configured'

export type SettingsResponse = {
  version: number
  model: ModelConfig | null
  model_config_sha256: string | null
  runtime_config_sha256: string | null
  readiness: {
    ready: boolean
    reasons: { code: string; message: string }[]
    credential_status: CredentialStatus
    provider_verified: false
  }
  disclosure: {
    version: 'model-data-v1'
    provider: 'DeepSeek'
    items: string[]
    message: string
    accepted: boolean
  }
  task_execution_enabled: boolean
}

type Notice = { kind: 'error' | 'success' | 'conflict'; message: string }

const DEFAULT_MODEL: ModelConfig = {
  provider: 'deepseek',
  model_id: 'deepseek-flash',
  base_url: 'https://api.deepseek.com',
  prompt_version: 'm1-05-model-v1',
  max_tokens: 1024,
  connect_seconds: 10,
  read_seconds: 30,
  total_seconds: 60,
  price_version: null,
}

// These choices mirror the public ModelConnection contract. Additional model
// names or arbitrary service addresses cannot become credential destinations.
const SUPPORTED_MODELS = ['deepseek-flash'] as const
const SUPPORTED_URLS = ['https://api.deepseek.com', 'https://api.deepseek.com/v1'] as const
const RATE = /^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,12})?$/

function isPricing(value: unknown): value is TokenPricing {
  if (!isRecord(value)) return false
  return value.basis === 'reported_input_output_tokens'
    && (value.currency === 'USD' || value.currency === 'CNY')
    && typeof value.input_per_million === 'string' && RATE.test(value.input_per_million)
    && typeof value.output_per_million === 'string' && RATE.test(value.output_per_million)
    && (value.cache_hit_input_per_million === null
      || (typeof value.cache_hit_input_per_million === 'string' && RATE.test(value.cache_hit_input_per_million)))
}

const CREDENTIAL_LABELS: Record<CredentialStatus, string> = {
  available: '本机凭据可读取',
  missing: '尚未保存凭据',
  locked: '系统凭据存储已锁定',
  access_denied: '系统凭据访问被拒绝',
  unavailable: '系统凭据存储暂不可用',
  unsupported: '当前系统不支持凭据存储',
  invalid: '已保存凭据的格式无效',
  not_configured: '尚未配置凭据',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isModel(value: unknown): value is ModelConfig {
  if (!isRecord(value)) return false
  return value.provider === 'deepseek'
    && value.model_id === 'deepseek-flash'
    && SUPPORTED_URLS.some((url) => value.base_url === url)
    && value.prompt_version === 'm1-05-model-v1'
    && ['max_tokens', 'connect_seconds', 'read_seconds', 'total_seconds']
      .every((key) => typeof value[key] === 'number' && Number.isFinite(value[key]))
    && (value.price_version === null || typeof value.price_version === 'string')
    && (value.pricing === undefined || isPricing(value.pricing))
}

function isSettings(value: unknown): value is SettingsResponse {
  if (!isRecord(value) || !isRecord(value.readiness) || !isRecord(value.disclosure)) return false
  const readiness = value.readiness
  const disclosure = value.disclosure
  return typeof value.version === 'number' && Number.isSafeInteger(value.version) && value.version >= 0
    && (value.model === null || isModel(value.model))
    && (value.model_config_sha256 === null || typeof value.model_config_sha256 === 'string')
    && (value.runtime_config_sha256 === null || typeof value.runtime_config_sha256 === 'string')
    && typeof readiness.ready === 'boolean'
    && readiness.provider_verified === false
    && typeof readiness.credential_status === 'string'
    && Object.hasOwn(CREDENTIAL_LABELS, readiness.credential_status)
    && Array.isArray(readiness.reasons)
    && readiness.reasons.every((reason: unknown) => isRecord(reason)
      && typeof reason.code === 'string' && typeof reason.message === 'string')
    && disclosure.version === 'model-data-v1' && disclosure.provider === 'DeepSeek'
    && Array.isArray(disclosure.items) && disclosure.items.every((item: unknown) => typeof item === 'string')
    && typeof disclosure.message === 'string' && typeof disclosure.accepted === 'boolean'
    && typeof value.task_execution_enabled === 'boolean'
}

function errorMessage(payload: unknown, fallback: string): string {
  return isRecord(payload) && typeof payload.message === 'string' ? payload.message : fallback
}

export type ModelSettingsProps = { onSettingsChange?: () => void }

export default function ModelSettings({ onSettingsChange }: ModelSettingsProps = {}) {
  const [settings, setSettings] = useState<SettingsResponse | null>(null)
  const [limits, setLimits] = useState({ maxTokens: '1024', connect: '10', read: '30', total: '60' })
  const [connection, setConnection] = useState({ modelId: DEFAULT_MODEL.model_id, baseUrl: DEFAULT_MODEL.base_url })
  const [pricingEnabled, setPricingEnabled] = useState(false)
  const [prices, setPrices] = useState({ currency: 'USD' as 'USD' | 'CNY', input: '', output: '', cache: '' })
  const [apiKey, setApiKey] = useState('')
  const [accepted, setAccepted] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [reload, setReload] = useState(0)
  const saveController = useRef<AbortController | null>(null)
  const settingsChanged = useRef(onSettingsChange)
  settingsChanged.current = onSettingsChange
  const busy = loading || saving

  function applySettings(value: SettingsResponse) {
    const model = value.model ?? DEFAULT_MODEL
    setSettings(value)
    setConnection({ modelId: model.model_id, baseUrl: model.base_url })
    setPricingEnabled(model.pricing !== undefined)
    setPrices({
      currency: model.pricing?.currency ?? 'USD', input: model.pricing?.input_per_million ?? '',
      output: model.pricing?.output_per_million ?? '', cache: model.pricing?.cache_hit_input_per_million ?? '',
    })
    setLimits({
      maxTokens: String(model.max_tokens), connect: String(model.connect_seconds),
      read: String(model.read_seconds), total: String(model.total_seconds),
    })
    settingsChanged.current?.()
  }

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    setLoading(true)
    setNotice(null)
    setApiKey('')
    setAccepted(false)

    async function loadSettings() {
      try {
        const response = await apiFetch('/api/v1/settings', {
          signal: controller.signal, cache: 'no-store', headers: { Accept: 'application/json' },
        })
        if (!response.ok) throw new Error('load-failed')
        const payload: unknown = await response.json()
        if (!isSettings(payload)) throw new Error('invalid-settings')
        if (!disposed) applySettings(payload)
      } catch {
        if (!disposed) {
          setSettings(null)
          setNotice({ kind: 'error', message: '暂时无法读取模型配置，请确认 API 已启动后重新载入。' })
        }
      } finally {
        window.clearTimeout(timeout)
        if (!disposed) setLoading(false)
      }
    }

    void loadSettings()
    return () => {
      disposed = true
      controller.abort()
      window.clearTimeout(timeout)
    }
  }, [reload])

  useEffect(() => () => { saveController.current?.abort() }, [])

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy || !settings) return
    setNotice(null)
    const values = {
      max_tokens: Number(limits.maxTokens), connect_seconds: Number(limits.connect),
      read_seconds: Number(limits.read), total_seconds: Number(limits.total),
    }
    if (!accepted) {
      setApiKey('')
      setNotice({ kind: 'error', message: '请先阅读并确认发送给模型供应商的数据范围。' })
      return
    }
    if (!Number.isInteger(values.max_tokens) || values.max_tokens < 1 || values.max_tokens > 32768
      || [values.connect_seconds, values.read_seconds, values.total_seconds]
        .some((value) => !Number.isFinite(value) || value <= 0 || value > 600)
      || Math.max(values.connect_seconds, values.read_seconds) > values.total_seconds) {
      setApiKey('')
      setNotice({ kind: 'error', message: '请检查参数：输出上限为 1–32768，超时为 0–600 秒且大于 0；总超时不得小于连接或读取超时。' })
      return
    }
    if (!SUPPORTED_MODELS.some((model) => model === connection.modelId)
      || !SUPPORTED_URLS.some((url) => url === connection.baseUrl)) {
      setApiKey('')
      setNotice({ kind: 'error', message: '请选择当前受支持的模型和供应商服务地址。' })
      return
    }
    if (pricingEnabled && (!RATE.test(prices.input) || !RATE.test(prices.output)
      || (prices.cache !== '' && !RATE.test(prices.cache)))) {
      setApiKey('')
      setNotice({ kind: 'error', message: '单价应为非负十进制数，最多 9 位整数和 12 位小数；不支持科学计数法。' })
      return
    }

    const controller = new AbortController()
    saveController.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 30000)
    setSaving(true)
    try {
      const body = {
        expected_version: settings.version,
        model: {
          provider: 'deepseek', model_id: connection.modelId, base_url: connection.baseUrl,
          prompt_version: DEFAULT_MODEL.prompt_version, ...values, price_version: null,
          ...(pricingEnabled ? { pricing: {
            basis: 'reported_input_output_tokens', currency: prices.currency,
            input_per_million: prices.input, output_per_million: prices.output,
            cache_hit_input_per_million: prices.cache === '' ? null : prices.cache,
          } } : {}),
        },
        accept_data_sharing: true,
        ...(apiKey ? { api_key: apiKey } : {}),
      }
      // Retain credentials only in this in-flight request, never in storage or
      // visible form state while waiting for the operating-system operation.
      const requestBody = JSON.stringify(body)
      setApiKey('')
      const response = await apiFetch('/api/v1/settings/model', {
        method: 'PUT', signal: controller.signal, cache: 'no-store',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: requestBody,
      })
      if (response.status === 409) {
        setNotice({ kind: 'conflict', message: '配置已被其他页面更新，本次没有覆盖。请重新载入最新配置，再确认需要保存的内容。' })
        return
      }
      const payload: unknown = await response.json()
      if (!response.ok) {
        setNotice({ kind: 'error', message: errorMessage(payload, `保存失败（HTTP ${response.status}），请检查配置后重试。`) })
        return
      }
      if (!isSettings(payload)) throw new Error('invalid-settings')
      applySettings(payload)
      setNotice({ kind: 'success', message: `已保存配置版本 ${payload.version}。供应商连接及凭据有效性尚未验证。` })
    } catch {
      if (saveController.current === controller) {
        setNotice({ kind: 'error', message: '保存结果暂时无法确认。请重新载入配置，核对版本后再操作。' })
      }
    } finally {
      window.clearTimeout(timeout)
      setApiKey('')
      setSaving(false)
      saveController.current = null
    }
  }

  return (
    <section className="model-settings" aria-labelledby="model-settings-heading">
      <div className="model-settings-heading">
        <div><span className="section-number">MODEL</span><h2 id="model-settings-heading">模型设置</h2></div>
        <span className="model-version">{loading ? '正在读取…' : settings ? `配置版本 ${settings.version}` : '配置未读取'}</span>
      </div>
      <p className="model-intro">配置模型与系统凭据存储，供任务运行使用。保存设置不会调用模型。</p>

      {settings && (
        <div className={`model-readiness ${settings.readiness.ready ? 'ready' : 'needs-input'}`} aria-live="polite">
          <strong>{settings.readiness.ready ? '本机模型配置就绪' : '本机模型配置尚未就绪'}</strong>
          <span>{CREDENTIAL_LABELS[settings.readiness.credential_status]} · 供应商尚未验证</span>
          {settings.readiness.reasons.length > 0 && (
            <ul>{settings.readiness.reasons.map((reason, index) => <li key={`${reason.code}-${index}`}>{reason.message}</li>)}</ul>
          )}
          <p>配置就绪表示本机设置可用；实际调用结果由运行记录确认。</p>
        </div>
      )}

      <form onSubmit={(event) => { void saveSettings(event) }} autoComplete="off">
        <fieldset disabled={busy || settings === null}>
          <legend className="model-visually-hidden">模型连接与凭据</legend>
          <div className="model-connection">
            <label htmlFor="model-provider">供应商
              <select id="model-provider" name="provider" value="deepseek" onChange={() => {}}><option value="deepseek">DeepSeek</option></select>
            </label>
            <label htmlFor="model-id">模型
              <select id="model-id" name="model_id" value={connection.modelId}
                onChange={(event) => setConnection({ ...connection, modelId: event.target.value })}>
                {SUPPORTED_MODELS.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            <label htmlFor="model-base-url">服务地址
              <select id="model-base-url" name="base_url" value={connection.baseUrl}
                onChange={(event) => setConnection({ ...connection, baseUrl: event.target.value })}>
                {SUPPORTED_URLS.map((url) => <option key={url} value={url}>{url}</option>)}
              </select>
            </label>
          </div>
          <p className="model-field-note">当前支持 DeepSeek 的 deepseek-flash，服务地址限上述官方地址。提示模板由服务管理，保存后会生成新配置版本；已有 Run 的配置快照保持不变。</p>

          <div className="model-limits">
            <label htmlFor="model-max-tokens">最大输出 token 数
              <input id="model-max-tokens" name="max_tokens" type="number" min="1" max="32768" step="1" required
                value={limits.maxTokens} onChange={(event) => setLimits({ ...limits, maxTokens: event.target.value })} />
            </label>
            <label htmlFor="model-connect-seconds">连接超时（秒）
              <input id="model-connect-seconds" name="connect_seconds" type="number" min="0" max="600" step="any" required
                value={limits.connect} onChange={(event) => setLimits({ ...limits, connect: event.target.value })} />
            </label>
            <label htmlFor="model-read-seconds">读取超时（秒）
              <input id="model-read-seconds" name="read_seconds" type="number" min="0" max="600" step="any" required
                value={limits.read} onChange={(event) => setLimits({ ...limits, read: event.target.value })} />
            </label>
            <label htmlFor="model-total-seconds">总超时（秒）
              <input id="model-total-seconds" name="total_seconds" type="number" min="0" max="600" step="any" required
                value={limits.total} onChange={(event) => setLimits({ ...limits, total: event.target.value })} />
            </label>
          </div>
          <p className="model-field-note">总超时不得小于连接或读取超时，所有超时均不超过 600 秒。</p>

          <div className="model-pricing">
            <h3>费用估算</h3>
            <p className="model-field-note">{settings?.model?.pricing
              ? `已保存 ${settings.model.pricing.currency} 静态 token 单价，价格版本随内容生成。`
              : '尚未配置单价，费用保持未知。不会把未知费用当作零。'}</p>
            <label className="model-consent" htmlFor="model-pricing-enabled">
              <input id="model-pricing-enabled" type="checkbox" checked={pricingEnabled}
                onChange={(event) => setPricingEnabled(event.target.checked)} />
              <span>为后续 Run 配置静态 token 单价</span>
            </label>
            {pricingEnabled && <div className="model-price-fields">
              <label htmlFor="model-price-currency">币种
                <select id="model-price-currency" value={prices.currency}
                  onChange={(event) => setPrices({ ...prices, currency: event.target.value as 'USD' | 'CNY' })}>
                  <option value="USD">USD</option><option value="CNY">CNY</option>
                </select>
              </label>
              <label htmlFor="model-price-input">输入 / 百万 token
                <input id="model-price-input" type="text" inputMode="decimal" maxLength={22} required
                  value={prices.input} onChange={(event) => setPrices({ ...prices, input: event.target.value })} />
              </label>
              <label htmlFor="model-price-output">输出 / 百万 token
                <input id="model-price-output" type="text" inputMode="decimal" maxLength={22} required
                  value={prices.output} onChange={(event) => setPrices({ ...prices, output: event.target.value })} />
              </label>
              <label htmlFor="model-price-cache">缓存命中输入 / 百万 token（可选）
                <input id="model-price-cache" type="text" inputMode="decimal" maxLength={22} placeholder="留空不使用缓存折扣"
                  value={prices.cache} onChange={(event) => setPrices({ ...prices, cache: event.target.value })} />
              </label>
            </div>}
            <p className="model-field-note">请根据自己的供应商计费约定填写。此处不会下载或验证实时价格，仅按报告的完整输入／输出 token 估算；缺少用量、价格或所需缓存命中信息时保持未知，截图及其他收费项不会猜测计价。取消勾选后保存表示新版本不估算费用。</p>
            {settings?.model?.price_version && <p className="model-price-version">已保存价格版本：<code>{settings.model.price_version}</code></p>}
          </div>

          <div className="model-disclosure" id="model-data-disclosure">
            <h3>发送给模型供应商的数据</h3>
            <p>所选供应商 DeepSeek 可能接收任务正文、过滤后的网页内容以及选中的截图。请确认这些数据适合发送给该供应商。</p>
            {settings && <><ul>{settings.disclosure.items.map((item, index) => <li key={index}>{item}</li>)}</ul><p>{settings.disclosure.message}</p></>}
            <label className="model-consent" htmlFor="model-data-consent">
              <input id="model-data-consent" name="accept_data_sharing" type="checkbox" required checked={accepted}
                onChange={(event) => setAccepted(event.target.checked)} />
              <span>我已阅读并同意上述数据发送范围。</span>
            </label>
          </div>

          <label className="model-key-label" htmlFor="model-api-key">API Key（可选）
            <input id="model-api-key" name="api_key" type="password" autoComplete="new-password" spellCheck={false}
              aria-describedby="model-key-note model-data-disclosure" placeholder="留空以保留已保存的凭据"
              value={apiKey} onChange={(event) => setApiKey(event.target.value)} />
          </label>
          <p className="model-field-note" id="model-key-note">凭据由系统凭据存储保管，不会回填到页面。首次配置可先留空保存；本次输入会在保存尝试后清空。</p>

          <button className="model-save" type="submit" disabled={!accepted || notice?.kind === 'conflict'}>
            {saving ? '正在保存…' : '保存模型设置'}
          </button>
        </fieldset>
      </form>

      {settings && settings.version > 0 && <details className="model-snapshot">
        <summary>已保存配置的版本与摘要</summary>
        <dl><div><dt>模型配置 SHA-256</dt><dd>{settings.model_config_sha256}</dd></div>
          <div><dt>运行配置 SHA-256</dt><dd>{settings.runtime_config_sha256}</dd></div></dl>
        <p>这些摘要标识已保存快照；任务运行使用后端冻结的配置。正在编辑的输入尚未保存。</p>
      </details>}

      {notice && <p className={`model-notice ${notice.kind}`} role={notice.kind === 'success' ? 'status' : 'alert'}>{notice.message}</p>}
      <div className="model-reload-row">
        <button type="button" className="model-reload" disabled={busy} onClick={() => setReload((value) => value + 1)}>
          {notice?.kind === 'conflict' ? '重新载入最新配置' : '重新载入配置'}
        </button>
        <span>重新载入会丢弃未保存的输入。</span>
      </div>
    </section>
  )
}
