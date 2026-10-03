"""Configuration versions and credential lifecycle, using only a fake OS store."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import sqlite3
import threading
from uuid import UUID

import pytest
from api_support import AuthenticatedTestClient as TestClient
from pydantic import SecretStr

from api_support import create_test_app as create_app
from webagent.config import Settings
from webagent.db import LATEST_VERSION, connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.events import read_events
from webagent.models.transport import ModelConfig
from webagent.settings import service
from webagent.settings.models import ModelSettingsRequest
from webagent.settings.secrets import CredentialError
from webagent.state import transition

KEY_ONE = 'SYNTHETIC_M107_KEY_ONE_DO_NOT_LOG_771'
KEY_TWO = 'SYNTHETIC_M107_KEY_TWO_DO_NOT_LOG_992'
MISSING = object()
FINANCE = {
    'instruction': 'Read a synthetic finance fixture', 'source_ids': ['local-fixture'],
    'scenario': 'finance', 'parameters': {'entity_id': 'fixture-company', 'report_version': '2025',
        'period_type': 'annual', 'metrics': ['revenue'], 'currency': 'USD'},
}


class FakeStore:
    """Emulates the narrow credential protocol; values never enter return bodies."""
    def __init__(self):
        self.values = {}
        self.calls = []
        self.put_error = None
        self.get_error = None
        self.delete_error = None
        self.on_put = None
        self.on_get = None
        self.lock = threading.Lock()

    def put(self, reference, secret):
        assert isinstance(secret, SecretStr)
        assert UUID(reference).version == 4
        with self.lock:
            self.calls.append(('put', reference))
            if self.put_error:
                raise CredentialError(self.put_error)
            if reference in self.values:
                raise CredentialError('already_exists')
            self.values[reference] = secret
        if self.on_put:
            self.on_put(reference)

    def get(self, reference):
        with self.lock:
            self.calls.append(('get', reference))
            if self.get_error:
                raise CredentialError(self.get_error)
            if reference not in self.values:
                raise CredentialError('missing')
            value = self.values[reference]
        if self.on_get:
            self.on_get(reference)
        return value

    def delete(self, reference):
        with self.lock:
            self.calls.append(('delete', reference))
            if self.delete_error:
                raise CredentialError(self.delete_error)
            self.values.pop(reference, None)


def body(version=0, *, key=KEY_ONE, max_tokens=1024):
    value = {'expected_version': version, 'model': ModelConfig(max_tokens=max_tokens).model_dump(mode='json'),
             'accept_data_sharing': True}
    if key is not MISSING:
        value['api_key'] = key
    return value


def request(version=0, *, key=KEY_ONE, max_tokens=1024):
    return ModelSettingsRequest.model_validate(body(version, key=key, max_tokens=max_tokens))


@contextmanager
def client_for(path, store):
    with TestClient(create_app(Settings(path.parent), secret_store=store),
                    base_url='http://127.0.0.1:8000') as client:
        yield client


def versions(path):
    with connect(path) as db:
        return [dict(row) for row in db.execute('SELECT * FROM model_settings_versions ORDER BY version')]


def counts(path):
    with connect(path) as db:
        return {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('runs', 'run_budgets', 'run_config_snapshots')}


def ready_task(path, store, *, suffix='1'):
    with client_for(path, store) as client:
        response = client.post('/v1/tasks', json=deepcopy(FINANCE),
                               headers={'Idempotency-Key': 'settings-task-' + suffix})
    assert response.status_code == 201, response.text
    return response.json()['task']['task_id']


def configured_run(path, store, *, version=1, run_id='run-1', task_id=None):
    task_id = task_id or ready_task(path, store, suffix=run_id)
    return service.create_configured_run(
        path, store, expected_settings_version=version, run_id=run_id, task_id=task_id,
        contract_version=1, graph_version='fixture-graph-v1', graph_state_schema_version='fixture-state-v1')


def assert_api_error(response, *, code=None, reason=None):
    """The shipped HTTP envelope stays inside the frozen public M0 contract."""
    mapping = {'BAD_REQUEST': 400, 'UNAUTHENTICATED': 401, 'FORBIDDEN': 403, 'NOT_FOUND': 404,
               'INVALID_PARAMETER': 422, 'CONTRACT_VERSION_CONFLICT': 409, 'STATE_CONFLICT': 409,
               'IDEMPOTENCY_CONFLICT': 409, 'RESOURCE_CONFLICT': 409, 'API_RATE_LIMIT': 429,
               'MODEL_RATE_LIMIT': 429, 'UPSTREAM_ERROR': 502, 'TIMEOUT': 504,
               'SERVICE_UNAVAILABLE': 503, 'INTERNAL_ERROR': 500}
    value = response.json()
    assert set(value) == {'request_id', 'status', 'code', 'message', 'details', 'retryable',
                         'current_contract_version', 'current_state_version'}
    assert value['code'] in mapping
    assert value['status'] == response.status_code == mapping[value['code']]
    assert value['request_id'] == response.headers['x-request-id']
    assert isinstance(value['message'], str) and value['message']
    assert type(value['retryable']) is bool
    assert value['details'] and all(set(item) == {'field', 'reason'} for item in value['details'])
    assert all(isinstance(item['reason'], str) and item['reason'] for item in value['details'])
    assert response.headers['cache-control'] == 'no-store'
    if code is not None:
        assert value['code'] == code
    if reason is not None:
        assert any(item['reason'] == reason for item in value['details'])
    assert KEY_ONE not in response.text and KEY_TWO not in response.text
    return value


def test_empty_settings_explain_not_ready_without_touching_credentials(database):
    store = FakeStore()
    with client_for(database, store) as client:
        result = client.get('/v1/settings')
    assert result.status_code == 200
    value = result.json()
    assert value['version'] == 0 and value['model'] is None
    assert value['model_config_sha256'] is None and value['runtime_config_sha256'] is None
    assert value['readiness']['ready'] is False
    assert value['readiness']['credential_status'] == 'not_configured'
    assert value['readiness']['reasons']
    assert value['readiness']['provider_verified'] is False
    assert value['task_execution_enabled'] is False
    assert value['disclosure']['version'] == 'model-data-v1'
    assert value['disclosure']['accepted'] is False
    assert store.calls == []


def test_save_roundtrip_publishes_one_version_without_secret_in_any_public_output(database, caplog):
    store = FakeStore()
    with client_for(database, store) as client:
        saved = client.put('/v1/settings/model', json=body())
        queried = client.get('/v1/settings')
    assert saved.status_code == queried.status_code == 200
    value = saved.json()
    assert value == queried.json()
    assert value['version'] == 1 and value['model']['model_id'] == 'deepseek-flash'
    assert value['readiness']['ready'] is True
    assert value['readiness']['credential_status'] == 'available'
    assert value['readiness']['provider_verified'] is False
    assert value['readiness']['reasons'] == []
    assert value['disclosure']['accepted'] is True
    assert value['disclosure']['items'] and value['disclosure']['message']
    assert len(store.values) == 1
    assert next(iter(store.values.values())).get_secret_value() == KEY_ONE
    assert KEY_ONE not in saved.text + queried.text + caplog.text
    assert 'api_key' not in value['model']
    with connect(database) as db:
        assert KEY_ONE not in '\n'.join(db.iterdump())
    assert counts(database) == {'runs': 0, 'run_budgets': 0, 'run_config_snapshots': 0}


def test_secret_input_is_excluded_from_request_repr_and_dump():
    value = request()
    assert KEY_ONE not in repr(value)
    assert 'api_key' not in value.model_dump()
    assert KEY_ONE not in value.model_dump_json()


def test_first_configuration_without_key_remains_explicitly_not_ready(database):
    store = FakeStore()
    saved = service.update_model(database, store, request(key=MISSING))
    assert saved['version'] == 1 and saved['model'] is not None
    assert saved['readiness']['ready'] is False
    assert saved['readiness']['credential_status'] == 'missing'
    assert saved['readiness']['reasons']
    assert store.values == {}


@pytest.mark.parametrize('reason', ['missing', 'locked', 'access_denied', 'unavailable', 'unsupported'])
def test_readiness_reports_credential_failures_without_claiming_provider_auth(database, reason):
    store = FakeStore()
    service.update_model(database, store, request())
    store.get_error = reason
    with client_for(database, store) as client:
        response = client.get('/v1/settings')
    assert response.status_code == 200
    state = response.json()['readiness']
    assert state['ready'] is False and state['provider_verified'] is False
    assert state['credential_status'] == reason and state['reasons']
    assert KEY_ONE not in response.text
    assert len(versions(database)) == 1


def test_stale_version_is_rejected_before_any_keychain_write(database):
    store = FakeStore()
    service.update_model(database, store, request())
    before_calls, before_versions = list(store.calls), versions(database)
    with client_for(database, store) as client:
        stale = client.put('/v1/settings/model', json=body(0, key=KEY_TWO))
    assert_api_error(stale, code='STATE_CONFLICT', reason='VERSION_CONFLICT')
    assert stale.json()['details'][0]['field'] == 'expected_version'
    assert store.calls == before_calls
    assert versions(database) == before_versions
    assert len(store.values) == 1 and KEY_TWO not in stale.text


def test_omitted_key_reuses_current_reference_without_overwriting_it(database):
    store = FakeStore()
    first = service.update_model(database, store, request())
    reference = next(iter(store.values))
    second = service.update_model(database, store, request(1, key=MISSING, max_tokens=512))
    assert first['version'] == 1 and second['version'] == 2
    assert second['readiness']['ready'] is True
    assert first['model_config_sha256'] != second['model_config_sha256']
    assert [version['credential_ref'] for version in versions(database)] == [reference, reference]
    assert [call for call in store.calls if call[0] == 'put'] == [('put', reference)]
    assert not any(call[0] == 'delete' for call in store.calls)


def test_rotation_pins_old_and_new_runs_to_independent_secret_revisions(database):
    store = FakeStore()
    first = service.update_model(database, store, request())
    old = configured_run(database, store)
    transition(database, run_id=old['run_id'], expected_state_version=0, target='RUNNING')
    with connect(database) as db:
        before_run = dict(db.execute('SELECT * FROM runs WHERE run_id=?', (old['run_id'],)).fetchone())
    second = service.update_model(database, store, request(1, key=KEY_TWO, max_tokens=512))
    new = configured_run(database, store, version=2, run_id='run-2')
    old_config = service.load_run_config(database, store, old['run_id'])
    new_config = service.load_run_config(database, store, new['run_id'])
    assert old_config.version == 1 and new_config.version == 2
    assert old_config.model.max_tokens == 1024 and new_config.model.max_tokens == 512
    assert old_config.api_key.get_secret_value() == KEY_ONE
    assert new_config.api_key.get_secret_value() == KEY_TWO
    assert KEY_ONE not in repr(old_config) and KEY_TWO not in repr(new_config)
    assert old['model_config_sha256'] == first['model_config_sha256']
    assert new['model_config_sha256'] == second['model_config_sha256']
    with connect(database) as db:
        assert dict(db.execute('SELECT * FROM runs WHERE run_id=?', (old['run_id'],)).fetchone()) == before_run
        assert db.execute('SELECT count(*) FROM run_config_snapshots').fetchone()[0] == 2
    assert len(store.values) == 2 and not any(call[0] == 'delete' for call in store.calls)


def test_rotation_with_identical_model_changes_runtime_snapshot_not_model_digest(database):
    store = FakeStore()
    first = service.update_model(database, store, request())
    old = configured_run(database, store)
    second = service.update_model(database, store, request(1, key=KEY_TWO))
    assert first['model_config_sha256'] == second['model_config_sha256']
    assert first['runtime_config_sha256'] != second['runtime_config_sha256']
    assert service.load_run_config(database, store, old['run_id']).api_key.get_secret_value() == KEY_ONE


def test_missing_historical_key_never_falls_back_to_current_credential(database):
    store = FakeStore()
    service.update_model(database, store, request())
    old = configured_run(database, store)
    old_reference = versions(database)[0]['credential_ref']
    service.update_model(database, store, request(1, key=KEY_TWO))
    del store.values[old_reference]
    with pytest.raises(BusinessError):
        service.load_run_config(database, store, old['run_id'])
    assert service.get_settings(database, store)['readiness']['ready'] is True


def test_new_run_and_budget_bind_atomically_without_starting_execution(database):
    store = FakeStore()
    service.update_model(database, store, request())
    result = configured_run(database, store)
    assert counts(database) == {'runs': 1, 'run_budgets': 1, 'run_config_snapshots': 1}
    with connect(database) as db:
        run = db.execute('SELECT * FROM runs').fetchone()
        budget = db.execute('SELECT * FROM run_budgets').fetchone()
        binding = db.execute('SELECT * FROM run_config_snapshots').fetchone()
        task = db.execute('SELECT * FROM tasks').fetchone()
    assert run['state'] == 'QUEUED' and run['state_version'] == 0
    assert task['current_run_id'] == run['run_id'] == result['run_id']
    assert budget['budget_record_id'] == result['budget_record_ref']
    assert binding['settings_version'] == result['settings_version'] == 1
    assert binding['model_config_sha256'] == run['model_config_sha256']
    assert binding['runtime_config_sha256'] == run['runtime_config_sha256']


def test_run_creation_rejects_stale_settings_and_active_task_run(database):
    store = FakeStore()
    service.update_model(database, store, request())
    task_id = ready_task(database, store)
    with pytest.raises(BusinessError):
        configured_run(database, store, version=0, task_id=task_id)
    assert counts(database) == {'runs': 0, 'run_budgets': 0, 'run_config_snapshots': 0}
    configured_run(database, store, task_id=task_id)
    with pytest.raises(BusinessError):
        configured_run(database, store, task_id=task_id, run_id='second-active')
    assert counts(database) == {'runs': 1, 'run_budgets': 1, 'run_config_snapshots': 1}


def test_unready_configuration_cannot_allocate_run_or_budget(database):
    store = FakeStore()
    service.update_model(database, store, request(key=MISSING))
    task_id = ready_task(database, store)
    with pytest.raises(BusinessError):
        configured_run(database, store, task_id=task_id)
    assert counts(database) == {'runs': 0, 'run_budgets': 0, 'run_config_snapshots': 0}


@pytest.mark.parametrize('reason', ['locked', 'access_denied', 'unavailable', 'unsupported'])
def test_failed_credential_save_never_publishes_a_new_configuration(database, reason):
    store = FakeStore()
    service.update_model(database, store, request())
    before = versions(database)
    store.put_error = reason
    with client_for(database, store) as client:
        response = client.put('/v1/settings/model', json=body(1, key=KEY_TWO))
    assert_api_error(response, code='SERVICE_UNAVAILABLE', reason='CREDENTIAL_' + reason.upper())
    assert response.status_code == 503
    assert versions(database) == before
    assert len(store.values) == 1
    assert KEY_ONE not in response.text and KEY_TWO not in response.text


def test_sql_publish_failure_compensates_only_new_key_and_retains_prior_version(database):
    store = FakeStore()
    service.update_model(database, store, request())
    old_reference = versions(database)[0]['credential_ref']
    with connect(database) as db:
        db.execute("""CREATE TRIGGER test_reject_settings BEFORE INSERT ON model_settings_versions
                      BEGIN SELECT RAISE(ABORT,'synthetic publish failure'); END""")
    with pytest.raises((BusinessError, sqlite3.IntegrityError)):
        service.update_model(database, store, request(1, key=KEY_TWO))
    assert len(versions(database)) == 1
    assert set(store.values) == {old_reference}
    assert not any(call == ('delete', old_reference) for call in store.calls)
    assert any(call[0] == 'delete' for call in store.calls)


def test_concurrent_updates_publish_one_version_and_compensate_losing_key(database):
    store = FakeStore()
    barrier = threading.Barrier(2)
    store.on_put = lambda reference: barrier.wait(timeout=3)

    def update(key):
        try:
            return service.update_model(database, store, request(key=key))
        except BusinessError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [KEY_ONE, KEY_TWO]))
    successes = [value for value in results if isinstance(value, dict)]
    failures = [value for value in results if isinstance(value, BusinessError)]
    assert len(successes) == len(failures) == 1
    assert successes[0]['version'] == 1
    assert failures[0].status == 409 and failures[0].code == 'VERSION_CONFLICT'
    assert len(versions(database)) == len(store.values) == 1
    assert set(store.values) == {versions(database)[0]['credential_ref']}


def test_native_store_operations_are_outside_sqlite_write_transactions(database):
    store = FakeStore()

    def check_unlocked(reference):
        with connect(database, busy_timeout_ms=0) as db, transaction(db):
            db.execute('SELECT 1')

    store.on_put = store.on_get = check_unlocked
    service.update_model(database, store, request())
    assert service.get_settings(database, store)['readiness']['ready'] is True
    result = configured_run(database, store)
    assert service.load_run_config(database, store, result['run_id']).version == 1


def test_versions_and_run_bindings_are_immutable(database):
    store = FakeStore()
    service.update_model(database, store, request())
    configured_run(database, store)
    for statement in (
        "UPDATE model_settings_versions SET credential_ref=NULL",
        'DELETE FROM model_settings_versions',
        'UPDATE run_config_snapshots SET settings_version=2',
        'DELETE FROM run_config_snapshots',
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with connect(database) as db, transaction(db):
                db.execute(statement)


@pytest.mark.parametrize('change', [
    {'expected_version': True}, {'expected_version': -1}, {'expected_version': '0'},
    {'accept_data_sharing': False}, {'accept_data_sharing': 'true'},
    {'api_key': None}, {'api_key': ''}, {'api_key': 'with spaces'}, {'api_key': 123},
    {'unrecognized': KEY_ONE}, {'credential_ref': 'caller-chosen-credential'},
])
def test_invalid_setting_requests_fail_without_saving_secret(database, change, caplog):
    store = FakeStore()
    payload = body()
    payload.update(change)
    with client_for(database, store) as client:
        response = client.put('/v1/settings/model', json=payload)
    assert response.status_code == 422
    assert_api_error(response, code='INVALID_PARAMETER')
    assert store.calls == [] and versions(database) == []
    assert KEY_ONE not in response.text + caplog.text


@pytest.mark.parametrize('change', [
    {'provider': 'other'}, {'model_id': 'unknown-model'}, {'base_url': 'https://attacker.example'},
    {'base_url': 'https://user:secret@api.deepseek.com'}, {'base_url': 'http://api.deepseek.com'},
    {'base_url': 'http://127.0.0.1:8765'}, {'prompt_version': 'unreviewed-prompt'},
    {'price_version': 'invented-price'}, {'max_tokens': True}, {'max_tokens': 0},
    {'total_seconds': -1}, {'extra': KEY_ONE},
])
def test_public_configuration_cannot_expand_provider_destination_or_frozen_prompt(database, change):
    store = FakeStore()
    payload = body()
    payload['model'].update(change)
    with client_for(database, store) as client:
        response = client.put('/v1/settings/model', json=payload)
    assert response.status_code == 422
    assert_api_error(response, code='INVALID_PARAMETER')
    assert store.calls == [] and versions(database) == []
    assert KEY_ONE not in response.text


@pytest.mark.parametrize('origin', ['https://attacker.example', 'null', 'http://127.0.0.1.attacker.example'])
def test_external_browser_origin_cannot_read_or_change_local_settings(database, origin):
    store = FakeStore()
    with client_for(database, store) as client:
        queried = client.get('/v1/settings', headers={'Origin': origin})
        updated = client.put('/v1/settings/model', json=body(), headers={'Origin': origin})
    assert_api_error(queried, code='FORBIDDEN')
    assert_api_error(updated, code='FORBIDDEN')
    assert store.calls == [] and versions(database) == []


def test_same_origin_and_cli_requests_are_allowed_but_form_posts_are_rejected(database):
    store = FakeStore()
    with client_for(database, store) as client:
        saved = client.put('/v1/settings/model', json=body(), headers={'Origin': 'http://127.0.0.1:8000'})
        form = client.put('/v1/settings/model', content=json.dumps(body(1, key=KEY_TWO)),
                          headers={'Content-Type': 'text/plain'})
        queried = client.get('/v1/settings')
    assert saved.status_code == queried.status_code == 200
    assert form.status_code == 422
    assert_api_error(form, code='INVALID_PARAMETER')
    assert queried.json()['version'] == 1 and len(store.values) == 1


def test_v5_upgrade_preserves_legacy_run_and_never_guesses_current_snapshot(tmp_path):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=5)
    with connect(path) as db, transaction(db):
        create_task(db, task_id='legacy-task', instruction='Legacy synthetic task', requested_fields=['contract'])
        add_contract(db, {'schema_version': 'm0-contract-v1', 'task_id': 'legacy-task', 'contract_version': 1,
                          'scenario': 'research', 'objective': 'Legacy fixture'})
        create_run(db, run_id='legacy-run', task_id='legacy-task', contract_version=1,
                   graph_version='legacy-graph', graph_state_schema_version='legacy-state',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
    transition(path, run_id='legacy-run', expected_state_version=0, target='RUNNING')
    before_events = read_events(path)
    with connect(path) as db:
        before_run = dict(db.execute('SELECT * FROM runs').fetchone())
    assert migrate(path)['applied'] == LATEST_VERSION - 5
    assert migrate(path)['applied'] == 0
    store = FakeStore()
    service.update_model(path, store, request())
    with pytest.raises(BusinessError) as caught:
        service.load_run_config(path, store, 'legacy-run')
    assert caught.value.code == 'CONFIG_SNAPSHOT_MISSING'
    assert read_events(path) == before_events
    with connect(path) as db:
        assert dict(db.execute('SELECT * FROM runs').fetchone()) == before_run
        assert db.execute('SELECT count(*) FROM run_config_snapshots').fetchone()[0] == 0


def test_loopback_frontend_port_is_allowed_and_settings_are_never_cacheable(database):
    store = FakeStore()
    with client_for(database, store) as client:
        result = client.put('/v1/settings/model', json=body(), headers={'Origin': 'http://127.0.0.1:5173'})
        assert result.status_code == 200
        assert result.headers['cache-control'] == 'no-store'
        result = client.get('/v1/settings', headers={'Origin': 'http://127.0.0.1:5173'})
        assert result.status_code == 200
        assert result.headers['cache-control'] == 'no-store'


def test_error_after_successful_commit_never_compensates_referenced_key(database, monkeypatch):
    store = FakeStore()
    service.update_model(database, store, request())
    original = service.transaction

    @contextmanager
    def uncertain_commit(db):
        with original(db):
            yield
        raise RuntimeError('Synthetic error after committed publication')

    monkeypatch.setattr(service, 'transaction', uncertain_commit)
    with pytest.raises(BusinessError):
        service.update_model(database, store, request(1, key=KEY_TWO))
    assert len(versions(database)) == len(store.values) == 2
    assert not any(call[0] == 'delete' for call in store.calls)
    view = service.get_settings(database, store)
    assert view['version'] == 2 and view['readiness']['ready'] is True
    reference = versions(database)[1]['credential_ref']
    assert store.values[reference].get_secret_value() == KEY_TWO


def test_failed_compensation_is_explicit_and_preserves_original_configuration(database):
    store = FakeStore()
    service.update_model(database, store, request())
    original_reference = versions(database)[0]['credential_ref']
    store.delete_error = 'access_denied'
    with connect(database) as db:
        db.execute("""CREATE TRIGGER test_reject_settings BEFORE INSERT ON model_settings_versions
                      BEGIN SELECT RAISE(ABORT,'synthetic publish failure'); END""")
    with pytest.raises(BusinessError) as caught:
        service.update_model(database, store, request(1, key=KEY_TWO))
    assert caught.value.code == 'CREDENTIAL_CLEANUP_REQUIRED' and caught.value.status == 503
    assert KEY_ONE not in str(caught.value) and KEY_TWO not in str(caught.value)
    assert len(versions(database)) == 1 and len(store.values) == 2
    assert store.values[original_reference].get_secret_value() == KEY_ONE
    assert service.get_settings(database, store)['version'] == 1


@pytest.mark.parametrize('raw', [
    '{broken',
    json.dumps(body())[:-1] + ',"api_key":"' + KEY_TWO + '"}',
    json.dumps(body()).replace('1024', 'NaN'),
    json.dumps(body()).replace('1024', '1e999'),
    json.dumps({**body(), KEY_ONE: KEY_TWO}),
])
def test_ambiguous_json_and_secret_named_fields_never_reach_store_or_error_diagnostics(database, raw, caplog):
    store = FakeStore()
    with client_for(database, store) as client:
        response = client.put('/v1/settings/model', content=raw, headers={'Content-Type': 'application/json'})
    assert response.status_code == 422
    assert_api_error(response, code='INVALID_PARAMETER')
    assert store.calls == [] and versions(database) == []
    assert KEY_ONE not in response.text + caplog.text and KEY_TWO not in response.text + caplog.text


def test_large_settings_body_is_rejected_before_native_store_access(database):
    store = FakeStore()
    payload = {**body(), 'extra': 'x' * 65536}
    with client_for(database, store) as client:
        response = client.put('/v1/settings/model', json=payload)
    assert response.status_code == 422 and store.calls == []
    assert_api_error(response, code='INVALID_PARAMETER')


@pytest.mark.parametrize('headers', [
    {'Host': 'attacker.example'}, {'Sec-Fetch-Site': 'cross-site'},
    [('Origin', 'http://localhost:5173'), ('Origin', 'https://attacker.example')],
])
def test_rebinding_host_cross_site_and_duplicate_origin_are_rejected(database, headers):
    store = FakeStore()
    with client_for(database, store) as client:
        result = client.put('/v1/settings/model', json=body(), headers=headers)
    assert result.status_code == 403 and store.calls == []
    assert_api_error(result, code='FORBIDDEN')


def test_corrupted_snapshot_is_service_unavailable_in_frozen_error_envelope(database):
    store = FakeStore()
    service.update_model(database, store, request())
    first = versions(database)[0]
    modified_model = json.loads(first['model_json'])
    modified_model['max_tokens'] = 512
    runtime = json.loads(first['runtime_json'])
    runtime['settings_version'] = 2
    runtime_json = canonical_json(runtime)
    # Simulate corrupt imported data: syntactically valid row with a mismatched
    # model digest. The runtime reader must verify the immutable snapshot bytes.
    with connect(database) as db, transaction(db):
        db.execute('INSERT INTO model_settings_versions VALUES (?,?,?,?,?,?,?,?)', (
            2, canonical_json(modified_model), first['model_config_sha256'], first['credential_ref'],
            runtime_json, hashlib.sha256(runtime_json.encode()).hexdigest(), first['disclosure_version'], utc_text(),
        ))
    before_calls = list(store.calls)
    with client_for(database, store) as client:
        response = client.get('/v1/settings')
    assert_api_error(response, code='SERVICE_UNAVAILABLE', reason='CONFIG_SNAPSHOT_INVALID')
    assert response.status_code == 503 and store.calls == before_calls
