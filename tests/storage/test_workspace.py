"""Workbench reads reconstruct durable facts without performing any work."""
import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest

from api_support import AuthenticatedTestClient, create_test_app
from webagent.budgets.store import BudgetStore
from webagent.config import Settings
from webagent.controls.models import ControlRequest
from webagent.db import connect, transaction
from webagent.db.repository import canonical_json, utc_text
from webagent.errors import BusinessError
from webagent.events import ActionEvent, OperationCompletedEvent, append_event
from webagent.settings.models import ModelConnection, ModelSettingsRequest
from webagent.settings.service import update_model
from webagent.state import transition
from webagent.tasks import service as tasks
from webagent.tasks.models import CreateTaskRequest, RevisionRequest
from webagent.verification.service import VerificationService
from webagent.workspace.store import run_events, workspace
from pydantic import SecretStr
from storage.test_graph_store import setup_run
from storage.test_gateway_store import fixture as gateway_fixture, observe
from storage.test_run_controls import FINANCE, prepared, request, start


def _ledger(path):
    with connect(path) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        return {table: [tuple(row) for row in db.execute('SELECT * FROM "' + table + '"')]
                for table in tables}


def _error(callback, code, status):
    with pytest.raises(BusinessError) as caught:
        callback()
    assert caught.value.code == code and caught.value.status == status


def test_prepared_workspace_has_contract_without_inventing_a_run(tmp_path):
    path, task, _, _, _ = prepared(tmp_path)
    before = _ledger(path)
    view = workspace(path, task['task_id'])
    assert view['task']['preparation_status'] == 'READY'
    assert view['run'] is view['checkpoint'] is view['observation'] is view['budget'] is view['queue'] is None
    assert view['current_subgoal'] is None and view['controls'] == view['events'] == view['graph_progress'] == []
    assert view['event_cursor'] == view['event_high_water'] == view['global_event_high_water'] == '0'
    assert not view['is_current_run'] and view['criteria_contract_version'] == 1
    assert view['criteria'] and not any(row['verified'] for row in view['criteria'])
    assert _ledger(path) == before


def test_missing_input_workspace_does_not_derive_fake_criteria(database):
    reply = tasks.create(database, CreateTaskRequest(instruction='还需补参'), 'draft')
    view = workspace(database, reply.body['task']['task_id'])
    assert view['task']['preparation_status'] == 'NEEDS_INPUT' and view['criteria_contract_version'] is None
    assert view['criteria'] == [] and view['run'] is None


def test_workspace_separates_pending_receipt_from_actual_run_state(tmp_path):
    path, task, _, _, controls = prepared(tmp_path)
    op = controls.request(task['task_id'], 'start', ControlRequest(expected_state_version=task['state_version'],
        contract_version=1, settings_version=1), 'start')['operation']
    pending = workspace(path, task['task_id'])
    assert pending['run']['state'] == 'QUEUED' and pending['run']['started_at'] is None
    assert pending['queue']['status'] == 'QUEUED' and pending['queue']['reason'] is None
    assert pending['controls'][0]['status'] == 'PENDING' and pending['controls'][0]['result'] is None
    assert pending['controls'][0]['requested_event_id'] == pending['events'][0]['event_id']
    assert pending['event_cursor'] == pending['controls'][0]['requested_event_id']
    assert controls.apply_idle(op['run_id'])['status'] == 'APPLIED'
    complete = workspace(path, task['task_id'])
    assert complete['controls'][0]['status'] == 'APPLIED' and complete['run']['state'] == 'QUEUED'
    assert complete['controls'][0]['completed_event_id'] == complete['event_cursor']
    assert not complete['budget']['initialized'] and not complete['budget']['exhausted']


def test_historical_run_is_bound_to_task_and_retains_frozen_contract_settings(tmp_path):
    case = prepared(tmp_path)
    path, task, secret, _, controls = case
    token, original = start(case)
    request(case, original['run_id'], 'cancel')
    assert controls.apply_at_boundary(token)['state'] == 'CANCELLED'
    other = tasks.create(path, CreateTaskRequest.model_validate(deepcopy(FINANCE)), 'other').body['task']
    _error(lambda: workspace(path, other['task_id'], run_id=original['run_id']), 'NOT_FOUND', 404)
    _error(lambda: workspace(path, task['task_id'], run_id='missing'), 'NOT_FOUND', 404)


