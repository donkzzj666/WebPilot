"""Recovery reads have current SQLite authority and independent original proof."""
import asyncio
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from webagent.db import connect, transaction
from webagent.errors import BusinessError
from webagent.gateway.service import BrowserGateway
from webagent.graph.recovery import RecoveryStore
from webagent.graph.recovery_state import load_saved_graph
from webagent.graph.models import GraphSnapshot
from webagent.sessions.models import SessionOwner
from unit.test_gateway_service import FakeBrowser, action
from unit.test_graph_executor import prepared, injected, current, Manager
from unit.test_session_manager import setup as managed_setup
from unit.test_verification_rules import setup
from unit.test_gateway_browser import backend, TOKEN, URL, OWNER, SCOPE, action as native_action
from webagent.gateway.browser import BrowserBackend
from dataclasses import asdict


class Saver:
    def __init__(self, values=None):
        self.values = values

    async def aget_tuple(self, config):
        if self.values is None:
            return None
        return SimpleNamespace(checkpoint={'channel_values': self.values})


class RecoveryBrowser(FakeBrowser):
    def __init__(self, owner, session, *, url, content=None, capture_hook=None):
        super().__init__(owner, session)
        self.url, self.content = url, content or setup()[2][0].content
        self.capture_hook = capture_hook
        self.navigated, self.closed = [], 0

    async def capture(self, token, **kwargs):
        if self.capture_hook:
            await self.capture_hook(token)
        value = await super().capture(token, **kwargs)
        body = json.dumps(self.content)
        value.update(visible_text=body, visible_sha256=hashlib.sha256(body.encode()).hexdigest())
        return value

    async def recovery_capture(self, token):
        return await self.capture(token)

    async def recovery_navigate(self, token, url):
        self.navigated.append(url)
        self.url = url
        return {'source_url': url, 'http_status': 200}

    async def aclose(self):
        self.closed += 1


def recovering(case):
    generation = case[1].start_worker('worker-1')
    return case[1].claim('worker-1', generation)


def live_session(case):
    owner = SessionOwner('run', 'run-1', 'local-fixture')
    return asyncio.run(case[3].create(owner, auth_ref=None, replaces=None, execution_token=case[2]))


def click_action(token, snapshot, *, step='step-one'):
    value = action(token, snapshot).model_dump(mode='json')
    value.update(action_type='click', step_id=step, args={})
    value['target']['locator'] = {'strategy': 'semantic', 'role': 'link',
                                 'accessible_name': 'Inspect', 'label': None}
    return value


def recovery_executor(case, *, url=None, content=None, capture_hook=None, **overrides):
    browsers = []
    def gateway(manager, session, *, scheduler):
        browser = RecoveryBrowser(session.owner, session, url=url or setup()[0].start_urls[0],
                                  content=content, capture_hook=capture_hook)
        browsers.append(browser)
        return BrowserGateway(case[0].business_db, browser, scheduler=scheduler)
    executor, provider, calls = injected(case, gateway_factory=gateway, **overrides)
    executor.checkpointer = Saver()
    return executor, provider, calls, browsers


def test_live_surface_proof_precedes_model_and_reuses_exact_context(tmp_path):
    case = prepared(tmp_path)
    session = live_session(case)
    token = recovering(case)
    executor, provider, calls, browsers = recovery_executor(case)
    result = asyncio.run(executor(token))
    assert result['state'] == 'PAUSED'
    assert len(case[3].created) == 1 and not case[3].closed
    assert browsers[0].session_id == session.session_id and not browsers[0].navigated
    assert browsers[0].capture_calls == 1 and browsers[0].execute_calls == 0
    fresh = next(c[1] for c in calls if c[0] == 'run')
    assert fresh.epoch == token.epoch and fresh.state_version == token.state_version + 1
    assert provider.closed == browsers[0].closed == 1
    with connect(case[0].business_db) as db:
        assert [r[0] for r in db.execute('SELECT phase FROM graph_recoveries ORDER BY recovery_seq')] == ['BEGIN', 'COMPLETE']
        complete = json.loads(db.execute("SELECT facts_json FROM graph_recoveries WHERE phase='COMPLETE'").fetchone()[0])
        assert complete['object_id'] == 'company' and complete['object_version'] == '2025'
        assert complete['proof_evidence_ids'] and complete['proof_bindings']
        assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0


