import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import test from 'node:test'
registerHooks({resolve(specifier,context,nextResolve) { if (context.parentURL?.includes('/frontend/src/') && specifier.startsWith('.') && !/\.[a-z]+$/.test(specifier)) return nextResolve(specifier+'.ts',context); return nextResolve(specifier,context) }})
const client: typeof import('./client') = await import(new URL('./client.ts',import.meta.url).href)
const now = '2026-10-02T08:00:00Z', originalFetch = globalThis.fetch
const body = {expected_state_version:3,contract_version:1,settings_version:2}
const operation = {operation_id:'operation-one',task_id:'task-one',run_id:'run-one',action:'pause',status:'PENDING',requested_state_version:3,accepted_run_state_version:3,contract_version:1,settings_version:2,reason:null,created_at:now,completed_at:null,state:null,state_version:null}
test('unknown outcome retains exactly the same route, body and idempotency key; changed versions get a fresh key', () => {
  const pending = client.pendingControl('task-one','run-one','pause',body)
  assert.equal(client.pendingControl('task-one','run-one','pause',body,pending),pending)
  assert.notEqual(client.pendingControl('task-one','run-one','pause',{...body,expected_state_version:4},pending).key,pending.key)
  assert.notEqual(client.pendingControl('task-one','run-two','pause',body,pending).key,pending.key)
})
test('control POST uses protected marker and immutable versions; HTTP 202 does not mean applied', async () => {
  const pending = client.pendingControl('task-one','run-one','pause',body); let init: RequestInit|undefined
  globalThis.fetch = async (_url,options) => { init = options; return Response.json({operation},{status:202}) }
  try { const result = await client.postControl(pending,new AbortController().signal); assert.equal(result.status,'PENDING'); assert.equal(new Headers(init?.headers).get('X-WebPilot-Client'),'1'); assert.equal(new Headers(init?.headers).get('Authorization'),null); assert.equal(new Headers(init?.headers).get('Idempotency-Key'),pending.key); assert.deepEqual(JSON.parse(init!.body as string),body) }
  finally { globalThis.fetch = originalFetch }
})
test('foreign receipts, wrong status and contradictory applied acceptance are rejected before a UI success', async () => {
  const pending = client.pendingControl('task-one','run-one','pause',body)
  try { for (const [status,value] of [[202,{...operation,run_id:'run-two'}],[200,operation],[202,{...operation,status:'APPLIED',completed_at:now,state:'PAUSED',state_version:4}]] as const) { globalThis.fetch = async () => Response.json({operation:value},{status}); await assert.rejects(client.postControl(pending,new AbortController().signal)) } }
  finally { globalThis.fetch = originalFetch }
})
test('operation query response must bind the exact operation ID and current task/run', async () => {
  globalThis.fetch = async () => Response.json({operation})
  try { assert.equal((await client.getOperation('operation-one','task-one','run-one',new AbortController().signal)).status,'PENDING'); await assert.rejects(client.getOperation('operation-two','task-one','run-one',new AbortController().signal)); await assert.rejects(client.getOperation('operation-one','task-one','run-two',new AbortController().signal)); await assert.rejects(client.getOperation('operation-one','task-one','run-one',new AbortController().signal,{...operation,action:'cancel'} as import('./types').Operation)) }
  finally { globalThis.fetch = originalFetch }
})
test('stream opening sends exact Last-Event-ID and rejects error media while canceling its body', async () => {
  let headers = new Headers(), canceled = false
  globalThis.fetch = async (_url,init) => { headers = new Headers(init?.headers); return new Response(new ReadableStream({cancel() { canceled = true }}),{status:503,headers:{'Content-Type':'application/json'}}) }
  try { await assert.rejects(client.openStream('task-one','run-one','9223372036854775807',new AbortController().signal)); assert.equal(headers.get('Last-Event-ID'),'9223372036854775807'); assert.equal(headers.get('X-WebPilot-Client'),'1'); assert.equal(canceled,true) }
  finally { globalThis.fetch = originalFetch }
})
test('JSON replay validates target, increasing decimal cursor and filtered global gaps', async () => {
  const event = {event_id:'200',task_id:'task-one',run_id:'run-one',event_type:'state_changed',state_version:1,occurred_at:now,payload:{event_type:'state_changed',previous_state:'QUEUED',current_state:'RUNNING',blocked_reason:null}}
  let page = {task_id:'task-one',run_id:'run-one',events:[event],cursor:'200',high_water:'200',global_high_water:'300',has_more:false,as_of:now}
  globalThis.fetch = async () => Response.json(page)
  try { await client.replayEvents('task-one','run-one','5',new AbortController().signal); page = {...page,run_id:'run-two'}; await assert.rejects(client.replayEvents('task-one','run-one','5',new AbortController().signal)); page = {...page,run_id:'run-one',cursor:'199'}; await assert.rejects(client.replayEvents('task-one','run-one','5',new AbortController().signal)) }
  finally { globalThis.fetch = originalFetch }
})
test('page derivative content is fetched by ID only and must match selected run, snapshot and filtered availability', async () => {
  const content = JSON.stringify({title:'Owned page',text:'Synthetic visible text'}), size = new TextEncoder().encode(content).byteLength
  let metadata = {evidence_id:'evidence-one',run_id:'run-one',snapshot_id:'snapshot-one',redaction_status:'FILTERED',availability:'AVAILABLE',artifact_kind:'text',mime_type:'text/plain; charset=utf-8',size_bytes:size}
  const paths: string[] = []
  globalThis.fetch = async (url,init) => { paths.push(String(url)); assert.equal(new Headers(init?.headers).get('X-WebPilot-Client'),'1'); return String(url).endsWith('/content') ? new Response(content,{headers:{'Content-Type':'text/plain; charset=utf-8'}}) : Response.json(metadata) }
  try { const result = await client.readEvidence('evidence-one','run-one','snapshot-one',new AbortController().signal); assert.equal(result.kind,'text'); assert.deepEqual(paths,['/api/v1/evidence/evidence-one','/api/v1/evidence/evidence-one/content']); metadata = {...metadata,snapshot_id:'foreign-snapshot'}; await assert.rejects(client.readEvidence('evidence-one','run-one','snapshot-one',new AbortController().signal)); assert.equal(paths.length,3) }
  finally { globalThis.fetch = originalFetch }
})
