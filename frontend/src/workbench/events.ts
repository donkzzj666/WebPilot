/** Bounded SSE decoding and decimal event cursors. No event is execution authority. */
export type BusinessEvent = { event_id: string; task_id: string; run_id: string; event_type: string;
  state_version: number; occurred_at: string; payload: Record<string, unknown> }
export type StreamFrame = { id: string; type: string; data: string }
export function decimal(value: unknown): value is string {
  return typeof value === 'string' && /^(0|[1-9]\d{0,18})$/.test(value) && BigInt(value) <= 9223372036854775807n
}
export const compareIds = (left: string, right: string) => BigInt(left) < BigInt(right) ? -1 : BigInt(left) > BigInt(right) ? 1 : 0
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/.test(value)
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const states = ['QUEUED','RUNNING','VERIFYING','WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED','RECONCILING','SUCCEEDED','PARTIAL','FAILED','CANCELLED']
const actions = ['start','retry','pause','resume','cancel']
export function parseEvent(value: unknown, taskId: string, runId: string): BusinessEvent {
  if (!record(value) || !decimal(value.event_id) || value.event_id === '0' || value.task_id !== taskId || value.run_id !== runId
    || !integer(value.state_version) || typeof value.occurred_at !== 'string' || !Number.isFinite(Date.parse(value.occurred_at))
    || !record(value.payload) || typeof value.event_type !== 'string' || value.payload.event_type !== value.event_type) throw new Error('事件格式或目标不一致。')
  const p = value.payload
  const valid = value.event_type === 'state_changed' ? (states.includes(p.current_state as string) && (p.previous_state === null || states.includes(p.previous_state as string)) && (p.blocked_reason === null || typeof p.blocked_reason === 'string'))
    : value.event_type === 'action_recorded' ? (id(p.step_id) && ['navigate','click','input','keypress','select','scroll','switch_tab','read_visible','screenshot','download_attachment'].includes(p.action_type as string)
      && ['INTENT','COMPLETED','FAILED','UNKNOWN'].includes(p.attempt_status as string) && Array.isArray(p.evidence_ids) && p.evidence_ids.every(id))
    : value.event_type === 'wait_registered' ? (id(p.wait_id) && ['ci','site','handoff','pause'].includes(p.reason as string) && (p.deadline === null || typeof p.deadline === 'string' && Number.isFinite(Date.parse(p.deadline))))
    : value.event_type === 'result_ready' ? (id(p.result_ref) && ['SUCCEEDED','PARTIAL','FAILED','CANCELLED'].includes(p.outcome as string))
    : value.event_type === 'operation_requested' ? (id(p.operation_id) && actions.includes(p.action as string))
    : value.event_type === 'operation_completed' ? (id(p.operation_id) && actions.includes(p.action as string) && ['APPLIED','REJECTED'].includes(p.status as string) && id(p.result_ref)) : false
  if (!valid) throw new Error('事件业务格式无效。')
  const keys: Record<string, string[]> = { state_changed:['previous_state','current_state','blocked_reason'], action_recorded:['step_id','action_type','attempt_status','evidence_ids'], wait_registered:['wait_id','reason','deadline'], result_ready:['result_ref','outcome'], operation_requested:['operation_id','action'], operation_completed:['operation_id','action','status','result_ref'] }
  const payload: Record<string, unknown> = { event_type:value.event_type }
  for (const key of keys[value.event_type]) payload[key] = p[key]
  return { event_id:value.event_id, task_id:taskId, run_id:runId, event_type:value.event_type,
    state_version:value.state_version, occurred_at:value.occurred_at, payload }
}