def test_lost_old_manager_context_rebuilds_with_current_scope_and_new_get(tmp_path):
    case = prepared(tmp_path)
    old = live_session(case)
    case[3].registry.lost(old.session_id, old.manager_id, 'manager_restarted')
    manager = Manager(case[0])
    case = (*case[:3], manager, case[4])
    token = recovering(case)
    executor, provider, calls, browsers = recovery_executor(case, url='about:blank')
    result = asyncio.run(executor(token))
    assert result['state'] == 'PAUSED' and len(manager.created) == 1
    new = manager.registry.list_owned(manager.manager_id)[0]
    assert new.restored_from_session_id == old.session_id and new.owner == old.owner
    assert new.session_id != old.session_id and new.generation > old.generation
    assert browsers[0].navigated == [setup()[0].start_urls[0]] and browsers[0].execute_calls == 0
    budget = case[1].budgets.status('run-1')
    assert budget['actions_used'] == 2 and budget['content_pages_used'] == 1
    with connect(case[0].business_db) as db:
        assert [r[0] for r in db.execute('SELECT status FROM graph_recovery_navigations ORDER BY navigation_seq')] == ['INTENT', 'COMPLETED']
        assert db.execute('SELECT session_id FROM scheduler_context_reservations').fetchone()[0] == new.session_id
    assert provider.closed == 1


@pytest.mark.parametrize('change,reason', [('object', 'object_mismatch'), ('version', 'object_version_mismatch'),
                                         ('version_without_checkpoint', 'object_version_mismatch')])
def test_changed_actual_object_or_version_blocks_before_provider(tmp_path, change, reason):
    case = prepared(tmp_path)
    live_session(case)
    content = setup()[2][0].content
    if change == 'object':
        content['values'][0]['entity_id'] = 'another-company'
    else:
        # A previous proven version establishes the immutable comparison.
        async def capture_old():
            session = case[3].registry.list_owned(case[3].manager_id)[0]
            old_browser = RecoveryBrowser(session.owner, session, url=setup()[0].start_urls[0])
            gateway = BrowserGateway(case[0].business_db, old_browser, scheduler=case[1])
            snapshot = await gateway.observe(case[2])
            RecoveryStore(tmp_path).checkpoint_facts('run-1', case[2], snapshot['snapshot_id'])
        if change == 'version':
            asyncio.run(capture_old())
        content['values'][0]['report_version'] = '2026'
    token = recovering(case)
    executor, provider, calls, browsers = recovery_executor(case, content=content)
    result = asyncio.run(executor(token))
    assert result['recovery_blocked'] and result['reason'] == reason
    assert not calls and provider.closed == 0 and browsers[0].closed == 1
    assert not browsers[0].navigated and browsers[0].execute_calls == 0
    assert current(case[0])['state'] == 'RECONCILING'
    assert case[1].recovery_blocked_receipt(token)


def test_unproven_preparer_boolean_cannot_grant_reconciliation(tmp_path):
    case = prepared(tmp_path)
    token = recovering(case)
    executor, provider, calls, browsers = recovery_executor(case, recovery_preparer=lambda *args: True)
    result = asyncio.run(executor(token))
    assert result['recovery_blocked'] and result['reason'] == 'proof_missing'
    assert not calls and not provider.closed and not browsers[0].capture_calls
    assert current(case[0])['state'] == 'RECONCILING'


def test_lost_authority_during_recovery_capture_cannot_publish_or_reconcile(tmp_path):
    case = prepared(tmp_path)
    token = recovering(case)
    async def revoke(token):
        case[1].abandon(token)
    executor, provider, calls, browsers = recovery_executor(case, capture_hook=revoke)
    with pytest.raises(BusinessError):
        asyncio.run(executor(token))
    assert not calls and not provider.closed
    with connect(case[0].business_db) as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM graph_recoveries WHERE phase='COMPLETE'").fetchone()[0] == 0


