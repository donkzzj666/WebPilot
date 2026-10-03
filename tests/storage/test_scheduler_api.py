"""Read-only scheduler access remains behind the local API boundary."""
import pytest
from fastapi.testclient import TestClient
from api_support import create_test_app, TEST_HEADERS
from webagent.config import Settings


@pytest.fixture
def client(tmp_path):
    with TestClient(create_test_app(Settings(tmp_path)),base_url='http://127.0.0.1:8000',headers=TEST_HEADERS) as client:
        yield client


def test_scheduler_status_is_safe_metadata_and_no_store(client):
    response=client.get('/v1/scheduler')
    assert response.status_code==200
    assert response.headers['cache-control']=='no-store'
    assert isinstance(response.json(),dict)
    assert 'password' not in response.text and 'credential_ref' not in response.text
    assert client.get('/health').json()['task_execution_enabled'] is True


@pytest.mark.parametrize('headers,status',[({'Authorization':''},401),({'Origin':'https://attacker.invalid'},403),({'Host':'attacker.invalid'},403)])
def test_status_uses_local_api_protection(client,headers,status):
    assert client.get('/v1/scheduler',headers=headers).status_code==status


@pytest.mark.parametrize('path',['/v1/scheduler','/v1/scheduler/claim','/v1/scheduler/release','/v1/scheduler/execute'])
def test_no_http_endpoint_accepts_lease_authority_or_executor(client,path):
    assert client.post(path,json={'epoch':100,'resources':['*'],'executor':'anything'}).status_code in (404,405)
