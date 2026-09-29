"""Compete across real processes/connections; coordination is test-only."""
import multiprocessing
import sqlite3
import pytest
from webagent.db import connect, transaction, migrate, StorageBusyError
from webagent.db.repository import add_contract, create_task
from conftest import seed

NOW='2026-09-29T00:00:00.000000Z'


def competitor(path,kind,start,ready,results,number):
    ready.put(number)
    if not start.wait(15):
        results.put('timeout'); return
    try:
        if kind=='migrate': migrate(path)
        else:
            with connect(path) as db:
                for _ in range(12 if kind=='increment' else 1):
                    with transaction(db):
                        if kind=='contract':
                            add_contract(db,{'task_id':'task-1','contract_version':2,'scenario':'research','schema_version':'m0-contract-v1'})
                        elif kind=='quota':
                            db.execute("INSERT INTO quota_debits VALUES (?, 'run-1','2026-09-29','public','ordinary',1,?)",(f'debit-{number}',NOW))
                        elif kind=='resource':
                            db.execute("INSERT INTO resource_leases VALUES ('active:1','active_slot','run-1',?,1,?,?,0,'worker',0)",(f'worker-{number}',NOW,NOW))
                        else:
                            db.execute("UPDATE tasks SET state_version=state_version+1 WHERE task_id='task-1'")
        results.put('ok')
    except sqlite3.IntegrityError: results.put('conflict')
    except Exception as e: results.put(type(e).__name__+':'+str(e))


def compete(path,kind,count=4):
    ctx=multiprocessing.get_context('spawn')
    start=ctx.Event();ready=ctx.Queue();results=ctx.Queue()
    processes=[ctx.Process(target=competitor,args=(path,kind,start,ready,results,i)) for i in range(count)]
    try:
        for p in processes:p.start()
        for _ in processes:ready.get(timeout=20)
        start.set()
        outcomes=[results.get(timeout=30) for _ in processes]
        for p in processes:
            p.join(10)
            assert p.exitcode==0
        return outcomes
    finally:
        for p in processes:
            if p.is_alive():p.terminate();p.join(5)
        ready.close();results.close()


@pytest.mark.parametrize('kind',['contract','quota','resource'])
def test_duplicate_keys_have_one_winner_across_processes(database,kind):
    with connect(database) as db, transaction(db):
        seed(db)
        db.execute("INSERT INTO quota_buckets(quota_date,quota_type) VALUES ('2026-09-29','public')")
    results=compete(database,kind)
    assert results.count('ok')==1,results
    assert results.count('conflict')==3,results


def test_concurrent_migrations_are_idempotent(tmp_path):
    path=tmp_path/'business.sqlite3'
    assert compete(path,'migrate')==['ok']*4
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]==2


def test_short_write_transactions_have_no_lost_updates(database):
    with connect(database) as db, transaction(db):seed(db)
    assert compete(database,'increment')==['ok']*4
    with connect(database) as db:
        assert db.execute("SELECT state_version FROM tasks WHERE task_id='task-1'").fetchone()[0]==48


def test_wal_reader_progress_and_bounded_writer_wait(database):
    with connect(database) as db, transaction(db):seed(db)
    with connect(database) as holder,transaction(holder):
        holder.execute("UPDATE tasks SET state_version=1")
        with connect(database) as reader:
            assert reader.execute('SELECT state_version FROM tasks').fetchone()[0]==0
        with pytest.raises(StorageBusyError):
            with connect(database,busy_timeout_ms=40) as blocked,transaction(blocked):
                pytest.fail('Contending writer must not enter transaction')
    with connect(database) as db,transaction(db):
        db.execute('UPDATE tasks SET state_version=state_version+1')
    with connect(database) as db:
        assert db.execute('SELECT state_version FROM tasks').fetchone()[0]==2


def uncommitted_writer(path,ready):
    with connect(path) as db,transaction(db):
        create_task(db,task_id='crash',instruction='synthetic',requested_fields=['contract'])
        ready.set()
        multiprocessing.Event().wait(30)


def test_process_death_discards_uncommitted_rows(database):
    ctx=multiprocessing.get_context('spawn');ready=ctx.Event()
    process=ctx.Process(target=uncommitted_writer,args=(database,ready))
    try:
        process.start();assert ready.wait(15)
        process.terminate();process.join(10)
        assert not process.is_alive()
        with connect(database) as db:
            assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==0
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:
        if process.is_alive():process.kill();process.join()
