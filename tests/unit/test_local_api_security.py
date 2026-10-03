"""Every route uses the same protection; token files fail closed on unsafe state."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat

from fastapi.testclient import TestClient
import pytest

from api_support import TEST_POLICY, TEST_HEADERS
from webagent.api import create_app
from webagent.config import Settings
from webagent.security import LocalApiPolicy, load_or_create_token
from webagent.security.token import LocalTokenError


def test_persistent_private_high_entropy_token_is_unique_and_concurrent(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: load_or_create_token(tmp_path), range(30)))
    assert len(set(tokens)) == 1 and len(tokens[0]) == 64
    assert load_or_create_token(tmp_path / 'other') != tokens[0]
    assert stat.S_IMODE((tmp_path / '.security').stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / '.security/local-api-token').stat().st_mode) == 0o600


def test_repeated_first_token_initialization_is_thread_safe(tmp_path):
    # Exercise first lock-file creation repeatedly, not only reads after the
    # first successful thread; macOS nonexclusive creation failed intermittently.
    for number in range(30):
        directory = tmp_path / str(number)
        with ThreadPoolExecutor(max_workers=8) as executor:
            tokens = list(executor.map(lambda _: load_or_create_token(directory), range(30)))
        assert len(set(tokens)) == 1


def _token_in_process(path):
    return load_or_create_token(path)


def test_first_token_initialization_is_process_safe(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as executor:
        tokens = list(executor.map(_token_in_process, [tmp_path] * 16))
    assert len(set(tokens)) == 1 and len(tokens[0]) == 64


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public-file', 'corrupt', 'oversized', 'public-dir', 'symlink-dir', 'fifo'])
def test_unsafe_or_corrupt_token_storage_is_rejected_without_rotation(tmp_path, kind):
    token = load_or_create_token(tmp_path)
    path = tmp_path / '.security/local-api-token'
    if kind == 'symlink':
        other = tmp_path / 'other'; path.rename(other); path.symlink_to(other)
    elif kind == 'hardlink':
        os.link(path, tmp_path / 'other')
    elif kind == 'public-file':
        path.chmod(0o644)
    elif kind == 'corrupt':
        path.write_text('bad')
    elif kind == 'oversized':
        path.write_text(token + 'a')
    elif kind == 'public-dir':
        path.parent.chmod(0o755)
    elif kind == 'fifo':
        path.unlink(); os.mkfifo(path, 0o600)
    else:
        other = tmp_path / 'other'; path.parent.rename(other); path.parent.symlink_to(other, target_is_directory=True)
    with pytest.raises(LocalTokenError, match='unavailable or unsafe'):
        load_or_create_token(tmp_path)


def test_symlink_ancestor_is_rejected(tmp_path):
    real = tmp_path / 'real'; real.mkdir()
    alias = tmp_path / 'alias'; alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(LocalTokenError):
        load_or_create_token(alias / 'data')


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(tmp_path), local_api_policy=TEST_POLICY)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        yield client


@pytest.mark.parametrize('path', ['/health', '/v1/settings', '/v1/tasks', '/v1/tasks/id', '/v1/events', '/missing'])
@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer wrong'}, {'Authorization': 'Basic abc'}])
def test_all_reads_and_streams_require_a_bearer(client, path, headers):
    response = client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers['cache-control'] == 'no-store'
    assert 'access-control-allow-origin' not in response.headers
    assert TEST_POLICY.token not in response.text


@pytest.mark.parametrize('headers', [
    {'Host': 'attacker.example'}, {'Host': 'localhost:8000'}, {'Host': '127.0.0.1:9000'},
    {'Host': '127.0.0.1:8000.attacker.example'}, {'Host': '127.0.0.1:8000,attacker.example'},
    {'Origin': 'null'}, {'Origin': 'http://127.0.0.1:9000'}, {'Origin': 'https://attacker.example'},
    {'Origin': 'http://127.0.0.1:5173/'}, {'Sec-Fetch-Site': 'cross-site'},
    {'Sec-Fetch-Site': 'same-site'}, {'Sec-Fetch-Site': 'none'},
    [('Origin', 'http://127.0.0.1:5173'), ('Origin', 'http://127.0.0.1:5173')],
    [('Host', '127.0.0.1:8000'), ('Host', '127.0.0.1:8000')],
])
def test_boundary_rejected_even_with_valid_bearer(client, headers):
    headers = list(headers.items()) if isinstance(headers, dict) else headers
    response = client.post('/v1/tasks', json={'instruction': 'untrusted'},
                           headers=[*TEST_HEADERS.items(), *headers])
    assert response.status_code == 403
    assert 'access-control-allow-origin' not in response.headers


def test_duplicate_bearer_denied(client):
    response = client.get('/health', headers=[*TEST_HEADERS.items(), *TEST_HEADERS.items()])
    assert response.status_code == 401


def test_explicit_authenticated_client_and_same_origin_proxy_work(client):
    assert client.get('/health', headers=TEST_HEADERS).status_code == 200
    response = client.get('/health', headers={**TEST_HEADERS, 'Origin': 'http://127.0.0.1:5173',
        'Sec-Fetch-Site': 'same-origin'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['cross-origin-resource-policy'] == 'same-origin'


def test_preflight_does_not_grant_cross_origin_permission(client):
    response = client.options('/v1/tasks', headers={'Origin': 'https://attacker.example',
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization'})
    assert response.status_code == 403 and 'access-control-allow-origin' not in response.headers


def test_default_testclient_has_no_automatic_exemption(tmp_path):
    with TestClient(create_app(Settings(tmp_path)), base_url='http://127.0.0.1:8000') as client:
        assert client.get('/health').status_code == 401
    assert (tmp_path / '.security/local-api-token').exists()


def test_policy_hides_token_in_repr_and_rejects_nonlocal_hosts():
    assert TEST_POLICY.token not in repr(TEST_POLICY)
    with pytest.raises(ValueError):
        LocalApiPolicy(TEST_POLICY.token, frozenset({'attacker.example'}), frozenset())


@pytest.mark.parametrize('method,path', [('POST', '/v1/tasks'), ('POST', '/v1/tasks/id/revisions'),
    ('POST', '/v1/tasks/id/clarifications'), ('PUT', '/v1/settings/model'), ('DELETE', '/v1/tasks/id')])
def test_every_mutation_requires_bearer_before_body_validation(client, method, path):
    assert client.request(method, path, content='invalid').status_code == 401


def test_url_token_and_cookies_cannot_authenticate(client):
    assert client.get('/health?token=' + TEST_POLICY.token).status_code == 401
    assert client.get('/health', headers={'Cookie': 'token=' + TEST_POLICY.token}).status_code == 401


@pytest.mark.parametrize('origin', ['http://user:pass@127.0.0.1:8000', 'http://127.0.0.1:0',
    'http://127.0.0.1:8000/', 'http://127.0.0.1:99999', 'https://127.0.0.1:8000'])
def test_explicit_policy_must_still_have_valid_loopback_origins(origin):
    with pytest.raises(ValueError):
        LocalApiPolicy(TEST_POLICY.token, TEST_POLICY.allowed_hosts, frozenset({origin}))
