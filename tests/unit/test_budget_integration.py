"""Scheduled model requests share persistent authority and independent budget cancellation."""
import asyncio
from dataclasses import replace
import json

import pytest

from webagent.budgets.deadline import BudgetDeadlineExceeded, DeadlineController
from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.evidence.service import EvidenceService
from webagent.models.adapter import ModelAdapter
from webagent.models.journal import list_attempts
from webagent.models.schema import ModelInput
from webagent.models.transport import ModelConfig, ProviderReply
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.scheduler.worker import QueueWorker
from webagent.tasks.compiler import compile_draft


VALID = json.dumps({'type': 'RequestInput', 'requested_fields': ['selection'],
                    'reason': 'Choose the next synthetic source'})


class Provider:
    config = ModelConfig()

    def __init__(self, callback=None):
        self.callback = callback
        self.calls = 0

    async def complete(self, model_input, schema, *, images=(), repair_errors=None):
        self.calls += 1
        if self.callback:
            return await self.callback()
        return ProviderReply(VALID)


def prepared(tmp_path, provider, *, active_seconds=1200, limits=None):
    path = tmp_path / 'business.sqlite3'
    migrate(path)
    now = utc_text()
    contract = compile_draft(
        {'instruction': 'Inspect synthetic publications', 'scenario': 'research',
         'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixture'],
          'topic_criteria': ['fixture evidence'], 'cutoff_at': now, 'max_items': 3}},
        task_id='model-task', version=1, created_at=now,
        provenance=[{'origin': 'api', 'reference': 'budget-model-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    contract['budget_profile']['max_active_seconds'] = active_seconds
    contract['budget_profile'].update(limits or {})
    with connect(path) as db, transaction(db):
        create_task(db, task_id='model-task', instruction=contract['original_instruction'],
                    requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id='model-run', task_id='model-task', contract_version=1,
                   graph_version='graph-v1', graph_state_schema_version='state-v1',
                   model_config_sha256=provider.config.config_sha256, runtime_config_sha256='b' * 64)
    store = SchedulerStore(path)
    generation = store.start_worker('model-worker')
    store.enqueue('model-run', [Resource.site_identity('local-fixture', 'model-account'),
                                Resource.browser_context('model-run')], expected_state_version=0)
    token = store.claim('model-worker', generation)
    with connect(path) as db:
        budget_ref = db.execute("SELECT budget_record_id FROM run_budgets WHERE run_id='model-run'").fetchone()[0]
    content = {
        'run_id': 'model-run', 'contract': contract,
        'observation': {'snapshot_id': 'snapshot', 'run_id': 'model-run', 'captured_at': now,
                        'source_url': contract['start_urls'][0], 'title': 'Synthetic page',
                        'tab_id': 'tab', 'frame_id': 'frame', 'page_version': 'page',
                        'width': 100, 'height': 100, 'visible_excerpt': 'Fixture',
                        'evidence_ids': [], 'redaction_status': 'FILTERED'},
        'verified_checkpoint': {'checkpoint_id': 'checkpoint', 'task_id': 'model-task',
                                'run_id': 'model-run', 'contract_version': 1,
                                'current_subgoal': 'inspect', 'verified_item_ids': [],
                                'pending_item_ids': ['item'], 'current_object_id': 'object',
                                'current_object_version': None, 'current_snapshot_id': 'snapshot',
                                'flow_version': None, 'action_sequence': 0, 'business_event_id': 0,
                                'budget_record_ref': budget_ref, 'identity_ref': None,
                                'pending_operation_ids': [], 'epoch': token.epoch,
                                'evidence_ids': [], 'saved_at': now},
        'image_evidence_ids': [], 'allowed_action_schema_ref': 'urn:webagent:m0-contract-v1:Action',
        'selected_flow_versions': [],
    }
    content['observation'] = EvidenceService(tmp_path).publish_observation(
        content['observation'], {'title': 'Synthetic page', 'text': 'Fixture'},
        execution_token=token)
    model_input = ModelInput.model_validate_json(canonical_json(content))
    return path, store, token, model_input


def test_real_worker_dispatch_count_exhaustion_finishes_failed_without_recovery_loop(tmp_path):
    async def exercise():
        provider = Provider()
        path, store, setup_token, _ = prepared(tmp_path, provider, limits={'max_actions': 2})
        store.finish(setup_token)
        with connect(path) as db, transaction(db):
            create_run(db, run_id='worker-run', task_id='model-task', contract_version=1,
                       graph_version='graph-v1', graph_state_schema_version='state-v1',
                       model_config_sha256=provider.config.config_sha256, runtime_config_sha256='b' * 64)
        store.enqueue('worker-run', [Resource.site_identity('local-fixture', 'worker-account'),
                                     Resource.browser_context('worker-run')], expected_state_version=0)
        entered = asyncio.Event()

        async def executor(token):
            entered.set()
            for number in range(3):
                store.budgets.consume(token, kind='action', attempt_id='dispatch-' + str(number))

        worker = QueueWorker(store, 'bounded-worker', executor=executor,
                             poll_seconds=.005, heartbeat_seconds=.02,
                             shutdown_seconds=.2, deadline_seconds=.005)
        try:
            await worker.tick()
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(asyncio.gather(*worker._claims.values()), 1)
            status = store.budgets.status('worker-run')
            assert status['actions_used'] == 2
            assert status['exhausted'] and status['reason'] == 'action_limit'
            with connect(path) as db:
                assert db.execute("SELECT state FROM runs WHERE run_id='worker-run'").fetchone()[0] == 'FAILED'
            assert not worker._failed
            assert not any(row['resource_type'] == 'active_slot' for row in store.snapshot()['leases'])
        finally:
            await worker.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize('qualification', ['missing', 'old_epoch', 'wrong_worker', 'wrong_run'])
def test_scheduled_model_requires_fresh_execution_qualification_before_any_attempt(tmp_path, qualification):
    provider = Provider()
    path, store, token, model_input = prepared(tmp_path, provider)
    changed = {'missing': None, 'old_epoch': replace(token, epoch=token.epoch + 1),
               'wrong_worker': replace(token, worker_id='another-worker'),
               'wrong_run': replace(token, run_id='other-run')}[qualification]
    with pytest.raises(BusinessError):
        asyncio.run(ModelAdapter(path, provider).generate(model_input, execution_token=changed))
    assert provider.calls == 0
    assert list_attempts(path, token.run_id) == []
    assert store.budgets.status(token.run_id)['model_calls_used'] == 0


def test_registered_model_request_and_repairs_charge_original_budget_once_per_attempt(tmp_path):
    async def exercise():
        responses = iter(['{broken', VALID])

        async def reply():
            return ProviderReply(next(responses))

        provider = Provider(reply)
        path, store, token, model_input = prepared(tmp_path, provider)
        result = await ModelAdapter(path, provider).generate(model_input, execution_token=token)
        assert result.output.type == 'RequestInput'
        assert provider.calls == 2
        assert [row['status'] for row in list_attempts(path, token.run_id)] == ['INVALID', 'VALID']
        assert store.budgets.status(token.run_id)['model_calls_used'] == 2
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0] == 1

    asyncio.run(exercise())


def test_reconciling_model_candidate_is_discarded_when_epoch_changes_without_state_version(tmp_path):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def suspended_reply():
            entered.set()
            await release.wait()
            return ProviderReply(VALID)

        provider = Provider(suspended_reply)
        path, store, first, model_input = prepared(tmp_path, provider)
        paused = store.defer(first, 'PAUSED')
        store.resume(first.run_id, paused['run_state_version'])
        token = store.claim(first.worker_id, first.worker_generation)
        assert store.validate(token, allow_reconciling=True)['state'] == 'RECONCILING'
        model_input = model_input.model_copy(update={'verified_checkpoint':
            model_input.verified_checkpoint.model_copy(update={'epoch': token.epoch})})
        operation = asyncio.create_task(ModelAdapter(path, provider).generate(model_input, execution_token=token))
        await asyncio.wait_for(entered.wait(), 1)
        store.start_worker('replacement-worker')
        with connect(path) as db:
            assert db.execute('SELECT state_version FROM runs WHERE run_id=?', (token.run_id,)).fetchone()[0] == token.state_version
        release.set()
        with pytest.raises(BusinessError) as discarded:
            await operation
        assert discarded.value.status == 409
        assert list_attempts(path, token.run_id)[0]['status'] == 'CANCELLED'

    asyncio.run(exercise())


def test_runtime_guard_accepts_trusted_reconciliation_but_old_action_token_stays_stale(tmp_path):
    async def exercise():
        path, store, setup_token, _ = prepared(tmp_path, Provider())
        store.abandon(setup_token)
        completed = asyncio.Event()

        async def executor(recovery_token):
            current = store.reconcile(recovery_token.run_id, recovery_token.state_version)
            with pytest.raises(BusinessError):
                store.budgets.consume(recovery_token, kind='action', attempt_id='stale-after-reconcile')
            # The watchdog and lease heartbeats must observe the queue's
            # trusted updated state binding, without authorizing old actions.
            await asyncio.sleep(.08)
            store.validate(current)
            store.finish(current)
            completed.set()

        worker = QueueWorker(store, 'recovery-worker', executor=executor,
                             poll_seconds=.005, heartbeat_seconds=.01,
                             shutdown_seconds=.2, deadline_seconds=.005)
        try:
            await worker.tick()
            await asyncio.wait_for(completed.wait(), 1)
            await asyncio.wait_for(asyncio.gather(*worker._claims.values()), 1)
            assert not worker._failed
            assert list_attempts(path, setup_token.run_id) == []
            assert store.budgets.status(setup_token.run_id)['actions_used'] == 0
        finally:
            await worker.aclose()

    asyncio.run(exercise())


def test_pause_closes_model_interval_without_duration_double_charge_or_late_candidate(tmp_path):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def suspended_reply():
            entered.set()
            await release.wait()
            return ProviderReply(VALID)

        provider = Provider(suspended_reply)
        path, store, token, model_input = prepared(tmp_path, provider)
        operation = asyncio.create_task(ModelAdapter(path, provider).generate(model_input, execution_token=token))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.02)
        store.defer(token, 'PAUSED')
        committed = store.budgets.status(token.run_id)['active_ms']
        await asyncio.sleep(.02)
        release.set()
        with pytest.raises(BusinessError) as stale:
            await operation
        assert stale.value.status == 409
        assert list_attempts(path, token.run_id)[0]['status'] == 'CANCELLED'
        assert store.budgets.status(token.run_id)['active_ms'] == committed

    asyncio.run(exercise())


