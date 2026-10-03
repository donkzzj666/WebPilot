"""HTTP login metadata contract, browser-independent API boundary checks."""
import json
import pytest
from fastapi.testclient import TestClient
from api_support import create_test_app, TEST_HEADERS
from webagent.config import Settings
from webagent.errors import BusinessError
from webagent.identities import routes

SECRET = 'SYNTHETIC_PASSWORD_OTP_NEVER_ECHO'
BASE = '/v1/identities'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    calls = []
    async def call(self, command, payload=None):
        calls.append((command, payload))
        if command == 'confirm':
            if payload['expected_version'] != 2:
                raise BusinessError('STATE_CONFLICT', 'Reload login state', status=409, current_state_version=2)
            return {'login_id': payload['login_session_id'], 'state': 'NEEDS_LOGIN', 'identity_ref': None}
        return {'command': command, 'state': 'AWAITING_USER'}
    monkeypatch.setattr(routes.LoginClient, 'call', call)
    with TestClient(create_test_app(Settings(tmp_path)), base_url='http://127.0.0.1:8000', headers=TEST_HEADERS) as client:
        yield client, calls


@pytest.mark.parametrize('method,path,command,payload,status', [
    ('GET', '', 'list_identities', None, 200), ('GET', '/sites', 'list_sites', None, 200),
    ('POST', '/login-sessions', 'create', {'site_id': 'github', 'expected_account': 'alice'}, 201),
    ('GET', '/login-sessions/login-test', 'get', None, 200),
    ('POST', '/login-sessions/login-test/confirm', 'confirm', {'expected_version': 2}, 200),
    ('POST', '/login-sessions/login-test/close', 'close', {'expected_version': 2}, 200)])
def test_metadata_dispatch_and_no_cache(rig, method, path, command, payload, status):
    client, calls = rig
    response = client.request(method, BASE + path, json=payload)
    assert response.status_code == status and calls[-1][0] == command
    assert response.headers['cache-control'] == 'no-store'
    assert 'access-control-allow-origin' not in response.headers
    if command == 'confirm':
        assert response.json()['state'] == 'NEEDS_LOGIN' and response.json()['identity_ref'] is None
    if command in ('get', 'confirm', 'close'):
        assert calls[-1][1]['login_session_id'] == 'login-test'


@pytest.mark.parametrize('field', ['password', 'otp', 'cookie', 'storage_state', 'selector', 'url', 'script'])
def test_credential_and_executable_input_never_reaches_worker(rig, field):
    client, calls = rig
    response = client.post(BASE + '/login-sessions', json={'site_id': 'github', 'expected_account': 'alice', field: SECRET})
    assert response.status_code == 422 and SECRET not in response.text and calls == []


@pytest.mark.parametrize('raw', ['{}', 'null', '[]', '{', '{"site_id":"github","site_id":"duplicate"}',
    '{"site_id":"github","expected_account":true}', '{"site_id":"github","expected_account":" alice "}',
    '{"site_id":"github","expected_account":"alice","extra":NaN}', 'x' * 8193])
def test_invalid_and_duplicate_json_does_not_dispatch(rig, raw):
    client, calls = rig
    response = client.post(BASE + '/login-sessions', content=raw, headers={'Content-Type': 'application/json'})
    assert response.status_code == 422 and calls == []


@pytest.mark.parametrize('value', [True, -1, 1.0, '1', None, 2**53])
def test_version_is_required_strict_and_bounded(rig, value):
    client, calls = rig
    response = client.post(BASE + '/login-sessions/login-test/confirm', json={'expected_version': value})
    assert response.status_code == 422 and calls == []


def test_form_credentials_and_unsupported_content_type_are_rejected(rig):
    client, calls = rig
    response = client.post(BASE + '/login-sessions', data={'password': SECRET, 'otp': SECRET})
    assert response.status_code == 422 and SECRET not in response.text and calls == []


def test_version_conflict_is_reported_without_automatic_retry(rig):
    client, calls = rig
    response = client.post(BASE + '/login-sessions/login-test/confirm', json={'expected_version': 1})
    assert response.status_code == 409 and response.json()['current_state_version'] == 2 and len(calls) == 1


@pytest.mark.parametrize('headers,status', [({'Authorization': ''}, 401),
    ({'Origin': 'https://attacker.invalid'}, 403), ({'Host': 'attacker.invalid'}, 403),
    ({'Sec-Fetch-Site': 'cross-site'}, 403)])
def test_api_boundary_applies_before_login_dispatch(rig, headers, status):
    client, calls = rig
    response = client.post(BASE + '/login-sessions', json={'site_id': 'github', 'expected_account': 'alice'}, headers=headers)
    assert response.status_code == status and calls == []


def test_missing_worker_is_503_and_does_not_start_a_browser(tmp_path):
    with TestClient(create_test_app(Settings(tmp_path)), base_url='http://127.0.0.1:8000', headers=TEST_HEADERS) as client:
        response = client.get(BASE + '/sites')
        assert response.status_code == 503 and response.json()['code'] == 'SERVICE_UNAVAILABLE'
        assert not (tmp_path / 'browser').exists()
