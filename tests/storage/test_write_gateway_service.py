"""Real journal/evidence authority with a bounded, synthetic browser adapter."""
import asyncio
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from webagent.db import connect, transaction
from webagent.db.repository import canonical_json, utc_text
from webagent.errors import BusinessError
from webagent.gateway.browser import PreparedTarget
from webagent.gateway.permissions import WriteAuthorization
from webagent.gateway.service import BrowserGateway
from webagent.writes.service import WriteCheckCapture
from storage.test_gateway_store import action, fixture


class Browser:
    def __init__(self, fixture):
        self.fixture = fixture
        self.owner, self.session_id = fixture.session.owner, fixture.session.session_id
        self.managed = SimpleNamespace(manager_id=fixture.session.manager_id)
        self.version, self.outcome, self.observed_version = 'page-v1', 'APPLIED', 'object-v1'
        self.calls, self.navigation_calls = [], []

    async def capture(self, token, **kwargs):
        return {'page_url': self.fixture.contract['start_urls'][0], 'title': 'Synthetic write result',
            'tab_id': 'tab-gateway', 'frame_id': 'frame-gateway', 'page_version': self.version,
            'width': 800, 'height': 600, 'visible_text': canonical_json({
                'outcome': self.outcome, 'observed_version': self.observed_version}),
            'links': [], 'elements': []}

    async def write_check_capture(self, token, operation_id):
        return await self.capture(token)

    async def write_check_navigate(self, token, url, operation_id):
        self.navigation_calls.append(url)
        return {'source_url': url}

    async def prepare(self, token, action, snapshot, **kwargs):
        return PreparedTarget(action.action_type, 'tab-gateway', 'frame-gateway', self.version,
            snapshot['source_url'], None, action.target.locator,
            hashlib.sha256(canonical_json(action.model_dump(mode='json')).encode()).hexdigest(),
            trusted_write=True)

    async def execute(self, token, action, prepared, **kwargs):
        assert prepared.action_sha256 == hashlib.sha256(
            canonical_json(action.model_dump(mode='json')).encode()).hexdigest()
        self.calls.append((action.step_id, action.target.write_scope.operation_id))
        return {'receipt': {'external_object': 'synthetic-result'}, 'dispatch_completed': True}


def setup(path, **kwargs):
    f = fixture(path, write=True, **kwargs)
    browser = Browser(f)

    async def authorize(action, snapshot, prepared):
        scope = action.target.write_scope
        return WriteAuthorization(scope.repository, scope.branch, scope.base_sha, scope.operation,
            tuple(scope.files), scope.identity_ref, (), expected_change_sha256='c' * 64,
            precondition_version=browser.observed_version, adapter_id='write-gateway-test-v1')

    async def verify(operation, surface):
        snapshot = await surface.observe()
        visible = json.loads(surface.capture(snapshot['snapshot_id'])['visible_text'])
        facts = {'outcome': visible['outcome'], 'identity_ref': operation['identity_ref'],
            'target': operation['target'], 'expected_change_sha256': operation['expected_change_sha256'],
            'precondition_version': operation['precondition_version'],
            'observed_version': visible['observed_version'], 'snapshot_id': snapshot['snapshot_id'],
            'receipt': {'external_object': 'synthetic-result'} if visible['outcome'] == 'APPLIED' else None}
        return WriteCheckCapture(canonical_json(facts).encode(), snapshot['source_url'], snapshot['snapshot_id'])

    gateway = BrowserGateway(path, browser, scheduler=f.scheduler, store=f.store,
        write_authorizer=authorize, write_verifier=verify)
    return f, browser, gateway, verify


async def dispatch(gateway, f, *, step='step-1'):
    snapshot = await gateway.observe(f.token)
    return await gateway.dispatch(f.token, action(f, step=step, snapshot=snapshot['snapshot_id'], kind='input', write=True))


def test_playwright_result_never_counts_as_write_receipt(database):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        result = await dispatch(gateway, f)
        assert result['status'] == 'UNKNOWN'
        assert browser.calls == [('step-1', 'operation-step-1')]
        with connect(database) as db:
            assert db.execute('SELECT status,receipt FROM write_intents').fetchone()[:] == ('UNKNOWN', None)
            assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2
        assert f.budgets.status(f.token.run_id)['actions_used'] == 1
    asyncio.run(exercise())


