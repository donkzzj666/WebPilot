"""Real StateGraph + async SQLite saver over isolated business qualifications.

The browser's bounded capture is a fake. Durable gateway/evidence/context/
budget services and graph interrupt scheduling are the production objects.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from webagent.db import connect
from webagent.db.repository import canonical_json
from webagent.errors import BusinessError
from webagent.gateway.service import BrowserGateway
from webagent.graph.runtime import StateGraphAdapter
from webagent.graph.store import GraphStore
from webagent.models.schema import RequestInput, RequestEvidence, ProposeResult, parse_model_output
from webagent.models.transport import ModelConfig
from webagent.sessions.models import SessionOwner
from webagent.verification.service import VerificationService
from unit.test_gateway_service import FakeBrowser
from unit.test_graph_executor import prepared, current
from unit.test_verification_rules import setup


class Model:
    def __init__(self, output='input', *, after=None, wait=False):
        self.provider = SimpleNamespace(config=ModelConfig())
        self.output, self.after, self.wait = output, after, wait
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.inputs = []
        self.requested_fields = ['source_document']

    async def generate(self, model_input, *, execution_token):
        self.inputs.append(model_input)
        self.started.set()
        if self.wait:
            await self.release.wait()
        if self.after:
            self.after(execution_token)
        if self.output == 'input':
            output = RequestInput(type='RequestInput', requested_fields=self.requested_fields,
                                  reason='A declared source document is required')
        elif self.output == 'evidence':
            output = RequestEvidence(type='RequestEvidence',
                criterion_ids=[criterion.criterion_id for criterion in model_input.contract.acceptance_criteria],
                source_ids=[source.source_id for source in model_input.contract.sources],
                needed='Current source does not contain the required facts')
        else:
            observation = model_input.observation
            output = parse_model_output(json.dumps({'type': 'Action', 'action': {
                'run_id': execution_token.run_id, 'step_id': 'runtime-action',
                'epoch': execution_token.epoch, 'snapshot_id': observation.snapshot_id,
                'action_type': 'scroll', 'expected_effect': 'read',
                'target': {'page_url': observation.source_url, 'tab_id': observation.tab_id,
                           'frame_id': observation.frame_id, 'locator': None, 'write_scope': None},
                'args': {'direction': 'down', 'pixels': 100}}}))
        return SimpleNamespace(output=output)


async def graph_case(tmp_path, saver, *, model=None):
    case = prepared(tmp_path)
    settings, store, token, manager, _ = case
    owner = SessionOwner('run', token.run_id, 'local-fixture')
    session = await manager.create(owner, auth_ref=None, replaces=None,
                                   execution_token=token, gateway_downloads=True)
    browser = FakeBrowser(owner, session)
    browser.url = GraphStore(settings.business_db).load_run(token.run_id)['contract'].start_urls[0]
    gateway = BrowserGateway(settings.business_db, browser, scheduler=store)
    verifier = VerificationService(tmp_path, scheduler=store)
    model = model or Model()
    graph = StateGraphAdapter(tmp_path, gateway, model, verifier, checkpointer=saver)
    return case, browser, gateway, verifier, model, graph


def test_real_stategraph_interrupt_has_persisted_wait_and_reference_only_checkpoint(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, _, _, model, graph = await graph_case(tmp_path, saver)
            result = await graph.run(case[2])
            assert current(case[0]) == {'state': 'PAUSED', 'state_version': 2}
            assert result['wait_id'] and not result['completed']
            assert result['diagnostic'] == 'input_required'
            assert len(model.inputs) == 1 and browser.capture_calls == 1
            saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            assert saved.next == ('wait',)
            assert saved.values['wait_id'] == result['wait_id']
            serialized = json.dumps(saved.values)
            for private in ('execution_token', 'model_input', 'visible_excerpt', 'requested_fields', 'provider', 'api_key'):
                assert private not in serialized
            with connect(case[0].business_db) as db:
                wait = db.execute("SELECT payload_json,state_version FROM task_events WHERE event_type='wait_registered'").fetchone()
                assert json.loads(wait['payload_json'])['wait_id'] == result['wait_id']
                assert wait['state_version'] == 2
                assert db.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM run_results").fetchone()[0] == 0
                request = db.execute('SELECT * FROM graph_input_requests').fetchone()
                assert request['wait_id'] == result['wait_id'] and request['state_version'] == 2
                assert json.loads(request['requested_fields_json']) == ['source_document']
    asyncio.run(exercise())


def test_same_run_concurrent_invocation_is_rejected_before_second_observation(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            model = Model(wait=True)
            case, browser, gateway, verifier, _, graph = await graph_case(tmp_path, saver, model=model)
            running = asyncio.create_task(graph.run(case[2]))
            try:
                await asyncio.wait_for(model.started.wait(), 2)
                contender = StateGraphAdapter(tmp_path, gateway, Model(), verifier, checkpointer=saver)
                with pytest.raises(BusinessError) as error:
                    await contender.run(case[2])
                assert error.value.code == 'RESOURCE_CONFLICT'
                assert browser.capture_calls == 1
            finally:
                model.release.set()
                await asyncio.wait_for(running, 2)
    asyncio.run(exercise())


def test_token_rechecked_after_model_await_blocks_old_action(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            model = Model(output='action', wait=True)
            case, browser, _, _, _, graph = await graph_case(tmp_path, saver, model=model)
            running = asyncio.create_task(graph.run(case[2]))
            await asyncio.wait_for(model.started.wait(), 2)
            case[1].defer(case[2], 'PAUSED')
            model.release.set()
            with pytest.raises(BusinessError) as error:
                await asyncio.wait_for(running, 2)
            assert error.value.code == 'RESOURCE_CONFLICT'
            assert browser.execute_calls == browser.prepare_calls == 0
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('pending', ['observe', 'decide', 'dispatch', 'verify', 'aggregate'])
def test_old_pending_framework_node_cannot_resume_without_business_recovery(tmp_path, pending):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, _, _, model, graph = await graph_case(tmp_path, saver)
            state = graph.store.load_state('run-1')
            with pytest.raises(BusinessError) as error:
                graph._saved(SimpleNamespace(values=state, next=(pending,)), case[2])
            assert error.value.code == 'STATE_CONFLICT'
            assert not model.inputs and browser.capture_calls == browser.execute_calls == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('changed', ['event_missing', 'event_version', 'different_contract', 'different_run'])
def test_inconsistent_saved_business_refs_fail_closed(tmp_path, changed):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, _, _, model, graph = await graph_case(tmp_path, saver)
            state = graph.store.load_state('run-1')
            if changed == 'event_missing':
                state['business_event_id'] = 0
            elif changed == 'event_version':
                state['state_version'] = 0
            elif changed == 'different_contract':
                state['contract_version'] = 2
            else:
                state['run_id'] = 'different-run'
            with pytest.raises(BusinessError) as error:
                graph._saved(SimpleNamespace(values=state, next=('wait',)), case[2])
            assert error.value.code == 'STATE_CONFLICT'
            assert not model.inputs and browser.capture_calls == browser.execute_calls == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('changed', ['wait_id', 'progress_id'])
def test_saved_wait_must_reference_actual_registered_wait(tmp_path, changed):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, _, _, _, graph = await graph_case(tmp_path, saver)
            await graph.run(case[2])
            saved = await graph.graph.aget_state({'configurable': {'thread_id': 'run-1'}})
            before = browser.capture_calls
            state = {**saved.values}
            state[changed] = 'unregistered-wait' if changed == 'wait_id' else 99999
            with pytest.raises(BusinessError) as error:
                graph._saved(SimpleNamespace(values=state, next=saved.next), case[2])
            assert error.value.code == 'STATE_CONFLICT'
            assert browser.capture_calls == before and browser.execute_calls == 0
    asyncio.run(exercise())


def test_wait_resume_replaces_provider_and_reobserves_after_explicit_business_reconciliation(tmp_path):
    from webagent.graph.recovery import RecoveryStore
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, gateway, verifier, model, graph = await graph_case(tmp_path, saver)
            captured=browser.capture
            target=graph.store.load_run('run-1')['contract'].targets[0].object_id
            async def actual_capture(*args, **kwargs):
                result=await captured(*args, **kwargs)
                visible=canonical_json({'object_id':target,'object_version':'2025'})
                result.update(visible_text=visible,visible_sha256=hashlib.sha256(visible.encode()).hexdigest())
                return result
            browser.capture=actual_capture
            browser.recovery_capture=actual_capture
            first = await graph.run(case[2])
            case[1].resume('run-1', first['state_version'])
            recovery = case[1].claim('worker-1', case[2].worker_generation)
            service=RecoveryStore(tmp_path,budgets=case[1].budgets)
            saved=await graph.graph.aget_state({'configurable':{'thread_id':'run-1'}})
            plan=service.begin(recovery,saved.values)
            proof=await gateway.recovery_observe(recovery,plan['recovery_id'])
            service.complete(recovery,proof['snapshot_id'],**service.observed_facts(recovery,proof['snapshot_id']))
            token = case[1].reconcile('run-1', recovery.state_version)
            replacement = Model()
            rebuilt = StateGraphAdapter(tmp_path, gateway, replacement, verifier, checkpointer=saver)
            result = await rebuilt.run(token)
            assert result['wait_id'] != first['wait_id'] and current(case[0])['state'] == 'PAUSED'
            assert len(model.inputs) == len(replacement.inputs) == 1
            assert browser.capture_calls == 3 and browser.execute_calls == 0
            assert replacement.inputs[0].observation.snapshot_id != model.inputs[0].observation.snapshot_id
            with connect(case[0].business_db) as db:
                rows = db.execute('SELECT * FROM graph_input_requests ORDER BY state_version').fetchall()
                assert len(rows) == 2 and rows[0]['wait_id'] == first['wait_id'] and rows[1]['wait_id'] == result['wait_id']
    asyncio.run(exercise())


@pytest.mark.parametrize('fields', [['field-' + str(i) for i in range(65)], ['x' * 257]])
def test_oversized_input_requests_spend_recovery_budget_without_persisting_raw_fields(tmp_path, fields):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            model = Model()
            model.requested_fields = fields
            case, browser, _, _, _, graph = await graph_case(tmp_path, saver, model=model)
            result = await graph.run(case[2])
            assert result['completed'] is True and current(case[0])['state'] == 'FAILED'
            assert len(model.inputs) == browser.capture_calls == 4
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM graph_input_requests').fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM graph_progress WHERE diagnostic='invalid_model_output'").fetchone()[0] >= 4
    asyncio.run(exercise())


def test_persistent_evidence_requests_terminate_by_durable_recovery_budget(tmp_path):
    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, _, _, model, graph = await graph_case(tmp_path, saver, model=Model(output='evidence'))
            result = await graph.run(case[2])
            assert result['completed'] is True and result['diagnostic'] == 'budget_exceeded'
            assert current(case[0])['state'] == 'FAILED'
            assert len(model.inputs) == browser.capture_calls == 4
            assert browser.execute_calls == 0
            budget = case[1].budgets.status('run-1')
            assert budget['exhausted'] is True
            assert list(budget['recovery_counts'].values()) == [3]
            with connect(case[0].business_db) as db:
                assert db.execute("SELECT count(DISTINCT recovery_key) FROM budget_attempts WHERE kind='recovery'").fetchone()[0] == 1
                assert db.execute("SELECT status FROM scheduler_queue").fetchone()[0] == 'FINISHED'
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
    asyncio.run(exercise())


def test_supplement_budget_exhaustion_aggregates_existing_verified_deliverables(tmp_path):
    """A current verifier capsule can produce PARTIAL without another call."""
    class ProposalModel(Model):
        async def generate(self, model_input, *, execution_token):
            self.inputs.append(model_input)
            body = setup()[1].model_dump(mode='json')
            evidence_ids = model_input.observation.evidence_ids
            body['evidence_ids'] = evidence_ids
            body['items']['values'][0]['evidence_ids'] = evidence_ids
            body['unresolved'] = ['An additional authorized source remains unread']
            return SimpleNamespace(output=ProposeResult.model_validate_json(canonical_json(body)))

    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            case, browser, _, verifier, model, graph = await graph_case(tmp_path, saver, model=ProposalModel())
            calls_at_aggregation = []
            verifier.fault_hook = lambda stage: calls_at_aggregation.append(len(model.inputs)) if stage == 'before_transition' else None
            captured = browser.capture
            original = deepcopy(setup()[2][0].content)
            visible = canonical_json(original)
            async def original_capture(*args, **kwargs):
                result = await captured(*args, **kwargs)
                result['visible_text'] = visible
                result['visible_sha256'] = hashlib.sha256(visible.encode()).hexdigest()
                return result
            browser.capture = original_capture
            state = await graph.run(case[2])
            assert state['completed'] is True and state['diagnostic'] == 'budget_exceeded'
            assert current(case[0])['state'] == 'PARTIAL'
            assert len(model.inputs) == browser.capture_calls and browser.execute_calls == 0
            assert calls_at_aggregation == [len(model.inputs)]
            recovery = case[1].budgets.status('run-1')['recovery_counts']
            assert max(recovery.values()) == 3 and all(count <= 3 for count in recovery.values())
            result = verifier.read('run-1')['result']
            assert result['generated_by'] == 'business_aggregator' and 'budget_exhausted' in result['unresolved']
            assert result['items']['values'][0]['normalized_value'] == original['values'][0]['normalized_value']
            with connect(case[0].business_db) as db:
                assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 1
                assert db.execute('SELECT count(*) FROM run_verifications').fetchone()[0] == len(model.inputs)
                assert db.execute("SELECT status FROM scheduler_queue").fetchone()[0] == 'FINISHED'
    asyncio.run(exercise())
