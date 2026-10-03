"""The diagnostic API shares the authenticated local boundary and has no writes."""
import pytest

from api_support import AuthenticatedTestClient, create_test_app
from webagent.config import Settings
from webagent.observability.routes import router
from webagent.db import connect
from test_observability import prepared, persisted_model, fingerprint


@pytest.fixture
def client(tmp_path):
    path, _, _ = prepared(tmp_path)
    persisted_model(path, 'request-1', usage={'input_tokens': 4, 'output_tokens': 2})
    persisted_model(path, 'request-2', status='ERROR')
    app = create_test_app(Settings(tmp_path))
    if not any(getattr(route, 'path', None) == '/v1/metrics' for route in app.routes):
        app.include_router(router)
    with AuthenticatedTestClient(app) as client:
        yield client


def test_independent_event_and_model_pages_are_read_only(client):
    database = client.app.state.settings.business_db
    before = fingerprint(database)
    response = client.get('/v1/diagnostics/runs/run-1?limit=1')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    first = response.json()
    second = client.get('/v1/diagnostics/runs/run-1?limit=1&after=' + str(first['next_event_cursor'])
        + '&model_after=' + str(first['next_model_cursor'])).json()
    assert first['model_attempts'][0]['request_id'] == 'request-1'
    assert second['model_attempts'][0]['request_id'] == 'request-2'
    assert not second['events']
    assert client.get('/v1/metrics').status_code == 200
    assert fingerprint(database) == before
    assert all(value not in response.text for value in ('api_key', 'source_url', 'artifact_path',
        'visible_excerpt', 'messages', 'action_json', 'system_fingerprint', 'provider_request_id'))


@pytest.mark.parametrize('query', ['after=-1', 'model_after=-1', 'limit=0', 'limit=1001',
    'raw=true', 'after=1&after=2', 'model_after=0&model_after=1', 'after=01',
    'model_after=9223372036854775808', 'limit=true'])
def test_invalid_or_unbounded_diagnostic_parameters_are_rejected(client, query):
    assert client.get('/v1/diagnostics/runs/run-1?' + query).status_code == 422


@pytest.mark.parametrize('path', ['/v1/diagnostics/runs/run-1', '/v1/metrics', '/v1/health/worker'])
@pytest.mark.parametrize('headers,status', [({'Authorization': ''}, 401),
    ({'Origin': 'https://attacker.invalid'}, 403), ({'Host': 'attacker.invalid'}, 403),
    ({'Sec-Fetch-Site': 'cross-site'}, 403)])
def test_observability_uses_existing_local_api_boundary(client, path, headers, status):
    assert client.get(path, headers=headers).status_code == status


@pytest.mark.parametrize('path', ['/v1/diagnostics/runs/run-1', '/v1/metrics', '/v1/health/worker'])
def test_queries_cannot_invoke_execution_or_mutate_resources(client, path):
    assert client.post(path, json={'action': 'arbitrary', 'code': 'do evil'}).status_code == 405


def test_unknown_run_and_unstarted_worker_are_safe(client):
    response = client.get('/v1/diagnostics/runs/unknown-run')
    assert response.status_code == 404 and 'sqlite' not in response.text
    worker = client.get('/v1/health/worker')
    assert worker.status_code == 503 and worker.json()['status'] == 'not_started'
    assert not worker.json()['tasks_success_implied']
    assert client.get('/health').status_code == 200


def test_real_registration_and_stop_change_worker_health_without_task_success(tmp_path):
    path, _, _ = prepared(tmp_path)
    # Use the production scheduler registration, independently of the graph Run.
    from webagent.scheduler.store import SchedulerStore
    scheduler = SchedulerStore(path)
    generation = scheduler.start_worker('health-worker')
    app = create_test_app(Settings(tmp_path))
    if not any(getattr(route, 'path', None) == '/v1/metrics' for route in app.routes):
        app.include_router(router)
    with AuthenticatedTestClient(app) as client:
        healthy = client.get('/v1/health/worker')
        assert healthy.status_code == 200 and healthy.json()['ready']
        assert healthy.json()['generation'] == generation
        with connect(path) as db:
            assert db.execute('SELECT state FROM runs WHERE run_id="run-1"').fetchone()[0] == 'RUNNING'
        scheduler.stop_worker('health-worker', generation)
        assert client.get('/v1/health/worker').status_code == 503
        assert client.get('/health').status_code == 200


@pytest.mark.parametrize('path', ['/v1/metrics', '/v1/health/worker'])
def test_nonpaginated_endpoints_reject_hidden_query_parameters(client, path):
    assert client.get(path + '?token=hidden').status_code == 422