def test_applied_query_saves_original_proof_and_reuses_without_dispatch(database):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        await dispatch(gateway, f)
        before = f.budgets.status(f.token.run_id)
        checked = await gateway.reconcile_write(f.token, 'operation-step-1')
        assert checked['status'] == 'CONFIRMED'
        after = f.budgets.status(f.token.run_id)
        assert after['actions_used'] == before['actions_used']
        assert after['observations_used'] == before['observations_used'] + 2
        with connect(database) as db:
            proof = db.execute('SELECT e.* FROM write_protocol_check_evidence p JOIN evidence e USING(evidence_id,run_id)').fetchone()
            assert proof['sensitivity'] == 'restricted' and proof['capture_status'] == 'COMPLETE'
            assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 0
            assert db.execute('SELECT status FROM steps').fetchone()[0] == 'UNKNOWN'
        # Use the current proof observation and a newly proposed operation_id.
        with connect(database) as db:
            snapshot_id = db.execute('SELECT snapshot_id FROM gateway_page_heads').fetchone()[0]
        reused = await gateway.dispatch(f.token, action(f, step='step-2', snapshot=snapshot_id, kind='input', write=True))
        assert reused['status'] == 'CONFIRMED' and not reused['dispatch_allowed']
        assert browser.calls == [('step-1', 'operation-step-1')]
        assert f.budgets.status(f.token.run_id)['actions_used'] == 1
    asyncio.run(exercise())


def test_not_applied_proof_allows_only_new_counted_attempt_with_same_operation(database):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        await dispatch(gateway, f)
        browser.outcome = 'NOT_APPLIED'
        assert (await gateway.reconcile_write(f.token, 'operation-step-1'))['status'] == 'NOT_APPLIED'
        result = await dispatch(gateway, f, step='step-2')
        assert result['status'] == 'UNKNOWN'
        assert browser.calls == [('step-1', 'operation-step-1'), ('step-2', 'operation-step-1')]
        assert f.budgets.status(f.token.run_id)['actions_used'] == 2
        with connect(database) as db:
            assert db.execute('SELECT count(*) FROM write_intents').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM write_protocol_dispatches WHERE check_id IS NOT NULL').fetchone()[0] == 1
        with pytest.raises(BusinessError):
            await dispatch(gateway, f, step='step-3')
        assert len(browser.calls) == 2
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome,version', [('UNKNOWN', 'object-v1'), ('NOT_APPLIED', 'object-v2')])
def test_unknown_or_changed_precondition_cannot_retry(database, outcome, version):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        await dispatch(gateway, f)
        browser.outcome, browser.observed_version = outcome, version
        assert (await gateway.reconcile_write(f.token, 'operation-step-1'))['status'] == 'UNKNOWN'
        with pytest.raises(BusinessError):
            await dispatch(gateway, f, step='step-2')
        assert len(browser.calls) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['snapshot', 'live_page'])
def test_query_page_change_before_commit_rejects_old_proof(database, change):
    async def exercise():
        f, browser, gateway, original = setup(database)
        await dispatch(gateway, f)
        async def changing(operation, surface):
            captured = await original(operation, surface)
            if change == 'snapshot':
                await surface.observe()
            else:
                browser.version = 'page-v2'
            return captured
        gateway.write_verifier = changing
        with pytest.raises(BusinessError):
            await gateway.reconcile_write(f.token, 'operation-step-1')
        with connect(database) as db:
            assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'UNKNOWN'
            assert db.execute('SELECT count(*) FROM write_protocol_checks').fetchone()[0] == 0
        assert len(browser.calls) == 1
    asyncio.run(exercise())


def test_verifier_cannot_publish_raw_dict_or_foreign_snapshot(database):
    async def exercise():
        f, browser, gateway, original = setup(database)
        await dispatch(gateway, f)
        async def unbounded(operation, surface):
            return {'outcome': 'APPLIED', 'receipt': {'object': 'forged'}}
        gateway.write_verifier = unbounded
        with pytest.raises(BusinessError):
            await gateway.reconcile_write(f.token, 'operation-step-1')
        assert len(browser.calls) == 1
        with connect(database) as db:
            assert db.execute('SELECT count(*) FROM write_protocol_checks').fetchone()[0] == 0
    asyncio.run(exercise())


def test_write_query_navigation_is_counted_get_work(database):
    async def exercise():
        f, browser, gateway, original = setup(database)
        await dispatch(gateway, f)
        async def query(operation, surface):
            await surface.navigate(f.contract['start_urls'][0])
            return await original(operation, surface)
        gateway.write_verifier = query
        assert (await gateway.reconcile_write(f.token, 'operation-step-1'))['status'] == 'CONFIRMED'
        assert browser.navigation_calls == f.contract['start_urls']
        assert f.budgets.status(f.token.run_id)['actions_used'] == 2
    asyncio.run(exercise())