def test_ordinary_actions_remain_closed_during_recovery(tmp_path):
    async def exercise():
        case = prepared(tmp_path)
        owner = SessionOwner('run', 'run-1', 'local-fixture')
        session = await case[3].create(owner, auth_ref=None, replaces=None, execution_token=case[2])
        token = recovering(case)
        browser = RecoveryBrowser(session.owner, session, url=setup()[0].start_urls[0])
        gateway = BrowserGateway(case[0].business_db, browser, scheduler=case[1])
        plan = RecoveryStore(tmp_path).begin(token)
        with pytest.raises(BusinessError):
            await gateway.observe(token)
        snapshot = await gateway.recovery_observe(token, plan['recovery_id'])
        with pytest.raises(BusinessError):
            await gateway.dispatch(token, action(token, snapshot))
        assert browser.capture_calls == 1 and not browser.execute_calls and not browser.prepare_calls
        fresh = case[1].reconcile('run-1', token.state_version)
        with pytest.raises(BusinessError):
            await gateway.recovery_observe(fresh, plan['recovery_id'])
    asyncio.run(exercise())


def test_managed_recovery_context_seam_does_not_open_normal_access(tmp_path):
    async def exercise():
        case = prepared(tmp_path)
        manager, _, _ = managed_setup(tmp_path)
        await manager.start()
        try:
            owner = SessionOwner('run', 'run-1', 'local-fixture')
            session = await manager.create(owner, execution_token=case[2])
            token = recovering(case)
            with pytest.raises(BusinessError):
                await manager.context(session.session_id, owner, execution_token=token)
            context = await manager.recovery_context(session.session_id, owner, execution_token=token)
            assert context is manager._contexts[session.session_id].context
            with pytest.raises(BusinessError):
                await manager.gateway_proxy_credentials(session.session_id, owner, execution_token=token)
            credentials = await manager.recovery_proxy_credentials(session.session_id, owner, execution_token=token)
            assert credentials['server'].startswith('http://127.0.0.1:')
            with pytest.raises(BusinessError):
                await manager.recovery_context(session.session_id, owner,
                                               execution_token=replace(token, epoch=token.epoch-1))
            fresh = case[1].reconcile('run-1', token.state_version)
            with pytest.raises(BusinessError):
                await manager.recovery_context(session.session_id, owner, execution_token=fresh)
            assert await manager.context(session.session_id, owner, execution_token=fresh) is context
        finally:
            await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('values', [{'raw_page': 'sensitive'}, {'branch:to:unknown': None},
                                   {'branch:to:dispatch': {'action': 'old click'}}, {123: None}])
def test_saved_loader_rejects_unknown_or_sensitive_channels(values):
    with pytest.raises(BusinessError) as error:
        asyncio.run(load_saved_graph(Saver(values), 'run-1'))
    assert error.value.field == 'graph_state_invalid'


def test_saved_loader_accepts_only_validated_application_and_known_framework_channels():
    state = GraphSnapshot(run_id='run-1', contract_version=1, state_version=1, business_event_id=1).state()
    values = {**state, 'branch:to:dispatch': None, '__start__': state}
    assert asyncio.run(load_saved_graph(Saver(values), 'run-1')) == state
    assert asyncio.run(load_saved_graph(Saver({'__start__': state}), 'run-1')) == state
    assert asyncio.run(load_saved_graph(Saver(), 'run-1')) is None


@pytest.mark.parametrize('kind', ['model', 'observation', 'screenshot'])
def test_recovery_budget_seam_does_not_broaden_ordinary_kinds(tmp_path, kind):
    case = prepared(tmp_path)
    token = recovering(case)
    with pytest.raises(BusinessError):
        case[1].budgets.consume(token, kind=kind, attempt_id='wrong-kind', allow_reconciling=True)
    assert case[1].budgets.status('run-1')['actions_used'] == 0


def test_recovery_budget_seam_requires_reconciling_and_retains_quota(tmp_path):
    case = prepared(tmp_path)
    with pytest.raises(BusinessError):
        case[1].budgets.consume(case[2], kind='action', attempt_id='running-recovery', allow_reconciling=True)
    token = recovering(case)
    charged = case[1].budgets.consume(token, kind='action', attempt_id='recovery-read', allow_reconciling=True)
    assert charged['dispatch_allowed'] and case[1].budgets.status('run-1')['actions_used'] == 1
    repeated = case[1].budgets.consume(token, kind='action', attempt_id='recovery-read', allow_reconciling=True)
    assert not repeated['dispatch_allowed'] and case[1].budgets.status('run-1')['actions_used'] == 1


