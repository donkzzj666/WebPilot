from datetime import datetime, timezone, timedelta
import sqlite3
import pytest
from webagent.db import connect, transaction
from webagent.db.repository import add_contract, canonical_json, create_task, get_contract, list_runs, utc_text
from conftest import seed

NOW='2026-09-29T00:00:00.000000Z'


def test_contract_versions_and_rerun_history(database):
    with connect(database) as db, transaction(db):
        old=seed(db)
        db.execute("UPDATE runs SET state='CANCELLED',ended_at=? WHERE run_id='run-1'",(NOW,))
        new=seed(db,run_id='run-2',version=2,parent='run-1')
        assert get_contract(db,'task-1',1)==old
        assert get_contract(db,'task-1',2)==new
        assert [r['state'] for r in list_runs(db,'task-1')]==['CANCELLED','QUEUED']
        assert list_runs(db,'task-1')[1]['parent_run_id']=='run-1'


@pytest.mark.parametrize('statement',[
 "UPDATE contracts SET content_json='{}'",
 "DELETE FROM contracts",
 "INSERT OR REPLACE INTO contracts SELECT * FROM contracts",
 "UPDATE runs SET contract_version=2",
 "UPDATE runs SET thread_id='other'",
 "UPDATE runs SET model_config_sha256='bad'",
 "DELETE FROM runs",
 "INSERT OR REPLACE INTO runs SELECT * FROM runs",
])
def test_no_overwriting_history(database,statement):
    with connect(database) as db, transaction(db): seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db): db.execute(statement)
    with connect(database) as db: assert len(list_runs(db,'task-1'))==1


def test_terminal_run_cannot_resume_or_change(database):
    with connect(database) as db, transaction(db):
        seed(db)
        db.execute("UPDATE runs SET state='CANCELLED',ended_at=?",(NOW,))
    with pytest.raises(sqlite3.IntegrityError,match='terminal'):
        with connect(database) as db, transaction(db): db.execute("UPDATE runs SET state='QUEUED',ended_at=NULL")


def test_cross_task_parent_and_current_contract_rejected(database):
    with connect(database) as db, transaction(db): seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db): seed(db,task_id='other',run_id='other-run',parent='run-1')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db): db.execute("UPDATE tasks SET current_contract_version=999")
    with connect(database) as db: assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1


def test_whole_transaction_and_deferred_commit_failure_rollback(database):
    with pytest.raises(RuntimeError):
        with connect(database) as db, transaction(db):
            seed(db)
            raise RuntimeError('simulated crash before commit')
    with connect(database) as db: assert not list_runs(db,'task-1')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db):
            seed(db)
            db.execute("UPDATE tasks SET current_run_id='missing'")
    with connect(database) as db: assert not db.execute('SELECT * FROM tasks').fetchall()


@pytest.mark.parametrize('value',[None,'bad','2026-09-29T08:00:00.000000+08:00','2026-02-30T00:00:00.000000Z'])
def test_utc_timestamp_constraint(database,value):
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db):
            seed(db)
            db.execute('INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json) VALUES (?,?,?,?,?,?)',('task-1','run-1','state_changed',0,value,'{}'))


def test_canonical_serialization_and_explicit_transaction(database):
    assert canonical_json({'b':2,'a':'中文'})=='{"a":"中文","b":2}'
    with pytest.raises(ValueError): canonical_json({'bad':float('nan')})
    with pytest.raises(ValueError): utc_text(datetime(2026,9,29))
    assert utc_text(datetime(2026,9,29,8,tzinfo=timezone(timedelta(hours=8))))==NOW
    with connect(database) as db:
        with pytest.raises(ValueError,match='explicit transaction'):
            create_task(db,task_id='t',instruction='test',requested_fields=['contract'])


def test_event_ids_survive_reopen_and_only_committed_events_visible(database):
    with connect(database) as db, transaction(db): seed(db)
    ids=[]
    for _ in range(2):
        with connect(database) as db, transaction(db):
            ids.append(db.execute('INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json) VALUES (?,?,?,?,?,?)',('task-1','run-1','state_changed',0,NOW,'{}')).lastrowid)
    assert ids[1]>ids[0]
    with connect(database) as db, transaction(db):
        db.execute('INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json) VALUES (?,?,?,?,?,?)',('task-1','run-1','state_changed',0,NOW,'{}'))
        with connect(database) as reader: assert reader.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]==2


def test_invalid_fraction_timestamp_is_rejected(database):
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db,transaction(db):
            seed(db)
            db.execute("INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json) VALUES ('task-1','run-1','state_changed',0,'2026-09-29T00:00:00.123xyzZ','{}')")
