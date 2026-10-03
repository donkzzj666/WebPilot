"""Progress is authenticated, paginated metadata rather than execution input."""
import pytest
from api_support import AuthenticatedTestClient, create_test_app
from webagent.config import Settings
from storage.test_graph_store import setup_run


@pytest.fixture
def client(tmp_path):
    store, _, _, _, _, _ = setup_run(tmp_path)
    store.checkpoint_observation('run-1','snapshot-1',expected_state_version=1)
    store.record_progress('run-1','observe',snapshot_id='snapshot-1',expected_state_version=1)
    store.record_progress('run-1','decide',iteration=1,expected_state_version=1)
    with AuthenticatedTestClient(create_test_app(Settings(tmp_path))) as client:
        yield client


def test_progress_is_ordered_and_resumes_after_cursor(client):
    response = client.get('/v1/runs/run-1/progress?limit=1')
    assert response.status_code == 200
    first = response.json()
    assert len(first['progress']) == 1 and first['progress'][0]['phase'] == 'observe'
    page = client.get('/v1/runs/run-1/progress?after=' + str(first['next_after'])).json()
    assert [row['phase'] for row in page['progress']] == ['decide']
    assert not any(key in response.text for key in ['api_key','artifact_path','visible_excerpt','messages'])
    assert client.post('/v1/runs/run-1/progress',json={'action':'arbitrary'}).status_code == 405


@pytest.mark.parametrize('query', ['raw=true','after=-1','limit=0','limit=1001','after=1&after=2',
                                  'after=9223372036854775808','limit=true','after=01'])
def test_progress_rejects_unknown_or_unbounded_input(client,query):
    assert client.get('/v1/runs/run-1/progress?' + query).status_code == 422


@pytest.mark.parametrize('headers,status',[({'Authorization':''},401),({'Origin':'https://attacker.invalid'},403),
    ({'Host':'attacker.invalid'},403),({'Sec-Fetch-Site':'cross-site'},403)])
def test_progress_uses_local_api_boundary(client,headers,status):
    assert client.get('/v1/runs/run-1/progress',headers=headers).status_code == status


def test_unknown_run_has_no_framework_or_filesystem_detail(client):
    response=client.get('/v1/runs/unknown-run/progress')
    assert response.status_code == 404 and 'sqlite' not in response.text