def test_native_recovery_is_only_bounded_read_and_get_even_with_old_mutation_allowance():
    async def exercise():
        browser, page, context, managed = backend()
        async def denied(*args, **kwargs):
            raise BusinessError('RESOURCE_CONFLICT', 'Ordinary execution is fenced', status=409)
        async def recovery_context(session_id, owner, *, execution_token):
            assert execution_token == TOKEN
            return context
        managed.context = denied
        managed.recovery_context = recovery_context
        browser._allowed_mutations = frozenset({('POST', URL)})
        capture = await browser.recovery_capture(TOKEN)
        assert capture.page_url == URL and not page.main_frame.calls
        snapshot = {**asdict(capture), 'snapshot_id': 'snapshot', 'source_url': URL}
        with pytest.raises(BusinessError):
            await browser.prepare(TOKEN, native_action(snapshot), snapshot)
        browser._recovery_token = TOKEN
        try:
            assert await browser._request_permitted('GET', URL)
            assert not await browser._request_permitted('POST', URL)
        finally:
            browser._recovery_token = None
        result = await browser.recovery_navigate(TOKEN, 'https://fixture.example/current')
        assert result['source_url'].endswith('/current')
        assert [call[0] for call in page.main_frame.calls] == ['goto']
        assert browser._recovery_token is None
        await browser.aclose()
    asyncio.run(exercise())


def test_replacement_backend_retires_old_closed_guard_only_after_new_guard_installed():
    async def exercise():
        browser, page, context, managed = backend()
        await browser.capture(TOKEN)
        await browser.aclose()
        class Route:
            request = SimpleNamespace(method='GET', url=URL, frame=page.main_frame,
                                      is_navigation_request=lambda: True,
                                      redirected_from=None, resource_type='document')
            def __init__(self):
                self.index, self.aborted, self.allowed = len(context.routes), False, False
            async def abort(self, reason):
                self.aborted = True
            async def fallback(self):
                self.index -= 1
                if self.index < 0:
                    self.allowed = True
                else:
                    await context.routes[self.index][1](self)
        old_request = Route()
        await old_request.fallback()
        assert old_request.aborted and not old_request.allowed
        replacement = BrowserBackend(managed, 'session', OWNER, allowed_sources=(SCOPE,))
        await replacement.capture(TOKEN)
        new_request = Route()
        await new_request.fallback()
        assert new_request.allowed and not new_request.aborted
        await replacement.aclose()
        stopped_request = Route()
        await stopped_request.fallback()
        assert stopped_request.aborted and not stopped_request.allowed
        previous_sessions = len(browser._cdps)
        browser._page_arrived(page)
        await asyncio.sleep(0)
        assert len(browser._cdps) == previous_sessions == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('account', ['alice', 'bob', None])
def test_account_recovery_requires_actual_visible_current_identity(tmp_path, account):
    from webagent.config import Settings
    from webagent.db import migrate
    from webagent.identities.store import IdentityStore
    from webagent.sessions.store import SessionRegistry
    from uuid import uuid4
    path = Settings(tmp_path).business_db
    migrate(path)
    identities = IdentityStore(path)
    login = identities.create(site_id='local-fixture', realm='public',
        origin='http://127.0.0.1:8765', expected_account='alice')
    registry = SessionRegistry(path)
    session = registry.reserve('login-manager', SessionOwner('login', login.login_id, login.site_id,
        login.expected_identity_ref, login.realm), auth_ref=login.restore_auth_ref)
    registry.opened(session.session_id, 'login-manager')
    login = identities.attach_session(login.login_id, login.state_version, session.session_id, 'login-manager')
    login = identities.begin_confirm(login.login_id, login.state_version)
    login = identities.identity_candidate(login.login_id, login.state_version, 'alice')
    identity = identities.finalize_verified(login.login_id, login.state_version,
        identity_ref=login.candidate_identity_ref, normalized_account='alice', auth_ref=str(uuid4()),
        auth_sha256='a'*64, verification_origin=login.origin, adapter_id='owned-test-adapter', evidence_sha256='e'*64)
    registry.closing(session.session_id, 'login-manager')
    registry.closed(session.session_id, 'login-manager')
    case = prepared(tmp_path, identity=identity.identity_ref)
    token = recovering(case)
    content = setup()[2][0].content
    content['recovery_context'] = {'object_id': 'company', 'object_version': '2025',
                                  'identity_ref': identity.identity_ref,
                                  'normalized_account': account}
    executor, provider, calls, browsers = recovery_executor(case, content=content)
    result = asyncio.run(executor(token))
    assert browsers[0].capture_calls == 1
    assert case[3].created[0][1]['auth_ref'] == identity.auth_ref
    if account == 'alice':
        assert result['state'] == 'PAUSED' and provider.closed == 1
        assert any(c[0] == 'model' for c in calls)
    else:
        assert result['recovery_blocked'] and result['reason'] == 'identity_mismatch'
        assert not calls and provider.closed == 0
        assert current(case[0])['state'] == 'RECONCILING'


