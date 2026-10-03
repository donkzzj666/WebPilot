import assert from 'node:assert/strict'
import test from 'node:test'
const contracts: typeof import('./types') = await import(new URL('./types.ts', import.meta.url).href)
const { parseTaskDetail, parseTaskList, parseIdentities, isPolicy, parseSourceScopes } = contracts

const now = '2026-10-02T08:00:00Z'
const task = { task_id: 'task-one', original_instruction: '读取 ACME 2025 年报营收', preparation_status: 'READY',
  current_contract_version: 1, current_run_id: null, state_version: 0, created_at: now, requested_fields: [] }
const contract = { schema_version: 'm0-contract-v1', task_id: 'task-one', contract_version: 1,
  scenario: 'finance', objective: task.original_instruction, original_instruction: task.original_instruction,
  targets: [{ object_id: 'ACME', kind: 'entity', canonical_name: 'ACME' }],
  sources: [{ source_id: 'reports', site_id: 'company', origin: 'https://example.com', path_prefix: '/reports' }],
  start_urls: ['https://example.com/reports/2025'], parameters: { scenario: 'finance', entity_id: 'ACME' },
  time_scope: { start: null, end: null, basis: '2025年报' }, action_policy: { mode: 'read_only' }, identity_ref: null,
  output_schema: [{ field_id: 'revenue', required: true, description: '营收' }],
  acceptance_criteria: [{ criterion_id: 'report', expected_rule: '报告对象与年份一致', check_method: 'rule', critical: true }] }
const detail = { task, contract_version: 1, contract, draft: null, missing_fields: [], current_run: null,
  historical_runs: [], contract_history: [contract] }
const identity = { identity_ref: 'identity-one', site_id: 'github', realm: 'public', origin: 'https://github.com',
  normalized_account: 'alice', state: 'VERIFIED', state_version: 0, created_at: now, updated_at: now,
  requires_recheck: true, requires_identity_check: true, requires_business_check: true }

test('task catalog supports initial state versions and exact 64-bit string pagination', () => {
  assert.equal(parseTaskList({ tasks: [task], next_cursor: '9223372036854775807' }).next_cursor, '9223372036854775807')
  for (const cursor of [0, 1, '0', '-1', '9223372036854775808', '1.5', '../secret']) {
    assert.throws(() => parseTaskList({ tasks: [task], next_cursor: cursor }))
  }
})
test('malformed catalog states and missing fields cannot become saved task cards', () => {
  assert.throws(() => parseTaskList({ tasks: [{ ...task, preparation_status: 'SUCCEEDED' }], next_cursor: null }))
  assert.throws(() => parseTaskList({ tasks: [{ ...task, requested_fields: null }], next_cursor: null }))
  assert.throws(() => parseTaskList({ tasks: [{ ...task, current_run_state: 'provider-secret' }], next_cursor: null }))
})
test('actual contract must belong to the selected task and saved version', () => {
  assert.equal(parseTaskDetail(detail).contract?.action_policy.mode, 'read_only')
  assert.throws(() => parseTaskDetail({ ...detail, contract: { ...contract, task_id: 'other-task' } }))
  assert.throws(() => parseTaskDetail({ ...detail, contract: { ...contract, contract_version: 2 } }))
  assert.throws(() => parseTaskDetail({ ...detail, contract_history: [{ ...contract, task_id: 'other-task' }] }))
})
test('missing-field and active-run associations cannot drift silently', () => {
  assert.throws(() => parseTaskDetail({ ...detail, missing_fields: ['parameters.currency'] }))
  assert.throws(() => parseTaskDetail({ ...detail, current_run: { run_id: 'foreign-run', state: 'RUNNING', state_version: 0, contract_version: 1 } }))
  assert.throws(() => parseTaskDetail({ ...detail, task: { ...task, current_run_id: 'run-missing' } }))
})
test('pending revision can show its prior contract without claiming latest readiness', () => {
  const pending = { ...detail, contract_version: 2, task: { ...task, preparation_status: 'NEEDS_INPUT', requested_fields: ['parameters.currency'] },
    missing_fields: ['parameters.currency'] }
  assert.equal(parseTaskDetail(pending).contract?.contract_version, 1)
  assert.throws(() => parseTaskDetail({ ...pending, task: { ...pending.task, preparation_status: 'READY' } }))
})
test('repository permissions accept only the frozen write-operation vocabulary', () => {
  const policy = { mode: 'repository_write', repository: 'owner/repo', base_branch: 'main', branch: 'repair',
    base_sha: 'a'.repeat(40), task_kind: 'ordinary_repair', allowed_files: ['src/app.py'], workflow_exception_files: [],
    protected_patterns: ['tests/*'], required_checks: ['unit'], independent_rules_ref: 'rules', allowed_operations: ['edit_file'] }
  assert.equal(isPolicy(policy), true)
  assert.equal(isPolicy({ ...policy, allowed_operations: ['merge'] }), false)
  assert.equal(isPolicy({ ...policy, allowed_operations: [] }), false)
})
test('historical identity metadata must still require all current execution checks', () => {
  assert.equal(parseIdentities([identity]).length, 1)
  assert.equal(parseIdentities([{ ...identity, realm: 'webarena' }])[0].realm, 'webarena')
  for (const flag of ['requires_recheck', 'requires_identity_check', 'requires_business_check']) {
    assert.throws(() => parseIdentities([{ ...identity, [flag]: false }]))
  }
})
test('identity guard rejects credential URLs, raw multiline metadata and invalid versions', () => {
  assert.throws(() => parseIdentities([{ ...identity, origin: 'https://token@github.com' }]))
  assert.throws(() => parseIdentities([{ ...identity, normalized_account: 'alice\nsecret' }]))
  assert.throws(() => parseIdentities([{ ...identity, state_version: 9007199254740992 }]))
})
test('persisted draft source authorization survives missing start URLs without broadening source scope', () => {
  const draft = { sources: contract.sources, start_urls: null }
  assert.deepEqual(parseSourceScopes(draft.sources), contract.sources)
  for (const badSource of [
    { ...contract.sources[0], origin: 'https://token@example.com' },
    { ...contract.sources[0], origin: 'https://example.com/private' },
    { ...contract.sources[0], path_prefix: '/reports/../private' },
    { ...contract.sources[0], path_prefix: '/reports/%2fprivate' },
    { ...contract.sources[0], path_prefix: '/reports//private' },
  ]) assert.throws(() => parseSourceScopes([badSource]))
  assert.throws(() => parseSourceScopes([contract.sources[0], contract.sources[0]]))
})
