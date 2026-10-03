"""Real SQLite controls at graph, gateway and Worker safe boundaries.

Only browser/model transports are bounded fixtures. No test applies an active
request from an API coroutine: the graph owning its token settles the request.
"""
import asyncio
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from webagent.controls.models import ControlPending, ControlRequest
from webagent.controls.store import ControlStore
from webagent.db import connect, transaction
from webagent.db.repository import canonical_json
from webagent.errors import BusinessError
from webagent.events import ActionEvent, OperationRequestedEvent, WaitingEvent, append_event
from webagent.gateway.service import BrowserGateway
from webagent.graph.runtime import StateGraphAdapter, _INVOCATIONS
from webagent.graph.executor import GraphExecutor
from webagent.models.journal import ModelCallRecord, ModelUsage, reserve_attempt, finish_attempt
from webagent.models.schema import ProposeResult, RequestInput
from webagent.models.transport import ModelConfig
from webagent.scheduler.worker import QueueWorker
from webagent.sessions.models import SessionOwner
from webagent.verification.service import VerificationService
from unit.test_gateway_service import FakeBrowser
from unit.test_graph_executor import prepared, current, Manager
from unit.test_graph_runtime import Model
from unit.test_verification_rules import setup


def request(case, action):
    state = current(case[0])
    return ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1]).request(
        'run-1', action, ControlRequest(expected_state_version=state['state_version'],
            contract_version=1, settings_version=1), uuid4().hex)['operation']


async def graph_case(tmp_path, saver, *, model=None, browser_hook=None, fault_hook=None):
    case = prepared(tmp_path, configured=True)
    owner = SessionOwner('run', 'run-1', 'local-fixture')
    session = await case[3].create(owner, auth_ref=None, replaces=None,
                                  execution_token=case[2], gateway_downloads=True)
    browser = FakeBrowser(owner, session, execute_hook=browser_hook)
    browser.url = setup()[0].start_urls[0]
    original_capture = browser.capture
    visible = canonical_json(deepcopy(setup()[2][0].content))
    async def capture(*args, **kwargs):
        result = await original_capture(*args, **kwargs)
        result.update(visible_text=visible, visible_sha256=hashlib.sha256(visible.encode()).hexdigest())
        return result
    browser.capture = capture
    gateway = BrowserGateway(case[0].business_db, browser, scheduler=case[1])
    verifier = VerificationService(tmp_path, scheduler=case[1])
    graph = StateGraphAdapter(tmp_path, gateway, model or Model(output='action'), verifier,
                              checkpointer=saver, fault_hook=fault_hook)
    graph.controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
    return case, session, browser, gateway, verifier, graph


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_request_does_not_interrupt_accepted_action_and_stops_the_next_dispatch(tmp_path, action):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            entered, release = asyncio.Event(), asyncio.Event()
            async def execute(token):
                entered.set()
                await release.wait()
                return {'dispatch_completed': True}
            case, session, browser, _, _, graph = await graph_case(tmp_path, saver, browser_hook=execute)
            operation_task = asyncio.create_task(graph.run(case[2]))
            await asyncio.wait_for(entered.wait(), 3)
            pending = request(case, action)
            assert pending['status'] == 'PENDING' and current(case[0])['state'] == 'RUNNING'
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT status FROM steps').fetchone()[0] == 'INTENT'
                assert db.execute('SELECT epoch FROM scheduler_queue').fetchone()[0] == case[2].epoch
            assert not operation_task.done()
            release.set()
            state = await asyncio.wait_for(operation_task, 5)
            assert current(case[0])['state'] == ('PAUSED' if action == 'pause' else 'CANCELLED')
            assert graph.controls.read(pending['operation_id'])['status'] == 'APPLIED'
            assert len(graph.model.inputs) == browser.execute_calls == browser.capture_calls == 1
            assert graph.controls.completion_receipt(case[2])
            saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            assert saved.next == (('wait',) if action == 'pause' else ())
            assert saved.values['state_version'] == state['state_version']
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT status FROM steps').fetchone()[0] == 'COMPLETED'
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM resource_leases WHERE resource_type='active_slot'").fetchone()[0] == 0
                if action == 'pause':
                    assert db.execute("SELECT state FROM browser_sessions WHERE session_id=?", (session.session_id,)).fetchone()[0] == 'OPEN'
                    assert db.execute("SELECT logical_hold FROM resource_leases WHERE resource_type='browser_context'").fetchone()[0] == 1
    asyncio.run(exercise())


