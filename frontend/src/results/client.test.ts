import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { registerHooks } from 'node:module'
import { deflateSync } from 'node:zlib'
import test from 'node:test'
registerHooks({resolve(specifier,context,nextResolve) { if (context.parentURL?.includes('/frontend/src/') && specifier.startsWith('.') && !/\.[a-z]+$/.test(specifier)) return nextResolve(specifier+'.ts',context); return nextResolve(specifier,context) }})
const client: typeof import('./client') = await import(new URL('./client.ts',import.meta.url).href)
const { sample }: typeof import('./test-fixtures') = await import(new URL('./test-fixtures.ts',import.meta.url).href)
const originalFetch = globalThis.fetch
function proof(content: string|Uint8Array, kind = 'text') {
  const data = typeof content === 'string' ? new TextEncoder().encode(content) : content, expected = sample().evidence[0]
  Object.assign(expected,{artifact_kind:kind,display_sha256:createHash('sha256').update(data).digest('hex'),display_size_bytes:data.byteLength,display_mime_type:kind === 'screenshot' ? 'image/png' : 'text/plain; charset=utf-8'})
  const metadata = {...expected,evidence_id:'display-one',run_id:'run-one',snapshot_id:null,original_evidence_id:'evidence-one',availability:'AVAILABLE',redaction_status:'FILTERED',artifact_kind:kind,sha256:expected.display_sha256,size_bytes:data.byteLength,mime_type:expected.display_mime_type}
  return {expected,metadata,data}
}
test('result request is read-only same-origin authenticated and binds returned task/run, including precise history cursor', async () => {
  const paths: string[] = []
  globalThis.fetch = async (url,init) => { paths.push(String(url)); assert.equal(init?.method,undefined); assert.equal(init?.credentials,'omit'); assert.equal(init?.redirect,'error'); assert.equal(new Headers(init?.headers).get('X-WebPilot-Client'),'1'); assert.equal(new Headers(init?.headers).get('Authorization'),null); return Response.json(sample()) }
  try {
    await client.getResults('task-one','run-one',new AbortController().signal,'9223372036854775807')
    assert.equal(paths[0],'/api/v1/tasks/task-one/results?run_id=run-one&before=9223372036854775807')
    await assert.rejects(client.getResults('task-one','run-other',new AbortController().signal))
    await assert.rejects(client.getResults('task-other','run-one',new AbortController().signal))
    const count = paths.length; await assert.rejects(client.getResults('../task',null,new AbortController().signal)); assert.equal(paths.length,count)
  } finally { globalThis.fetch = originalFetch }
})
test('controlled text and diff reads use only display IDs and cryptographically verify exact returned bytes', async () => {
  for (const kind of ['text','diff']) {
    const {expected,metadata} = proof('<script>literal text, never HTML</script>',kind), paths: string[] = []
    globalThis.fetch = async (url,init) => { paths.push(String(url)); assert.equal(new Headers(init?.headers).get('X-WebPilot-Client'),'1'); return String(url).endsWith('/content') ? new Response('<script>literal text, never HTML</script>',{headers:{'Content-Type':metadata.mime_type!}}) : Response.json(metadata) }
    try { const result = await client.readEvidence(expected,'run-one',new AbortController().signal); assert.equal(result.kind,kind); assert.equal('text' in result && result.text,'<script>literal text, never HTML</script>'); assert.deepEqual(paths,['/api/v1/evidence/display-one','/api/v1/evidence/display-one/content']) }
    finally { globalThis.fetch = originalFetch }
  }
})
test('foreign metadata is rejected before content fetch; corrupted same-length content and mismatched MIME never render', async () => {
  const {expected,metadata} = proof('safe'); let calls = 0
  globalThis.fetch = async () => {calls++; return Response.json({...metadata,run_id:'foreign-run'})}
  try { await assert.rejects(client.readEvidence(expected,'run-one',new AbortController().signal)); assert.equal(calls,1) }
  finally { globalThis.fetch = originalFetch }
  for (const [content,mime] of [['evil','text/plain; charset=utf-8'],['safe','text/html'],['saf','text/plain; charset=utf-8'],['safer','text/plain; charset=utf-8']]) {
    globalThis.fetch = async (url) => String(url).endsWith('/content') ? new Response(content,{headers:{'Content-Type':mime}}) : Response.json(metadata)
    try { await assert.rejects(client.readEvidence(expected,'run-one',new AbortController().signal)) } finally { globalThis.fetch = originalFetch }
  }
})
test('oversized streaming evidence is cancelled and evidence errors do not include arbitrary server bodies', async () => {
  const {expected,metadata} = proof('safe'); let canceled = false
  globalThis.fetch = async (url) => String(url).endsWith('/content') ? new Response(new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode('oversized'))},cancel() {canceled = true}}),{headers:{'Content-Type':metadata.mime_type!}}) : Response.json(metadata)
  try { await assert.rejects(client.readEvidence(expected,'run-one',new AbortController().signal)); assert.equal(canceled,true) }
  finally { globalThis.fetch = originalFetch }
  globalThis.fetch = async () => new Response('untrusted body',{status:503})
  try { await assert.rejects(client.getResults('task-one',null,new AbortController().signal),(error: Error) => !error.message.includes('untrusted')) }
  finally { globalThis.fetch = originalFetch }
})
function chunk(name: string, data: Uint8Array) {
  const type = Buffer.from(name), payload = Buffer.concat([type,data]), result = Buffer.alloc(data.length+12); let crc = 0xffffffff
  for (const byte of payload) {crc ^= byte; for (let bit=0;bit<8;bit++) crc = crc & 1 ? 0xedb88320 ^ crc >>> 1 : crc >>> 1}
  result.writeUInt32BE(data.length,0); payload.copy(result,4); result.writeUInt32BE((crc ^ 0xffffffff) >>> 0,result.length-4); return result
}
function png(pixel = 128, extra = false) {
  const header = Buffer.alloc(13); header.writeUInt32BE(1,0); header.writeUInt32BE(1,4); header[8] = 8; header[9] = 2
  return new Uint8Array(Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),...(extra ? [chunk('tEXt',Buffer.from('unsafe metadata'))] : []),chunk('IDAT',deflateSync(Buffer.from([0,pixel,pixel,pixel]))),chunk('IEND',Buffer.alloc(0))]))
}
test('only canonical all-neutral PNG pixels are displayed; active formats, metadata chunks, changed pixels and bad CRC fail', async () => {
  assert.equal(await client.neutralPng(png()),true)
  for (const data of [png(0),png(128,true),new TextEncoder().encode('<svg></svg>'),new Uint8Array([...png(),0])]) assert.equal(await client.neutralPng(data),false)
  const damaged = png(); damaged[30] ^= 1; assert.equal(await client.neutralPng(damaged),false)
  const data = png(), {expected,metadata} = proof(data,'screenshot')
  globalThis.fetch = async (url) => String(url).endsWith('/content') ? new Response(data,{headers:{'Content-Type':'image/png'}}) : Response.json(metadata)
  try { assert.equal((await client.readEvidence(expected,'run-one',new AbortController().signal)).kind,'screenshot') }
  finally { globalThis.fetch = originalFetch }
})
