"""Identity publication uses real SQLite; no websites, passwords or Keychain."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from uuid import uuid4

import pytest

from webagent.db import connect, migrate, transaction
from webagent.errors import BusinessError
from webagent.identities.models import FAILURE_REASONS
from webagent.identities.store import IdentityStore
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry
from conftest import seed


def create(store, **values):
    return store.create(**dict(site_id='synthetic-git', realm='public', origin='https://git.example',
                              expected_account='alice') | values)


def opened(path, store, login=None, manager='manager-one'):
    login = login or create(store)
    registry = SessionRegistry(path)
    context = registry.reserve(manager, SessionOwner('login', login.login_id, login.site_id,
        login.expected_identity_ref, login.realm), auth_ref=login.restore_auth_ref)
    registry.opened(context.session_id, manager)
    return store.attach_session(login.login_id, login.state_version, context.session_id, manager)


def candidate(path, store, login=None, normalized_account='alice'):
    login = login or opened(path, store)
    confirming = store.begin_confirm(login.login_id, login.state_version)
    return store.identity_candidate(login.login_id, confirming.state_version, normalized_account)


def publish(store, login, **overrides):
    args = dict(identity_ref=login.candidate_identity_ref, normalized_account=login.candidate_account,
        auth_ref=str(uuid4()), auth_sha256='a' * 64, verification_origin=login.origin,
        adapter_id='synthetic-adapter-v1', evidence_sha256='e' * 64)
    return store.finalize_verified(login.login_id, login.state_version, **(args | overrides))


def count(path, table):
    assert table in ('identities', 'identity_verifications', 'browser_auth_snapshots', 'runs', 'identity_login_events')
    with connect(path) as db:
        return db.execute('SELECT count(*) FROM ' + table).fetchone()[0]


def history(path, login_id):
    with connect(path) as db:
        return [dict(row) for row in db.execute('SELECT * FROM identity_login_events WHERE login_id=? ORDER BY event_id', (login_id,))]


def test_creation_confirmation_and_candidate_do_not_publish_identity(database):
    store = IdentityStore(database)
    login = create(store)
    assert login.state == 'OPENING' and login.state_version == 0
    assert login.identity_ref is None and login.candidate_identity_ref is None
    login = opened(database, store, login)
    assert login.state == 'AWAITING_USER' and login.state_version == 1
    login = candidate(database, store, login)
    assert login.state == 'VERIFYING' and login.state_version == 3 and login.candidate_identity_ref
    assert store.list_identities() == []
    assert count(database, 'identities') == count(database, 'identity_verifications') == 0
    assert count(database, 'browser_auth_snapshots') == count(database, 'runs') == 0
    assert [event['state'] for event in history(database, login.login_id)] == [
        'OPENING', 'AWAITING_USER', 'VERIFYING', 'VERIFYING']
    assert [event['state_version'] for event in history(database, login.login_id)] == list(range(4))


def test_success_publishes_one_scoped_identity_auth_receipt_and_immutable_history(database):
    store = IdentityStore(database)
    proposed = candidate(database, store)
    verified = publish(store, proposed)
    assert verified.state == 'VERIFIED' and verified.state_version == 4
    identity = store.get_identity(verified.identity_ref)
    assert identity.identity_ref == proposed.candidate_identity_ref
    assert identity.normalized_account == 'alice' and identity.state == 'VERIFIED'
    assert identity.origin == 'https://git.example'
    assert identity.requires_identity_check and identity.requires_business_check
    assert verified.auth_ref == identity.auth_ref and identity.last_verification_id == verified.verification_id
    assert count(database, 'identities') == count(database, 'identity_verifications') == count(database, 'browser_auth_snapshots') == 1
    with connect(database) as db:
        session = db.execute('SELECT * FROM browser_sessions WHERE session_id=?', (verified.session_id,)).fetchone()
        assert session['identity_ref'] is None and session['auth_ref'] is None
        assert session['requires_identity_check'] == session['requires_business_check'] == 1
        verification = dict(db.execute('SELECT * FROM identity_verifications').fetchone())
        assert verification['evidence_sha256'] == 'e' * 64
        assert verification['normalized_account'] == 'alice'
        assert set(verification) == {'verification_id', 'login_id', 'login_state_version', 'identity_ref',
            'session_id', 'site_id', 'realm', 'normalized_account', 'verification_origin', 'adapter_id',
            'evidence_sha256', 'auth_ref', 'verified_at'}
    assert IdentityStore(database).get(verified.login_id) == verified
    assert store.list_identities() == [identity]


@pytest.mark.parametrize('reason', sorted(FAILURE_REASONS))
def test_failed_verification_never_creates_identity_or_auth(database, reason):
    store = IdentityStore(database)
    proposed = candidate(database, store)
    failed = store.fail_confirm(proposed.login_id, proposed.state_version, reason)
    assert failed.state == 'NEEDS_LOGIN' and failed.reason == reason
    assert failed.identity_ref is None and failed.candidate_identity_ref is None
    assert count(database, 'identities') == count(database, 'identity_verifications') == count(database, 'browser_auth_snapshots') == 0
    with pytest.raises(BusinessError) as rejected:
        publish(store, proposed)
    assert rejected.value.code == 'STATE_CONFLICT'


def test_unknown_failure_content_is_never_saved_or_reflected(database):
    secret = 'synthetic-password=SECRET otp=123456 Cookie: SECRET'
    store = IdentityStore(database)
    login = create(store)
    failed = store.fail_confirm(login.login_id, login.state_version, secret)
    assert failed.state == 'FAILED' and failed.reason == 'unknown'
    assert secret not in json.dumps(failed.as_dict()) + repr(history(database, login.login_id))
    with connect(database) as db:
        dump = '\n'.join(db.iterdump())
    assert secret not in dump and 'SECRET' not in dump


@pytest.mark.parametrize('phase', ['begin', 'candidate', 'publish', 'fail', 'close'])
def test_cas_rejects_stale_versions_without_partial_writes(database, phase):
    store = IdentityStore(database)
    login = opened(database, store)
    if phase != 'begin':
        login = candidate(database, store, login)
    before = len(history(database, login.login_id))
    with pytest.raises(BusinessError) as rejected:
        if phase == 'begin':
            store.begin_confirm(login.login_id, login.state_version - 1)
        elif phase == 'candidate':
            store.identity_candidate(login.login_id, login.state_version - 1, 'alice')
        elif phase == 'publish':
            store.finalize_verified(login.login_id, login.state_version - 1,
                identity_ref=login.candidate_identity_ref, normalized_account='alice', auth_ref=str(uuid4()),
                auth_sha256='a' * 64, verification_origin=login.origin, adapter_id='test', evidence_sha256='b' * 64)
        elif phase == 'fail':
            store.fail_confirm(login.login_id, login.state_version - 1, 'not_logged_in')
        else:
            store.close(login.login_id, login.state_version - 1)
    assert rejected.value.status == 409 and rejected.value.current_state_version == login.state_version
    assert len(history(database, login.login_id)) == before
    assert count(database, 'identities') == count(database, 'browser_auth_snapshots') == 0


def test_two_confirmers_only_one_can_claim_verification(database):
    store = IdentityStore(database)
    login = opened(database, store)
    def claim(_):
        try:
            return IdentityStore(database).begin_confirm(login.login_id, login.state_version)
        except BusinessError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(not isinstance(item, BusinessError) for item in results) == 1
    assert sum(isinstance(item, BusinessError) and item.code == 'STATE_CONFLICT' for item in results) == 1
    assert len(history(database, login.login_id)) == 3


def test_two_same_account_candidates_cannot_publish_mismatched_encrypted_bindings(database):
    store = IdentityStore(database)
    proposals = [candidate(database, store), candidate(database, store)]
    assert proposals[0].candidate_identity_ref != proposals[1].candidate_identity_ref
    def finalize(login):
        try:
            return publish(IdentityStore(database), login)
        except BusinessError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finalize, proposals))
    verified = [item for item in results if not isinstance(item, BusinessError)]
    assert len(verified) == 1
    assert count(database, 'identities') == count(database, 'identity_verifications') == count(database, 'browser_auth_snapshots') == 1
    loser = next(item for item in proposals if item.candidate_identity_ref != verified[0].identity_ref)
    # The stale unpublished candidate can be closed, and can be explicitly
    # re-resolved then exported under the winning canonical identity.
    updated = store.identity_candidate(loser.login_id, loser.state_version, 'alice')
    assert updated.candidate_identity_ref == verified[0].identity_ref
    assert publish(store, updated).identity_ref == verified[0].identity_ref
    assert count(database, 'identities') == 1 and count(database, 'identity_verifications') == 2


@pytest.mark.parametrize('change', ['origin', 'account', 'identity', 'session_lost', 'session_closing'])
def test_publish_rechecks_every_binding_and_live_session(database, change):
    store = IdentityStore(database)
    proposal = candidate(database, store)
    overrides = {}
    if change == 'origin': overrides['verification_origin'] = 'https://evil.example'
    if change == 'account': overrides['normalized_account'] = 'bob'
    if change == 'identity': overrides['identity_ref'] = 'identity-other'
    if change == 'session_lost': SessionRegistry(database).lost(proposal.session_id, proposal.manager_id, 'window_closed')
    if change == 'session_closing': SessionRegistry(database).closing(proposal.session_id, proposal.manager_id)
    with pytest.raises(BusinessError) as rejected:
        publish(store, proposal, **overrides)
    assert rejected.value.code == 'STATE_CONFLICT'
    assert count(database, 'identities') == count(database, 'identity_verifications') == count(database, 'browser_auth_snapshots') == 0


@pytest.mark.parametrize('dimension', ['owner_id', 'kind', 'site_id', 'realm', 'identity_ref', 'manager'])
def test_attach_does_not_accept_other_session_ownership(database, dimension):
    store = IdentityStore(database)
    login = create(store)
    values = dict(kind='login', owner_id=login.login_id, site_id=login.site_id, realm=login.realm)
    if dimension == 'owner_id': values['owner_id'] = 'other-login'
    if dimension == 'kind': values['kind'] = 'verification'
    if dimension == 'site_id': values['site_id'] = 'other-site'
    if dimension == 'realm': values['realm'] = 'webarena'
    if dimension == 'identity_ref': values['identity_ref'] = 'pretend-identity'
    registry = SessionRegistry(database)
    session = registry.reserve('manager', SessionOwner(**values))
    registry.opened(session.session_id, 'manager')
    with pytest.raises(BusinessError):
        store.attach_session(login.login_id, 0, session.session_id, 'wrong-manager' if dimension == 'manager' else 'manager')
    assert store.get(login.login_id).session_id is None


def test_restore_is_bound_to_original_account_and_snapshot_and_requires_confirmation(database):
    store = IdentityStore(database)
    original = publish(store, candidate(database, store))
    restore = create(store, expected_account=None, expected_identity_ref=original.identity_ref)
    assert restore.expected_account == 'alice' and restore.restore_auth_ref == original.auth_ref
    restore = opened(database, store, restore)
    assert restore.state == 'AWAITING_USER' and restore.identity_ref is None
    verifying = store.begin_confirm(restore.login_id, restore.state_version)
    with pytest.raises(BusinessError):
        store.identity_candidate(restore.login_id, verifying.state_version, 'bob')
    failed = store.fail_confirm(verifying.login_id, verifying.state_version, 'account_mismatch')
    assert failed.identity_ref is None
    assert store.get_identity(original.identity_ref).state == 'NEEDS_LOGIN'
    assert count(database, 'identities') == count(database, 'identity_verifications') == 1
    retry = candidate(database, store, failed)
    assert retry.candidate_identity_ref == original.identity_ref
    renewed = publish(store, retry)
    identity = store.get_identity(original.identity_ref)
    assert identity.state == 'VERIFIED' and renewed.auth_ref != original.auth_ref
    assert count(database, 'identities') == 1 and count(database, 'identity_verifications') == 2


@pytest.mark.parametrize('changed', [dict(site_id='other-site'), dict(realm='webarena'),
                                    dict(origin='https://other.example'), dict(expected_account='bob')])
def test_restore_rejects_wrong_scope_before_creating_login(database, changed):
    store = IdentityStore(database)
    verified = publish(store, candidate(database, store))
    with pytest.raises(BusinessError):
        create(store, **(dict(expected_identity_ref=verified.identity_ref) | changed))
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM login_requests').fetchone()[0] == 1


def test_old_restore_failure_cannot_invalidate_a_newer_success(database):
    store = IdentityStore(database)
    first = publish(store, candidate(database, store))
    old_restore = opened(database, store, create(store, expected_identity_ref=first.identity_ref))
    newer = publish(store, candidate(database, store))
    assert newer.identity_ref == first.identity_ref and newer.auth_ref != first.auth_ref
    store.fail_confirm(old_restore.login_id, old_restore.state_version, 'not_authenticated')
    assert store.get_identity(first.identity_ref).state == 'VERIFIED'
    assert store.get_identity(first.identity_ref).auth_ref == newer.auth_ref


def test_restore_auth_failure_before_browser_attaches_invalidates_only_its_frozen_snapshot(database):
    store = IdentityStore(database)
    first = publish(store, candidate(database, store))
    restore = create(store, expected_identity_ref=first.identity_ref)
    failed = store.fail_confirm(restore.login_id, restore.state_version, 'auth_unavailable')
    assert failed.state == 'FAILED' and failed.session_id is None
    assert store.get_identity(first.identity_ref).state == 'NEEDS_LOGIN'
    assert store.get(first.login_id).state == 'VERIFIED'


@pytest.mark.parametrize('state', ['CLOSED', 'LOST'])
def test_window_close_or_loss_retains_verified_history_but_login_is_no_longer_ready(database, state):
    store = IdentityStore(database)
    verified = publish(store, candidate(database, store))
    registry = SessionRegistry(database)
    if state == 'CLOSED':
        registry.closing(verified.session_id, verified.manager_id)
        registry.closed(verified.session_id, verified.manager_id)
    else:
        registry.lost(verified.session_id, verified.manager_id, 'window_closed')
    ended = store.reconcile(verified.login_id)
    assert ended.state == state and ended.identity_ref == verified.identity_ref
    assert ended.as_dict()['identity_ref'] is None
    assert store.get_identity(verified.identity_ref).state == 'VERIFIED'
    assert store.reconcile(verified.login_id) == ended
    assert count(database, 'identity_verifications') == 1


def test_manager_restart_orphans_unattached_and_old_manager_requests(database):
    store = IdentityStore(database)
    unattached = create(store)
    prior = opened(database, store)
    current = opened(database, store, manager='manager-two')
    recovered = store.recover_orphans('manager-two')
    assert {item.login_id for item in recovered} == {unattached.login_id, prior.login_id}
    assert all(item.state == 'LOST' and item.reason == 'manager_restarted' for item in recovered)
    assert store.get(current.login_id) == current
    assert store.recover_orphans('manager-two') == []


def test_public_dtos_never_expose_unpublished_identity_or_auth_receipt(database):
    store = IdentityStore(database)
    proposal = candidate(database, store)
    public = proposal.as_dict()
    assert public['identity_ref'] is None and public['capture_blocked'] is True
    assert proposal.candidate_identity_ref not in json.dumps(public)
    assert not {'candidate_identity_ref', 'candidate_account', 'auth_ref', 'auth_sha256',
                'restore_auth_ref', 'restore_auth_sha256', 'manager_id'} & public.keys()
    verified = publish(store, proposal)
    identity_public = store.get_identity(verified.identity_ref).as_dict()
    assert identity_public['requires_recheck'] is True
    assert verified.auth_ref not in json.dumps(identity_public) + json.dumps(verified.as_dict())
    assert verified.auth_sha256 not in json.dumps(identity_public) + json.dumps(verified.as_dict())


@pytest.mark.parametrize('table', ['identities', 'identity_verifications', 'identity_login_events', 'login_requests'])
def test_sql_cannot_delete_history_or_replace_identity_accounts(database, table):
    store = IdentityStore(database)
    publish(store, candidate(database, store))
    with connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM ' + table)
        if table == 'identities':
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE identities SET normalized_account='bob',state_version=state_version+1")
        elif table == 'identity_verifications':
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE identity_verifications SET evidence_sha256=?", ('b' * 64,))
        elif table == 'identity_login_events':
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE identity_login_events SET state='VERIFIED'")
        else:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE login_requests SET expected_account='bob',state_version=state_version+1")


def test_failure_after_auth_metadata_rolls_back_identity_history_state_and_audit(database):
    store = IdentityStore(database)
    proposal = candidate(database, store)
    with connect(database) as db:
        db.execute("CREATE TRIGGER synthetic_abort BEFORE INSERT ON identity_verifications BEGIN SELECT RAISE(ABORT,'synthetic fault'); END")
    with pytest.raises(sqlite3.IntegrityError):
        publish(store, proposal)
    assert store.get(proposal.login_id) == proposal
    assert count(database, 'identities') == count(database, 'identity_verifications') == count(database, 'browser_auth_snapshots') == 0
    assert len(history(database, proposal.login_id)) == 4


def test_schema_eight_upgrade_preserves_tasks_and_browser_history(tmp_path):
    path = tmp_path / 'upgrade.sqlite3'
    migrate(path, target=8)
    with connect(path) as db, transaction(db):
        seed(db)
    registry = SessionRegistry(path)
    owner = SessionOwner('verification', 'fixture', 'synthetic')
    old_session = registry.reserve('old-manager', owner)
    registry.opened(old_session.session_id, 'old-manager')
    with connect(path) as db:
        old = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
               for table in ('tasks', 'contracts', 'runs', 'browser_sessions', 'browser_session_events')}
    result = migrate(path, target=9)
    assert result == {'previous_version': 8, 'schema_version': 9, 'applied': 1}
    with connect(path) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
        for table, expected in old.items():
            assert [tuple(row) for row in db.execute('SELECT * FROM ' + table)] == expected
    assert IdentityStore(path).list_identities() == []
    assert migrate(path, target=9)['applied'] == 0


@pytest.mark.parametrize('value', [True, -1, '0', 0.0, None])
def test_versions_are_strict_integers(database, value):
    store = IdentityStore(database)
    login = opened(database, store)
    with pytest.raises(BusinessError) as error:
        store.begin_confirm(login.login_id, value)
    assert error.value.code == 'INVALID_PARAMETER'


@pytest.mark.parametrize('change', [dict(origin='https://user:secret@git.example'),
    dict(origin='https://git.example/path?secret=1'), dict(expected_account='\npassword'),
    dict(expected_account=' alice '), dict(realm='private'), dict(site_id='')])
def test_invalid_metadata_rejected_with_static_errors(database, change):
    with pytest.raises(BusinessError) as error:
        create(IdentityStore(database), **change)
    assert error.value.code == 'INVALID_PARAMETER'
    assert 'secret' not in str(error.value) and 'password' not in str(error.value)