def test_control_between_model_return_and_intent_is_checked_in_the_intent_transaction(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, browser, gateway, _, graph = await graph_case(tmp_path, saver)
            prepare = gateway.store.prepare
            pending = []
            def request_before_intent(*args, **kwargs):
                pending.append(request(case, 'pause'))
                return prepare(*args, **kwargs)
            gateway.store.prepare = request_before_intent
            state = await graph.run(case[2])
            assert state['wait_id'] and browser.execute_calls == 0
            assert browser.prepare_calls == 1 and len(graph.model.inputs) == 1
            assert graph.controls.read(pending[0]['operation_id'])['status'] == 'APPLIED'
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0
            assert case[1].budgets.status('run-1')['actions_used'] == 0
    asyncio.run(exercise())


def test_accepted_capture_finishes_and_keeps_both_counters_before_pause(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, browser, gateway, _, graph = await graph_case(tmp_path, saver)
            entered, release = asyncio.Event(), asyncio.Event()
            capture = browser.capture
            async def blocked_capture(*args, **kwargs):
                entered.set()
                await release.wait()
                return await capture(*args, **kwargs)
            browser.capture = blocked_capture
            operation = asyncio.create_task(gateway.observe(case[2], include_screenshot=True))
            await asyncio.wait_for(entered.wait(), 3)
            request(case, 'pause')
            counts = case[1].budgets.status('run-1')
            assert counts['observations_used'] == counts['screenshots_used'] == 1
            release.set()
            snapshot = await asyncio.wait_for(operation, 5)
            assert snapshot['screenshot_evidence_id'] and current(case[0])['state'] == 'RUNNING'
            counts = case[1].budgets.status('run-1')
            assert counts['observations_used'] == counts['screenshots_used'] == 1
            state = await graph.run(case[2])
            assert state['wait_id'] and not graph.model.inputs and browser.capture_calls == 1
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM gateway_observations').fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM evidence WHERE original_evidence_id IS NULL AND capture_status='COMPLETE'").fetchone()[0] > 0
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_control_after_verification_before_finalize_never_publishes_success(tmp_path, action):
    class ProposalModel(Model):
        async def generate(self, model_input, *, execution_token):
            self.inputs.append(model_input)
            body = setup()[1].model_dump(mode='json')
            body['evidence_ids'] = model_input.observation.evidence_ids
            body['items']['values'][0]['evidence_ids'] = model_input.observation.evidence_ids
            return SimpleNamespace(output=ProposeResult.model_validate_json(canonical_json(body)))
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            pending = []
            def hook(stage, run_id):
                if stage == 'business_before':
                    pending.append(request(case, action))
            case, _, browser, _, _, graph = await graph_case(tmp_path, saver,
                model=ProposalModel(), fault_hook=hook)
            state = await graph.run(case[2])
            assert current(case[0])['state'] == ('PAUSED' if action == 'pause' else 'CANCELLED')
            assert graph.controls.read(pending[0]['operation_id'])['status'] == 'APPLIED'
            assert len(graph.model.inputs) == browser.capture_calls == 1
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM run_verifications').fetchone()[0] == 1
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
            assert state['completed'] is (action == 'cancel')
    asyncio.run(exercise())


def test_cancel_arriving_before_prepare_wait_never_interrupts_a_terminal_run(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            def hook(stage, run_id):
                if stage == 'decide_return_before':
                    request(case, 'cancel')
            case, _, _, _, _, graph = await graph_case(tmp_path, saver,
                model=Model(output='input'), fault_hook=hook)
            state = await graph.run(case[2])
            assert state['completed'] and current(case[0])['state'] == 'CANCELLED'
            saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            assert not saved.next and saved.values['completed']
            with connect(case[0].business_db) as db:
                assert db.execute("SELECT count(*) FROM task_events WHERE event_type='wait_registered'").fetchone()[0] == 0
    asyncio.run(exercise())


def test_pending_model_admission_is_atomic_and_does_not_create_an_attempt(tmp_path):
    class JournalModel(Model):
        async def generate(self, model_input, *, execution_token):
            self.inputs.append(model_input)
            pending.append(request(case, 'pause'))
            reserve_attempt(case[0].business_db, model_input, config,
                call_id='controlled-call', request_id='controlled-request', attempt_number=1,
                execution_token=execution_token)
            raise AssertionError('The pending control must stop provider admission')
    async def exercise():
        nonlocal case, config
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, browser, _, _, graph = await graph_case(tmp_path, saver, model=JournalModel())
            with connect(case[0].business_db) as db:
                config = ModelConfig.model_validate_json(db.execute('SELECT model_json FROM model_settings_versions WHERE version=1').fetchone()[0])
            state = await graph.run(case[2])
            assert state['wait_id'] and len(graph.model.inputs) == 1
            assert graph.controls.read(pending[0]['operation_id'])['status'] == 'APPLIED'
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM model_attempts').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM model_generations').fetchone()[0] == 0
    case, config, pending = None, None, []
    asyncio.run(exercise())


def test_inflight_model_usage_is_completed_before_pending_cancel_is_applied(tmp_path):
    class JournalModel(Model):
        async def generate(self, model_input, *, execution_token):
            self.inputs.append(model_input)
            reserve_attempt(case[0].business_db, model_input, config,
                call_id='controlled-call', request_id='controlled-request', attempt_number=1,
                execution_token=execution_token)
            entered.set()
            await release.wait()
            record = ModelCallRecord(run_id='run-1', request_id='controlled-request',
                provider_request_id=None, provider='deepseek', model_id=config.model_id,
                config_sha256=config.config_sha256, prompt_version=config.prompt_version,
                usage=ModelUsage(input_tokens=3, output_tokens=4), duration_ms=1,
                format_repairs=0, error_class=None)
            assert finish_attempt(case[0].business_db, record, status='VALID', execution_token=execution_token)
            return SimpleNamespace(output=RequestInput(type='RequestInput',
                requested_fields=['unused_candidate'], reason='Pending controls discard this candidate'))
    async def exercise():
        nonlocal case, config, entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, _, _, _, _, graph = await graph_case(tmp_path, saver, model=JournalModel())
            with connect(case[0].business_db) as db:
                config = ModelConfig.model_validate_json(db.execute('SELECT model_json FROM model_settings_versions WHERE version=1').fetchone()[0])
            operation = asyncio.create_task(graph.run(case[2]))
            await asyncio.wait_for(entered.wait(), 3)
            request(case, 'cancel')
            assert current(case[0])['state'] == 'RUNNING' and not operation.done()
            release.set()
            state = await asyncio.wait_for(operation, 5)
            assert state['completed'] and current(case[0])['state'] == 'CANCELLED'
            with connect(case[0].business_db) as db:
                row = db.execute('SELECT status,record_json FROM model_attempts').fetchone()
                assert row['status'] == 'VALID' and '"input_tokens":3' in row['record_json']
                assert db.execute('SELECT model_calls_used FROM run_budgets').fetchone()[0] == 1
                assert db.execute('SELECT count(*) FROM graph_input_requests').fetchone()[0] == 0
    case, config, entered, release = None, None, None, None
    asyncio.run(exercise())


def test_committed_pause_with_missing_framework_save_is_repaired_without_reregistering_wait(tmp_path):
    async def exercise():
        case = prepared(tmp_path, configured=True)
        controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
        pending = request(case, 'pause')
        applied = controls.apply_at_boundary(case[2])
        assert applied['status'] == 'APPLIED' and controls.read(pending['operation_id'])['status'] == 'APPLIED'
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            graph = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            first = await graph.settle_control(applied)
            second = await graph.settle_control(applied)
            assert first['wait_id'] == second['wait_id']
            saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            assert saved.next == ('wait',) and saved.values['wait_id'] == applied['wait_id']
        with connect(case[0].business_db) as db:
            assert db.execute("SELECT count(*) FROM task_events WHERE event_type='wait_registered'").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM graph_progress WHERE phase='wait'").fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM model_attempts').fetchone()[0] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('saved_first', [False, True])
def test_pending_resume_preserves_or_rebuilds_exact_pause_anchor_in_real_saver(tmp_path, saved_first):
    from webagent.graph.recovery import RecoveryStore
    async def exercise():
        case = prepared(tmp_path, configured=True)
        controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
        request(case, 'pause')
        applied = controls.apply_at_boundary(case[2])
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            graph = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            config = {'configurable': {'thread_id': 'run-1'}}
            anchor = graph.store.load_control_state(applied)
            if saved_first:
                await graph.settle_control(applied)
                original = await saver.aget_tuple(config)
            pending = request(case, 'resume')
            assert pending['status'] == 'PENDING'
            # The ordinary latest-event projection must remain conservative.
            assert graph.store.load_state('run-1')['progress_id'] is None
            state = await graph.settle_control(applied)
            assert state == anchor
            saved = await graph.graph.aget_state(config)
            assert saved.next == ('wait',) and saved.values == anchor
            if saved_first:
                assert (await saver.aget_tuple(config)).config == original.config
            assert (await graph.settle_control(applied)) == anchor
            resumed = controls.apply_idle('run-1')
            assert resumed['operation_id'] == pending['operation_id'] and resumed['status'] == 'APPLIED'
            assert current(case[0])['state'] == 'RECONCILING'
            token = case[1].claim(case[2].worker_id, case[2].worker_generation)
            assert token is not None
            recovery = RecoveryStore(tmp_path)
            plan = recovery.begin(token, saved.values)
            assert plan['allowed'] and plan['recovery_id']
            with connect(case[0].business_db) as db:
                assert db.execute("SELECT count(*) FROM task_events WHERE event_type='wait_registered'").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM graph_progress WHERE phase='wait'").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM graph_recoveries WHERE phase='BLOCKED'").fetchone()[0] == 0
                assert db.execute("SELECT phase FROM graph_recoveries ORDER BY recovery_seq DESC LIMIT 1").fetchone()[0] == 'BEGIN'
    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['resumed', 'new_pause', 'repeated_wait', 'business_event', 'unregistered_request'])
