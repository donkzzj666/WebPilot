"""Upgrade the previously delivered M1-03 database without rewriting its history."""
from api_support import AuthenticatedTestClient as TestClient

from api_support import create_test_app as create_app
from webagent.config import Settings
from webagent.db import LATEST_VERSION, connect, migrate, transaction
from webagent.events import read_events
from webagent.state import transition
from conftest import seed


def test_v3_upgrade_retains_old_contract_failed_run_and_event_ids(tmp_path):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=3)
    with connect(path) as db, transaction(db):
        seed(db)
        db.execute("UPDATE tasks SET preparation_status='READY',requested_fields_json='[]',current_contract_version=1,current_run_id='run-1'")
    transition(path, run_id='run-1', expected_state_version=0, target='RUNNING')
    transition(path, run_id='run-1', expected_state_version=1, target='FAILED')
    with connect(path) as db:
        contract = dict(db.execute('SELECT * FROM contracts').fetchone())
        run = dict(db.execute('SELECT * FROM runs').fetchone())
    events = read_events(path)
    assert migrate(path)['applied'] == LATEST_VERSION - 3
    assert migrate(path)['applied'] == 0
    with connect(path) as db:
        assert dict(db.execute('SELECT * FROM contracts').fetchone()) == contract
        assert dict(db.execute('SELECT * FROM runs').fetchone()) == run
        assert db.execute('SELECT COUNT(*) FROM task_revisions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM api_idempotency').fetchone()[0] == 0
    assert read_events(path) == events
    with TestClient(create_app(Settings(tmp_path))) as client:
        detail = client.get('/v1/tasks/task-1').json()
        assert detail['current_run']['state'] == 'FAILED'
        assert detail['contract']['contract_version'] == 1
        assert detail['draft'] is None and detail['revisions'] == []
        result = client.post('/v1/tasks/task-1/clarifications',
                             headers={'Idempotency-Key':'legacy-edit'},
                             json={'contract_version':1,'values':{'scenario':'finance'}})
        assert result.status_code == 409  # Do not fabricate an editable preparation history.
