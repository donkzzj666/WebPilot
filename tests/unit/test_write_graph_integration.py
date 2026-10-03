"""Real graph/checkpointer and write journals over a controlled local browser."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from webagent.config import Settings
from webagent.controls.models import ControlRequest
from webagent.controls.store import ControlStore
from webagent.db import connect, transaction
from webagent.db.repository import create_run
from webagent.db.repository import canonical_json
from webagent.errors import BusinessError
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.graph.runtime import StateGraphAdapter
from webagent.graph.store import GraphStore
from webagent.models.schema import RequestInput, parse_model_output
from webagent.models.transport import ModelConfig
from webagent.scheduler.models import Resource
from webagent.sessions.models import SessionOwner
from webagent.verification.service import VerificationService
from webagent.writes.service import WriteCheckCapture
from storage import test_gateway_store as gateway_fixture
from storage.test_write_gateway_service import setup, dispatch
from unit.test_graph_executor import prepared, injected, current, Manager, Secrets


class WriteThenWaitModel:
    def __init__(self, fixture, calls):
        self.fixture, self.calls = fixture, calls
        self.provider = SimpleNamespace(config=ModelConfig())
        self.inputs = []

    async def generate(self, model_input, *, execution_token):
        self.inputs.append(model_input)
        self.calls.append('model')
        if len(self.inputs) == 1:
            value = gateway_fixture.action(self.fixture, step='graph-write-step',
                snapshot=model_input.observation.snapshot_id, kind='input', write=True)
            output = parse_model_output(json.dumps({'type': 'Action', 'action': value}))
        else:
            output = RequestInput(type='RequestInput', requested_fields=['source_document'],
                                  reason='The synthetic task requires another declared document')
        return SimpleNamespace(output=output)


async def graph_case(tmp_path, monkeypatch, saver, *, outcome='APPLIED', cancel=False):
    # Keep the shared gateway fixture's default unchanged. Only this isolated
    # Run uses the production graph/schema versions at creation time.
    original_create = gateway_fixture.create_run
    def create_graph_run(db, **kwargs):
        kwargs.update(graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION)
        return original_create(db, **kwargs)
    monkeypatch.setattr(gateway_fixture, 'create_run', create_graph_run)
    from webagent.db import migrate
    database = tmp_path / 'business.sqlite3'
    migrate(database)
    fixture, browser, gateway, _ = setup(database)
    browser.outcome = outcome
    calls = []
    original_observe, original_dispatch, original_check = gateway.observe, gateway.dispatch, gateway.reconcile_write
    async def observe(*args, **kwargs):
        calls.append('observe')
        return await original_observe(*args, **kwargs)
    async def dispatched(*args, **kwargs):
        calls.append('dispatch')
        result = await original_dispatch(*args, **kwargs)
        calls.append('dispatch_return')
        return result
    async def checked(*args, **kwargs):
        calls.append('query')
        result = await original_check(*args, **kwargs)
        calls.append('query_return')
        return result
    gateway.observe, gateway.dispatch, gateway.reconcile_write = observe, dispatched, checked
    verifier = VerificationService(tmp_path, scheduler=fixture.scheduler)
    model = WriteThenWaitModel(fixture, calls)
    graph = StateGraphAdapter(tmp_path, gateway, model, verifier, checkpointer=saver)
    control = None
    if cancel:
        original_execute = browser.execute
        async def execute(*args, **kwargs):
            nonlocal control
            result = await original_execute(*args, **kwargs)
            control = ControlStore(database, scheduler=fixture.scheduler).request(
                fixture.token.run_id, 'cancel', ControlRequest(expected_state_version=fixture.token.state_version,
                    contract_version=1, settings_version=0), uuid4().hex)['operation']
            calls.append('cancel_requested_after_write')
            return result
        browser.execute = execute
    return fixture, browser, gateway, model, graph, calls, lambda: control


def next_run(fixture, browser):
    """Create a real qualified successor, with no current write proof."""
    previous = fixture.token
    fixture.scheduler.finish(previous, 'CANCELLED')
    manager = fixture.session.manager_id
    fixture.registry.closing(fixture.session.session_id, manager)
    fixture.registry.closed(fixture.session.session_id, manager)
    run_id = 'run-gateway-next'
    with connect(fixture.path) as db, transaction(db):
        create_run(db, run_id=run_id, task_id=fixture.contract['task_id'], contract_version=1,
            graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256='a' * 64, runtime_config_sha256='b' * 64,
            parent_run_id=previous.run_id)
    identity = fixture.contract['identity_ref']
    fixture.scheduler.enqueue(run_id, [Resource.site_identity('local-fixture', identity),
        Resource.repository_write('fixture/project'), Resource.browser_context(run_id)],
        expected_state_version=0)
    fixture.token = fixture.scheduler.claim(previous.worker_id, previous.worker_generation)
    assert fixture.token.run_id == run_id
    fixture.session = fixture.registry.reserve(manager,
        SessionOwner('run', run_id, 'local-fixture', identity), execution_token=fixture.token)
    fixture.registry.opened(fixture.session.session_id, manager, execution_token=fixture.token)
    fixture.binding.update(session_id=fixture.session.session_id,
                           session_generation=fixture.session.generation)
    browser.owner, browser.session_id = fixture.session.owner, fixture.session.session_id
    return previous


@pytest.mark.parametrize('outcome', ['APPLIED', 'UNKNOWN'])
def test_new_run_queries_historical_confirmed_write_before_reuse_or_persistent_pause(tmp_path, monkeypatch, outcome):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, browser, gateway, model, graph, calls, _ = await graph_case(tmp_path, monkeypatch, saver)
            # Only the original Run is explicitly checked here. The successor
            # starts with historical truth and must query through the graph.
            await dispatch(gateway, fixture)
            checked = await gateway.reconcile_write(fixture.token, 'operation-step-1')
            assert checked['status'] == 'CONFIRMED' and checked['verified_current']
            previous = next_run(fixture, browser)
            browser.outcome = outcome
            calls.clear()
            with connect(fixture.path) as db:
                assert db.execute('SELECT count(*) FROM write_protocol_checks WHERE run_id=?',
                                  (fixture.token.run_id,)).fetchone()[0] == 0
            query = gateway.reconcile_write
            async def counted_query(token, operation_id):
                assert operation_id == 'operation-step-1'
                # The historical admission must not charge another write.
                assert fixture.budgets.status(token.run_id)['actions_used'] == 0
                result = await query(token, operation_id)
                assert fixture.budgets.status(token.run_id)['actions_used'] == 0
                return result
            gateway.reconcile_write = counted_query
            result = await graph.run(fixture.token)
            assert result['wait_id'] and not result['completed']
            expected = ['observe', 'model', 'dispatch', 'dispatch_return', 'query', 'query_return']
            assert calls == expected + (['observe', 'model'] if outcome == 'APPLIED' else [])
            assert browser.calls == [('step-1', 'operation-step-1')]
            assert len(model.inputs) == (2 if outcome == 'APPLIED' else 1)
            saved = await graph.graph.aget_state({'configurable': {'thread_id': fixture.token.run_id}})
            assert saved.next == ('wait',)
            with connect(fixture.path) as db:
                assert db.execute('SELECT count(*) FROM write_intents').fetchone()[0] == 1
                assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'CONFIRMED'
                assert db.execute('SELECT state FROM runs WHERE run_id=?',
                                  (fixture.token.run_id,)).fetchone()[0] == 'PAUSED'
                assert db.execute('SELECT count(*) FROM gateway_attempts WHERE run_id=?',
                                  (fixture.token.run_id,)).fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM write_protocol_dispatches').fetchone()[0] == 1
                checks = db.execute('SELECT run_id,facts_json FROM write_protocol_checks').fetchall()
                assert sorted((row['run_id'], json.loads(row['facts_json'])['outcome']) for row in checks) == sorted([
                    (previous.run_id, 'APPLIED'), (fixture.token.run_id, outcome)])
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
            if outcome == 'UNKNOWN':
                assert result['diagnostic'] == 'recovery_required'
                assert fixture.scheduler.claim(fixture.token.worker_id, fixture.token.worker_generation) is None
                with pytest.raises(BusinessError):
                    await graph.run(fixture.token)
                assert calls == expected and len(model.inputs) == 1
                assert browser.calls == [('step-1', 'operation-step-1')]
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['APPLIED', 'NOT_APPLIED'])
def test_external_write_query_finishes_before_next_observation_or_decision(tmp_path, monkeypatch, outcome):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, browser, _, model, graph, calls, _ = await graph_case(tmp_path, monkeypatch, saver, outcome=outcome)
            result = await graph.run(fixture.token)
            assert result['wait_id'] and not result['completed']
            assert calls == ['observe', 'model', 'dispatch', 'dispatch_return',
                             'query', 'query_return', 'observe', 'model']
            assert len(browser.calls) == 1 and len(model.inputs) == 2
            with connect(fixture.path) as db:
                assert db.execute('SELECT count(*) FROM write_protocol_dispatches').fetchone()[0] == 1
                assert db.execute('SELECT status FROM write_intents').fetchone()[0] == (
                    'CONFIRMED' if outcome == 'APPLIED' else 'NOT_APPLIED')
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'PAUSED'
                phases = [r[0] for r in db.execute('SELECT phase FROM graph_progress ORDER BY progress_id')]
                assert phases.count('dispatch') == 1 and phases.count('confirm') == 1
    asyncio.run(exercise())


def test_unknown_write_enters_persistent_wait_without_second_observation_or_retry(tmp_path, monkeypatch):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, browser, gateway, model, graph, calls, _ = await graph_case(tmp_path, monkeypatch, saver, outcome='UNKNOWN')
            result = await graph.run(fixture.token)
            assert result['diagnostic'] == 'recovery_required' and result['wait_id']
            assert calls == ['observe', 'model', 'dispatch', 'dispatch_return', 'query', 'query_return']
            assert len(browser.calls) == len(model.inputs) == 1
            saved = await graph.graph.aget_state({'configurable': {'thread_id': fixture.token.run_id}})
            assert saved.next == ('wait',)
            with connect(fixture.path) as db:
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'PAUSED'
                assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'UNKNOWN'
                assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
            with pytest.raises(BusinessError):
                await graph.run(fixture.token)
            with pytest.raises(BusinessError):
                await gateway.observe(fixture.token)
            assert len(browser.calls) == len(model.inputs) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['state_conflict', 'invalid_json', 'identity_mismatch'])
def test_rejected_read_only_write_proof_becomes_durable_pause_without_hot_retry(tmp_path, monkeypatch, failure):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, browser, gateway, model, graph, calls, _ = await graph_case(tmp_path, monkeypatch, saver)
            original = gateway.write_verifier
            async def rejected(operation, surface):
                if failure == 'state_conflict':
                    raise BusinessError('STATE_CONFLICT', 'Synthetic result changed during read-only lookup', status=409)
                captured = await original(operation, surface)
                if failure == 'invalid_json':
                    return WriteCheckCapture(b'{invalid synthetic JSON', captured.source_url, captured.snapshot_id)
                facts = json.loads(captured.data)
                facts['identity_ref'] = 'another-synthetic-account'
                return WriteCheckCapture(canonical_json(facts).encode(), captured.source_url, captured.snapshot_id)
            gateway.write_verifier = rejected
            result = await graph.run(fixture.token)
            assert result['wait_id'] and result['diagnostic'] == 'recovery_required'
            assert calls[:5] == ['observe', 'model', 'dispatch', 'dispatch_return', 'query']
            assert calls[5:] == (['query_return'] if failure == 'identity_mismatch' else [])
            assert len(browser.calls) == len(model.inputs) == 1
            saved = await graph.graph.aget_state({'configurable': {'thread_id': fixture.token.run_id}})
            assert saved.next == ('wait',)
            with connect(fixture.path) as db:
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'PAUSED'
                assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'UNKNOWN'
                assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == 'WAITING'
                assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2
                assert db.execute('SELECT count(*) FROM write_protocol_dispatches').fetchone()[0] == 1
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
            assert fixture.scheduler.claim(fixture.token.worker_id, fixture.token.worker_generation) is None
            with pytest.raises(BusinessError):
                await graph.run(fixture.token)
            assert len(browser.calls) == len(model.inputs) == 1
    asyncio.run(exercise())


def test_cancel_after_physical_write_keeps_unknown_and_skips_confirmation_query(tmp_path, monkeypatch):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, browser, _, model, graph, calls, control = await graph_case(tmp_path, monkeypatch, saver, cancel=True)
            result = await graph.run(fixture.token)
            assert result['completed'] and result['wait_id'] is None
            assert calls == ['observe', 'model', 'dispatch', 'cancel_requested_after_write', 'dispatch_return']
            assert len(browser.calls) == len(model.inputs) == 1
            receipt = graph.controls.read(control()['operation_id'])
            assert receipt['status'] == 'APPLIED' and receipt['state'] == 'CANCELLED'
            saved = await graph.graph.aget_state({'configurable': {'thread_id': fixture.token.run_id}})
            assert saved.next == ()
            with connect(fixture.path) as db:
                assert db.execute('SELECT state FROM runs').fetchone()[0] == 'CANCELLED'
                assert db.execute('SELECT status,receipt FROM write_intents').fetchone()[:] == ('UNKNOWN', None)
                assert db.execute('SELECT count(*) FROM write_protocol_checks').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2
                assert db.execute('SELECT count(*) FROM write_protocol_dispatches').fetchone()[0] == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['APPLIED', 'NOT_APPLIED', 'UNKNOWN'])
def test_aggregator_consumes_protocol_facts_without_treating_old_step_as_success(tmp_path, monkeypatch, outcome):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, _, gateway, _, _, _, _ = await graph_case(tmp_path, monkeypatch, saver, outcome=outcome)
            await dispatch(gateway, fixture)
            await gateway.reconcile_write(fixture.token, 'operation-step-1')
            verifier = VerificationService(tmp_path, scheduler=fixture.scheduler)
            run = GraphStore(fixture.path).load_run(fixture.token.run_id)
            with verifier.evidence.files.locked(), connect(fixture.path) as db:
                effects, unresolved, ids = verifier._effects(db, run, run['contract'])
            assert len(effects) == 1 and ids
            assert effects[0].status == {'APPLIED': 'CONFIRMED', 'NOT_APPLIED': 'NOT_APPLIED', 'UNKNOWN': 'UNKNOWN'}[outcome]
            if outcome == 'UNKNOWN':
                assert 'unresolved_write:operation-step-1' in unresolved
            else:
                assert not unresolved
            with connect(fixture.path) as db:
                assert db.execute('SELECT status FROM steps').fetchone()[0] == 'UNKNOWN'
    asyncio.run(exercise())


def test_executor_default_still_pauses_repository_write_before_client_creation(tmp_path):
    case = prepared(tmp_path, scenario='code')
    executor, provider, calls = injected(case)
    assert not executor.write_protocol_enabled
    result = asyncio.run(executor(case[2]))
    assert result['diagnostic'] == 'write_adapter_unavailable'
    assert current(case[0])['state'] == 'PAUSED'
    assert not calls and not case[3].created and not provider.closed


def test_executor_write_protocol_enablement_requires_private_factory(tmp_path):
    from webagent.graph.executor import GraphExecutor
    case = prepared(tmp_path)
    with pytest.raises(ValueError):
        GraphExecutor(case[0], case[3], checkpointer='controlled-saver', write_protocol_enabled=True)


def test_executor_queries_old_write_before_business_recovery_begin(tmp_path, monkeypatch):
    from webagent.graph.executor import GraphExecutor
    from webagent.graph.recovery import RecoveryStore
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, browser, gateway, _, _, calls, _ = await graph_case(tmp_path, monkeypatch, saver)
            await dispatch(gateway, fixture)
            fixture.scheduler.abandon(fixture.token)
            recovery_token = fixture.scheduler.claim(fixture.token.worker_id, fixture.token.worker_generation)
            manager = Manager(Settings(tmp_path))
            manager.manager_id = browser.managed.manager_id
            recovery = RecoveryStore(tmp_path, budgets=fixture.budgets)
            def begin(token, saved):
                calls.append('business_recovery_begin')
                with connect(fixture.path) as db:
                    assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'CONFIRMED'
                    assert db.execute('SELECT count(*) FROM write_protocol_checks').fetchone()[0] == 1
                # Stop at the boundary being tested. The default M1-17 object
                # adapter has separate recovery tests; it cannot trust this
                # intentionally minimal synthetic write page as object proof.
                raise BusinessError('STATE_CONFLICT', 'Controlled object proof is absent', status=409, field='proof_missing')
            monkeypatch.setattr(recovery, 'begin', begin)
            executor = GraphExecutor(Settings(tmp_path), manager, checkpointer=saver,
                scheduler=fixture.scheduler, secret_store=Secrets(), recovery_store=recovery,
                gateway_factory=lambda *args, **kwargs: gateway, write_protocol_enabled=True)
            result = await executor(recovery_token)
            assert result['recovery_blocked'] and result['reason'] == 'proof_missing'
            assert calls == ['observe', 'dispatch', 'dispatch_return', 'query', 'query_return', 'business_recovery_begin']
            assert len(browser.calls) == 1
    asyncio.run(exercise())


def test_historical_confirmed_write_without_current_run_proof_cannot_aggregate(tmp_path, monkeypatch):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, _, gateway, _, _, _, _ = await graph_case(tmp_path, monkeypatch, saver)
            await dispatch(gateway, fixture)
            await gateway.reconcile_write(fixture.token, 'operation-step-1')
            with connect(fixture.path) as db, transaction(db):
                create_run(db, run_id='another-run', task_id=fixture.contract['task_id'], contract_version=1,
                    graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
                    model_config_sha256='a' * 64, runtime_config_sha256='b' * 64,
                    parent_run_id=fixture.token.run_id)
            verifier = VerificationService(tmp_path, scheduler=fixture.scheduler)
            run = GraphStore(fixture.path).load_run('another-run')
            with verifier.evidence.files.locked(), connect(fixture.path) as db:
                with pytest.raises(BusinessError) as caught:
                    verifier._effects(db, run, run['contract'])
            assert caught.value.code == 'EVIDENCE_MISSING'
            with connect(fixture.path) as db:
                assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'CONFIRMED'
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
    asyncio.run(exercise())


def test_corrupted_current_write_proof_blocks_receipt_verification(tmp_path, monkeypatch):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            fixture, _, gateway, _, _, _, _ = await graph_case(tmp_path, monkeypatch, saver)
            await dispatch(gateway, fixture)
            await gateway.reconcile_write(fixture.token, 'operation-step-1')
            with connect(fixture.path) as db:
                path = db.execute('''SELECT e.artifact_path FROM write_protocol_check_evidence p
                    JOIN evidence e USING(evidence_id,run_id)''').fetchone()[0]
            (tmp_path / path).write_bytes(b'{"receipt":"synthetic-corrupt-artifact"}')
            verifier = VerificationService(tmp_path, scheduler=fixture.scheduler)
            run = GraphStore(fixture.path).load_run(fixture.token.run_id)
            with verifier.evidence.files.locked(), connect(fixture.path) as db:
                effects, unresolved, _ = verifier._effects(db, run, run['contract'])
            assert len(effects) == 1 and effects[0].status == 'CONFIRMED'
            assert 'side_effect_receipt_not_verified:operation-step-1' in unresolved
            with connect(fixture.path) as db:
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
    asyncio.run(exercise())