def test_read_old_run_after_contract_revision_and_retry_does_not_follow_new_task_head(tmp_path):
    case = prepared(tmp_path)
    path, task, secret, scheduler, controls = case
    token, first = start(case)
    request(case, first['run_id'], 'cancel')
    cancelled = controls.apply_at_boundary(token)
    update_model(path, secret, ModelSettingsRequest(expected_version=1, model=ModelConnection(),
        accept_data_sharing=True, api_key=SecretStr('SYNTHETIC_WORKSPACE_ROTATED_KEY')))
    before = tasks.detail(path, task['task_id'])
    version = before['task']['current_contract_version']
    revised = tasks.change(path, task['task_id'], RevisionRequest.model_validate({
        **deepcopy(FINANCE), 'contract_version': version, 'instruction': 'Read revised finance'}), 'revise', clarification=False)
    current = revised.body['task']
    second = controls.request(task['task_id'], 'retry', ControlRequest(expected_state_version=cancelled['state_version'],
        contract_version=2, settings_version=2), 'retry')['operation']
    old = workspace(path, task['task_id'], run_id=first['run_id'])
    new = workspace(path, task['task_id'])
    assert old['run']['state'] == 'CANCELLED' and old['run']['contract_version'] == old['criteria_contract_version'] == 1
    assert old['run']['settings_version'] == 1 and not old['is_current_run']
    assert new['run']['run_id'] == second['run_id'] and new['run']['contract_version'] == 2
    assert new['run']['settings_version'] == 2 and new['is_current_run']
    assert all(event['run_id'] == first['run_id'] for event in old['events'])
    assert all(event['run_id'] == second['run_id'] for event in new['events'])


def test_verified_criteria_come_from_actual_verification_checkpoint_not_proposal(tmp_path):
    store, _, _, evidence, _, case = setup_run(tmp_path)
    contract, proposal, documents, bindings = case
    first = workspace(store.path, 'task-1', run_id='run-1')
    assert not any(item['verified'] for item in first['criteria']) and first['current_subgoal'] is None
    doc = documents[0]
    evidence.store.publish('run-1', canonical_json(doc.content).encode(), evidence_id='e1',
        source_url=doc.source_url, captured_at=doc.captured_at, object_id=doc.object_id,
        query_scope='original', locator_or_page='json')
    verifier = VerificationService(tmp_path)
    verifier.begin('run-1', 1)
    record = asyncio.run(verifier.verify('run-1', proposal, bindings, expected_state_version=2))
    cp = store.checkpoint_observation('run-1', 'snapshot-1', expected_state_version=2)
    store.record_progress('run-1', 'verify', expected_state_version=2,
                          verification_id=record['verification_id'])
    view = workspace(store.path, 'task-1', run_id='run-1')
    passed = {item['criterion_id'] for item in record['checks'] if item['verdict'] == 'PASS'}
    assert passed and {item['criterion_id'] for item in view['criteria'] if item['verified']} == passed
    assert view['checkpoint']['verified_item_ids'] == cp.verified_item_ids
    assert view['current_subgoal'] == cp.current_subgoal
    assert view['graph_progress'][-1]['phase'] == 'verify'
    assert view['run']['state'] == 'VERIFYING' and not view['observation']