def test_old_pause_settlement_rejects_business_changes_and_preserves_framework_checkpoint(tmp_path, change):
    async def exercise():
        case = prepared(tmp_path, configured=True)
        controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
        request(case, 'pause')
        applied = controls.apply_at_boundary(case[2])
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            graph = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            config = {'configurable': {'thread_id': 'run-1'}}
            await graph.settle_control(applied)
            original = await saver.aget_tuple(config)
            if change in ('resumed', 'new_pause'):
                request(case, 'resume' if change == 'resumed' else 'pause')
                assert controls.apply_idle('run-1')['status'] == 'APPLIED'
            elif change == 'repeated_wait':
                with connect(case[0].business_db) as db, transaction(db):
                    append_event(db, run_id='run-1', expected_state_version=current(case[0])['state_version'],
                                 payload=WaitingEvent(wait_id=applied['wait_id'], reason='pause'))
                graph.store.record_wait_progress('run-1', applied['wait_id'],
                    expected_state_version=current(case[0])['state_version'])
            else:
                event = (ActionEvent(step_id='changed-business-step', action_type='read_visible',
                            attempt_status='COMPLETED', evidence_ids=[]) if change == 'business_event' else
                         OperationRequestedEvent(operation_id='unregistered-control', action='resume'))
                with connect(case[0].business_db) as db, transaction(db):
                    append_event(db, run_id='run-1', expected_state_version=current(case[0])['state_version'], payload=event)
            with pytest.raises(BusinessError) as caught:
                await graph.settle_control(applied)
            assert caught.value.code == 'STATE_CONFLICT'
            unchanged = await saver.aget_tuple(config)
            assert unchanged.config == original.config and unchanged.checkpoint == original.checkpoint
    asyncio.run(exercise())


