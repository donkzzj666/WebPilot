import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import test from 'node:test'

// Resolve browser extensionless TS imports for Node's own type stripping.
// This hook is confined to local frontend source modules, without a loader dependency.
registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/frontend/src/') && specifier.startsWith('.') && !/\.[a-z]+$/.test(specifier)) {
    return nextResolve(specifier + '.ts', context)
  }
  return nextResolve(specifier, context)
} })
const client: typeof import('./client') = await import(new URL('./client.ts', import.meta.url).href)
const inputs: typeof import('./inputs') = await import(new URL('./inputs.ts', import.meta.url).href)
const types: typeof import('./types') = await import(new URL('./types.ts', import.meta.url).href)
const now = '2026-10-02T08:00:00Z'
const task = { task_id: 'task-B', original_instruction: '读取 ACME', preparation_status: 'NEEDS_INPUT',
  current_contract_version: null, current_run_id: null, state_version: 1, created_at: now, requested_fields: ['parameters.report_version'] }
const detail = { task, contract_version: 1, contract: null, draft: null, missing_fields: task.requested_fields,
  current_run: null, historical_runs: [], contract_history: [] }
const originalFetch = globalThis.fetch

test('GET detail must return its requested task, even when the wrong object is internally consistent', async () => {
  globalThis.fetch = async () => Response.json(detail)
  try {
    await assert.rejects(client.getTask('task-A'), (error: unknown) => error instanceof client.TaskApiError && error.code === 'INVALID_RESPONSE')
    assert.equal((await client.getTask('task-B')).task.task_id, 'task-B')
  } finally { globalThis.fetch = originalFetch }
})
test('revision and clarification receipts must match the exact mutation route', async () => {
  globalThis.fetch = async () => Response.json(detail)
  try {
    for (const operation of ['revisions', 'clarifications']) {
      await assert.rejects(client.postTask(`/api/v1/tasks/task-A/${operation}`, {}, 'test-key'),
        (error: unknown) => error instanceof client.TaskApiError && error.code === 'INVALID_RESPONSE')
      assert.equal((await client.postTask(`/api/v1/tasks/task-B/${operation}`, {}, 'test-key')).task.task_id, 'task-B')
    }
    assert.equal((await client.postTask('/api/v1/tasks', {}, 'create-key')).task.task_id, 'task-B')
  } finally { globalThis.fetch = originalFetch }
})
test('same in-memory request reuses its key; changed body, version or route gets a fresh key', () => {
  const body = { contract_version: 1, values: { 'parameters.report_version': '2025' } }
  const prior = client.submissionKey(null, '/api/v1/tasks/task-A/clarifications', body)
  assert.equal(client.submissionKey(prior, '/api/v1/tasks/task-A/clarifications', body).key, prior.key)
  assert.notEqual(client.submissionKey(prior, '/api/v1/tasks/task-A/clarifications', { ...body, contract_version: 2 }).key, prior.key)
  assert.notEqual(client.submissionKey(prior, '/api/v1/tasks/task-B/clarifications', body).key, prior.key)
  assert.notEqual(client.submissionKey(prior, '/api/v1/tasks/task-A/clarifications', { ...body, values: { 'parameters.report_version': '2026' } }).key, prior.key)
})
test('task requests include the local client marker and idempotency key without Authorization', async () => {
  let observed: Headers | null = null
  globalThis.fetch = async (_url, init) => { observed = new Headers(init?.headers); return Response.json(detail) }
  try {
    await client.postTask('/api/v1/tasks', {}, 'test-key')
    assert.equal((observed as Headers | null)?.get('X-WebPilot-Client'), '1')
    assert.equal((observed as Headers | null)?.get('Authorization'), null)
    assert.equal((observed as Headers | null)?.get('Idempotency-Key'), 'test-key')
  } finally { globalThis.fetch = originalFetch }
})
test('source and write authority require explicit confirmation; historical realm mismatch cannot bind an account', () => {
  const rows = [{ sourceId: 'github-source', siteId: 'github', origin: 'https://github.com', path: '/', startUrl: 'https://github.com/owner/repo' }]
  const input = { instruction: '读取仓库', scenario: '' as const, rows, authorized: false, permission: 'read_only' as const,
    write: { ...inputs.EMPTY_WRITE }, writeAuthorized: false, identityRef: '', identities: [] }
  assert.throws(() => inputs.createRequest(input))
  assert.equal(inputs.createRequest({ ...input, authorized: true }).action_policy.mode, 'read_only')
  assert.throws(() => inputs.writePolicy(inputs.EMPTY_WRITE, false))
  const identity = { identity_ref: 'identity-one', site_id: 'github', realm: 'webarena' as const, origin: 'https://github.com',
    normalized_account: 'alice', state: 'VERIFIED' as const, state_version: 0, created_at: now, updated_at: now,
    requires_recheck: true as const, requires_identity_check: true, requires_business_check: true }
  assert.equal(inputs.matchingIdentity(identity, inputs.sourceValues(rows).sources), false)
  assert.throws(() => inputs.createRequest({ ...input, authorized: true, identityRef: identity.identity_ref, identities: [identity] }))
})
test('account matching reads committed draft scopes independently from still-missing start URLs', () => {
  const draft = { sources: [{ source_id: 'github-source', site_id: 'github', origin: 'https://github.com', path_prefix: '/owner' }], start_urls: null }
  const identity = { identity_ref: 'identity-one', site_id: 'github', realm: 'public' as const, origin: 'https://github.com',
    normalized_account: 'alice', state: 'VERIFIED' as const, state_version: 1, created_at: now, updated_at: now,
    requires_recheck: true as const, requires_identity_check: true, requires_business_check: true }
  assert.equal(inputs.matchingIdentity(identity, types.parseSourceScopes(draft.sources)), true)
  assert.equal(inputs.matchingIdentity({ ...identity, site_id: 'other-site' }, types.parseSourceScopes(draft.sources)), false)
  assert.equal(inputs.matchingIdentity({ ...identity, realm: 'webarena' }, types.parseSourceScopes(draft.sources)), false)
})