def test_gateway_observation_and_budget_are_real_read_only_facts(database):
    f = gateway_fixture(database)
    snapshot = observe(f)
    before = _ledger(database)
    view = workspace(database, 'task-gateway', run_id=f.token.run_id)
    assert view['observation']['snapshot_id'] == snapshot['snapshot_id'] and view['observation']['valid']
    assert view['observation']['state_version'] == f.token.state_version
    assert view['observation']['page_version'] == f.binding['page_version']
    assert view['observation']['evidence'] == [] and view['observation']['screenshot_evidence_id'] is None
    assert view['budget']['observations_used'] == 1 and view['budget']['actions_used'] == 0
    assert view['queue']['status'] == 'ACTIVE'
    assert 'SECRET' not in json.dumps(view) and not any(key in view['observation'] for key in (
        'source_url', 'title', 'visible_excerpt', 'session_id', 'manager_id'))
    assert _ledger(database) == before
    with connect(database) as db, transaction(db):
        db.execute('UPDATE gateway_page_heads SET valid=0 WHERE run_id=?', (f.token.run_id,))
    assert not workspace(database, 'task-gateway', run_id=f.token.run_id)['observation']['valid']


def test_one_snapshot_remains_coherent_when_writer_commits_between_projection_reads(tmp_path, monkeypatch):
    case = prepared(tmp_path)
    path, task, _, _, _ = case
    token, op = start(case)
    original = BudgetStore._status
    def concurrent_status(self, db, run_id, **kwargs):
        transition(path, run_id=run_id, expected_state_version=token.state_version,
                   target='PAUSED', blocked_reason='waiting')
        return original(self, db, run_id, **kwargs)
    monkeypatch.setattr(BudgetStore, '_status', concurrent_status)
    view = workspace(path, task['task_id'])
    assert view['run']['state'] == 'RUNNING' and view['run']['state_version'] == token.state_version
    assert view['events'][-1]['payload']['current_state'] == 'RUNNING'
    with connect(path) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (op['run_id'],)).fetchone()[0] == 'PAUSED'


def test_event_cursors_are_exact_64_bit_text_and_global_gaps_are_not_loss(tmp_path):
    case = prepared(tmp_path)
    path, task, _, _, _ = case
    token, op = start(case)
    with connect(path) as db, transaction(db):
        db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='task_events'", (2**53 + 123,))
        first = append_event(db, run_id=token.run_id, expected_state_version=token.state_version,
            payload=ActionEvent(step_id='step-first', action_type='read_visible', attempt_status='COMPLETED', evidence_ids=[]))
        # The global sequence can contain committed events from other Runs.
        db.execute("UPDATE sqlite_sequence SET seq=seq+10 WHERE name='task_events'")
        second = append_event(db, run_id=token.run_id, expected_state_version=token.state_version,
            payload=ActionEvent(step_id='step-second', action_type='read_visible', attempt_status='COMPLETED', evidence_ids=[]))
    page = run_events(path, token.run_id, after=first['event_id'] - 1, limit=1)
    assert page['events'][0]['event_id'] == str(first['event_id']) and page['has_more']
    last = run_events(path, token.run_id, after=int(page['cursor']), limit=1)
    assert last['events'][0]['event_id'] == str(second['event_id']) and not last['has_more']
    assert last['cursor'] == last['high_water'] == last['global_high_water'] == str(second['event_id'])
    view = workspace(path, task['task_id'])
    assert view['event_cursor'] == str(second['event_id'])
    assert run_events(path, token.run_id, after=second['event_id'])['events'] == []
    _error(lambda: run_events(path, token.run_id, after=second['event_id'] + 1), 'INVALID_PARAMETER', 422)


def test_workspace_event_window_is_bounded_but_replay_recovers_full_history(tmp_path):
    case = prepared(tmp_path)
    path, task, _, _, _ = case
    token, _ = start(case)
    with connect(path) as db, transaction(db):
        for index in range(110):
            append_event(db, run_id=token.run_id, expected_state_version=token.state_version,
                payload=ActionEvent(step_id=f'step-{index}', action_type='read_visible',
                                    attempt_status='COMPLETED', evidence_ids=[]))
    view = workspace(path, task['task_id'])
    assert len(view['events']) == 100 and view['has_earlier_events']
    cursor, replay = 0, []
    while True:
        page = run_events(path, token.run_id, after=cursor, limit=31)
        replay.extend(page['events'])
        cursor = int(page['cursor'])
        if not page['has_more']:
            break
    assert len(replay) == 113 and len({item['event_id'] for item in replay}) == 113
    assert replay[-100:] == view['events'] and str(cursor) == view['event_cursor']


