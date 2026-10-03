"""State matrix, atomicity, process races and durable replay acceptance."""
from datetime import datetime, timezone, timedelta
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from api_support import AuthenticatedTestClient as TestClient
from pydantic import ValidationError

from api_support import create_test_app as create_app
from webagent.config import Settings
from webagent.db import LATEST_VERSION, connect, migrate, transaction
from webagent.errors import BusinessError
from webagent.events import ActionEvent, WaitingEvent, ResultEvent, append_event, read_events
from webagent.state import transition, transition_in_transaction
from conftest import seed

# Independent expected matrix from TRD §4.1, including all-nonterminal cancellation,
# fault reconciliation, CI/handoff timeout outcomes, and M1-18 safe pause boundaries.
# M1-23 adds a conditional QUEUED -> RECONCILING edge for an explicitly
# registered related write-query Run; it is not an unconditional matrix edge.
EXPECTED = {
 'QUEUED': {'RUNNING', 'CANCELLED'},
 'RUNNING': {'VERIFYING', 'WAITING_CI', 'WAITING_SITE', 'WAITING_HANDOFF', 'PAUSED',
             'RECONCILING', 'PARTIAL', 'FAILED', 'CANCELLED'},
 'VERIFYING': {'RUNNING', 'WAITING_HANDOFF', 'RECONCILING', 'PAUSED', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'},
 'WAITING_CI': {'RECONCILING', 'PARTIAL', 'FAILED', 'CANCELLED'},
 'WAITING_SITE': {'RECONCILING', 'FAILED', 'CANCELLED'},
 'WAITING_HANDOFF': {'RECONCILING', 'PARTIAL', 'FAILED', 'CANCELLED'},
 'PAUSED': {'RECONCILING', 'FAILED', 'CANCELLED'},
 'RECONCILING': {'RUNNING', 'VERIFYING', 'PAUSED', 'FAILED', 'CANCELLED'},
 'SUCCEEDED': set(), 'PARTIAL': set(), 'FAILED': set(), 'CANCELLED': set(),
}
DEADLINE = datetime(2030, 1, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def move(path, target, version=0, run='run-1', **kwargs):
    return transition(path, run_id=run, expected_state_version=version, target=target, **kwargs)


def reach(path, state):
    with connect(path) as db, transaction(db):
        seed(db)
    if state == 'QUEUED':
        return 0
    if state == 'CANCELLED':
        move(path, state)
        return 1
    move(path, 'RUNNING')
    if state == 'RUNNING':
        return 1
    if state == 'SUCCEEDED':
        move(path, 'VERIFYING', 1)
        move(path, state, 2)
        return 3
    move(path, state, 1, **({'handoff_deadline': DEADLINE} if state == 'WAITING_HANDOFF' else {}))
    return 2


@pytest.mark.parametrize('source', EXPECTED)
@pytest.mark.parametrize('target', [*EXPECTED, 'NEEDS_INPUT'])
def test_every_legal_and_illegal_transition(database, source, target):
    version = reach(database, source)
    before = read_events(database)
    arguments = {'handoff_deadline': DEADLINE} if target == 'WAITING_HANDOFF' else {}
    if (source, target) == ('QUEUED', 'RECONCILING'):
        with pytest.raises(sqlite3.IntegrityError, match='explicit write reconciliation binding'):
            move(database, target, version)
        assert read_events(database) == before
        with connect(database) as db:
            assert tuple(db.execute('SELECT state,state_version FROM runs').fetchone()) == (source, version)
        return
    if target in EXPECTED[source]:
        event = move(database, target, version, **arguments)
        assert event['state_version'] == version + 1
        assert event['payload'] == {'event_type': 'state_changed', 'previous_state': source,
                                   'current_state': target, 'blocked_reason': None}
        assert read_events(database) == before + [event]
        with connect(database) as db:
            row = db.execute('SELECT * FROM runs').fetchone()
            assert row['state'] == target and row['state_version'] == version + 1
            assert (row['ended_at'] is not None) == (not EXPECTED[target])
    else:
        with pytest.raises(BusinessError) as error:
            move(database, target, version, **arguments)
        assert error.value.code == 'INVALID_PARAMETER'
        assert read_events(database) == before
        with connect(database) as db:
            row = db.execute('SELECT state,state_version FROM runs').fetchone()
            assert tuple(row) == (source, version)


def test_stale_version_is_http_409_with_current_versions(database):
    reach(database, 'RUNNING')
    app = create_app(Settings(database.parent))
    # Exercise the real common HTTP adapter without publishing a generic state setter.
    @app.post('/test-only-transition')
    def test_transition():
        return move(database, 'VERIFYING', 0)
    with TestClient(app) as client:
        response = client.post('/test-only-transition')
        assert response.status_code == 409
        body = response.json()
        assert body['code'] == 'STATE_CONFLICT'
        assert body['current_state_version'] == 1
        assert body['current_contract_version'] == 1
        assert body['retryable'] is False
        assert body['request_id'] == response.headers['x-request-id']
    assert len(read_events(database)) == 1


@pytest.mark.parametrize('value', [-1, True, 1.5, '0', 2**63])
def test_invalid_expected_version(database, value):
    reach(database, 'QUEUED')
    with pytest.raises(BusinessError, match='version'):
        move(database, 'RUNNING', value)
    assert not read_events(database)


def test_missing_run_and_handoff_metadata(database):
    with pytest.raises(BusinessError) as error:
        move(database, 'RUNNING')
    assert error.value.status == 404
    reach(database, 'RUNNING')
    for deadline in (None, datetime(2030, 1, 1)):
        with pytest.raises(BusinessError):
            move(database, 'WAITING_HANDOFF', 1, handoff_deadline=deadline)
    event = move(database, 'WAITING_HANDOFF', 1, blocked_reason='challenge', handoff_deadline=DEADLINE)
    assert event['payload']['blocked_reason'] == 'challenge'
    move(database, 'RECONCILING', 2)
    with connect(database) as db:
        row = db.execute('SELECT * FROM runs').fetchone()
        assert row['blocked_reason'] is None and row['handoff_deadline'] is None
        assert row['started_at'] is not None


@pytest.mark.parametrize('statement', [
 "UPDATE runs SET state='RUNNING',started_at='2026-09-29T00:00:00.000000Z'",
 "UPDATE runs SET state_version=state_version+1",
 "UPDATE runs SET blocked_reason='hidden change'",
 "UPDATE runs SET state='SUCCEEDED',state_version=1,started_at='2026-09-29T00:00:00.000000Z',ended_at='2026-09-29T00:00:00.000000Z'",
 "DELETE FROM run_transitions",
 "UPDATE run_transitions SET current_state='SUCCEEDED'",
 "INSERT INTO run_transitions VALUES ('QUEUED','SUCCEEDED')",
])
def test_direct_sql_cannot_bypass_state_guards(database, statement):
    reach(database, 'QUEUED')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db):
            db.execute(statement)
    assert not read_events(database)


def test_direct_valid_transition_still_generates_event(database):
    reach(database, 'QUEUED')
    with connect(database) as db, transaction(db):
        db.execute("UPDATE runs SET state='CANCELLED',state_version=1,ended_at='2026-09-29T00:00:00.000000Z'")
    assert read_events(database)[0]['payload']['current_state'] == 'CANCELLED'
    with pytest.raises(sqlite3.IntegrityError, match='duplicate'):
        with connect(database) as db, transaction(db):
            db.execute('''INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json)
                          SELECT task_id,run_id,event_type,state_version,occurred_at,payload_json FROM task_events''')


def test_event_failure_and_commit_failure_roll_back_state(database):
    reach(database, 'QUEUED')
    with connect(database) as db:
        db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON task_events BEGIN SELECT RAISE(ABORT,'injected disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        move(database, 'RUNNING')
    with connect(database) as db:
        assert tuple(db.execute('SELECT state,state_version FROM runs').fetchone()) == ('QUEUED', 0)
        db.execute('DROP TRIGGER fail_event')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db):
            transition_in_transaction(db, run_id='run-1', expected_state_version=0, target='RUNNING')
            db.execute("UPDATE tasks SET current_run_id='missing'")
    assert not read_events(database)
    assert move(database, 'RUNNING')['state_version'] == 1


@pytest.mark.parametrize('committed', [False, True])
def test_process_interruption_before_and_after_commit(database, committed):
    reach(database, 'QUEUED')
    script = '''import os,sys
from pathlib import Path
from webagent.db import connect,transaction
from webagent.state import transition_in_transaction
with connect(Path(sys.argv[1])) as db, transaction(db):
    transition_in_transaction(db,run_id='run-1',expected_state_version=0,target='RUNNING')
    if sys.argv[2]=='before': os._exit(73)
os._exit(74)
'''
    result = subprocess.run([sys.executable, '-c', script, str(database), 'after' if committed else 'before'],
                            env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend')}, timeout=10)
    assert result.returncode == (74 if committed else 73)
    with connect(database) as db:
        assert tuple(db.execute('SELECT state,state_version FROM runs').fetchone()) == (
            ('RUNNING', 1) if committed else ('QUEUED', 0))
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert len(read_events(database)) == int(committed)
    if committed:
        with pytest.raises(BusinessError) as error:
            move(database, 'RUNNING')
        assert error.value.code == 'STATE_CONFLICT'
        assert move(database, 'VERIFYING', 1)['event_id'] > read_events(database)[0]['event_id']


def contender(path, start, ready, results):
    ready.put(True)
    start.wait(10)
    try:
        results.put(move(path, 'RUNNING')['state_version'])
    except BusinessError as error:
        results.put(error.code)


def test_competing_processes_have_one_transition_and_one_event(database):
    reach(database, 'QUEUED')
    ctx = multiprocessing.get_context('spawn')
    start, ready, results = ctx.Event(), ctx.Queue(), ctx.Queue()
    processes = [ctx.Process(target=contender, args=(database, start, ready, results)) for _ in range(4)]
    try:
        for process in processes: process.start()
        for _ in processes: assert ready.get(timeout=20)
        start.set()
        values = [results.get(timeout=20) for _ in processes]
        assert values.count(1) == 1 and values.count('STATE_CONFLICT') == 3
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive(): process.terminate(); process.join(5)
        ready.close(); results.close()
    assert len(read_events(database)) == 1


def test_uncommitted_event_hidden_then_rollback_and_monotonic_restart(database):
    reach(database, 'QUEUED')
    with pytest.raises(RuntimeError):
        with connect(database) as db, transaction(db):
            provisional = transition_in_transaction(db, run_id='run-1', expected_state_version=0, target='RUNNING')
            assert provisional['event_id'] > 0
            assert read_events(database) == []
            raise RuntimeError('rollback')
    committed = move(database, 'RUNNING')
    assert read_events(database) == [committed]
    following = move(database, 'VERIFYING', 1)
    assert following['event_id'] > committed['event_id']
    assert read_events(database, after=committed['event_id']) == [following]


def test_v2_upgrade_preserves_legacy_rows_without_fabricated_history(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    migrate(path, target=2)
    with connect(path) as db, transaction(db):
        seed(db)
        db.execute("UPDATE runs SET state='RUNNING',state_version=4,started_at='2026-09-29T00:00:00.000000Z'")
        for _ in range(2):
            db.execute("INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json) VALUES ('task-1','run-1','state_changed',0,'2026-09-29T00:00:00.000000Z','{}')")
    before = read_events(path)
    assert migrate(path)['applied'] == LATEST_VERSION - 2
    assert read_events(path) == before
    assert move(path, 'VERIFYING', 4)['state_version'] == 5
    assert len(read_events(path)) == 3
    assert migrate(path)['applied'] == 0


def test_structured_business_events_filters_and_paging(database):
    reach(database, 'RUNNING')
    with connect(database) as db, transaction(db):
        seed(db, task_id='task-2', run_id='run-2')
        action = append_event(db, run_id='run-1', expected_state_version=1, payload=ActionEvent(
            step_id='step-1', action_type='read_visible', attempt_status='COMPLETED', evidence_ids=['ev-1']))
        waiting = append_event(db, run_id='run-2', expected_state_version=0, payload=WaitingEvent(
            wait_id='wait-1', reason='site', deadline=DEADLINE.astimezone(timezone(timedelta(hours=8)))))
        with pytest.raises(BusinessError):
            append_event(db, run_id='run-1', expected_state_version=0, payload=WaitingEvent(wait_id='w', reason='pause'))
        with pytest.raises(BusinessError):
            append_event(db, run_id='run-1', expected_state_version=1, payload={'raw_model_output': 'private'})
        with pytest.raises(BusinessError):
            append_event(db, run_id='run-1', expected_state_version=1, payload=ResultEvent(result_ref='r', outcome='SUCCEEDED'))
    assert waiting['payload']['deadline'] == '2030-01-01T00:00:00.000000Z'
    assert read_events(database, run_id='run-2') == [waiting]
    assert read_events(database, task_id='task-1', after=1) == [action]
    assert read_events(database, task_id='task-1', run_id='run-2') == []
    assert len(read_events(database, limit=1)) == 1
    move(database, 'FAILED', 1)
    with connect(database) as db, transaction(db):
        result = append_event(db, run_id='run-1', expected_state_version=2,
                              payload=ResultEvent(result_ref='result-1', outcome='FAILED'))
    assert result['payload']['outcome'] == 'FAILED'
    with pytest.raises(ValidationError):
        ActionEvent(step_id='s', action_type='read_visible', attempt_status='COMPLETED', evidence_ids=[], raw_prompt='secret')


@pytest.mark.parametrize('header', ['-1', '1.2', 'abc', '１', '9223372036854775808', '999'])
def test_invalid_sse_cursor_returns_contract_error(database, header):
    with TestClient(create_app(Settings(database.parent))) as client:
        response = client.get('/v1/events', headers={'Last-Event-ID': header.encode('utf-8')})
        assert response.status_code == 422
        assert response.json()['code'] == 'INVALID_PARAMETER'
        assert response.json()['details'][0]['field'] == 'Last-Event-ID'


def test_empty_sse_filter_is_rejected(database):
    with TestClient(create_app(Settings(database.parent))) as client:
        assert client.get('/v1/events?task_id=').status_code == 422