def test_independent_deadline_fences_hanging_model_before_cancel_and_records_cancelled(tmp_path):
    async def exercise():
        entered, cleaned = asyncio.Event(), asyncio.Event()
        cancelled_state = []
        fixture = {}

        async def hanging():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                with connect(fixture['path']) as db:
                    cancelled_state.append(db.execute("SELECT state FROM runs WHERE run_id='model-run'").fetchone()[0])
                cleaned.set()

        provider = Provider(hanging)
        path, store, token, model_input = prepared(tmp_path, provider, active_seconds=1)
        fixture['path'] = path

        async def check():
            return await asyncio.to_thread(store.budgets.flush, token)

        async def expire(reason):
            await asyncio.to_thread(store.expire_budget, token.run_id, reason)

        guard = DeadlineController(check, expire, poll_seconds=.01, cancel_seconds=.5)
        operation = asyncio.create_task(guard.run(
            ModelAdapter(path, provider).generate(model_input, execution_token=token)))
        await asyncio.wait_for(entered.wait(), 1)
        # A durable consumed-time checkpoint simulates a deadline reached while
        # the provider is hanging; no provider reply drives termination.
        with connect(path) as db, transaction(db):
            db.execute("UPDATE run_budgets SET active_ms=1000,state_version=state_version+1 WHERE run_id='model-run'")
        with pytest.raises(BudgetDeadlineExceeded) as expired:
            await asyncio.wait_for(operation, 1)
        assert expired.value.reason == 'active_time'
        assert expired.value.cancellation_completed
        assert cleaned.is_set() and cancelled_state == ['FAILED']
        attempts = list_attempts(path, token.run_id)
        assert len(attempts) == 1 and attempts[0]['status'] == 'CANCELLED'
        assert attempts[0]['record']['usage']['input_tokens'] is None
        assert store.budgets.status(token.run_id)['model_calls_used'] == 1
        with pytest.raises(BusinessError):
            store.validate(token)
        assert not any(row['resource_type'] == 'active_slot' for row in store.snapshot()['leases'])

    asyncio.run(exercise())