def test_unknown_blocking_prose_and_event_extras_never_leak_to_projection(tmp_path):
    case = prepared(tmp_path)
    path, task, _, _, _ = case
    token, _ = start(case)
    transition(path, run_id=token.run_id, expected_state_version=token.state_version,
               target='PAUSED', blocked_reason='SYNTHETIC_PRIVATE_PROVIDER_PROMPT_CANARY')
    with connect(path) as db, transaction(db):
        db.execute('''INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json)
            SELECT task_id,run_id,'action_recorded',state_version,created_at,? FROM runs WHERE run_id=?''',
            (canonical_json({'event_type': 'action_recorded', 'step_id': 'safe-id',
                'action_type': 'read_visible', 'attempt_status': 'COMPLETED', 'evidence_ids': [],
                'provider_prompt': 'SYNTHETIC_PRIVATE_PROVIDER_PROMPT_CANARY'}), token.run_id))
    view = workspace(path, task['task_id'])
    assert view['run']['blocked_reason'] == 'unrecognized_reason'
    assert 'SYNTHETIC_PRIVATE_PROVIDER_PROMPT_CANARY' not in json.dumps(view)
    assert 'provider_prompt' not in json.dumps(run_events(path, token.run_id))


def test_latest_control_and_graph_windows_remain_bounded_and_authoritative(tmp_path):
    case = prepared(tmp_path)
    path, task, _, _, controls = case
    token, _ = start(case)
    request(case, token.run_id, 'pause', key='initial-pause')
    controls.apply_at_boundary(token)
    for index in range(25):
        request(case, token.run_id, 'pause', key=f'pause-{index}')
        assert controls.apply_idle(token.run_id)['state'] == 'PAUSED'
    view = workspace(path, task['task_id'])
    assert view['run']['state'] == 'PAUSED' and view['queue']['status'] == 'WAITING'
    assert view['queue']['reason'] == 'waiting' and view['run']['blocked_reason'] == 'waiting'
    assert len(view['controls']) == len(view['graph_progress']) == 20
    assert all(row['status'] == 'APPLIED' and row['state'] == 'PAUSED' for row in view['controls'])
    assert all(row['phase'] == 'wait' for row in view['graph_progress'])
    assert [int(row['operation_seq']) for row in view['controls']] == sorted(
        int(row['operation_seq']) for row in view['controls'])


@pytest.mark.parametrize('payload', [
    {'event_type': 'action_recorded', 'step_id': 'step', 'action_type': 'PRIVATE_CANARY',
     'attempt_status': 'COMPLETED', 'evidence_ids': []},
    {'event_type': 'action_recorded', 'step_id': 'step', 'action_type': 'read_visible',
     'attempt_status': 'PRIVATE_CANARY', 'evidence_ids': []},
    {'event_type': 'wait_registered', 'wait_id': 'wait', 'reason': 'PRIVATE_CANARY', 'deadline': None},
    {'event_type': 'result_ready', 'result_ref': 'result', 'outcome': 'PRIVATE_CANARY'},
])
def test_malformed_metadata_enum_fails_closed_without_egress(tmp_path, payload):
    case = prepared(tmp_path)
    path, task, _, _, _ = case
    token, _ = start(case)
    with connect(path) as db, transaction(db):
        db.execute('''INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json)
            SELECT task_id,run_id,?,state_version,created_at,? FROM runs WHERE run_id=?''',
            (payload['event_type'], canonical_json(payload), token.run_id))
    for reader in (lambda: workspace(path, task['task_id']), lambda: run_events(path, token.run_id)):
        _error(reader, 'STORAGE_UNAVAILABLE', 503)