def test_control_anchor_reads_all_authority_from_one_sqlite_snapshot(tmp_path, monkeypatch):
    from webagent.graph.store import GraphStore
    case = prepared(tmp_path, configured=True)
    controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
    request(case, 'pause')
    applied = controls.apply_at_boundary(case[2])
    store = GraphStore(case[0].business_db)
    anchor = store.load_control_state(applied)
    request(case, 'resume')
    original = store._run
    advanced = False
    def advancing(db, run_id):
        nonlocal advanced
        run = original(db, run_id)
        if not advanced:
            advanced = True
            assert controls.apply_idle('run-1')['status'] == 'APPLIED'
        return run
    monkeypatch.setattr(store, '_run', advancing)
    assert store.load_control_state(applied) == anchor
    assert current(case[0])['state'] == 'RECONCILING'
    with pytest.raises(BusinessError):
        store.load_control_state(applied)


def test_idle_control_settlement_cannot_enter_a_thread_owned_by_active_graph(tmp_path):
    async def exercise():
        case = prepared(tmp_path, configured=True)
        controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
        request(case, 'pause')
        applied = controls.apply_at_boundary(case[2])
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            graph = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            key = (str(graph.database.resolve()), 'run-1')
            gate = _INVOCATIONS.setdefault(key, asyncio.Lock())
            async with gate:
                with pytest.raises(BusinessError) as failure:
                    await graph.settle_control(applied)
                assert failure.value.code == 'RESOURCE_CONFLICT'
                assert await saver.aget_tuple({'configurable': {'thread_id': 'run-1'}}) is None
            assert (await graph.settle_control(applied))['wait_id']
    asyncio.run(exercise())


