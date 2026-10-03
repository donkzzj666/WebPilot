"""Authenticated, read-only budget projections never create or debit a Run."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api_support import create_test_app, TEST_HEADERS
from conftest import seed
from webagent.budgets.routes import current_quota
from webagent.config import Settings
from webagent.db import connect, transaction
from webagent.db.repository import add_contract, create_run, create_task, utc_text
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore


@pytest.fixture
def client(tmp_path):
    settings = Settings(tmp_path)
    with TestClient(create_test_app(settings), base_url='http://127.0.0.1:8000', headers=TEST_HEADERS) as client:
        client.database = settings.business_db
        yield client


def snapshot(path):
    with connect(path) as db:
        tables = ('runs', 'run_budgets', 'budget_limits', 'budget_timers', 'budget_attempts',
                  'quota_buckets', 'quota_debits', 'quota_monitor_sources', 'resource_leases',
                  'scheduler_queue', 'scheduler_events', 'task_events')
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                for name in tables}


def test_empty_quota_projection_is_no_store_and_does_not_initialize_daily_bucket(client):
    before = snapshot(client.database)
    response = client.get('/v1/budgets')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body['timezone'] == 'Asia/Shanghai'
    assert body['scope'] == 'configured_data_directory'
    assert body['public']['capacity'] == body['public']['remaining'] == 50
    assert body['public']['ordinary'] == {'capacity': 42, 'used': 0, 'remaining': 42}
    assert body['public']['monitoring']['reserved'] == body['public']['monitoring']['remaining'] == 8
    assert set(body['public']['monitoring']['sources']) == {'cisa_kev', 'security_community'}
    assert all(value == {'capacity': 4, 'used': 0, 'remaining': 4}
               for value in body['public']['monitoring']['sources'].values())
    assert body['webarena']['uses_public_quota'] is False
    assert snapshot(client.database) == before


def test_queued_run_projection_does_not_start_timer_or_consume_quota(client):
    with connect(client.database) as db, transaction(db):
        seed(db)
    before = snapshot(client.database)
    for _ in range(2):
        response = client.get('/v1/budgets/runs/run-1')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert response.json() == {'run_id': 'run-1', 'initialized': False, 'exhausted': False, 'reason': None}
    assert snapshot(client.database) == before


def test_running_budget_and_monitor_quota_show_only_safe_metadata_without_settling(client):
    path = client.database
    with connect(path) as db, transaction(db):
        seed(db)
        create_task(db, task_id='monitor-task', instruction='fixture-secret-do-not-export', requested_fields=['contract'])
        add_contract(db, {'schema_version': 'm0-contract-v1', 'task_id': 'monitor-task',
                          'contract_version': 1, 'scenario': 'monitoring', 'objective': 'fixture',
                          'parameters': {'source_kind': 'cisa_kev'}, 'sources': ['local-fixture'],
                          'action_policy': {'mode': 'read_only'}})
        create_run(db, run_id='monitor-run', task_id='monitor-task', contract_version=1,
                   graph_version='graph-v1', graph_state_schema_version='state-v1',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
    store = SchedulerStore(path)
    generation = store.start_worker('api-fixture-worker')
    for run_id, queue_class in [('run-1', 'ordinary'), ('monitor-run', 'monitoring')]:
        store.enqueue(run_id, [Resource.site_identity('local-fixture', run_id), Resource.browser_context(run_id)],
                      expected_state_version=0, queue_class=queue_class)
    monitor = store.claim('api-fixture-worker', generation)
    ordinary = store.claim('api-fixture-worker', generation)
    assert monitor.run_id == 'monitor-run' and ordinary.run_id == 'run-1'
    before = snapshot(path)
    quota = client.get('/v1/budgets')
    run = client.get('/v1/budgets/runs/run-1')
    assert quota.status_code == run.status_code == 200
    assert quota.json()['public']['used'] == 2
    assert quota.json()['public']['ordinary']['used'] == 1
    assert quota.json()['public']['monitoring']['sources']['cisa_kev']['used'] == 1
    assert run.json()['initialized'] and run.json()['quota']['debit_kind'] == 'ordinary'
    assert run.json()['limits']['max_actions'] == 150
    assert run.json()['active_ms'] >= 0
    assert 'fixture-secret-do-not-export' not in quota.text + run.text
    for forbidden in ('credential_ref', 'auth_ref', 'clock_domain', 'anchor_mono_ns', 'worker_id'):
        assert forbidden not in quota.text + run.text
    assert snapshot(path) == before


def test_missing_run_has_public_404_and_no_mutation(client):
    before = snapshot(client.database)
    response = client.get('/v1/budgets/runs/missing')
    assert response.status_code == 404 and response.json()['code'] == 'NOT_FOUND'
    assert response.headers['cache-control'] == 'no-store'
    assert snapshot(client.database) == before


@pytest.mark.parametrize('path', ['/v1/budgets', '/v1/budgets/runs/missing'])
@pytest.mark.parametrize('headers,status', [({'Authorization': ''}, 401),
                                           ({'Origin': 'https://attacker.invalid'}, 403),
                                           ({'Host': 'attacker.invalid'}, 403)])
def test_budget_reads_use_existing_local_api_boundary(client, path, headers, status):
    assert client.get(path, headers=headers).status_code == status


@pytest.mark.parametrize('path', ['/v1/budgets', '/v1/budgets/runs/run-1',
                                 '/v1/budgets/consume', '/v1/budgets/reset'])
def test_http_cannot_change_counters_limits_or_quota(client, path):
    before = snapshot(client.database)
    assert client.post(path, json={'active_ms': 0, 'max_actions': 9999, 'epoch': 1}).status_code in (404, 405)
    assert snapshot(client.database) == before


def test_quota_projection_cuts_day_in_shanghai_without_creating_either_bucket(client):
    before = snapshot(client.database)
    earlier = current_quota(client.database, now=datetime(2026, 9, 30, 15, 59, 59, tzinfo=timezone.utc))
    later = current_quota(client.database, now=datetime(2026, 9, 30, 16, 0, 0, tzinfo=timezone.utc))
    assert earlier['quota_date'] == '2026-09-30' and later['quota_date'] == '2026-10-01'
    assert snapshot(client.database) == before


def legacy_debits(path, count, *, kind='ordinary', bucket_used=0):
    date = current_quota(path)['quota_date']
    with connect(path) as db, transaction(db):
        db.execute("INSERT INTO quota_buckets(quota_date,quota_type,used) VALUES(?,'public',?)", (date, bucket_used))
        for index in range(count):
            run_id = 'legacy-' + str(index)
            seed(db, task_id='task-' + run_id, run_id=run_id)
            db.execute('''INSERT INTO quota_debits(debit_id,run_id,quota_date,quota_type,debit_kind,debited_at)
                VALUES(?,?,?,'public',?,?)''', ('debit-' + run_id, run_id, date, kind, utc_text()))


@pytest.mark.parametrize('bucket_used,debits,expected', [(0, 50, 50), (7, 2, 7)])
def test_quota_read_conservatively_combines_legacy_ledger_and_bucket_counter(client, bucket_used, debits, expected):
    legacy_debits(client.database, debits, bucket_used=bucket_used)
    before = snapshot(client.database)
    body = client.get('/v1/budgets').json()['public']
    assert body['used'] == expected and body['remaining'] == 50 - expected
    assert body['ordinary']['remaining'] <= body['remaining']
    assert body['monitoring']['remaining'] <= body['remaining']
    assert snapshot(client.database) == before


@pytest.mark.parametrize('count', [1, 8])
def test_legacy_monitor_debits_with_unknown_source_cannot_report_fresh_reserved_quota(client, count):
    legacy_debits(client.database, count, kind='monitoring')
    before = snapshot(client.database)
    body = client.get('/v1/budgets').json()['public']
    assert body['used'] == count and body['remaining'] == 50 - count
    assert body['monitoring']['used'] == body['monitoring']['unknown_source_used'] == count
    source_remaining = max(0, 4 - count)
    assert body['monitoring']['remaining'] == min(8 - count, source_remaining * 2)
    assert all(value['remaining'] == source_remaining for value in body['monitoring']['sources'].values())
    assert body['monitoring']['used'] + body['monitoring']['remaining'] <= 8
    assert snapshot(client.database) == before
