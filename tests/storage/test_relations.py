import sqlite3
import pytest
from webagent.db import connect,transaction
from conftest import seed

NOW='2026-09-29T00:00:00.000000Z'


def insert(db,table,**fields):
    db.execute(f"INSERT INTO {table}({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",tuple(fields.values()))


def records(db,run='run-1',suffix='1'):
    insert(db,'observations',snapshot_id='snapshot-'+suffix,run_id=run,captured_at=NOW,
           source_url='http://localhost/fixture',title='synthetic',tab_id='tab',frame_id='frame',
           page_version='v1',width=800,height=600,visible_excerpt='',redaction_status='FILTERED')
    insert(db,'steps',step_id='step-'+suffix,run_id=run,sequence=1,step_kind='decision',epoch=1,
           input_snapshot_id='snapshot-'+suffix,started_at=NOW)
    insert(db,'evidence',evidence_id='evidence-'+suffix,run_id=run,source_url='http://localhost/fixture',
           captured_at=NOW,object_id='fixture',query_scope='local',artifact_path=f'evidence/{suffix}.txt',
           sha256='c'*64,locator_or_page='body',excerpt='synthetic',sensitivity='public',
           capture_status='COMPLETE',artifact_kind='text')
    insert(db,'run_budgets',budget_record_id='budget-'+suffix,run_id=run)
    event=db.execute("INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json) VALUES ('task-1',?,'state_changed',0,?,'{}')",(run,NOW)).lastrowid
    insert(db,'run_checkpoints',checkpoint_id='checkpoint-'+suffix,task_id='task-1',run_id=run,
           contract_version=1,current_subgoal='subgoal',current_object_id='object',action_sequence=1,
           business_event_id=event,budget_record_ref='budget-'+suffix,epoch=1,saved_at=NOW)


def test_core_records_and_cross_run_reference_rejection(database):
    with connect(database) as db,transaction(db):
        seed(db);seed(db,run_id='run-2');records(db);records(db,run='run-2',suffix='2')
        insert(db,'steps_evidence',run_id='run-1',step_id='step-1',evidence_id='evidence-1')
        insert(db,'run_checkpoints_evidence',run_id='run-1',checkpoint_id='checkpoint-1',evidence_id='evidence-1')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db,transaction(db):
            insert(db,'steps_evidence',run_id='run-1',step_id='step-1',evidence_id='evidence-2')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db,transaction(db):
            insert(db,'steps',step_id='duplicate-sequence',run_id='run-1',sequence=1,step_kind='decision',epoch=1,input_snapshot_id='snapshot-1',started_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db,transaction(db):
            db.execute("INSERT INTO run_checkpoints SELECT 'bad','task-1','run-2',contract_version,current_subgoal,verified_item_ids_json,pending_item_ids_json,current_object_id,current_object_version,current_snapshot_id,flow_version,action_sequence,business_event_id,budget_record_ref,identity_ref,epoch,saved_at FROM run_checkpoints WHERE checkpoint_id='checkpoint-1'")


@pytest.mark.parametrize('sql',[
 "UPDATE evidence SET excerpt='rewritten'",
 "INSERT OR REPLACE INTO evidence SELECT * FROM evidence",
 "UPDATE run_checkpoints SET action_sequence=99",
 "UPDATE run_budgets SET active_ms=-1",
 "INSERT OR REPLACE INTO run_budgets SELECT * FROM run_budgets",
 "DELETE FROM run_budgets",
 "UPDATE runs SET state='invented'",
 "UPDATE tasks SET requested_fields_json='not json'",
])
def test_immutable_evidence_and_domain_constraints(database,sql):
    with connect(database) as db,transaction(db):seed(db);records(db)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db,transaction(db):db.execute(sql)


def test_write_operation_survives_reruns_and_quarantine_has_independent_lifetime(database):
    with connect(database) as db,transaction(db):
        seed(db);seed(db,run_id='run-2',parent='run-1');records(db,run='run-2',suffix='2')
        insert(db,'write_intents',operation_id='op',business_key='stable-key',task_id='task-1',originating_run_id='run-1',target='fixture',expected_change='synthetic',identity_ref='identity',precondition_version='v1',created_at=NOW,updated_at=NOW)
        insert(db,'write_intent_attempts',task_id='task-1',operation_id='op',run_id='run-2',step_id='step-2')
        insert(db,'write_intent_evidence',task_id='task-1',operation_id='op',run_id='run-2',evidence_id='evidence-2')
        insert(db,'resource_leases',resource_key='repo:fixture',resource_type='repository_write',holder_run_id='run-2',worker_id='worker',epoch=1,expires_at=NOW,heartbeat_at=NOW)
        insert(db,'resource_quarantines',resource_key='repo:fixture',operation_id='op',created_at=NOW)
        db.execute('DELETE FROM resource_leases')
        assert db.execute('SELECT COUNT(*) FROM resource_quarantines').fetchone()[0]==1
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db,transaction(db):db.execute("INSERT OR REPLACE INTO write_intents SELECT * FROM write_intents")


def test_budget_and_quota_links_and_no_double_debit(database):
    with connect(database) as db,transaction(db):
        seed(db);seed(db,run_id='run-2');records(db)
        insert(db,'quota_buckets',quota_date='2026-09-29',quota_type='public')
        insert(db,'quota_debits',debit_id='d1',run_id='run-1',quota_date='2026-09-29',quota_type='public',debit_kind='ordinary',debited_at=NOW)
        db.execute("UPDATE run_budgets SET quota_debit_id='d1',active_ms=50")
    for sql in ["UPDATE run_budgets SET active_ms=1","UPDATE run_budgets SET quota_debit_id=NULL","DELETE FROM quota_debits","INSERT OR REPLACE INTO quota_debits SELECT * FROM quota_debits"]:
        with pytest.raises(sqlite3.IntegrityError):
            with connect(database) as db,transaction(db):db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db,transaction(db):insert(db,'run_budgets',budget_record_id='b2',run_id='run-2',quota_debit_id='d1')


def test_per_obstacle_budget_usage_is_persisted_and_cannot_regress(database):
    with connect(database) as db,transaction(db):
        seed(db);records(db)
        db.execute('UPDATE run_budgets SET observations_used=2,screenshots_used=1,model_calls_used=1,recovery_counts_json=?',('{"challenge":2}',))
    for value in ['{}','{"challenge":1}','{"challenge":-1}','{"challenge":"2"}']:
        with pytest.raises(sqlite3.IntegrityError):
            with connect(database) as db,transaction(db):
                db.execute('UPDATE run_budgets SET recovery_counts_json=?',(value,))
    with connect(database) as db:
        row=db.execute('SELECT * FROM run_budgets').fetchone()
        assert row['recovery_counts_json']=='{"challenge":2}'
        assert row['observations_used']==2 and row['screenshots_used']==1
        assert row['last_persisted_at'].endswith('Z')
