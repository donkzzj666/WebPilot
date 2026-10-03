"""Recovery diagnostics remain readable and cannot supply authority."""
import pytest
from api_support import AuthenticatedTestClient, create_test_app
from webagent.graph.recovery import RecoveryStore
from unit.test_graph_executor import prepared


@pytest.fixture
def client(tmp_path):
    settings, scheduler, token, _, _ = prepared(tmp_path)
    scheduler.abandon(token)
    recovery = scheduler.claim('worker-1', token.worker_generation)
    store = RecoveryStore(settings.business_db)
    store.begin(recovery)
    store.blocked(recovery, 'object_mismatch')
    scheduler.abandon(recovery)
    with AuthenticatedTestClient(create_test_app(settings)) as client:
        yield client


def test_recovery_history_is_bounded_and_contains_no_private_proof(client):
    first = client.get('/v1/runs/run-1/recovery?limit=1')
    assert first.status_code == 200
    body = first.json()
    assert body['state'] == 'RECONCILING' and body['recoveries'][0]['phase'] == 'BEGIN'
    rest = client.get('/v1/runs/run-1/recovery?after=' + str(body['next_after'])).json()
    assert [row['phase'] for row in rest['recoveries']] == ['BLOCKED']
    assert rest['recoveries'][0]['reason'] == 'object_mismatch'
    for private in ('facts_json', 'normalized_account', 'input_sha256', 'artifact_path', 'api_key'):
        assert private not in first.text
    assert client.post('/v1/runs/run-1/recovery', json={'clear': True}).status_code == 405


@pytest.mark.parametrize('query', ['after=-1', 'after=1&after=2', 'limit=0', 'limit=1001',
                                  'raw=true', 'after=9223372036854775808', 'limit=true'])
def test_recovery_rejects_unbounded_or_unsupported_input(client, query):
    assert client.get('/v1/runs/run-1/recovery?' + query).status_code == 422


@pytest.mark.parametrize('headers,status', [({'Authorization': ''},401),
    ({'Origin':'https://untrusted.invalid'},403), ({'Host':'untrusted.invalid'},403)])
def test_recovery_inherits_api_access_boundary(client, headers, status):
    assert client.get('/v1/runs/run-1/recovery', headers=headers).status_code == status


def test_missing_run_returns_no_framework_detail(client):
    response = client.get('/v1/runs/missing-run/recovery')
    assert response.status_code == 404 and 'sqlite' not in response.text