def test_no_verifier_means_no_external_reconciliation_capability(database):
    f, browser, gateway, _ = setup(database)
    gateway.write_verifier = None
    assert not gateway.supports_write_protocol
    with pytest.raises(BusinessError):
        asyncio.run(gateway.reconcile_write(f.token, 'missing'))


def test_confirmed_historical_receipt_requires_current_page_proof(database):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        await dispatch(gateway, f)
        await gateway.reconcile_write(f.token, 'operation-step-1')
        browser.version = 'different-current-object'
        browser.observed_version = 'object-v2'
        result = await dispatch(gateway, f, step='step-2')
        assert result['operation_id'] == 'operation-step-1'
        assert result['status'] == 'UNKNOWN' and result['ledger_status'] == 'CONFIRMED'
        assert result['requires_reconciliation'] and not result['verified_current']
        assert not result['dispatch_allowed']
        assert browser.calls == [('step-1', 'operation-step-1')]
        assert f.budgets.status(f.token.run_id)['actions_used'] == 1
        with connect(database) as db:
            assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'CONFIRMED'
            assert db.execute('SELECT count(*) FROM write_protocol_checks').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM gateway_attempts').fetchone()[0] == 1
        # Only a fresh trusted query can establish a usable current result.
        checked = await gateway.reconcile_write(f.token, result['operation_id'])
        assert checked['status'] == 'CONFIRMED' and checked['verified_current']
        assert browser.calls == [('step-1', 'operation-step-1')]
    asyncio.run(exercise())


@pytest.mark.parametrize('missing', ['expected_change_sha256', 'precondition_version', 'adapter_id'])
def test_protocol_write_requires_explicit_trusted_semantic_facts(database, missing):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        authorize = gateway.write_authorizer
        async def incomplete(*args):
            return replace(await authorize(*args), **{missing: None})
        gateway.write_authorizer = incomplete
        with pytest.raises(BusinessError) as caught:
            await dispatch(gateway, f)
        assert caught.value.code == 'FORBIDDEN'
        assert not browser.calls and f.budgets.status(f.token.run_id)['actions_used'] == 0
        with connect(database) as db:
            assert db.execute('SELECT count(*) FROM write_intents').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['APPLIED', 'NOT_APPLIED'])
def test_cached_receipt_or_retry_requires_readable_unchanged_original_proof(database, outcome):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        await dispatch(gateway, f)
        browser.outcome = outcome
        await gateway.reconcile_write(f.token, 'operation-step-1')
        with connect(database) as db:
            path = db.execute('''SELECT e.artifact_path FROM write_protocol_check_evidence p
                JOIN evidence e USING(evidence_id,run_id)''').fetchone()[0]
        (database.parent / path).write_bytes(b'{"outcome":"synthetic-corruption"}')
        with pytest.raises(BusinessError):
            await dispatch(gateway, f, step='step-2')
        assert len(browser.calls) == 1
        assert f.budgets.status(f.token.run_id)['actions_used'] == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('cancel', [False, True])
def test_write_check_get_waits_for_site_pacing_and_cancellation_never_dispatches(database, cancel):
    async def exercise():
        f, browser, gateway, _ = setup(database)
        await dispatch(gateway, f)
        with connect(database) as db, transaction(db):
            db.execute('INSERT INTO site_pacing VALUES(?,?,?,?,?)', ('public:local-fixture',
                utc_text(f.clock.utcnow()), f.clock.monotonic_ns(), f.clock.domain, 3))
        import threading
        throttled = threading.Event()
        consume = f.budgets.consume
        def consumed(*args, **kwargs):
            try:
                return consume(*args, **kwargs)
            except BusinessError as error:
                if error.code == 'SITE_THROTTLED':
                    throttled.set()
                raise
        f.budgets.consume = consumed
        query = asyncio.create_task(gateway._write_check_navigate(f.token, f.contract['start_urls'][0], 'operation-step-1'))
        try:
            async with asyncio.timeout(2):
                while not throttled.is_set():
                    await asyncio.sleep(.01)
            assert not browser.navigation_calls and f.budgets.status(f.token.run_id)['actions_used'] == 1
            if cancel:
                query.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(query, 2)
                with pytest.raises(BusinessError):
                    f.scheduler.validate(f.token)
                assert not browser.navigation_calls and f.budgets.status(f.token.run_id)['actions_used'] == 1
                with connect(database) as db:
                    assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'UNKNOWN'
                    assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2
            else:
                f.clock.advance(3)
                await asyncio.wait_for(query, 2)
                assert browser.navigation_calls == f.contract['start_urls']
                assert f.budgets.status(f.token.run_id)['actions_used'] == 2
        finally:
            if not query.done():
                query.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await query
    asyncio.run(exercise())