def test_completed_read_receipt_restores_actual_destination_without_replaying_click(tmp_path):
    case = prepared(tmp_path)
    old = live_session(case)
    destination = setup()[0].start_urls[0] + '/data'
    async def original_click():
        async def clicked(token):
            browser.url = destination
            return {'source_url': destination, 'untrusted_page_field': 'must remain hash-only'}
        browser = RecoveryBrowser(old.owner, old, url=setup()[0].start_urls[0])
        browser.execute_hook = clicked
        gateway = BrowserGateway(case[0].business_db, browser, scheduler=case[1])
        snapshot = await gateway.observe(case[2])
        result = await gateway.dispatch(case[2], click_action(case[2], snapshot))
        assert result['actual_result']['source_url'] == destination
        assert 'untrusted_page_field' not in result['actual_result']
    asyncio.run(original_click())
    case[3].registry.lost(old.session_id, old.manager_id, 'manager_restarted')
    manager = Manager(case[0])
    case = (*case[:3], manager, case[4])
    token = recovering(case)
    assert RecoveryStore(tmp_path).inspect('run-1')['restore_url'] == destination
    executor, _, _, browsers = recovery_executor(case, url='about:blank')
    result = asyncio.run(executor(token))
    assert result['state'] == 'PAUSED' and browsers[0].navigated == [destination]
    assert browsers[0].execute_calls == 0 and case[1].budgets.status('run-1')['actions_used'] == 3
    with connect(case[0].business_db) as db:
        assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 1
        row = db.execute('SELECT status,actual_result_json FROM steps').fetchone()
        assert row['status'] == 'COMPLETED' and json.loads(row['actual_result_json'])['source_url'] == destination


@pytest.mark.parametrize('url', ['https://outside.example/data',
    'http://127.0.0.1:8765/finance?access_token=synthetic-secret'])
def test_completed_read_receipt_does_not_persist_unscoped_or_sensitive_restore_url(tmp_path, url):
    async def exercise():
        case = prepared(tmp_path)
        owner = SessionOwner('run', 'run-1', 'local-fixture')
        session = await case[3].create(owner, auth_ref=None, replaces=None, execution_token=case[2])
        browser = RecoveryBrowser(owner, session, url=setup()[0].start_urls[0])
        async def result(token):
            return {'source_url': url}
        browser.execute_hook = result
        gateway = BrowserGateway(case[0].business_db, browser, scheduler=case[1])
        snapshot = await gateway.observe(case[2])
        completed = await gateway.dispatch(case[2], click_action(case[2], snapshot))
        assert completed['status'] == 'COMPLETED'
        assert set(completed['actual_result']) == {'result_sha256'}
    asyncio.run(exercise())