def test_worker_applies_idle_cancel_before_claim_without_invoking_an_executor(tmp_path):
    async def exercise():
        case = prepared(tmp_path, configured=True)
        controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
        dispatched = []
        async def unexpected(token):
            dispatched.append(token)
            raise AssertionError('Idle cancellation must complete before claiming')
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            graph = StateGraphAdapter(tmp_path, None, None, None, checkpointer=saver)
            worker = QueueWorker(case[1], 'controls-worker', executor=unexpected,
                controls=controls, control_settler=graph.settle_control)
            await worker.start()
            request(case, 'cancel')
            try:
                await worker.tick()
                assert not dispatched and current(case[0])['state'] == 'CANCELLED'
                saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
                assert not saved.next and saved.values['completed']
            finally:
                await worker.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('human', [False, True])
def test_idle_cancel_settler_closes_only_the_current_manager_nonhuman_context(tmp_path, human):
    from webagent.db import transaction
    async def exercise():
        case = prepared(tmp_path, configured=True)
        owner = SessionOwner('run', 'run-1', 'local-fixture')
        session = await case[3].create(owner, auth_ref=None, replaces=None, execution_token=case[2])
        controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
        request(case, 'pause')
        controls.apply_at_boundary(case[2])
        if human:
            with connect(case[0].business_db) as db, transaction(db):
                db.execute("UPDATE resource_leases SET control_owner='human',state_version=state_version+1 WHERE holder_run_id='run-1'")
        request(case, 'cancel')
        applied = controls.apply_idle('run-1')
        assert applied['status'] == 'APPLIED'
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            executor = GraphExecutor(case[0], case[3], checkpointer=saver,
                                     scheduler=case[1], secret_store=case[4])
            assert (await executor.settle_control(applied))['completed']
        assert case[3].registry.get(session.session_id, owner).state == ('OPEN' if human else 'CLOSED')
        with connect(case[0].business_db) as db:
            count = db.execute('SELECT count(*) FROM scheduler_context_reservations').fetchone()[0]
            assert count == (1 if human else 0)
            if human:
                assert db.execute("SELECT control_owner FROM resource_leases WHERE resource_type='browser_context'").fetchone()[0] == 'human'
    asyncio.run(exercise())


def test_cancel_committed_before_manager_restart_releases_lost_reservation_without_browser_access(tmp_path):
    async def exercise():
        case = prepared(tmp_path, configured=True)
        owner = SessionOwner('run', 'run-1', 'local-fixture')
        session = await case[3].create(owner, auth_ref=None, replaces=None, execution_token=case[2])
        controls = ControlStore(case[0].business_db, secret_store=case[4], scheduler=case[1])
        request(case, 'cancel')
        applied = controls.apply_at_boundary(case[2])
        with connect(case[0].business_db) as db:
            assert db.execute('SELECT count(*) FROM scheduler_context_reservations').fetchone()[0] == 1
        replacement = Manager(case[0])
        replacement.registry.recover_orphans(replacement.manager_id)
        assert replacement.registry.get(session.session_id, owner).state == 'LOST'
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            executor = GraphExecutor(case[0], replacement, checkpointer=saver,
                                     scheduler=case[1], secret_store=case[4])
            assert (await executor.settle_control(applied))['completed']
        assert not replacement.created and not replacement.closed
        with connect(case[0].business_db) as db:
            assert db.execute('SELECT count(*) FROM scheduler_context_reservations').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM resource_leases').fetchone()[0] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['pause', 'cancel'])