def test_model_own_timeout_reports_shared_budget_stop_before_slower_watchdog(tmp_path):
    async def exercise():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def hanging():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        provider = Provider(hanging)
        path, store, setup_token, model_input = prepared(tmp_path, provider, active_seconds=1)
        store.abandon(setup_token)

        async def executor(token):
            current_input = model_input.model_copy(update={'verified_checkpoint':
                model_input.verified_checkpoint.model_copy(update={'epoch': token.epoch})})
            await ModelAdapter(path, provider).generate(current_input, execution_token=token)

        worker = QueueWorker(store, 'timeout-worker', executor=executor,
                             poll_seconds=.005, heartbeat_seconds=.02,
                             shutdown_seconds=.2, deadline_seconds=2)
        try:
            await worker.tick()
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(asyncio.gather(*worker._claims.values()), 3)
            assert cleaned.is_set()
            assert list_attempts(path, setup_token.run_id)[0]['status'] == 'CANCELLED'
            status = store.budgets.status(setup_token.run_id)
            assert status['exhausted'] and status['reason'] == 'active_time'
            with connect(path) as db:
                assert db.execute('SELECT state FROM runs WHERE run_id=?', (setup_token.run_id,)).fetchone()[0] == 'FAILED'
            assert not worker._failed
        finally:
            await worker.aclose()

    asyncio.run(exercise())
