"""Task preparation, immutable contracts and durable HTTP idempotency acceptance.

These tests use an isolated migrated database. Runs are seeded only to prove that
task edits cannot rewrite an existing execution or its contract.
"""
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from api_support import AuthenticatedTestClient as TestClient

from api_support import create_test_app as create_app
from webagent.config import Settings
from webagent.db import connect, transaction
from webagent.db.repository import create_run
from webagent.state import transition


FINANCE = {
    'instruction': '读取夹具财报',
    'source_ids': ['local-fixture'],
    'scenario': 'finance',
    'parameters': {
        'entity_id': 'fixture-company', 'report_version': '2025',
        'period_type': 'annual', 'metrics': ['revenue'], 'currency': 'USD',
    },
}


@pytest.fixture
def client(database):
    with TestClient(create_app(Settings(database.parent))) as current:
        yield current


def post(client, body, *, key='create-1', path='/v1/tasks'):
    return client.post(path, json=body, headers={'Idempotency-Key': key})


def create_ready(client, key='create-1'):
    response = post(client, deepcopy(FINANCE), key=key)
    assert response.status_code == 201, response.text
    return response.json()


def counts(database):
    with connect(database) as db:
        return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('tasks', 'contracts', 'runs', 'task_revisions', 'api_idempotency')}


