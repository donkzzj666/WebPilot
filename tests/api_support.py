"""Explicit authenticated test clients; production protection is never disabled."""
from fastapi.testclient import TestClient as _TestClient
from webagent.api import create_app as _create_app
from webagent.security import LocalApiPolicy

TEST_TOKEN = 'm112-' + 'a' * 59
TEST_HEADERS = {'Authorization': 'Bearer ' + TEST_TOKEN}
TEST_POLICY = LocalApiPolicy(TEST_TOKEN,
    frozenset({'127.0.0.1', '127.0.0.1:8000'}),
    frozenset({'http://127.0.0.1', 'http://127.0.0.1:8000', 'http://127.0.0.1:5173'}))


def create_test_app(settings, **kwargs):
    return _create_app(settings, local_api_policy=TEST_POLICY, **kwargs)


class AuthenticatedTestClient(_TestClient):
    def __init__(self, app, *args, **kwargs):
        kwargs.setdefault('base_url', 'http://127.0.0.1:8000')
        kwargs['headers'] = {**TEST_HEADERS, **kwargs.get('headers', {})}
        super().__init__(app, *args, **kwargs)
