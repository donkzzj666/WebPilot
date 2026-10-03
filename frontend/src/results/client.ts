import { apiFetch } from '../api'
import { decimal, id, parseDisplayMetadata, parseResults } from './contracts'
import type { DisplayContent, Evidence } from './types'

const combined = (signal: AbortSignal) => AbortSignal.any([signal, AbortSignal.timeout(15000)])
async function bytes(response: Response, maximum: number): Promise<Uint8Array<ArrayBuffer>> {
  if (!response.body) throw new Error('服务返回空内容。')
  const reader = response.body.getReader(), chunks: Uint8Array[] = []; let size = 0
  try {
    while (true) {
      const part = await reader.read(); if (part.done) break
      size += part.value.byteLength
      if (size > maximum) throw new Error('读取内容超过展示上限。')
      chunks.push(part.value)
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
  const output = new Uint8Array(size); let offset = 0
  for (const chunk of chunks) { output.set(chunk,offset); offset += chunk.byteLength }
  return output
}
async function request(path: string, signal: AbortSignal): Promise<unknown> {
  let response: Response
  try { response = await apiFetch(path, { signal: combined(signal), headers: { Accept: 'application/json' } }) }
  catch { throw new Error('结果连接中断或超时，请刷新重试。') }
  if (!response.ok) {
    await response.body?.cancel()
    throw new Error(response.status === 404 ? '所选任务或运行不存在，无法展示结果。' : response.status === 401 || response.status === 403 ? '本机连接认证失败，请检查启动配置。' : '结果服务暂不可用，请刷新重试。')
  }
  if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') { await response.body?.cancel(); throw new Error('结果响应格式无效。') }
  try { return JSON.parse(new TextDecoder('utf-8',{ fatal:true }).decode(await bytes(response,8 * 1024 * 1024))) }
  catch { throw new Error('结果响应不可读，请刷新重试。') }
}
export async function getResults(taskId: string, runId: string|null, signal: AbortSignal, before: string|null = null) {
  if (!id(taskId) || runId !== null && !id(runId) || before !== null && !decimal(before)) throw new Error('所选结果标识无效。')
  const query = new URLSearchParams()
  if (runId) query.set('run_id',runId)
  if (before) query.set('before',before)
  const raw = await request(`/api/v1/tasks/${encodeURIComponent(taskId)}/results${query.size ? '?' + query.toString() : ''}`,signal)
  return parseResults(raw,taskId,runId)
}
function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff
  for (const byte of bytes) { crc ^= byte; for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0) }
  return (crc ^ 0xffffffff) >>> 0
}
/** Accept only the same canonical all-neutral PNG format used by the evidence service. */
export async function neutralPng(data: Uint8Array<ArrayBuffer>): Promise<boolean> {
  const signature = [137,80,78,71,13,10,26,10]
  if (data.length < 57 || data.length > 16 * 1024 * 1024 || !signature.every((byte,index) => data[index] === byte)) return false
  const view = new DataView(data.buffer,data.byteOffset,data.byteLength), names = ['IHDR','IDAT','IEND']
  let offset = 8, width = 0, height = 0, compressed: Uint8Array<ArrayBuffer>|null = null
  for (const name of names) {
    if (offset + 12 > data.length) return false
    const length = view.getUint32(offset), end = offset + 12 + length
    if (end > data.length || String.fromCharCode(...data.slice(offset+4,offset+8)) !== name || view.getUint32(end-4) !== crc32(data.slice(offset+4,end-4))) return false
    if (name === 'IHDR') {
      if (length !== 13) return false
      width = view.getUint32(offset+8); height = view.getUint32(offset+12)
      if (width < 1 || height < 1 || width > 8192 || height > 8192 || width * height > 16_777_216 || data[offset+16] !== 8 || data[offset+17] !== 2 || data[offset+18] !== 0 || data[offset+19] !== 0 || data[offset+20] !== 0) return false
    } else if (name === 'IDAT') compressed = data.slice(offset+8,end-4)
    else if (length !== 0) return false
    offset = end
  }
  if (offset !== data.length || !compressed) return false
  const rowLength = width * 3 + 1, expected = rowLength * height
  if (expected > 64 * 1024 * 1024) return false
  try {
    const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('deflate'))
    const decoded = await bytes(new Response(stream),expected)
    return decoded.length === expected && decoded.every((byte,index) => byte === (index % rowLength === 0 ? 0 : 128))
  } catch { return false }
}
export async function readEvidence(expected: Evidence, runId: string, signal: AbortSignal): Promise<DisplayContent> {
  if (!id(expected.evidence_id) || !id(expected.display_evidence_id) || !id(runId)) throw new Error('证据没有可读的受控副本。')
  const path = `/api/v1/evidence/${encodeURIComponent(expected.display_evidence_id)}`
  const metadata = parseDisplayMetadata(await request(path,signal),expected,runId)
  const response = await apiFetch(path + '/content', { signal:combined(signal), headers: { Accept:metadata.mime_type } })
  if (!response.ok || response.headers.get('Content-Type')?.toLowerCase().replace(/\s+/g,'') !== metadata.mime_type.toLowerCase().replace(/\s+/g,'')) {
    await response.body?.cancel(); throw new Error('证据已不可读或展示类型变化，请刷新结果。')
  }
  const data = await bytes(response,metadata.size_bytes)
  if (signal.aborted) throw new Error('已切换所选结果。')
  if (data.byteLength !== metadata.size_bytes) throw new Error('证据长度校验失败。')
  const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',data))).map((byte) => byte.toString(16).padStart(2,'0')).join('')
  if (actual !== metadata.sha256) throw new Error('证据内容校验失败，请刷新结果。')
  if (metadata.artifact_kind === 'screenshot') {
    if (!await neutralPng(data)) throw new Error('图片未通过中性展示副本校验。')
    return { kind:'screenshot', blob:new Blob([data],{type:'image/png'}) }
  }
  const text = new TextDecoder('utf-8',{ fatal:true }).decode(data)
  if (text.length > 1_000_000) throw new Error('文本证据超过展示上限。')
  return { kind:metadata.artifact_kind, text }
}