def attach_run(database, task_id, run_id='old-run'):
    with connect(database) as db, transaction(db):
        create_run(db, run_id=run_id, task_id=task_id, contract_version=1,
                   graph_version='test-graph', graph_state_schema_version='test-state',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
        db.execute('UPDATE tasks SET current_run_id=? WHERE task_id=?', (run_id, task_id))
    return run_id


def test_missing_input_is_a_durable_preparation_task_without_execution(client, database):
    response = post(client, {'instruction': '还需要澄清的任务'})
    assert response.status_code == 201
    data = response.json()
    task = data['task']
    assert task['preparation_status'] == 'NEEDS_INPUT'
    assert task['state_version'] == 1
    assert task['original_instruction'] == '还需要澄清的任务'
    assert data['contract_version'] == 1
    assert task['current_contract_version'] is None
    assert task['requested_fields'] == data['missing_fields']
    assert 'scenario' in data['missing_fields'] and 'source_ids' in data['missing_fields']
    assert data['contract'] is None and data['current_run'] is None
    assert data['historical_runs'] == [] and data['contract_history'] == []
    assert task['current_run_id'] is None and task['historical_run_ids'] == []
    assert len(data['revisions']) == 1
    detail = client.get(f"/v1/tasks/{task['task_id']}")
    assert detail.status_code == 200
    for field in ('task', 'contract_version', 'contract', 'draft', 'missing_fields', 'revisions'):
        assert detail.json()[field] == data[field]
    assert counts(database) == {'tasks': 1, 'contracts': 0, 'runs': 0,
                                'task_revisions': 1, 'api_idempotency': 1}


def test_complete_task_freezes_contract_and_queries_have_consistent_history(client, database):
    data = create_ready(client)
    task = data['task']
    task_id = task['task_id']
    assert task['preparation_status'] == 'READY'
    assert task['state_version'] == 1
    assert task['current_contract_version'] == data['contract_version'] == 1
    assert data['missing_fields'] == task['requested_fields'] == []
    assert data['contract']['task_id'] == task_id
    assert data['contract']['contract_version'] == 1
    assert data['contract']['original_instruction'] == FINANCE['instruction']
    assert data['contract']['scenario'] == 'finance'
    assert data['current_run'] is None and data['historical_runs'] == []
    assert data['contract_history'] == [data['contract']]
    assert client.get(f'/v1/tasks/{task_id}/contracts').json() == {
        'task_id': task_id, 'contracts': [data['contract']]}
    assert client.get(f'/v1/tasks/{task_id}/runs').json() == {'task_id': task_id, 'runs': []}
    assert client.get('/health').json()['task_execution_enabled'] is True
    assert counts(database) == {'tasks': 1, 'contracts': 1, 'runs': 0,
                                'task_revisions': 1, 'api_idempotency': 1}


def test_normalized_body_and_optional_body_key_replay_the_exact_response(client, database):
    first = post(client, FINANCE, key='normalized')
    assert first.status_code == 201
    reordered = dict(reversed(list(FINANCE.items())))
    reordered['parameters'] = dict(reversed(list(FINANCE['parameters'].items())))
    reordered['idempotency_key'] = 'normalized'
    replay = post(client, reordered, key='normalized')
    assert replay.status_code == first.status_code
    assert replay.content == first.content
    assert replay.headers['x-request-id'] == first.headers['x-request-id']
    assert first.json()['request_id'] == first.headers['x-request-id']
    assert counts(database)['tasks'] == counts(database)['api_idempotency'] == 1


def test_replay_survives_api_restart_and_later_task_edits(database):
    app_settings = Settings(database.parent)
    with TestClient(create_app(app_settings)) as first_client:
        first = post(first_client, FINANCE)
        assert first.status_code == 201
        task_id = first.json()['task']['task_id']
        revision = {**deepcopy(FINANCE), 'contract_version': 1, 'instruction': '改查夹具财报'}
        changed = post(first_client, revision, key='edit', path=f'/v1/tasks/{task_id}/revisions')
        assert changed.status_code == 200, changed.text
    with TestClient(create_app(app_settings)) as restarted:
        replay = post(restarted, FINANCE)
        assert replay.status_code == first.status_code
        assert replay.content == first.content
        assert restarted.get(f'/v1/tasks/{task_id}').json()['contract_version'] == 2
    assert counts(database) == {'tasks': 1, 'contracts': 2, 'runs': 0,
                                'task_revisions': 2, 'api_idempotency': 2}


def test_reused_key_rejects_changed_input_without_writes(client, database):
    create_ready(client, key='same-key')
    before = counts(database)
    changed = deepcopy(FINANCE)
    changed['parameters']['currency'] = 'EUR'
    response = post(client, changed, key='same-key')
    assert response.status_code == 409
    assert response.json()['code'] == 'IDEMPOTENCY_CONFLICT'
    assert counts(database) == before


def test_list_order_is_part_of_idempotent_request_identity(client):
    body = deepcopy(FINANCE)
    body['parameters']['metrics'] = ['revenue', 'net_income']
    first = post(client, body)
    assert first.status_code == 201, first.text
    body['parameters']['metrics'].reverse()
    conflict = post(client, body)
    assert conflict.status_code == 409
    assert conflict.json()['code'] == 'IDEMPOTENCY_CONFLICT'


def test_omitted_and_explicit_defaults_share_request_identity(client, database):
    first = post(client, FINANCE)
    explicit = {**deepcopy(FINANCE), 'action_policy': {'mode': 'read_only'}, 'identity_ref': None}
    replay = post(client, explicit)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert counts(database)['tasks'] == 1


@pytest.mark.parametrize('headers,body', [
    ({}, FINANCE),
    ({'Idempotency-Key': ''}, FINANCE),
    ({'Idempotency-Key': '   '}, FINANCE),
    ({'Idempotency-Key': 'header'}, {**FINANCE, 'idempotency_key': 'body'}),
    ({}, {**FINANCE, 'idempotency_key': 'body-only'}),
])
def test_mutations_require_a_nonempty_matching_header_key(client, database, headers, body):
    response = client.post('/v1/tasks', json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()['code'] == 'INVALID_PARAMETER'
    assert counts(database)['tasks'] == counts(database)['api_idempotency'] == 0


def test_duplicate_key_headers_and_missing_keys_on_followups_are_rejected(client, database):
    duplicate = client.post('/v1/tasks', json=FINANCE,
                            headers=[('Idempotency-Key', 'same'), ('Idempotency-Key', 'same')])
    assert duplicate.status_code == 422
    task_id = create_ready(client)['task']['task_id']
    before = counts(database)
    for suffix, body in [('revisions', {**FINANCE, 'contract_version': 1}),
                         ('clarifications', {'contract_version': 1, 'values': {'scenario': 'finance'}})]:
        response = client.post(f'/v1/tasks/{task_id}/{suffix}', json=body)
        assert response.status_code == 422
    assert counts(database) == before


@pytest.mark.parametrize('change', [
    {'instruction': ''}, {'instruction': '   '}, {'instruction': 123},
    {'scenario': 'unknown'}, {'source_ids': 'local-fixture'},
    {'parameters': []}, {'unexpected': True},
])
def test_supplied_malformed_create_values_are_rejected(client, database, change):
    response = post(client, {**deepcopy(FINANCE), **change})
    assert response.status_code == 422, response.text
    assert counts(database)['tasks'] == counts(database)['api_idempotency'] == 0


def test_partial_clarification_advances_draft_cursor_and_old_retry_stays_exact(client, database):
    incomplete = deepcopy(FINANCE)
    del incomplete['parameters']['currency']
    del incomplete['parameters']['metrics']
    created = post(client, incomplete)
    assert created.status_code == 201
    task_id = created.json()['task']['task_id']
    path = f'/v1/tasks/{task_id}/clarifications'
    assert set(created.json()['missing_fields']) == {'parameters.currency', 'parameters.metrics'}
    first_body = {'contract_version': 1, 'values': {'parameters.currency': 'USD'}}
    first = post(client, first_body, path=path, key='clarify-one')
    assert first.status_code == 200, first.text
    assert first.json()['contract_version'] == 2
    assert first.json()['missing_fields'] == ['parameters.metrics']
    assert first.json()['contract'] is None
    ready = post(client, {'contract_version': 2, 'values': {'parameters.metrics': ['revenue']}},
                 path=path, key='clarify-two')
    assert ready.status_code == 200, ready.text
    data = ready.json()
    assert data['contract_version'] == data['contract']['contract_version'] == 3
    assert data['task']['current_contract_version'] == 3
    assert data['task']['preparation_status'] == 'READY'
    assert [revision['revision'] for revision in data['revisions']] == [1, 2, 3]
    assert data['revisions'][0]['parent_revision'] is None
    assert [revision['parent_revision'] for revision in data['revisions'][1:]] == [1, 2]
    assert len(data['contract_history']) == 1
    stale = post(client, first_body, path=path, key='new-stale-key')
    assert stale.status_code == 409
    assert stale.json()['code'] == 'CONTRACT_VERSION_CONFLICT'
    assert stale.json()['current_contract_version'] == 3
    replay = post(client, first_body, path=path, key='clarify-one')
    assert replay.content == first.content and replay.status_code == 200
    assert counts(database) == {'tasks': 1, 'contracts': 1, 'runs': 0,
                                'task_revisions': 3, 'api_idempotency': 3}


def test_clarification_retains_every_contributing_request_provenance_and_revision_replaces_it(client):
    incomplete = deepcopy(FINANCE)
    incomplete['action_policy'] = {'mode': 'read_only'}
    incomplete['identity_ref'] = None
    del incomplete['parameters']['currency']
    del incomplete['parameters']['metrics']
    created = post(client, incomplete).json()
    task_id = created['task']['task_id']
    path = f'/v1/tasks/{task_id}/clarifications'
    currency_input = {'contract_version': 1, 'values': {'parameters.currency': 'USD'}}
    currency = post(client, currency_input, path=path, key='add-currency')
    assert currency.status_code == 200
    metrics_input = {'contract_version': 2, 'values': {'parameters.metrics': ['revenue']}}
    ready = post(client, metrics_input, path=path, key='add-metrics')
    assert ready.status_code == 200
    compiled = ready.json()

    def api_grant(body, request_id):
        normalized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {'origin': 'api', 'reference': request_id,
                'content_sha256': hashlib.sha256(normalized.encode('utf-8')).hexdigest(),
                'authorizes_execution': True}

    expected_grants = [api_grant(incomplete, created['request_id']),
                       api_grant(currency_input, currency.json()['request_id']),
                       api_grant(metrics_input, compiled['request_id'])]
    grants = [item for item in compiled['contract']['provenance'] if item['origin'] == 'api']
    assert grants == expected_grants
    assert [item['request_sha256'] for item in compiled['revisions']] == [
        grant['content_sha256'] for grant in expected_grants]

    replacement_input = {**deepcopy(FINANCE), 'contract_version': 3,
                         'instruction': '明确替换任务目标', 'action_policy': {'mode': 'read_only'},
                         'identity_ref': None}
    replacement = post(client, replacement_input, key='replace-all',
                       path=f'/v1/tasks/{task_id}/revisions')
    assert replacement.status_code == 200
    changed = replacement.json()
    changed_grants = [item for item in changed['contract']['provenance'] if item['origin'] == 'api']
    assert changed_grants == [api_grant(replacement_input, changed['request_id'])]
    assert changed['contract_history'][0] == compiled['contract']


def test_invalid_clarification_does_not_consume_key_or_change_draft(client, database):
    body = deepcopy(FINANCE)
    del body['parameters']['currency']
    created = post(client, body).json()
    path = f"/v1/tasks/{created['task']['task_id']}/clarifications"
    before = counts(database)
    for values in ({'parameters.currency': 123}, {'parameters.entity_id': 'replacement'},
                   {'unknown': 'value'}, {}):
        rejected = post(client, {'contract_version': 1, 'values': values}, path=path, key='retry')
        assert rejected.status_code == 422, rejected.text
        assert counts(database) == before
    accepted = post(client, {'contract_version': 1, 'values': {'parameters.currency': 'USD'}},
                    path=path, key='retry')
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['task']['preparation_status'] == 'READY'


@pytest.mark.parametrize('version', [0, -1, True, '1', 1.5])
def test_draft_version_requires_a_positive_integer(client, version):
    created = create_ready(client)
    task_id = created['task']['task_id']
    response = post(client, {**deepcopy(FINANCE), 'contract_version': version},
                    key='revise', path=f'/v1/tasks/{task_id}/revisions')
    assert response.status_code == 422


def test_revision_preserves_original_instruction_failed_run_and_old_contract(client, database):
    original = create_ready(client)
    task_id = original['task']['task_id']
    run_id = attach_run(database, task_id)
    transition(database, run_id=run_id, expected_state_version=0, target='RUNNING')
    transition(database, run_id=run_id, expected_state_version=1, target='FAILED')
    with connect(database) as db:
        old_run = dict(db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone())
        old_contract = tuple(db.execute('SELECT content_json,contract_sha256 FROM contracts WHERE task_id=?',
                                        (task_id,)).fetchone())
    revised = deepcopy(FINANCE)
    revised.update(instruction='改查下一年夹具财报', contract_version=1)
    revised['parameters']['report_version'] = '2026'
    response = post(client, revised, key='revision', path=f'/v1/tasks/{task_id}/revisions')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['task']['original_instruction'] == FINANCE['instruction']
    assert data['contract']['original_instruction'] == FINANCE['instruction']
    assert data['draft']['instruction'] == revised['instruction']
    assert data['contract']['objective'] == revised['instruction']
    assert data['contract_version'] == data['task']['current_contract_version'] == 2
    assert data['contract_history'][0] == original['contract']
    assert [item['contract_version'] for item in data['contract_history']] == [1, 2]
    assert [item['run_id'] for item in data['historical_runs']] == [run_id]
    assert data['historical_runs'][0]['state'] == 'FAILED'
    assert data['historical_runs'][0]['contract_version'] == 1
    runs = client.get(f'/v1/tasks/{task_id}/runs').json()['runs']
    assert len(runs) == 1 and runs[0]['run_id'] == run_id
    with connect(database) as db:
        assert dict(db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()) == old_run
        assert tuple(db.execute('SELECT content_json,contract_sha256 FROM contracts WHERE task_id=? AND contract_version=1',
                                (task_id,)).fetchone()) == old_contract
    stale = post(client, revised, key='fresh-key', path=f'/v1/tasks/{task_id}/revisions')
    assert stale.status_code == 409 and stale.json()['code'] == 'CONTRACT_VERSION_CONFLICT'


def test_replacement_draft_does_not_silently_inherit_missing_inputs(client):
    original = create_ready(client)
    task_id = original['task']['task_id']
    response = post(client, {'instruction': '换个需要澄清的目标', 'contract_version': 1},
                    key='replacement', path=f'/v1/tasks/{task_id}/revisions')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['contract_version'] == 2
    assert data['task']['preparation_status'] == 'NEEDS_INPUT'
    assert data['draft']['parameters'] == {}
    assert data['draft']['source_ids'] is None and data['draft']['scenario'] is None
    assert data['contract_history'] == [original['contract']]
    # The old contract remains available as history while the replacement is incomplete.
    assert data['task']['current_contract_version'] == 1
    assert data['revisions'][-1]['contract_version'] is None
    selected = post(client, {'contract_version': 2, 'values': {
        'source_ids': FINANCE['source_ids'], 'scenario': FINANCE['scenario']}},
        key='select-scenario', path=f'/v1/tasks/{task_id}/clarifications')
    assert selected.status_code == 200
    assert selected.json()['task']['current_contract_version'] == 1
    assert selected.json()['contract_version'] == 3
    ready = post(client, {'contract_version': 3, 'values': {
        'parameters.' + field: value for field, value in FINANCE['parameters'].items()}},
        key='complete-replacement', path=f'/v1/tasks/{task_id}/clarifications')
    assert ready.status_code == 200
    assert ready.json()['task']['preparation_status'] == 'READY'
    assert [item['contract_version'] for item in ready.json()['contract_history']] == [1, 4]


@pytest.mark.parametrize('state', ['QUEUED', 'RUNNING', 'PAUSED', 'VERIFYING'])
def test_nonterminal_run_prevents_revising_its_task(client, database, state):
    data = create_ready(client)
    task_id = data['task']['task_id']
    run_id = attach_run(database, task_id)
    if state != 'QUEUED':
        transition(database, run_id=run_id, expected_state_version=0, target='RUNNING')
    if state not in ('QUEUED', 'RUNNING'):
        transition(database, run_id=run_id, expected_state_version=1, target=state)
    before = counts(database)
    response = post(client, {**deepcopy(FINANCE), 'contract_version': 1, 'instruction': '新目标'},
                    key='revision', path=f'/v1/tasks/{task_id}/revisions')
    assert response.status_code == 409, response.text
    assert counts(database) == before
    with connect(database) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] == state


def test_noncurrent_queued_run_still_prevents_contract_changes(client, database):
    task_id = create_ready(client)['task']['task_id']
    attach_run(database, task_id)
    with connect(database) as db, transaction(db):
        db.execute('UPDATE tasks SET current_run_id=NULL WHERE task_id=?', (task_id,))
    response = post(client, {**deepcopy(FINANCE), 'contract_version': 1},
                    key='revision', path=f'/v1/tasks/{task_id}/revisions')
    assert response.status_code == 409
    assert counts(database)['contracts'] == 1


def test_idempotency_keys_are_scoped_to_route_and_task(client, database):
    first = create_ready(client, key='shared')
    second = create_ready(client, key='other-create')
    for data in (first, second):
        task_id = data['task']['task_id']
        response = post(client, {**deepcopy(FINANCE), 'contract_version': 1, 'instruction': '修订后的目标'},
                        key='shared', path=f'/v1/tasks/{task_id}/revisions')
        assert response.status_code == 200, response.text
        assert response.json()['task']['task_id'] == task_id
    assert counts(database) == {'tasks': 2, 'contracts': 4, 'runs': 0,
                                'task_revisions': 4, 'api_idempotency': 4}


@pytest.mark.parametrize('suffix', ['', '/runs', '/contracts'])
def test_query_missing_task_returns_structured_not_found(client, suffix):
    response = client.get('/v1/tasks/does-not-exist' + suffix)
    assert response.status_code == 404
    body = response.json()
    assert body['code'] == 'NOT_FOUND' and body['retryable'] is False
    assert body['request_id'] == response.headers['x-request-id']


@pytest.mark.parametrize('suffix,body', [
    ('revisions', {**FINANCE, 'contract_version': 1}),
    ('clarifications', {'contract_version': 1, 'values': {'scenario': 'finance'}}),
])
def test_mutating_missing_task_does_not_consume_idempotency_key(client, database, suffix, body):
    response = post(client, body, path='/v1/tasks/does-not-exist/' + suffix)
    assert response.status_code == 404 and response.json()['code'] == 'NOT_FOUND'
    assert counts(database)['api_idempotency'] == 0


@pytest.mark.parametrize('table', ['task_revisions', 'api_idempotency'])
def test_preparation_history_and_idempotency_receipts_are_append_only(client, database, table):
    original = create_ready(client)
    with connect(database) as db:
        before = [dict(row) for row in db.execute(f'SELECT * FROM {table}')]
    for statement in (f"UPDATE {table} SET created_at='2030-01-01T00:00:00.000000Z'",
                      f'DELETE FROM {table}', f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
        with pytest.raises(sqlite3.IntegrityError):
            with connect(database) as db, transaction(db):
                db.execute(statement)
    with connect(database) as db:
        assert [dict(row) for row in db.execute(f'SELECT * FROM {table}')] == before
    assert post(client, FINANCE).json() == original


def test_ledger_failure_rolls_back_task_contract_and_revision_before_retry(database):
    # Inject failure after compilation without relying on a service-internal seam.
    with TestClient(create_app(Settings(database.parent)), raise_server_exceptions=False) as client:
        with connect(database) as db:
            db.execute("""CREATE TRIGGER fail_api_receipt BEFORE INSERT ON api_idempotency
                          BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END""")
        failed = post(client, FINANCE, key='recoverable')
        assert failed.status_code == 500
        error = failed.json()
        assert error['status'] == 500 and error['code'] == 'INTERNAL_ERROR'
        assert error['request_id'] == failed.headers['x-request-id']
        assert error['retryable'] is False
        assert 'injected' not in failed.text.lower() and 'sqlite' not in failed.text.lower()
        assert counts(database) == {'tasks': 0, 'contracts': 0, 'runs': 0,
                                    'task_revisions': 0, 'api_idempotency': 0}
        with connect(database) as db:
            db.execute('DROP TRIGGER fail_api_receipt')
        retry = post(client, FINANCE, key='recoverable')
        assert retry.status_code == 201, retry.text
    assert counts(database) == {'tasks': 1, 'contracts': 1, 'runs': 0,
                                'task_revisions': 1, 'api_idempotency': 1}


def test_failed_revision_receipt_preserves_current_contract_and_can_retry(database):
    with TestClient(create_app(Settings(database.parent)), raise_server_exceptions=False) as client:
        original = create_ready(client)
        task_id = original['task']['task_id']
        path = f'/v1/tasks/{task_id}/revisions'
        body = {**deepcopy(FINANCE), 'contract_version': 1, 'instruction': '新目标'}
        with connect(database) as db:
            db.execute("""CREATE TRIGGER fail_revision_receipt BEFORE INSERT ON api_idempotency
                          WHEN NEW.idempotency_key='edit'
                          BEGIN SELECT RAISE(ABORT,'injected revision receipt failure'); END""")
        response = post(client, body, path=path, key='edit')
        assert response.status_code == 500
        error = response.json()
        assert error['status'] == 500 and error['code'] == 'INTERNAL_ERROR'
        assert error['request_id'] == response.headers['x-request-id']
        assert error['retryable'] is False
        assert 'injected' not in response.text.lower() and 'sqlite' not in response.text.lower()
        current = client.get(f'/v1/tasks/{task_id}').json()
        for field in ('task', 'contract', 'contract_version', 'draft', 'revisions', 'contract_history'):
            assert current[field] == original[field]
        assert counts(database)['contracts'] == counts(database)['task_revisions'] == 1
        with connect(database) as db:
            db.execute('DROP TRIGGER fail_revision_receipt')
        retried = post(client, body, path=path, key='edit')
        assert retried.status_code == 200 and retried.json()['contract_version'] == 2


@pytest.mark.parametrize('committed', [False, True])
def test_process_exit_around_commit_never_leaves_a_task_without_its_receipt(database, committed):
    script = '''import json,os,sys
from pathlib import Path
from webagent.tasks import service
from webagent.tasks.models import CreateTaskRequest
if sys.argv[2]=='before':
    persist=service._persist_reply
    def stop_before_commit(*args,**kwargs):
        persist(*args,**kwargs)
        os._exit(73)
    service._persist_reply=stop_before_commit
service.create(Path(sys.argv[1]),CreateTaskRequest.model_validate(json.loads(sys.argv[3])),'interrupted')
os._exit(74)
'''
    root = Path(__file__).resolve().parents[2]
    exited = subprocess.run(
        [sys.executable, '-c', script, str(database), 'after' if committed else 'before', json.dumps(FINANCE)],
        env={**os.environ, 'PYTHONPATH': str(root / 'backend')}, timeout=15, capture_output=True, text=True)
    assert exited.returncode == (74 if committed else 73), exited.stderr
    expected = int(committed)
    assert counts(database) == {'tasks': expected, 'contracts': expected, 'runs': 0,
                                'task_revisions': expected, 'api_idempotency': expected}
    with connect(database) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        receipt = db.execute('SELECT response_json FROM api_idempotency').fetchone()
    with TestClient(create_app(Settings(database.parent))) as restarted:
        recovered = post(restarted, FINANCE, key='interrupted')
        assert recovered.status_code == 201
        if committed:
            assert recovered.content == receipt[0].encode('utf-8')
    assert counts(database) == {'tasks': 1, 'contracts': 1, 'runs': 0,
                                'task_revisions': 1, 'api_idempotency': 1}


def api_contender(database, start, ready, results, path, body, key):
    """Independent processes prove the ledger is not an in-memory request cache."""
    with TestClient(create_app(Settings(database.parent))) as client:
        ready.put(True)
        if not start.wait(20):
            raise RuntimeError('Concurrent request gate timed out')
        response = post(client, body, path=path, key=key)
        results.put((response.status_code, response.json()))


def race(database, requests):
    context = multiprocessing.get_context('spawn')
    start, ready, results = context.Event(), context.Queue(), context.Queue()
    processes = [context.Process(target=api_contender,
                                args=(database, start, ready, results, path, body, key))
                 for path, body, key in requests]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=25)
        start.set()
        responses = [results.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        return responses
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
        results.close()


def test_simultaneous_same_key_creates_one_task_and_identical_receipts(database):
    responses = race(database, [('/v1/tasks', FINANCE, 'concurrent')] * 4)
    assert [status for status, _ in responses] == [201] * 4
    assert all(body == responses[0][1] for _, body in responses)
    assert counts(database) == {'tasks': 1, 'contracts': 1, 'runs': 0,
                                'task_revisions': 1, 'api_idempotency': 1}


def test_simultaneous_revisions_compare_the_same_version_atomically(client, database):
    task_id = create_ready(client)['task']['task_id']
    path = f'/v1/tasks/{task_id}/revisions'
    requests = [(path, {**deepcopy(FINANCE), 'contract_version': 1,
                       'instruction': f'并发修订 {number}'}, f'revision-{number}')
                for number in range(4)]
    responses = race(database, requests)
    assert [status for status, _ in responses].count(200) == 1
    conflicts = [body for status, body in responses if status == 409]
    assert len(conflicts) == 3
    assert all(body['code'] == 'CONTRACT_VERSION_CONFLICT' and
               body['current_contract_version'] == 2 for body in conflicts)
    assert counts(database) == {'tasks': 1, 'contracts': 2, 'runs': 0,
                                'task_revisions': 2, 'api_idempotency': 2}