@pytest.mark.parametrize('repair_failure', ['none', 'conflict', 'hang'])
def test_action_timeout_settles_accepted_control_after_graph_exit_without_restarting_pump(
        tmp_path, action, repair_failure):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            entered, graph_exited = asyncio.Event(), asyncio.Event()
            pending = []
            async def execute(token):
                # Admission already committed INTENT; this physical operation
                # is cancelled by the actual gateway deadline after acceptance.
                pending.append(request(case, action))
                entered.set()
                await asyncio.Event().wait()
            case, session, browser, gateway, _, graph = await graph_case(
                tmp_path, saver, browser_hook=execute)
            bounded = gateway._bounded
            async def short_physical_deadline(token, factory, timeout, *, step_id=None, read_check=False):
                # Accelerate only this trusted physical transport deadline;
                # all production journal, revocation and cancellation code runs.
                return await bounded(token, factory, min(timeout, .04) if step_id else timeout,
                                     step_id=step_id, read_check=read_check)
            gateway._bounded = short_physical_deadline
            gateway.cancel_seconds = .1
            async def executor(token):
                try:
                    return await graph.run(token)
                finally:
                    graph_exited.set()
            settled = []
            async def settle(operation):
                assert graph_exited.is_set()
                key = (str(graph.database.resolve()), 'run-1')
                assert not _INVOCATIONS[key].locked()
                with pytest.raises(BusinessError):
                    case[1].validate(case[2])
                settled.append(operation['operation_id'])
                # Let several pump ticks run during framework cleanup: they
                # must neither cancel this settlement nor dispatch more work.
                await asyncio.sleep(.02)
                if repair_failure == 'conflict':
                    raise BusinessError('STATE_CONFLICT', 'The saved graph is unavailable', status=409)
                if repair_failure == 'hang':
                    await asyncio.Event().wait()
                return await graph.settle_control(operation)
            worker = QueueWorker(case[1], case[2].worker_id, executor=executor,
                controls=graph.controls, control_settler=settle,
                poll_seconds=.002, heartbeat_seconds=.01,
                deadline_seconds=.005, shutdown_seconds=.2)
            worker.generation = case[2].worker_generation
            try:
                worker._claims['run-1'] = asyncio.create_task(worker._execute(case[2]))
                with pytest.raises(RuntimeError, match='Scheduler stopped'):
                    await asyncio.wait_for(worker.run(asyncio.Event()), 5)
                assert entered.is_set() and graph_exited.is_set()
                assert worker._failed
                operation = graph.controls.read(pending[0]['operation_id'])
                assert operation['status'] == 'APPLIED'
                assert operation['state'] == ('PAUSED' if action == 'pause' else 'CANCELLED')
                assert current(case[0])['state'] == operation['state']
                assert settled == [pending[0]['operation_id']]
                assert len(graph.model.inputs) == browser.capture_calls == browser.execute_calls == 1
                with connect(case[0].business_db) as db:
                    step = db.execute('SELECT status,error_code FROM steps').fetchone()
                    assert step['status'] == 'UNKNOWN' and step['error_code'] == 'TIMEOUT'
                    assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == (
                        'WAITING' if action == 'pause' else 'FINISHED')
                    assert db.execute("SELECT count(*) FROM resource_leases WHERE resource_type='active_slot'").fetchone()[0] == 0
                    assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
                assert browser.execute_calls == 1 and len(graph.model.inputs) == 1
                if repair_failure == 'none':
                    saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
                    assert saved.next == (('wait',) if action == 'pause' else ())
            finally:
                await worker.aclose()
    asyncio.run(exercise())