def test_proven_old_read_intent_remains_history_and_allows_only_fresh_actions(tmp_path):
    case = prepared(tmp_path)
    old = live_session(case)
    async def old_intent():
        browser = RecoveryBrowser(old.owner, old, url=setup()[0].start_urls[0])
        gateway = BrowserGateway(case[0].business_db, browser, scheduler=case[1])
        snapshot = await gateway.observe(case[2])
        binding = {key: snapshot[key] for key in ('session_id', 'manager_id', 'session_generation',
            'tab_id', 'frame_id', 'page_version', 'width', 'height')}
        gateway.store.prepare(case[2], click_action(case[2], snapshot, step='old-uncertain-read'), binding)
    asyncio.run(old_intent())
    token = recovering(case)
    async def graph(data_dir, gateway, model, verifier, **kwargs):
        async def run(fresh):
            snapshot = await gateway.observe(fresh)
            result = await gateway.dispatch(fresh, action(fresh, snapshot, step='fresh-read'))
            assert result['status'] == 'COMPLETED'
            snapshot = await gateway.observe(fresh)
            binding = {key: snapshot[key] for key in ('session_id', 'manager_id', 'session_generation',
                'tab_id', 'frame_id', 'page_version', 'width', 'height')}
            gateway.store.prepare(fresh, action(fresh, snapshot, step='current-uncertain'), binding)
            snapshot = await gateway.observe(fresh)
            binding = {key: snapshot[key] for key in binding}
            with pytest.raises(BusinessError):
                gateway.store.prepare(fresh, action(fresh, snapshot, step='denied-second'), binding)
            return case[1].defer(fresh, 'PAUSED')
        return SimpleNamespace(run=run)
    executor, _, _, browsers = recovery_executor(case, graph_factory=graph)
    result = asyncio.run(executor(token))
    assert result['state'] == 'PAUSED' and browsers[0].executed == ['fresh-read']
    with connect(case[0].business_db) as db:
        old_row = db.execute("SELECT * FROM steps WHERE step_id='old-uncertain-read'").fetchone()
        assert old_row['status'] == 'INTENT' and old_row['ended_at'] is None
        proof = json.loads(db.execute("SELECT facts_json FROM graph_recoveries WHERE phase='COMPLETE'").fetchone()[0])
        assert proof['reconciled_read_step_ids'] == ['old-uncertain-read']
        assert proof['read_reconciliation'] == 'read_observed_no_replay'
        assert db.execute("SELECT count(*) FROM steps WHERE step_id='denied-second'").fetchone()[0] == 0


@pytest.mark.parametrize('stale', [False, True])
def test_human_control_blocks_before_reads_without_clearing_ownership(tmp_path, stale):
    case = prepared(tmp_path)
    token = recovering(case)
    with connect(case[0].business_db) as db, transaction(db):
        db.execute("UPDATE resource_leases SET control_owner='human',state_version=state_version+1 WHERE resource_type='browser_context'")
    executor, provider, calls, browsers = recovery_executor(case)
    if stale:
        with pytest.raises(BusinessError):
            asyncio.run(executor(case[2]))
    else:
        result = asyncio.run(executor(token))
        assert result['recovery_blocked'] and result['reason'] == 'human_control'
        assert case[1].recovery_blocked_receipt(token)
    assert not calls and not provider.closed and not browsers and not case[3].created
    with connect(case[0].business_db) as db:
        assert db.execute("SELECT control_owner FROM resource_leases WHERE resource_type='browser_context'").fetchone()[0] == 'human'
        assert db.execute('SELECT count(*) FROM graph_recoveries').fetchone()[0] == (0 if stale else 1)


@pytest.mark.parametrize('change', ['human', 'epoch', 'current', 'paused'])
def test_preparation_close_rechecks_authority_after_manager_gate_wait(tmp_path, change):
    async def exercise():
        case = prepared(tmp_path)
        manager, _, _ = managed_setup(tmp_path)
        await manager.start()
        try:
            owner = SessionOwner('run', 'run-1', 'local-fixture')
            session = await manager.create(owner, execution_token=case[2])
            await manager._gate.acquire()
            cleanup = asyncio.create_task(manager.close_preparation(session.session_id, owner,
                execution_token=case[2], paused_settlement=change == 'paused'))
            await asyncio.sleep(0)
            if change == 'human':
                with connect(case[0].business_db) as db, transaction(db):
                    db.execute("UPDATE resource_leases SET control_owner='human',state_version=state_version+1 WHERE resource_type='browser_context'")
            elif change == 'epoch':
                case[1].start_worker('worker-1')
            elif change == 'paused':
                case[1].defer(case[2], 'PAUSED')
            manager._gate.release()
            if change in ('human', 'epoch'):
                with pytest.raises(BusinessError):
                    await cleanup
                assert manager.registry.get(session.session_id, owner).state == 'OPEN'
                assert not manager._contexts[session.session_id].context.closed
            else:
                assert (await cleanup).state == 'CLOSED'
        finally:
            await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('paused', [False, True])
