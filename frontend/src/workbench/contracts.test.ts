import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import test from 'node:test'
registerHooks({resolve(specifier,context,nextResolve) { if (context.parentURL?.includes('/frontend/src/') && specifier.startsWith('.') && !/\.[a-z]+$/.test(specifier)) return nextResolve(specifier+'.ts',context); return nextResolve(specifier,context) }})
const contracts: typeof import('./contracts') = await import(new URL('./contracts.ts',import.meta.url).href)
const {parseWorkspace,parseOperation,parseReadiness,controlAllowed,currentOperation} = contracts
const presentation: typeof import('./presentation') = await import(new URL('./presentation.ts',import.meta.url).href)
const now = '2026-10-02T08:00:00Z'
const workspace = () => ({task:{task_id:'task-one',preparation_status:'READY',state_version:0,current_contract_version:1,current_run_id:null},run:null,
  is_current_run:true,criteria_contract_version:1,criteria:[],current_subgoal:null,checkpoint:null,graph_progress:[],observation:null,budget:null,queue:null,controls:[],events:[],event_cursor:'0',event_high_water:'0',global_event_high_water:'100',has_earlier_events:false,as_of:now})
const run = {run_id:'run-one',task_id:'task-one',contract_version:1,settings_version:2,state:'RUNNING',state_version:3,blocked_reason:null,created_at:now,started_at:now,ended_at:null,handoff_deadline:null}
const active = () => ({...workspace(),task:{...workspace().task,current_run_id:'run-one'},run})
const operation = {operation_id:'operation-one',task_id:'task-one',run_id:'run-one',action:'pause',status:'PENDING',requested_state_version:3,accepted_run_state_version:3,contract_version:1,settings_version:2,reason:null,created_at:now,completed_at:null,state:null,state_version:null}
test('workspace is bound to the exact selected task and current run rather than a structurally valid foreign object', () => {
  assert.equal(parseWorkspace(workspace(),'task-one').run,null)
  assert.throws(() => parseWorkspace(workspace(),'task-two'))
  assert.throws(() => parseWorkspace({...active(),run:{...run,task_id:'task-two'}},'task-one'))
  assert.throws(() => parseWorkspace({...active(),run:{...run,run_id:'run-two'}},'task-one'))
  assert.throws(() => parseWorkspace({...active(),run:{...run,contract_version:2}},'task-one'))
})
test('safe integer versions and exact decimal event watermarks cannot silently round or drift', () => {
  assert.equal(parseWorkspace({...workspace(),global_event_high_water:'9223372036854775807'},'task-one').global_event_high_water,'9223372036854775807')
  assert.throws(() => parseWorkspace({...workspace(),task:{...workspace().task,state_version:9007199254740992}},'task-one'))
  assert.throws(() => parseWorkspace({...workspace(),event_cursor:'2',event_high_water:'1'},'task-one'))
  assert.throws(() => parseWorkspace({...workspace(),event_cursor:'101',event_high_water:'101'},'task-one'))
})
test('control request acceptance remains pending and is bound to action, target and every requested version', () => {
  const expected = {action:'pause' as const,runId:'run-one',body:{expected_state_version:3,contract_version:1,settings_version:2}}
  assert.equal(parseOperation(operation,'task-one',expected).status,'PENDING')
  for (const change of [{run_id:'run-two'},{task_id:'task-two'},{action:'cancel'},{requested_state_version:4},{contract_version:2},{settings_version:3},{state:'PAUSED'}]) assert.throws(() => parseOperation({...operation,...change},'task-one',expected))
  assert.throws(() => parseOperation(operation,'task-one',{operationId:'operation-two'}))
})
test('pending requests block all controls and terminal or missing frozen configuration cannot authorize another control', () => {
  assert.equal(controlAllowed(parseWorkspace(workspace(),'task-one'),'start'),true)
  const actual = parseWorkspace(active(),'task-one'); assert.equal(controlAllowed(actual,'pause'),true)
  assert.equal(controlAllowed({...actual,controls:[operation as import('./types').Operation]},'cancel'),false)
  assert.equal(controlAllowed({...actual,run:{...actual.run!,settings_version:0}},'cancel'),false)
  assert.equal(controlAllowed({...actual,run:{...actual.run!,state:'SUCCEEDED'}},'cancel'),false)
})
test('a task selection change immediately prevents old snapshot controls before the next React effect runs', () => {
  assert.equal(controlAllowed(parseWorkspace(workspace(),'task-one'),'start','task-two'),false)
  const current = parseWorkspace(active(),'task-one')
  for (const action of ['pause','resume','cancel'] as const) assert.equal(controlAllowed(current,action,'task-two'),false)
})
test('resume requires a paused recovery/waiting queue and an available budget', () => {
  const actual = parseWorkspace(active(),'task-one'), paused = {...actual,run:{...actual.run!,state:'PAUSED'},queue:{status:'WAITING',queue_class:'ordinary',reason:null,available_at:now,updated_at:now}}
  assert.equal(controlAllowed(paused,'resume'),true)
  assert.equal(controlAllowed({...paused,queue:null},'resume'),false)
  assert.equal(controlAllowed({...paused,budget:{run_id:'run-one',initialized:true,exhausted:true,reason:'active_time'}},'resume'),false)
})
test('start readiness requires real versioned model, persisted disclosure and task-execution readiness together', () => {
  const settings = {version:3,readiness:{ready:true,provider_verified:false},task_execution_enabled:true,disclosure:{accepted:true},model:{provider:'deepseek'}}
  assert.deepEqual(parseReadiness(settings),{version:3,ready:true})
  assert.equal(parseReadiness({...settings,disclosure:{accepted:false}}).ready,false)
  assert.equal(parseReadiness({...settings,model:null}).ready,false)
  assert.throws(() => parseReadiness({...settings,readiness:{ready:true,provider_verified:true}}))
})
test('Chinese criterion IDs are supported; verified flags require a matching durable checkpoint', () => {
  const criterion = {criterion_id:'报告年份核对',expected_rule:'报告年份一致',check_method:'rule',critical:true,verified:false}
  const actual = {...active(),criteria:[criterion]}
  assert.equal(parseWorkspace(actual,'task-one').criteria[0].criterion_id,'报告年份核对')
  assert.throws(() => parseWorkspace({...actual,criteria:[{...criterion,verified:true}]},'task-one'))
  const checkpoint = {checkpoint_id:'checkpoint-one',verified_item_ids:['报告年份核对'],pending_item_ids:[],action_sequence:1,saved_at:now}
  assert.equal(parseWorkspace({...actual,checkpoint,criteria:[{...criterion,verified:true}],current_subgoal:'aggregate'},'task-one').criteria[0].verified,true)
  assert.throws(() => parseWorkspace({...actual,checkpoint:{...checkpoint,verified_item_ids:['foreign-condition']}},'task-one'))
})
test('initialized budgets require actual numeric counters and remaining limits', () => {
  assert.throws(() => parseWorkspace({...active(),budget:{run_id:'run-one',initialized:true,exhausted:false,reason:null}},'task-one'))
  assert.equal(parseWorkspace({...active(),budget:{run_id:'run-one',initialized:false,exhausted:false,reason:null}},'task-one').budget?.initialized,false)
})
test('a newer control request from another client remains processing instead of being hidden by the prior local receipt', () => {
  const prior = parseOperation({...operation,status:'APPLIED',completed_at:now,state:'PAUSED',state_version:4},'task-one')
  const newer = parseOperation({...operation,operation_id:'operation-two',action:'cancel'},'task-one')
  const current = {...parseWorkspace(active(),'task-one'),controls:[prior,newer]}
  assert.equal(currentOperation(current,prior)?.operation_id,'operation-two')
  assert.equal(currentOperation(current,prior)?.status,'PENDING')
  assert.equal(controlAllowed(current,'cancel'),false)
  assert.equal(currentOperation({...current,controls:[]},{...prior,run_id:'foreign-run'}),undefined)
})
test('the accepted banner transitions to applied or rejected without retaining a processing claim after completion', () => {
  const pending = parseOperation(operation,'task-one')
  assert.match(presentation.operationNotice(pending),/正在等待安全边界/)
  const applied = parseOperation({...operation,status:'APPLIED',completed_at:now,state:'PAUSED',state_version:4},'task-one')
  assert.match(presentation.operationNotice(applied),/请求已应用/)
  assert.doesNotMatch(presentation.operationNotice(applied),/等待|处理中|受理/)
  const rejected = parseOperation({...operation,status:'REJECTED',completed_at:now,state:'RUNNING',state_version:3,reason:'version_conflict'},'task-one')
  assert.match(presentation.operationNotice(rejected),/请求未应用.*版本已变化/)
  const actual = {...parseWorkspace(active(),'task-one'),controls:[applied,rejected]}
  assert.match(presentation.operationNotice(currentOperation(actual,pending)),/请求未应用/)
})
