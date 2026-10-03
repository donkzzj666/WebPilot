import assert from 'node:assert/strict'
import test from 'node:test'
import type { StreamFrame } from './events'
const events: typeof import('./events') = await import(new URL('./events.ts', import.meta.url).href)
const { SseParser, EventTracker, parseFrame, parseEvent, decimal } = events
const event = (eventId = '5') => ({ event_id:eventId, task_id:'task-one', run_id:'run-one', event_type:'state_changed', state_version:1,
  occurred_at:'2026-10-02T08:00:00Z', payload:{ event_type:'state_changed', previous_state:'QUEUED', current_state:'RUNNING', blocked_reason:null } })
const frame = (eventId = '5') => `id: ${eventId}\nevent: state_changed\ndata: ${JSON.stringify(event(eventId))}\n\n`
test('chunked SSE accepts split CRLF, Unicode data, heartbeats and multiline JSON without dispatching partial frames', () => {
  const parser = new SseParser(), wire = ': keep-alive\r\n\r\n' + frame().replaceAll('\n','\r\n')
  const frames: StreamFrame[] = []; let heartbeat = false
  for (const ch of wire) { const batch = parser.push(ch); frames.push(...batch.frames); heartbeat ||= batch.heartbeat }
  assert.equal(heartbeat,true); assert.equal(frames.length,1); assert.equal(parseFrame(frames[0],'task-one','run-one').event_id,'5')
  const p2 = new SseParser(); assert.equal(p2.push('id: 1\ndata: {"字":\n').frames.length,0)
  assert.equal(p2.push('data: "值"}\n\n').frames[0].data,'{"字":\n"值"}')
})
test('decimal frame ID preserves full SQLite 64-bit numeric JSON lexeme and rejects rounded or contradictory data', () => {
  const data = JSON.stringify(event('9223372036854775807')).replace('"event_id":"9223372036854775807"','"event_id":9223372036854775807')
  assert.equal(parseFrame({id:'9223372036854775807',type:'state_changed',data},'task-one','run-one').event_id,'9223372036854775807')
  assert.throws(() => parseFrame({id:'9223372036854775806',type:'state_changed',data},'task-one','run-one'))
  for (const id of ['01','-1','1.0','9223372036854775808',1]) assert.equal(decimal(id),false)
})
test('event target and event type binding fail closed, unknown payload fields are never retained', () => {
  assert.throws(() => parseFrame({ id:'5',type:'state_changed',data:JSON.stringify(event()) },'task-two','run-one'))
  assert.throws(() => parseEvent({...event(),run_id:'run-two'},'task-one','run-one'))
  assert.throws(() => parseFrame({ id:'5',type:'action_recorded',data:JSON.stringify(event()) },'task-one','run-one'))
  const parsed = parseEvent({...event(),internal_reasoning:'private',payload:{...event().payload,internal_reasoning:'private'}},'task-one','run-one')
  assert.equal('internal_reasoning' in parsed,false); assert.equal('internal_reasoning' in parsed.payload,false)
})
test('oversized or ambiguous frames are bounded and rejected before becoming progress', () => {
  assert.throws(() => new SseParser(32).push('data: '+ 'x'.repeat(33)))
  assert.throws(() => new SseParser().push('id:\nid: 5\ndata: {}\n\n'))
  assert.throws(() => new SseParser().push(frame().repeat(257)))
  assert.throws(() => parseFrame({id:'5',type:'state_changed',data:JSON.stringify(event()).replace('"event_id":"5"','"event_id":"5","event_id":"5"')},'task-one','run-one'))
})
test('known duplicates, filtered global ID gaps, same-ID conflict and unknown lower delivery have distinct outcomes', () => {
  const tracker = new EventTracker('0'), first = parseEvent(event('5'),'task-one','run-one')
  assert.equal(tracker.accept(first),'new'); assert.equal(tracker.accept(first),'duplicate')
  assert.equal(tracker.accept(parseEvent(event('200'),'task-one','run-one')),'new')
  assert.equal(tracker.accept(first),'duplicate')
  assert.equal(tracker.accept(parseEvent({...event('5'),state_version:2},'task-one','run-one')),'resync')
  assert.equal(tracker.accept(parseEvent(event('199'),'task-one','run-one')),'resync')
  assert.equal(tracker.cursor,'200')
})
test('snapshot seeds deduplicate replayed known IDs while an unseen lower ID requires authoritative resynchronization', () => {
  const first = parseEvent(event(),'task-one','run-one'), tracker = new EventTracker('8',[first])
  assert.equal(tracker.accept(first),'duplicate'); assert.equal(tracker.accept(parseEvent(event('6'),'task-one','run-one')),'resync')
  assert.throws(() => new EventTracker('4',[first]))
})
