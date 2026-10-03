import assert from 'node:assert/strict'
import test from 'node:test'

// A URL import allows Node's built-in TypeScript stripping without a loader or
// frontend test dependency. Type-check the API through its extensionless name.
const contracts: typeof import('./identity-contracts') = await import(new URL('./identity-contracts.ts', import.meta.url).href)
const { isIdentitySites, isPublicIdentities, isLoginSession, isLoginId, loginReason } = contracts

const now = '2026-10-02T08:00:00.000000Z'
const site = { site_id: 'github', realm: 'public', login_url: 'https://github.com/login?return_to=%2Fsettings%2Fprofile',
  verification_url: 'https://github.com/settings/profile', adapter_id: 'github-auth-v1' }
const identity = { identity_ref: 'identity-1', site_id: 'github', realm: 'public', origin: 'https://github.com',
  normalized_account: 'alice', state: 'VERIFIED', state_version: 2, created_at: now, updated_at: now,
  requires_recheck: true, requires_identity_check: true, requires_business_check: true }
const login = { login_session_id: 'login-1', site_id: 'github', realm: 'public', origin: 'https://github.com',
  expected_account: 'alice', expected_identity_ref: null, state: 'AWAITING_USER', state_version: 1,
  identity_ref: null, reason: null, capture_blocked: true, created_at: now, updated_at: now }

test('public site metadata accepts its configured URLs and rejects credential-bearing or executable addresses', () => {
  assert.equal(isIdentitySites([site]), true)
  assert.equal(isIdentitySites([{ ...site, login_url: 'javascript:alert(1)' }]), false)
  assert.equal(isIdentitySites([{ ...site, verification_url: 'https://user:pass@github.com/settings/profile' }]), false)
  assert.equal(isIdentitySites([{ ...site, realm: 'custom' }]), false)
})

test('historical verification remains subject to every required current-run check', () => {
  assert.equal(isPublicIdentities([identity]), true)
  assert.equal(isPublicIdentities([{ ...identity, state: 'NEEDS_LOGIN' }]), true)
  for (const flag of ['requires_recheck', 'requires_identity_check', 'requires_business_check']) {
    assert.equal(isPublicIdentities([{ ...identity, [flag]: false }]), false)
  }
  assert.equal(isPublicIdentities([{ ...identity, state: 'READY' }]), false)
  assert.equal(isPublicIdentities([{ ...identity, state_version: 9007199254740992 }]), false)
})

test('HTTP acceptance and pending login records cannot publish a verified identity', () => {
  assert.equal(isLoginSession(login), true)
  assert.equal(isLoginSession({ ...login, state: 'NEEDS_LOGIN', reason: 'wrong_account' }), true)
  assert.equal(isLoginSession({ ...login, state: 'VERIFIED', identity_ref: 'identity-1' }), true)
  assert.equal(isLoginSession({ ...login, state: 'VERIFIED' }), false)
  assert.equal(isLoginSession({ ...login, identity_ref: 'identity-1' }), false)
  assert.equal(isLoginSession({ ...login, state: 'SUCCESS' }), false)
})

test('login response never admits capture permission, credential URL or multiline account metadata', () => {
  assert.equal(isLoginSession({ ...login, capture_blocked: false }), false)
  assert.equal(isLoginSession({ ...login, origin: 'https://secret@github.com' }), false)
  assert.equal(isLoginSession({ ...login, expected_account: 'alice\nsecret' }), false)
})

test('lookup keys are bounded opaque IDs, never URLs or file paths', () => {
  assert.equal(isLoginId('03574c4b-2861-4ad0-9cd6-318ff40f0e12'), true)
  assert.equal(isLoginId('../secret'), false)
  assert.equal(isLoginId('https://github.com'), false)
})

test('wrong-account state is explicit and unrecognized reasons never echo raw input', () => {
  assert.equal(loginReason('wrong_account'), loginReason('account_mismatch'))
  assert.equal(loginReason('api-secret-synthetic'), '身份核验未通过，请查询最新状态并重新核对。')
  assert.equal(loginReason(null), null)
})
