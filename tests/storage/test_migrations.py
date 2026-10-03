import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest
from webagent.db import LATEST_VERSION, connect, transaction, migrate, MigrationError
from webagent.db import migrations
from webagent.db.repository import get_contract, list_runs
from conftest import seed


def test_empty_and_legacy_wal_upgrade_without_touching_graph(tmp_path):
    graph=tmp_path/'graph.sqlite3'
    graph.write_bytes(b'graph sentinel')
    path=tmp_path/'business.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
    assert migrate(path)=={'previous_version':0,'schema_version':LATEST_VERSION,'applied':LATEST_VERSION}
    with connect(path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert {'tasks','contracts','runs','task_events','steps','observations','evidence','run_checkpoints','write_intents','resource_leases','quota_buckets','quota_debits','run_budgets'} <= tables
        assert 'checkpoints' not in tables
    assert graph.read_bytes()==b'graph sentinel'
    assert migrate(path)['applied']==0


def test_populated_v1_upgrade_preserves_contract_and_terminal_run(tmp_path):
    path=tmp_path/'business.sqlite3'
    migrate(path,target=1)
    with connect(path) as db, transaction(db):
        original=seed(db)
        db.execute("UPDATE runs SET state='CANCELLED',ended_at='2026-09-29T00:00:00.000000Z' WHERE run_id='run-1'")
        before=list_runs(db,'task-1')
    assert migrate(path)['applied']==LATEST_VERSION-1
    with connect(path) as db:
        assert get_contract(db,'task-1',1)==original
        assert list_runs(db,'task-1')==before
        assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]==LATEST_VERSION
    with pytest.raises(MigrationError,match='Downgrades'):
        migrate(path,target=1)


def test_failed_upgrade_rolls_back_ddl_data_and_version(tmp_path,monkeypatch):
    path=tmp_path/'business.sqlite3'
    migrate(path,target=1)
    with connect(path) as db, transaction(db):
        seed(db)
    directory=tmp_path/'migrations'
    shutil.copytree(migrations.SQL_DIR,directory)
    (directory/'0002_resources_quotas.sql').write_text(
        "CREATE TABLE transient(value TEXT);\nINSERT INTO transient VALUES ('partial');\nINVALID SQL;\n")
    monkeypatch.setattr(migrations,'SQL_DIR',directory)
    with pytest.raises(sqlite3.OperationalError): migrate(path)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]==1
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='transient'").fetchone()
        assert len(list_runs(db,'task-1'))==1
    monkeypatch.undo()
    assert migrate(path)['schema_version']==LATEST_VERSION


@pytest.mark.parametrize('damage', ['checksum','newer','schema','missing_history'])
def test_drift_is_rejected(database,damage):
    with connect(database) as db:
        if damage=='checksum': db.execute("UPDATE schema_migrations SET sha256='bad' WHERE version=1")
        elif damage=='newer': db.execute('PRAGMA user_version=99')
        elif damage=='schema': db.execute('DROP INDEX runs_due')
        else: db.execute('DELETE FROM schema_migrations WHERE version=1')
    before=database.read_bytes()
    with pytest.raises(MigrationError): migrate(database)
    assert database.read_bytes()==before


def test_refuse_foreign_database_without_modifying_it(tmp_path):
    path=tmp_path/'graph.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE checkpoints(id TEXT)')
    before=path.read_bytes()
    with pytest.raises(MigrationError,match='Not a WebAgent'): migrate(path)
    assert path.read_bytes()==before


def test_every_connection_uses_foreign_keys_and_durable_settings(database):
    for _ in range(2):
        with connect(database,busy_timeout_ms=123) as db:
            for name,value in [('foreign_keys',1),('recursive_triggers',1),('synchronous',2),('busy_timeout',123)]:
                assert db.execute('PRAGMA '+name).fetchone()[0]==value


def test_failed_first_install_is_empty_and_can_retry(tmp_path,monkeypatch):
    path=tmp_path/'business.sqlite3'
    directory=tmp_path/'sql';shutil.copytree(migrations.SQL_DIR,directory)
    with (directory/'0002_resources_quotas.sql').open('a') as f:f.write('INVALID SQL;\n')
    monkeypatch.setattr(migrations,'SQL_DIR',directory)
    with pytest.raises(sqlite3.OperationalError):migrate(path)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==0
        assert db.execute('PRAGMA application_id').fetchone()[0]==0
        assert not db.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
    monkeypatch.undo()
    assert migrate(path)['applied']==LATEST_VERSION
