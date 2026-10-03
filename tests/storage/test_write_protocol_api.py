"""Write diagnostics reveal pending effects without granting authority."""
import json

import pytest

from api_support import AuthenticatedTestClient, create_test_app
from webagent.db import connect, transaction
from webagent.db.repository import utc_text
from unit.test_graph_executor import prepared


@pytest.fixture
def client(tmp_path):
    settings, _, token, _, _ = prepared(tmp_path)
    with connect(settings.business_db) as db, transaction(db):
        for index in range(2):
            db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
                target,expected_change,identity_ref,precondition_version,status,receipt,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'UNKNOWN',NULL,?,?)''',
                ('write-'+str(index), 'business-'+str(index), 'task-1', token.run_id,
                 'PRIVATE_TARGET', 'PRIVATE_CHANGE', 'PRIVATE_IDENTITY', 'PRIVATE_VERSION', utc_text(), utc_text()))
    with AuthenticatedTestClient(create_test_app(settings)) as client:
        yield client


def test_pending_operations_are_visible_and_paginated_without_private_facts(client):
    response = client.get('/v1/runs/run-1/write-intents?limit=1')
    assert response.status_code == 200
    body = response.json()
    assert body['pending_count'] == 2
    assert [row['operation_id'] for row in body['write_intents']] == ['write-0']
    assert body['write_intents'][0]['status'] == 'UNKNOWN'
    assert body['write_intents'][0]['attempts'] == 0
    second = client.get('/v1/runs/run-1/write-intents?after=' + str(body['next_after'])).json()
    assert [row['operation_id'] for row in second['write_intents']] == ['write-1']
    assert 'PRIVATE_' not in response.text
    assert not any(name in response.text for name in ('facts_json', 'artifact_path', 'expected_change', 'identity_ref'))
    assert client.post('/v1/runs/run-1/write-intents', json={'status': 'CONFIRMED'}).status_code == 405


@pytest.mark.parametrize('query', ['after=-1', 'after=1&after=2', 'limit=0', 'limit=1001',
                                  'raw=true', 'after=9223372036854775808', 'limit=true'])
def test_query_rejects_unbounded_or_unsupported_input(client, query):
    assert client.get('/v1/runs/run-1/write-intents?' + query).status_code == 422


@pytest.mark.parametrize('headers,status', [({'Authorization': ''}, 401),
    ({'Origin': 'https://untrusted.invalid'}, 403), ({'Host': 'untrusted.invalid'}, 403)])
def test_diagnostics_inherit_local_api_access_boundary(client, headers, status):
    assert client.get('/v1/runs/run-1/write-intents', headers=headers).status_code == status


def test_missing_run_returns_no_storage_detail(client):
    response = client.get('/v1/runs/missing-run/write-intents')
    assert response.status_code == 404 and 'sqlite' not in response.text
