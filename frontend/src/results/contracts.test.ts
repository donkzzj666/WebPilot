import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import test from 'node:test'
registerHooks({resolve(specifier,context,nextResolve) { if (context.parentURL?.includes('/frontend/src/') && specifier.startsWith('.') && !/\.[a-z]+$/.test(specifier)) return nextResolve(specifier+'.ts',context); return nextResolve(specifier,context) }})
const { parseResults, canVerifySuccess, parseDisplayMetadata, decimal, mergeHistoryPage }: typeof import('./contracts') = await import(new URL('./contracts.ts',import.meta.url).href)
const { sample }: typeof import('./test-fixtures') = await import(new URL('./test-fixtures.ts',import.meta.url).href)
const { resolveResultPointer, resultFieldValue, checkExplanation }: typeof import('./fields') = await import(new URL('./fields.ts',import.meta.url).href)

test('results bind request task, selected historical run, contract, persisted state and assistance count', () => {
  assert.equal(parseResults(sample(),'task-one').display_complete_success,true)
  assert.throws(() => parseResults(sample(),'foreign-task'))
  assert.throws(() => parseResults(sample(),'task-one','foreign-run'))
  for (const key of ['task_id','run_id','contract_version','outcome','assistance_count']) {
    const value = sample(); Object.assign(value.result!,{[key]:key === 'contract_version' || key === 'assistance_count' ? 2 : 'foreign'})
    assert.throws(() => parseResults(value,'task-one'))
  }
  const historical = sample(); historical.current_run_id = 'new-run'; historical.runs = [{...historical.selected_run!,run_id:'new-run',parent_run_id:'run-one'},...historical.runs]
  assert.equal(parseResults(historical,'task-one','run-one').selected_run?.run_id,'run-one')
  assert.throws(() => parseResults(historical,'task-one'))
})
test('empty preparation and current-null historical fallback remain distinct from completed results', () => {
  const empty = {...sample(),current_run_id:null,runs:[],selected_run:null,result_status:'NOT_READY',result:null,field_checks:[],verification_id:null,assistance:null,evidence:[],display_complete_success:false}
  assert.equal(parseResults(empty,'task-one').selected_run,null)
  assert.throws(() => parseResults({...empty,display_complete_success:true},'task-one'))
  const fallback = sample(); fallback.current_run_id = null
  assert.equal(parseResults(fallback,'task-one').selected_run?.run_id,'run-one')
})
test('complete success rejects every non-PASS verdict, uncovered item, unresolved write and unavailable artifact', () => {
  const mutations = [
    (v: ReturnType<typeof sample>) => {v.field_checks[0].verdict = 'CONFLICT'},
    (v: ReturnType<typeof sample>) => {v.result!.checks[0].verdict = 'FAIL'},
    (v: ReturnType<typeof sample>) => {v.result!.checks[0].verdict = 'INSUFFICIENT'},
    (v: ReturnType<typeof sample>) => {v.result!.coverage.complete = false; v.result!.coverage.gaps.push('range-unread')},
    (v: ReturnType<typeof sample>) => {v.result!.unresolved.push('missing-field')},
    (v: ReturnType<typeof sample>) => {v.evidence[0].displayable = false; v.evidence[0].display_status = 'EXPIRED'},
    (v: ReturnType<typeof sample>) => {v.pending_write_count = 1},
    (v: ReturnType<typeof sample>) => {v.write_intents_truncated = true},
    (v: ReturnType<typeof sample>) => {v.write_intents.push({operation_id:'write-one',originating_run_id:'run-one',status:'UNKNOWN',receipt_available:false,critical_violation:false,recorded_in_selected_result:true})},
    (v: ReturnType<typeof sample>) => {v.write_intents.push({operation_id:'write-one',originating_run_id:'run-one',status:'INTENT',receipt_available:false,critical_violation:false,recorded_in_selected_result:true})},
    (v: ReturnType<typeof sample>) => {v.write_intents.push({operation_id:'write-one',originating_run_id:'run-one',status:'CONFIRMED',receipt_available:true,critical_violation:false,recorded_in_selected_result:false})},
  ]
  for (const mutate of mutations) { const value = sample(); mutate(value); assert.equal(canVerifySuccess(value),false); assert.throws(() => parseResults(value,'task-one')); value.display_complete_success = false; assert.doesNotThrow(() => parseResults(value,'task-one')) }
})
test('PASS without evidence, duplicate fields and evidence from another run are rejected', () => {
  const missing = sample(); missing.field_checks[0].evidence_ids = []; assert.throws(() => parseResults(missing,'task-one'))
  const duplicate = sample(); duplicate.field_checks.push({...duplicate.field_checks[0]}); assert.throws(() => parseResults(duplicate,'task-one'))
  const foreign = sample(); foreign.evidence[0].run_id = 'run-two'; assert.throws(() => parseResults(foreign,'task-one'))
  const unlisted = sample(); unlisted.result!.checks[0].evidence_ids.push('not-in-catalog'); assert.throws(() => parseResults(unlisted,'task-one'))
  const extended = sample(); extended.evidence.push({...extended.evidence[0],evidence_id:'evidence-two',display_evidence_id:'display-two'}); extended.result!.checks[0].evidence_ids.push('evidence-two')
  assert.doesNotThrow(() => parseResults(extended,'task-one'))
})
test('metadata binds exact derivative, source evidence, snapshot, hash, length, MIME, selected run and filtered availability', () => {
  const expected = sample().evidence[0], metadata = {...expected,evidence_id:'display-one',run_id:'run-one',snapshot_id:null,original_evidence_id:'evidence-one',availability:'AVAILABLE',redaction_status:'FILTERED',artifact_kind:'text',sha256:'0'.repeat(64),size_bytes:4,mime_type:'text/plain; charset=utf-8'}
  assert.equal(parseDisplayMetadata(metadata,expected,'run-one').evidence_id,'display-one')
  for (const [key,value] of Object.entries({evidence_id:'other',run_id:'other',snapshot_id:'other',original_evidence_id:'other',sha256:'1'.repeat(64),size_bytes:3,mime_type:'text/html',availability:'EXPIRED',redaction_status:'BLOCKED',artifact_kind:'pdf',source_url:'https://foreign.test',object_id:'other',captured_at:'2020-01-01T00:00:00Z',locator_or_page:'elsewhere'})) assert.throws(() => parseDisplayMetadata({...metadata,[key]:value},expected,'run-one'))
})
test('history cursors preserve SQLite precision and reject alternate encodings or overflow', () => {
  assert.equal(decimal('9223372036854775807'),true)
  for (const value of ['9223372036854775808','00','01','-1','1e3',9007199254740992,null]) assert.equal(decimal(value),false)
  const page = sample(); page.next_cursor = '9223372036854775807'; assert.equal(parseResults(page,'task-one').next_cursor,page.next_cursor)
})
test('history pagination adopts newly failed evidence/write proof even when selected run version is unchanged', () => {
  const previous = sample(); previous.next_cursor = '50'
  const next = sample(); next.next_cursor = '30'; next.evidence[0].availability = 'CORRUPT'; next.evidence[0].displayable = false
  next.display_complete_success = false; next.display_blockers = ['evidence_not_readable']
  next.runs = [{...next.runs[0],run_id:'older-run',state:'FAILED'}]
  const merged = mergeHistoryPage(previous,next,'50',previous.runs)
  assert.equal(merged.page,next); assert.equal(canVerifySuccess(merged.page),false); assert.deepEqual(merged.runs.map((run) => run.run_id),['run-one','older-run'])
  const changed = sample(); changed.current_run_id = 'new-run'; assert.throws(() => mergeHistoryPage(previous,changed,'50',previous.runs))
  next.next_cursor = '51'; assert.throws(() => mergeHistoryPage(previous,next,'50',previous.runs))
})
test('field display resolves actual business values and escaped RFC 6901 keys instead of checker diagnostic codes', () => {
  const page = sample(), field = page.field_checks[0]
  field.actual = {code:'original_value_matches'}
  assert.deepEqual(resultFieldValue(page.result!,field.result_path),{found:true,value:'1.23'})
  assert.equal(checkExplanation(field.actual),'结果值与对应证据一致。')
  assert.deepEqual(resolveResultPointer({'a/b':{'~value':['zero',{answer:42}]}},'/a~1b/~0value/1/answer'),{found:true,value:42})
  assert.deepEqual(resolveResultPointer({'~1':'escaped only once'},'/~01'),{found:true,value:'escaped only once'})
  assert.deepEqual(resolveResultPointer({'':'empty key'},'/'),{found:true,value:'empty key'})
  assert.deepEqual(resolveResultPointer(null,''),{found:true,value:null})
  assert.deepEqual(resultFieldValue(page.result!,'/coverage/content_pages'),{found:true,value:1})
})
test('field display distinguishes missing and null, rejects invalid pointers, and never traverses prototypes or accessors', () => {
  const source = {values:[null,'one']}
  assert.deepEqual(resolveResultPointer(source,'/values/0'),{found:true,value:null})
  for (const path of ['/missing','/values/2','/values/-','/values/01','/values/length','/values/9007199254740993','values/0','/~2','/~','/values/0/key','/__proto__/value','/constructor/prototype','/toString']) assert.deepEqual(resolveResultPointer(source,path),{found:false},path)
  assert.deepEqual(resolveResultPointer(JSON.parse('{"__proto__":{"secret":"hidden"}}'),'/__proto__/secret'),{found:false})
  const inherited = Object.create({secret:'hidden'}); inherited.visible = 'public'
  assert.deepEqual(resolveResultPointer(inherited,'/secret'),{found:false})
  let reads = 0
  const accessor = Object.defineProperty({},'secret',{get() {reads++; return 'hidden'}})
  assert.deepEqual(resolveResultPointer(accessor,'/secret'),{found:false}); assert.equal(reads,0)
  assert.deepEqual(resultFieldValue(sample().result!,'/context/unavailable'),{found:false})
})
