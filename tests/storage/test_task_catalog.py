"""Persistent task pagination without model calls or changes to business facts."""
from copy import deepcopy
import sqlite3

import pytest

from api_support import AuthenticatedTestClient, create_test_app
from webagent.config import Settings
from webagent.db import connect, transaction
from webagent.db.repository import create_run
from webagent.errors import BusinessError
from webagent.tasks.catalog import list_tasks
from webagent.tasks.models import CreateTaskRequest
from webagent.tasks import service
from test_task_api import FINANCE


def prepare(path, key, *, complete=False):
    body = deepcopy(FINANCE) if complete else {'instruction': '需要补充的任务 ' + key}
    return service.create(path, CreateTaskRequest.model_validate(body), key).body


def test_cursor_does_not_repeat_tasks_when_new_tasks_arrive_between_pages(database):
    ids = [prepare(database, str(i))['task']['task_id'] for i in range(5)]
    first = list_tasks(database, limit=2)
    assert [x['task_id'] for x in first['tasks']] == ids[-1:-3:-1]
    assert isinstance(first['next_cursor'], str)
    added = prepare(database, 'new')['task']['task_id']
    rest = []
    cursor = first['next_cursor']
    while cursor:
        page = list_tasks(database, before=int(cursor), limit=2)
        rest.extend(x['task_id'] for x in page['tasks'])
        cursor = page['next_cursor']
    assert rest == list(reversed(ids[:-2])) and added not in rest
    assert list_tasks(database, limit=1)['tasks'][0]['task_id'] == added


def test_catalog_keeps_preparation_separate_from_run_state_and_reads_committed_revisions(database):
    draft = prepare(database, 'draft')
    ready = prepare(database, 'ready', complete=True)
    task_id = ready['task']['task_id']
    with connect(database) as db, transaction(db):
        create_run(db, run_id='catalog-run', task_id=task_id, contract_version=1,
                   graph_version='catalog-graph', graph_state_schema_version='catalog-state',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
        db.execute('UPDATE tasks SET current_run_id=? WHERE task_id=?', ('catalog-run', task_id))
    rows = {row['task_id']: row for row in list_tasks(database)['tasks']}
    assert rows[task_id]['preparation_status'] == 'READY'
    assert rows[task_id]['current_run_state'] == 'QUEUED'
    assert rows[task_id]['revision'] == 1 and rows[task_id]['requested_fields'] == []
    assert rows[draft['task']['task_id']]['current_run_state'] is None
    assert rows[draft['task']['task_id']]['requested_fields'] == draft['missing_fields']
    assert set(rows[task_id]) == {'task_id', 'original_instruction', 'preparation_status',
        'current_contract_version', 'current_run_id', 'state_version', 'created_at',
        'requested_fields', 'current_run_state', 'updated_at', 'revision'}
    assert 'credential' not in repr(rows) and 'model_config_sha256' not in repr(rows)


def test_http_catalog_is_authenticated_no_store_and_cannot_modify_task_ledgers(database):
    prepare(database, 'draft')
    tables = ('tasks', 'contracts', 'task_revisions', 'runs', 'task_events', 'api_idempotency')
    def snapshot():
        with connect(database) as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)] for table in tables}
    before = snapshot()
    with AuthenticatedTestClient(create_test_app(Settings(database.parent))) as client:
        response = client.get('/v1/tasks')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert len(response.json()['tasks']) == 1
        assert client.get('/v1/tasks', headers={'Authorization': ''}).status_code == 401
    assert snapshot() == before


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'limit=-1', 'limit=01', 'limit=true',
    'before=0', 'before=-1', 'before=01', 'before=9223372036854775808',
    'limit=1&limit=2', 'before=1&before=2', 'after=1', 'limit=', 'before=1%20OR%201=1'])
def test_http_catalog_rejects_ambiguous_or_unbounded_cursor_parameters(database, query):
    with AuthenticatedTestClient(create_test_app(Settings(database.parent))) as client:
        assert client.get('/v1/tasks?' + query).status_code == 422


def test_read_only_catalog_does_not_create_missing_storage(tmp_path):
    path = tmp_path / 'business.sqlite3'
    with pytest.raises(BusinessError) as caught:
        list_tasks(path)
    assert caught.value.status == 503 and not path.exists()


def test_read_only_catalog_does_not_migrate_old_storage(tmp_path):
    path = tmp_path / 'business.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=1')
    before = path.read_bytes()
    with pytest.raises(BusinessError) as caught:
        list_tasks(path)
    assert caught.value.status == 503 and path.read_bytes() == before


def test_empty_catalog_has_no_fake_task_or_cursor(database):
    result = list_tasks(database)
    assert result['tasks'] == [] and result['next_cursor'] is None
    assert result['as_of'].endswith('Z')