@pytest.mark.parametrize('change', ['human', 'epoch'])
def test_preparation_close_rechecks_db_ownership_after_session_lookup(tmp_path, paused, change):
    async def exercise():
        case = prepared(tmp_path)
        manager, _, _ = managed_setup(tmp_path)
        await manager.start()
        try:
            owner = SessionOwner('run', 'run-1', 'local-fixture')
            session = await manager.create(owner, execution_token=case[2])
            if paused:
                case[1].defer(case[2], 'PAUSED')
            original_get = manager.registry.get
            changed = False
            def lookup_then_transfer(session_id, current_owner):
                nonlocal changed
                result = original_get(session_id, current_owner)
                if not changed:
                    changed = True
                    if change == 'human':
                        with connect(case[0].business_db) as db, transaction(db):
                            db.execute("UPDATE resource_leases SET control_owner='human',state_version=state_version+1 WHERE resource_type='browser_context'")
                    else:
                        case[1].start_worker('worker-1')
                return result
            manager.registry.get = lookup_then_transfer
            with pytest.raises(BusinessError):
                await manager.close_preparation(session.session_id, owner,
                    execution_token=case[2], paused_settlement=paused)
            assert original_get(session.session_id, owner).state == 'OPEN'
            assert not manager._contexts[session.session_id].context.closed
            with connect(case[0].business_db) as db:
                assert db.execute("SELECT count(*) FROM browser_session_events WHERE session_id=? AND event_type IN ('closing','lost')",
                                  (session.session_id,)).fetchone()[0] == 0
        finally:
            await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('paused', [False, True])
def test_preparation_closing_holds_sqlite_writer_lock_through_qualification(tmp_path, paused, monkeypatch):
    from webagent.db.connection import StorageBusyError
    from webagent.scheduler.store import SchedulerStore
    async def exercise():
        case = prepared(tmp_path)
        manager, _, _ = managed_setup(tmp_path)
        await manager.start()
        try:
            owner = SessionOwner('run', 'run-1', 'local-fixture')
            session = await manager.create(owner, execution_token=case[2])
            if paused:
                case[1].defer(case[2], 'PAUSED')
            guarded = []
            def competing_writer():
                with pytest.raises(StorageBusyError):
                    with connect(case[0].business_db, busy_timeout_ms=0) as other, transaction(other):
                        other.execute("UPDATE resource_leases SET control_owner='human' WHERE holder_run_id='run-1'")
                guarded.append(True)
            if paused:
                original = SchedulerStore.settlement
                def check(store, token):
                    result = original(store, token)
                    competing_writer()
                    return result
                monkeypatch.setattr(SchedulerStore, 'settlement', check)
            else:
                original = manager.registry._execution
                def check(db, *args, **kwargs):
                    result = original(db, *args, **kwargs)
                    competing_writer()
                    return result
                monkeypatch.setattr(manager.registry, '_execution', check)
            assert (await manager.close_preparation(session.session_id, owner,
                execution_token=case[2], paused_settlement=paused)).state == 'CLOSED'
            assert guarded == [True]
        finally:
            await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('human', [False, True])
def test_terminal_cleanup_preserves_human_window_after_real_budget_failure(tmp_path, human):
    from datetime import datetime, timedelta, timezone
    async def exercise():
        case = prepared(tmp_path)
        manager, _, _ = managed_setup(tmp_path)
        await manager.start()
        try:
            owner = SessionOwner('run', 'run-1', 'local-fixture')
            session = await manager.create(owner, execution_token=case[2])
            case[1].defer(case[2], 'WAITING_HANDOFF', control_owner='human' if human else 'worker',
                handoff_deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
            result = case[1].expire_budget('run-1', 'handoff')
            assert result['state'] == 'FAILED'
            if human:
                with pytest.raises(BusinessError):
                    await manager.close_terminal(session.session_id, owner)
                assert manager.registry.get(session.session_id, owner).state == 'OPEN'
                assert not manager._contexts[session.session_id].context.closed
                with connect(case[0].business_db) as db:
                    assert db.execute("SELECT control_owner FROM resource_leases WHERE holder_run_id='run-1' AND resource_type='browser_context'").fetchone()[0] == 'human'
                    assert db.execute("SELECT count(*) FROM browser_session_events WHERE session_id=? AND event_type IN ('closing','lost')",
                        (session.session_id,)).fetchone()[0] == 0
            else:
                assert (await manager.close_terminal(session.session_id, owner)).state == 'CLOSED'
        finally:
            await manager.aclose()
    asyncio.run(exercise())