def test_corrupt_graph_diagnostic_does_not_export_raw_provider_prose(tmp_path):
    store, _, _, _, _, _ = setup_run(tmp_path)
    store.record_progress('run-1', 'decide', expected_state_version=1)
    # Explicit owned corruption fixture, never a production mutation path.
    with connect(store.path) as db, transaction(db):
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('''INSERT INTO graph_progress(run_id,state_version,contract_sha256,graph_version,
            state_schema_version,phase,business_event_id,iteration,idempotency_key,occurred_at,diagnostic)
            SELECT run_id,state_version,contract_sha256,graph_version,state_schema_version,phase,
                business_event_id,iteration,?,occurred_at,'PRIVATE_CANARY' FROM graph_progress LIMIT 1''',
            ('c' * 64,))
    _error(lambda: workspace(store.path, 'task-1', run_id='run-1'), 'STORAGE_UNAVAILABLE', 503)


@pytest.mark.parametrize('effects', [['PRIVATE_CANARY'], [{'status': 'PRIVATE_CANARY'}]])
def test_malformed_control_result_is_safe_storage_failure(tmp_path, effects):
    path, task, _, _, controls = prepared(tmp_path)
    operation = controls.request(task['task_id'], 'start', ControlRequest(
        expected_state_version=task['state_version'], contract_version=1, settings_version=1), 'start')['operation']
    with connect(path) as db, transaction(db):
        event = append_event(db, run_id=operation['run_id'], expected_state_version=0,
            payload=OperationCompletedEvent(operation_id=operation['operation_id'], action='start',
                                             status='APPLIED', result_ref=operation['operation_id']))
        result = {'run_id': operation['run_id'], 'state': 'QUEUED', 'state_version': 0,
                  'wait_id': None, 'side_effects': effects}
        db.execute('''UPDATE run_controls SET status='APPLIED',completed_event_id=?,completed_at=?,
            result_json=? WHERE operation_id=?''', (event['event_id'], utc_text(), canonical_json(result),
                                                   operation['operation_id']))
    _error(lambda: workspace(path, task['task_id']), 'STORAGE_UNAVAILABLE', 503)


def test_http_workspace_and_replay_are_authenticated_no_store_and_read_only(tmp_path):
    case = prepared(tmp_path)
    path, task, secret, _, _ = case
    _, op = start(case)
    before = _ledger(path)
    with AuthenticatedTestClient(create_test_app(Settings(path.parent), secret_store=secret)) as client:
        for route in ('/v1/tasks/' + task['task_id'] + '/workspace', '/v1/runs/' + op['run_id'] + '/events'):
            response = client.get(route)
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
            assert client.get(route, headers={'Authorization': ''}).status_code == 401
    assert _ledger(path) == before


@pytest.mark.parametrize('route,query', [
    ('workspace', 'run_id='), ('workspace', 'run_id=a&run_id=b'), ('workspace', 'after=0'),
    ('events', 'after=-1'), ('events', 'after=01'), ('events', 'after=9223372036854775808'),
    ('events', 'after=1&after=2'), ('events', 'after='), ('events', 'limit=0'),
    ('events', 'limit=101'), ('events', 'limit=01'), ('events', 'limit=true'),
    ('events', 'limit=1&limit=2'), ('events', 'run_id=other'), ('events', 'after=0%20OR%201=1'),
])
def test_http_workspace_queries_reject_unbounded_or_ambiguous_values(tmp_path, route, query):
    path, task, secret, _, _ = prepared(tmp_path)
    with AuthenticatedTestClient(create_test_app(Settings(path.parent), secret_store=secret)) as client:
        url = '/v1/tasks/' + task['task_id'] + '/workspace' if route == 'workspace' else '/v1/runs/not-found/events'
        assert client.get(url + '?' + query).status_code == 422


@pytest.mark.parametrize('reader', [lambda p: workspace(p, 'task'), lambda p: run_events(p, 'run')])
def test_workspace_never_creates_or_migrates_missing_old_storage(tmp_path, reader):
    path = tmp_path / 'business.sqlite3'
    _error(lambda: reader(path), 'STORAGE_UNAVAILABLE', 503)
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=1')
    before = path.read_bytes()
    _error(lambda: reader(path), 'STORAGE_UNAVAILABLE', 503)
    assert path.read_bytes() == before
