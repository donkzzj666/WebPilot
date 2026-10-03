"""Offline adapter behavior with real SQLite accounting and cancellable providers."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

import pytest
from api_support import AuthenticatedTestClient as TestClient

from api_support import create_test_app as create_app
from webagent.config import Settings
from webagent.db import LATEST_VERSION, connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.events import read_events
from webagent.evidence.service import EvidenceService
from webagent.models.adapter import ModelAdapter, ModelError
from webagent.models.journal import list_attempts, reserve_attempt
from webagent.models.schema import ModelInput
from webagent.models.transport import ModelConfig, ProviderFailure, ProviderReply
from webagent.state import transition
from webagent.tasks.compiler import compile_draft

NOW = '2026-09-29T00:00:00Z'
VALID = json.dumps({'type': 'RequestInput', 'requested_fields': ['selection'],
                    'reason': 'Please choose the next source'})
USAGE = {'input_tokens': 12, 'output_tokens': 7, 'image_units': None,
         'provider_usage': {'total_tokens': 19}}


class FakeProvider:
    def __init__(self, *replies, config=None):
        self.config = config or ModelConfig()
        self.replies = list(replies)
        self.calls = []

    async def complete(self, model_input, schema, *, images=(), repair_errors=None):
        self.calls.append({'input': deepcopy(model_input), 'schema': deepcopy(schema),
                           'images': images, 'repair_errors': deepcopy(repair_errors)})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply()
        if isinstance(reply, ProviderReply):
            return reply
        return ProviderReply(reply, provider_request_id='provider-' + str(len(self.calls)), usage=deepcopy(USAGE))


def seed_input(path, provider, *, repairs=2, budget=True, state='RUNNING', active_ms=0,
               publish_evidence=True):
    contract = compile_draft(
        {'instruction': 'Inspect synthetic publications', 'scenario': 'research',
         'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixtures'],
          'topic_criteria': ['fixture evidence'], 'cutoff_at': NOW, 'max_items': 3}},
        task_id='task-1', version=1, created_at=NOW,
        provenance=[{'origin': 'api', 'reference': 'synthetic-request',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    contract['budget_profile']['max_model_format_repairs'] = repairs
    with connect(path) as db, transaction(db):
        create_task(db, task_id='task-1', instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id='run-1', task_id='task-1', contract_version=1,
                   graph_version='graph-v1', graph_state_schema_version='graph-schema-v1',
                   model_config_sha256=provider.config.config_sha256, runtime_config_sha256='b' * 64)
        if budget:
            db.execute('INSERT INTO run_budgets(budget_record_id,run_id,active_ms) VALUES (?,?,?)',
                       ('budget-1', 'run-1', active_ms))
    if state != 'QUEUED':
        transition(path, run_id='run-1', expected_state_version=0, target='RUNNING')
        if state != 'RUNNING':
            transition(path, run_id='run-1', expected_state_version=1, target=state)
    content = {
        'run_id': 'run-1', 'contract': contract,
        'observation': {'snapshot_id': 'snapshot-1', 'run_id': 'run-1', 'captured_at': NOW,
                        'source_url': contract['start_urls'][0], 'title': 'Synthetic page',
                        'tab_id': 'tab-1', 'frame_id': 'frame-1', 'page_version': 'page-1',
                        'width': 100, 'height': 100, 'visible_excerpt': 'Fixture content',
                        'evidence_ids': ['text-1'], 'redaction_status': 'FILTERED'},
        'verified_checkpoint': {'checkpoint_id': 'checkpoint-1', 'task_id': 'task-1', 'run_id': 'run-1',
                                'contract_version': 1, 'current_subgoal': 'inspect', 'verified_item_ids': [],
                                'pending_item_ids': ['item-1'], 'current_object_id': 'object-1',
                                'current_object_version': None, 'current_snapshot_id': 'snapshot-1',
                                'flow_version': None, 'action_sequence': 0, 'business_event_id': 0,
                                'budget_record_ref': 'budget-1', 'identity_ref': None,
                                'pending_operation_ids': [], 'epoch': 1, 'evidence_ids': [], 'saved_at': NOW},
        'image_evidence_ids': [], 'allowed_action_schema_ref': 'urn:webagent:m0-contract-v1:Action',
        'selected_flow_versions': [],
    }
    if publish_evidence:
        content['observation'] = EvidenceService(path.parent).publish_observation(
            content['observation'], {'title': 'Synthetic page', 'text': 'Fixture content'})
    return ModelInput.model_validate_json(canonical_json(content))


def run_row(path):
    with connect(path) as db:
        return dict(db.execute('SELECT * FROM runs WHERE run_id=?', ('run-1',)).fetchone())


def budget_row(path):
    with connect(path) as db:
        return dict(db.execute('SELECT * FROM run_budgets WHERE run_id=?', ('run-1',)).fetchone())


def recorded_usage(number=1):
    return {**USAGE, 'provider_usage': {**USAGE['provider_usage'], 'attempt_number': number}}


def test_success_persists_actual_usage_and_preserves_run_state(database):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider)
    before_run, before_events = run_row(database), read_events(database)
    result = asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert result.output.type == 'RequestInput'
    attempts = list_attempts(database, 'run-1')
    assert len(attempts) == len(result.records) == 1
    assert attempts[0]['status'] == 'VALID'
    record = attempts[0]['record']
    assert record['run_id'] == 'run-1'
    assert record['request_id'] and record['provider_request_id'] == 'provider-1'
    assert record['config_sha256'] == provider.config.config_sha256
    assert record['usage'] == recorded_usage()
    assert record['duration_ms'] >= 0
    assert record['format_repairs'] == 0 and record['error_class'] is None
    assert record['estimated_cost'] is None and record['price_version'] is None
    assert budget_row(database)['model_calls_used'] == 1
    assert run_row(database) == before_run
    assert read_events(database) == before_events


@pytest.mark.parametrize('repairs', [0, 1, 2])
def test_invalid_output_stops_at_contract_format_repair_cap(database, repairs):
    provider = FakeProvider('{broken', '{broken', '{broken', VALID)
    prepared = seed_input(database, provider, repairs=repairs)
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.error_class == 'invalid_output'
    attempts = list_attempts(database, 'run-1')
    assert len(provider.calls) == len(attempts) == repairs + 1
    assert budget_row(database)['model_calls_used'] == repairs + 1
    assert [item['status'] for item in attempts] == ['INVALID'] * (repairs + 1)
    assert [item['record']['format_repairs'] for item in attempts] == list(range(repairs + 1))
    assert all(item['record']['error_class'] == 'invalid_output' for item in attempts)
    assert len({item['record']['request_id'] for item in attempts}) == repairs + 1
    assert provider.calls[0]['repair_errors'] is None
    assert all(call['repair_errors'] for call in provider.calls[1:])


@pytest.mark.parametrize('failures', [1, 2])
def test_repaired_output_retains_failed_attempts_and_original_run_budget(database, failures):
    provider = FakeProvider(*(['{broken'] * failures), VALID)
    prepared = seed_input(database, provider)
    result = asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert result.output.type == 'RequestInput'
    attempts = list_attempts(database, 'run-1')
    assert [item['status'] for item in attempts] == ['INVALID'] * failures + ['VALID']
    assert budget_row(database)['model_calls_used'] == failures + 1
    assert all(item['record']['run_id'] == prepared.run_id for item in attempts)
    assert all(item['record']['usage'] == recorded_usage(number) for number, item in enumerate(attempts, 1))
    assert all(call['input'] == prepared.model_dump(mode='json') for call in provider.calls)


@pytest.mark.parametrize('bad_output', [
    '{"type":"SUCCEEDED"}', '{"success":true}',
    '{"type":"Action","action":{"action_type":"evaluate","code":"run"}}',
    '{"type":"Action","action":{"action_type":"shell","command":"run"}}',
    '{"type":"RequestEvidence","criterion_ids":["invented"],"source_ids":["local-fixture"],"needed":"x"}',
])
def test_unsafe_output_and_reference_inventions_never_become_accepted_output(database, bad_output):
    provider = FakeProvider(bad_output)
    prepared = seed_input(database, provider, repairs=0)
    before = run_row(database)
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.error_class == 'invalid_output'
    assert list_attempts(database, 'run-1')[0]['status'] == 'INVALID'
    assert run_row(database) == before


@pytest.mark.parametrize('error_class,subtype,http_status,code,status', [
    ('invalid_credentials', 'http_401', 401, 'UPSTREAM_ERROR', 502),
    ('rate_limit', 'http_429', 429, 'MODEL_RATE_LIMIT', 429),
    ('provider_error', 'http_402', 402, 'UPSTREAM_ERROR', 502),
    ('provider_error', 'http_503', 503, 'UPSTREAM_ERROR', 502),
    ('timeout', 'read_timeout', None, 'TIMEOUT', 504),
])
def test_provider_faults_have_distinct_errors_no_format_retries_or_site_gate(database, error_class, subtype, http_status, code, status):
    provider = FakeProvider(ProviderFailure(error_class, subtype, http_status=http_status), VALID)
    prepared = seed_input(database, provider)
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert (caught.value.error_class, caught.value.code, caught.value.status) == (error_class, code, status)
    assert len(provider.calls) == 1
    attempts = list_attempts(database, 'run-1')
    assert attempts[0]['status'] == 'ERROR'
    assert attempts[0]['record']['error_class'] == error_class
    assert attempts[0]['record']['usage']['input_tokens'] is None
    assert budget_row(database)['model_calls_used'] == 1
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM site_gates').fetchone()[0] == 0


def test_shared_deadline_cancels_inflight_repair_instead_of_resetting_timeout(database):
    cancelled = []

    async def slow_invalid():
        await asyncio.sleep(0.12)
        return ProviderReply('{broken')

    async def suspended():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    config = ModelConfig(connect_seconds=0.3, read_seconds=0.3, total_seconds=0.3)
    provider = FakeProvider(slow_invalid, suspended, config=config)
    prepared = seed_input(database, provider)
    started = time.monotonic()
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.code == 'TIMEOUT'
    assert time.monotonic() - started < 0.7
    assert cancelled == [True] and len(provider.calls) == 2
    attempts = list_attempts(database, 'run-1')
    assert [item['status'] for item in attempts] == ['INVALID', 'ERROR']
    assert attempts[1]['record']['usage']['input_tokens'] is None


def test_external_cancellation_is_propagated_and_persisted(database):
    async def exercise():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def suspended():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        provider = FakeProvider(suspended)
        prepared = seed_input(database, provider)
        task = asyncio.create_task(ModelAdapter(database, provider).generate(prepared))
        await asyncio.wait_for(entered.wait(), 1)
        assert list_attempts(database, 'run-1')[0]['status'] == 'STARTED'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    asyncio.run(exercise())
    attempts = list_attempts(database, 'run-1')
    assert attempts[0]['status'] == 'CANCELLED'
    assert attempts[0]['record']['usage']['input_tokens'] is None
    assert budget_row(database)['model_calls_used'] == 1


@pytest.mark.parametrize('binding', ['config', 'contract', 'budget_reference', 'missing_budget'])
def test_unbound_input_is_rejected_before_call_or_budget_charge(database, binding):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider, budget=binding != 'missing_budget')
    if binding == 'config':
        provider.config = ModelConfig(max_tokens=512)
    elif binding == 'contract':
        body = prepared.model_dump(mode='json')
        body['contract']['objective'] = 'Different contract content'
        prepared = ModelInput.model_validate_json(canonical_json(body))
    elif binding == 'budget_reference':
        body = prepared.model_dump(mode='json')
        body['verified_checkpoint']['budget_record_ref'] = 'other-budget'
        prepared = ModelInput.model_validate_json(canonical_json(body))
    with pytest.raises(BusinessError):
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert provider.calls == []
    assert list_attempts(database, 'run-1') == []
    if binding != 'missing_budget':
        assert budget_row(database)['model_calls_used'] == 0


@pytest.mark.parametrize('state', ['QUEUED', 'PAUSED', 'WAITING_CI', 'WAITING_SITE', 'FAILED', 'CANCELLED'])
def test_nonexecuting_run_does_not_start_model_requests(database, state):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider, state=state)
    with pytest.raises(BusinessError):
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert provider.calls == [] and budget_row(database)['model_calls_used'] == 0


@pytest.mark.parametrize('state', ['RUNNING', 'VERIFYING', 'RECONCILING'])
def test_eligible_states_allow_model_proposals_without_changing_state(database, state):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider, state=state)
    before = run_row(database)
    asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert run_row(database) == before


def test_state_change_while_waiting_discards_model_result(database):
    async def changed():
        transition(database, run_id='run-1', expected_state_version=1, target='PAUSED')
        return ProviderReply(VALID, usage=deepcopy(USAGE))

    provider = FakeProvider(changed)
    prepared = seed_input(database, provider)
    with pytest.raises(BusinessError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.code == 'STATE_CONFLICT'
    assert run_row(database)['state'] == 'PAUSED'
    attempts = list_attempts(database, 'run-1')
    assert attempts[0]['status'] != 'VALID'
    assert attempts[0]['record']['usage'] == recorded_usage()


def test_exhausted_active_budget_prevents_provider_call(database):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider, active_ms=1200 * 1000)
    with pytest.raises(BusinessError):
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert provider.calls == []
    assert budget_row(database)['model_calls_used'] == 0


def test_expired_caller_deadline_never_starts_provider(database):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider)
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared, deadline=time.monotonic() - 1))
    assert caught.value.code == 'TIMEOUT'
    assert provider.calls == []


def test_migration_v4_preserves_existing_data_and_events(tmp_path):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=4)
    provider = FakeProvider(VALID)
    prepared = seed_input(path, provider, publish_evidence=False)
    before_run, before_budget, before_events = run_row(path), budget_row(path), read_events(path)
    with connect(path) as db:
        before_contract = dict(db.execute('SELECT * FROM contracts').fetchone())
    assert migrate(path)['applied'] == LATEST_VERSION - 4
    assert migrate(path)['applied'] == 0
    assert run_row(path) == before_run and budget_row(path) == before_budget
    assert read_events(path) == before_events
    with connect(path) as db:
        assert dict(db.execute('SELECT * FROM contracts').fetchone()) == before_contract
    assert list_attempts(path, 'run-1') == []
    body = prepared.model_dump(mode='json')
    body['observation'] = EvidenceService(path.parent).publish_observation(
        body['observation'], {'title': 'Synthetic page', 'text': 'Fixture content'})
    prepared = ModelInput.model_validate_json(canonical_json(body))
    asyncio.run(ModelAdapter(path, provider).generate(prepared))
    assert budget_row(path)['model_calls_used'] == 1


def test_run_cannot_have_parallel_provider_calls(database):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def suspended():
            entered.set()
            await release.wait()
            return ProviderReply(VALID)

        provider = FakeProvider(suspended)
        prepared = seed_input(database, provider)
        first = asyncio.create_task(ModelAdapter(database, provider).generate(prepared))
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(BusinessError) as caught:
            await ModelAdapter(database, provider).generate(prepared)
        assert caught.value.code == 'STATE_CONFLICT'
        release.set()
        await first
        assert len(provider.calls) == 1

    asyncio.run(exercise())
    assert budget_row(database)['model_calls_used'] == 1
    assert len(list_attempts(database, 'run-1')) == 1


def test_unfinished_attempt_survives_reopening_and_never_triggers_blind_retry(database):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider)
    reserve_attempt(database, prepared, provider.config, call_id='interrupted-call',
                    request_id='interrupted-request', attempt_number=1)
    # Simulate the persisted boundary after process exit, before provider outcome.
    assert migrate(database)['applied'] == 0
    with pytest.raises(BusinessError):
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    attempts = list_attempts(database, 'run-1')
    assert len(attempts) == 1 and attempts[0]['status'] == 'STARTED'
    assert attempts[0]['record'] is None
    assert provider.calls == [] and budget_row(database)['model_calls_used'] == 1


def test_completed_records_cannot_be_rewritten_or_removed(database):
    provider = FakeProvider(VALID)
    prepared = seed_input(database, provider)
    asyncio.run(ModelAdapter(database, provider).generate(prepared))
    statements = [
        "UPDATE model_attempts SET diagnostic_subtype='changed'",
        'DELETE FROM model_attempts', 'DELETE FROM model_generations',
        "UPDATE model_generations SET repair_limit=0",
    ]
    for statement in statements:
        with pytest.raises(sqlite3.IntegrityError):
            with connect(database) as db, transaction(db):
                db.execute(statement)
    assert list_attempts(database, 'run-1')[0]['status'] == 'VALID'


def test_missing_usage_stays_unknown_instead_of_becoming_zero(database):
    provider = FakeProvider(ProviderReply(VALID))
    prepared = seed_input(database, provider)
    asyncio.run(ModelAdapter(database, provider).generate(prepared))
    record = list_attempts(database, 'run-1')[0]['record']
    assert record['usage'] == {'input_tokens': None, 'output_tokens': None,
                               'image_units': None, 'provider_usage': {'attempt_number': 1}}
    assert record['estimated_cost'] is None


def test_invalid_response_body_never_enters_journal_or_repair_prompt(database):
    sentinel = 'SYNTHETIC_PRIVATE_PROVIDER_RESPONSE_779'
    bad = json.dumps({'type': 'RequestInput', 'requested_fields': ['selection'],
                      'reason': 'Select a source', sentinel: sentinel})
    provider = FakeProvider(bad, VALID)
    prepared = seed_input(database, provider)
    asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert sentinel not in repr(list_attempts(database, 'run-1'))
    assert sentinel not in repr(provider.calls[1]['repair_errors'])
    with connect(database) as db:
        assert sentinel not in repr([tuple(row) for row in db.execute('SELECT * FROM model_generations')])


@pytest.mark.parametrize('with_failure', [False, True])
def test_replacement_provider_malformed_metadata_fails_without_leaking_content(database, with_failure):
    secret = 'SYNTHETIC_BAD_METADATA_SECRET_991'
    reply = ProviderReply(VALID, usage={'input_tokens': secret})
    value = ProviderFailure('provider_error', 'invalid_response', reply=reply) if with_failure else reply
    provider = FakeProvider(value)
    prepared = seed_input(database, provider)
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.error_class == 'provider_error'
    assert secret not in str(caught.value)
    attempts = list_attempts(database, 'run-1')
    assert attempts[0]['status'] == 'ERROR'
    assert secret not in repr(attempts)


@pytest.mark.parametrize('error_class,subtype,http_status,expected_status,expected_code,retry', [
    ('invalid_credentials', 'provider_authentication', 401, 502, 'UPSTREAM_ERROR', None),
    ('rate_limit', 'provider_error', 429, 429, 'MODEL_RATE_LIMIT', 2.2),
])
def test_http_error_boundary_distinguishes_upstream_auth_and_rate_limit(database, error_class, subtype,
                                                                      http_status, expected_status, expected_code, retry):
    provider = FakeProvider(ProviderFailure(error_class, subtype, http_status=http_status,
                                           retry_after_seconds=retry))
    prepared = seed_input(database, provider)
    app = create_app(Settings(database.parent))

    @app.post('/test-only/model-proposal')
    async def proposal():
        await ModelAdapter(database, provider).generate(prepared)

    with TestClient(app) as client:
        result = client.post('/test-only/model-proposal')
    assert result.status_code == expected_status
    assert result.json()['code'] == expected_code
    assert result.json()['request_id'] == result.headers['X-Request-ID']
    assert result.json()['retryable'] is (error_class == 'rate_limit')
    if retry is None:
        assert 'Retry-After' not in result.headers
    else:
        assert result.headers['Retry-After'] == '3'
    assert list_attempts(database, 'run-1')[0]['record']['error_class'] == error_class


def test_active_interval_is_flushed_once_without_double_counting_model_duration(database):
    async def delayed():
        await asyncio.sleep(0.05)
        return ProviderReply(VALID)

    provider = FakeProvider(delayed)
    prepared = seed_input(database, provider, active_ms=1000)
    interval_start = datetime.now(timezone.utc) - timedelta(seconds=1)
    with connect(database) as db, transaction(db):
        db.execute('UPDATE run_budgets SET active_interval_started_at=? WHERE run_id=?',
                   (utc_text(interval_start), prepared.run_id))
    asyncio.run(ModelAdapter(database, provider).generate(prepared))
    budget = budget_row(database)
    persisted_at = datetime.fromisoformat(budget['last_persisted_at'].replace('Z', '+00:00'))
    elapsed = (persisted_at - interval_start).total_seconds() * 1000
    assert abs(budget['active_ms'] - (1000 + elapsed)) < 3
    assert budget['active_interval_started_at'] == budget['last_persisted_at']


def test_remaining_active_budget_limits_provider_wait(database):
    cleaned = []

    async def suspended():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    provider = FakeProvider(suspended)
    prepared = seed_input(database, provider, active_ms=1200 * 1000 - 50)
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.code == 'TIMEOUT'
    assert cleaned == [True]
    assert list_attempts(database, 'run-1')[0]['status'] == 'ERROR'


def test_reservation_consuming_deadline_prevents_any_provider_dispatch(database, monkeypatch):
    from webagent.models import adapter as adapter_module

    original = adapter_module.reserve_attempt

    def delayed_reservation(*args, **kwargs):
        original(*args, **kwargs)
        time.sleep(0.04)

    monkeypatch.setattr(adapter_module, 'reserve_attempt', delayed_reservation)
    config = ModelConfig(connect_seconds=0.02, read_seconds=0.02, total_seconds=0.02)
    provider = FakeProvider(VALID, config=config)
    prepared = seed_input(database, provider)
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.code == 'TIMEOUT'
    assert provider.calls == []
    attempt = list_attempts(database, 'run-1')[0]
    assert attempt['status'] == 'ERROR'
    assert attempt['record']['error_class'] == 'timeout'
    assert attempt['record']['usage']['input_tokens'] is None


def test_provider_config_rebinding_during_wait_invalidates_candidate(database):
    async def reconfigured():
        provider.config = ModelConfig(max_tokens=512)
        return ProviderReply(VALID, usage=deepcopy(USAGE))

    provider = FakeProvider(reconfigured)
    prepared = seed_input(database, provider)
    original_digest = provider.config.config_sha256
    with pytest.raises(ModelError) as caught:
        asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert caught.value.error_class == 'provider_error'
    assert len(provider.calls) == 1
    attempt = list_attempts(database, 'run-1')[0]
    assert attempt['status'] == 'ERROR'
    assert attempt['record']['config_sha256'] == original_digest
    assert run_row(database)['model_config_sha256'] == original_digest


def test_provider_cannot_mutate_input_or_schema_of_subsequent_repair(database):
    class MutatingProvider(FakeProvider):
        async def complete(self, model_input, schema, *, images=(), repair_errors=None):
            self.calls.append({'input': deepcopy(model_input), 'schema': deepcopy(schema)})
            if len(self.calls) == 1:
                model_input['contract']['objective'] = 'An accidentally mutated request'
                model_input['observation']['visible_excerpt'] = 'Changed observation'
                schema.clear()
                return ProviderReply('{broken')
            return ProviderReply(VALID)

    provider = MutatingProvider()
    prepared = seed_input(database, provider)
    original = prepared.model_dump(mode='json')
    result = asyncio.run(ModelAdapter(database, provider).generate(prepared))
    assert result.output.type == 'RequestInput'
    assert provider.calls[0]['input'] == provider.calls[1]['input'] == original
    assert provider.calls[0]['schema'] == provider.calls[1]['schema']
    assert provider.calls[1]['schema']['$defs']
    assert prepared.model_dump(mode='json') == original