/** Preserve the top-level integer lexeme before JSON.parse can round a SQLite ID. */
export function parseFrame(frame: StreamFrame, taskId: string, runId: string): BusinessEvent {
  if (!decimal(frame.id) || frame.id === '0') throw new Error('事件游标无效。')
  let depth = 0, quoted = false, escaped = false, start = -1, found = 0, wire = frame.data
  for (let i = 0; i < frame.data.length; i++) {
    const ch = frame.data[i]
    if (quoted) {
      if (escaped) { escaped = false; continue }
      if (ch === '\\') { escaped = true; continue }
      if (ch !== '"') continue
      quoted = false
      if (depth === 1 && frame.data.slice(start, i + 1) === '"event_id"' && /^\s*:/.test(frame.data.slice(i + 1))) {
        const tail = frame.data.slice(i + 1), match = /^\s*:\s*("(?:0|[1-9]\d{0,18})"|(?:0|[1-9]\d{0,18}))\s*(?=[,}])/.exec(tail)
        if (!match) throw new Error('事件编号格式无效。')
        const token = match[1], lexeme = token.replaceAll('"', '')
        if (lexeme !== frame.id || ++found !== 1) throw new Error('事件编号不一致。')
        const offset = i + 1 + match[0].indexOf(token)
        wire = frame.data.slice(0, offset) + JSON.stringify(lexeme) + frame.data.slice(offset + token.length)
      }
    } else if (ch === '"') { quoted = true; start = i }
    else if (ch === '{' || ch === '[') depth++
    else if (ch === '}' || ch === ']') depth--
  }
  if (found !== 1) throw new Error('事件缺少编号。')
  const event = parseEvent(JSON.parse(wire), taskId, runId)
  if (event.event_type !== frame.type) throw new Error('事件类型不一致。')
  return event
}

export class SseParser {
  private line = ''
  private pendingCR = false
  private fields: string[] = []
  private size = 0
  readonly maxFrame: number
  constructor(maxFrame = 65536) { this.maxFrame = maxFrame }
  push(chunk: string): { frames: StreamFrame[]; heartbeat: boolean } {
    const frames: StreamFrame[] = []; let heartbeat = false
    const endLine = () => {
      if (this.line.startsWith(':')) heartbeat = true
      else if (this.line === '') {
        let eventId = '', type = 'message', hasId = false; const data: string[] = []
        for (const line of this.fields) {
          const index = line.indexOf(':'), field = index < 0 ? line : line.slice(0, index)
          let value = index < 0 ? '' : line.slice(index + 1); if (value.startsWith(' ')) value = value.slice(1)
          if (field === 'id') { if (hasId || value.includes('\0')) throw new Error('事件编号格式无效。'); eventId = value; hasId = true }
          if (field === 'event') type = value
          if (field === 'data') data.push(value)
        }
        if (data.length) frames.push({ id: eventId, type, data: data.join('\n') })
        this.fields = []; this.size = 0
      } else { this.fields.push(this.line); this.size += this.line.length + 1 }
      this.line = ''
      if (frames.length > 256) throw new Error('事件批次超过限制。')
    }
    for (const ch of chunk) {
      if (this.pendingCR) { this.pendingCR = false; if (ch === '\n') continue }
      if (ch === '\r') { endLine(); this.pendingCR = true }
      else if (ch === '\n') endLine()
      else this.line += ch
      if (this.size + this.line.length > this.maxFrame) throw new Error('事件超过读取限制。')
    }
    return { frames, heartbeat }
  }
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  if (record(value)) return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
  return JSON.stringify(value)
}
export class EventTracker {
  cursor: string
  private seen = new Map<string, string>()
  constructor(cursor = '0', seeds: BusinessEvent[] = []) {
    if (!decimal(cursor)) throw new Error('游标无效。'); this.cursor = cursor
    for (const event of seeds.slice(-256)) {
      if (!decimal(event.event_id) || compareIds(event.event_id, cursor) > 0 || this.seen.has(event.event_id)) throw new Error('快照事件游标不一致。')
      this.seen.set(event.event_id, canonical(event))
    }
  }
  accept(event: BusinessEvent): 'new' | 'duplicate' | 'resync' {
    const fingerprint = canonical(event), old = this.seen.get(event.event_id)
    if (old !== undefined) return old === fingerprint ? 'duplicate' : 'resync'
    if (compareIds(event.event_id, this.cursor) <= 0) return 'resync'
    this.seen.set(event.event_id, fingerprint); this.cursor = event.event_id
    if (this.seen.size > 256) this.seen.delete(this.seen.keys().next().value!)
    return 'new'
  }
}
