"""Control acceptance and immutable completion over isolated real SQLite."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import sqlite3

import pytest
from pydantic import SecretStr

from webagent.controls.models import ControlRequest
from webagent.controls.store import ControlStore
from webagent.db import connect, migrate, transaction
from webagent.db.repository import utc_text
from webagent.errors import BusinessError
from webagent.graph.store import GraphStore
from webagent.graph.recovery import RecoveryStore
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.settings.models import ModelConnection, ModelSettingsRequest
from webagent.settings.secrets import CredentialError
from webagent.settings.service import update_model
from webagent.state import transition_in_transaction
from webagent.tasks import service as tasks
from webagent.tasks.models import CreateTaskRequest, RevisionRequest


FINANCE=dict(instruction='Read original finance',scenario='finance',source_ids=['local-fixture'],
    parameters=dict(entity_id='company',report_version='2025',period_type='annual',metrics=['revenue'],currency='USD'))


class Secrets:
    def __init__(self):
        self.values={}
    def put(self, reference, value):
        self.values[reference]=value
    def get(self, reference):
        if reference not in self.values:
            raise CredentialError('missing')
        return self.values[reference]
    def delete(self, reference):
        self.values.pop(reference,None)


def prepared(tmp_path, *, resource_factory=None):
    path=tmp_path/'business.sqlite3'
    migrate(path)
    reply=tasks.create(path,CreateTaskRequest.model_validate(deepcopy(FINANCE)),'prepare')
    task=reply.body['task']
    secret=Secrets()
    update_model(path,secret,ModelSettingsRequest(expected_version=0,model=ModelConnection(),
        accept_data_sharing=True,api_key=SecretStr('SYNTHETIC_CONTROL_KEY')))
    scheduler=SchedulerStore(path,lease_seconds=300)
    controls=ControlStore(path,secret_store=secret,scheduler=scheduler,resource_factory=resource_factory)
    return path,task,secret,scheduler,controls


def start(case):
    path,task,_,scheduler,controls=case
    accepted=controls.request(task['task_id'],'start',ControlRequest(expected_state_version=task['state_version'],contract_version=1,settings_version=1),'start')
    operation=accepted['operation']
    assert operation['status']=='PENDING'
    assert controls.apply_idle(operation['run_id'])['status']=='APPLIED'
    generation=scheduler.start_worker('worker-1')
    token=scheduler.claim('worker-1',generation)
    assert token is not None and token.run_id==operation['run_id']
    return token,operation


def request(case,run_id,action, *, key=None,settings_version=1):
    with connect(case[0]) as db:
        row=db.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
    return case[4].request(run_id,action,ControlRequest(expected_state_version=row['state_version'],
        contract_version=row['contract_version'],settings_version=settings_version),key or action)['operation']


def intent(path,run_id, *, status='INTENT'):
    now=utc_text()
    with connect(path) as db,transaction(db):
        task=db.execute('SELECT task_id FROM runs WHERE run_id=?',(run_id,)).fetchone()[0]
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,target,
            expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('write-1','business-1',?,?, 'owned-target','declared change','identity-1','v1',?,?,?)''',
            (task,run_id,status,now,now))


def test_start_acceptance_is_atomic_and_does_not_start_or_deduct_quota(tmp_path):
    path,task,secret,scheduler,controls=prepared(tmp_path)
    body=ControlRequest(expected_state_version=task['state_version'],contract_version=1,settings_version=1)
    accepted=controls.request(task['task_id'],'start',body,'start')
    op=accepted['operation']
    generation=scheduler.start_worker('worker')
    assert scheduler.claim('worker',generation) is None
    with connect(path) as db:
        run=db.execute('SELECT * FROM runs').fetchone()
        assert run['state']=='QUEUED' and run['started_at'] is None and run['thread_id']==run['run_id']
        assert db.execute('SELECT count(*) FROM run_config_snapshots').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM run_budgets').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM scheduler_queue').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==0
        assert db.execute('SELECT current_run_id FROM tasks').fetchone()[0]==op['run_id']
    assert controls.apply_idle(op['run_id'])['status']=='APPLIED'
    assert scheduler.claim('worker',generation).run_id==op['run_id']
    assert controls.request(task['task_id'],'start',body,'start')==accepted
    assert controls.read(op['operation_id'])['status']=='APPLIED'


def test_failed_enqueue_rolls_back_run_snapshots_budget_and_task_head(tmp_path):
    def invalid(contract,run_id):
        return [Resource.site_identity(contract.sources[0].site_id)]
    case=prepared(tmp_path,resource_factory=invalid)
    path,task,_,_,controls=case
    with pytest.raises(BusinessError):
        controls.request(task['task_id'],'start',ControlRequest(expected_state_version=task['state_version'],contract_version=1,settings_version=1),'start')
    with connect(path) as db:
        for table in ('runs','run_config_snapshots','run_budgets','scheduler_queue','run_controls'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert db.execute('SELECT state_version,current_run_id FROM tasks').fetchone()[:]==(task['state_version'],None)


def test_missing_credential_cannot_create_half_started_run(tmp_path):
    path,task,secret,_,controls=prepared(tmp_path)
    secret.values.clear()
    with pytest.raises(BusinessError) as caught:
        controls.request(task['task_id'],'start',ControlRequest(expected_state_version=task['state_version'],contract_version=1,settings_version=1),'start')
    assert caught.value.code=='CONFIG_NOT_READY'
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==0


def test_replay_conflict_is_checked_before_live_state_or_credentials(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    op=request(case,token.run_id,'pause')
    case[4].apply_at_boundary(token)
    case[2].values.clear()
    original=ControlRequest(expected_state_version=1,contract_version=1,settings_version=1)
    assert case[4].request(token.run_id,'pause',original,'pause')['operation']['status']=='PENDING'
    with pytest.raises(BusinessError) as caught:
        case[4].request(token.run_id,'pause',ControlRequest(expected_state_version=2,contract_version=1,settings_version=1),'pause')
    assert caught.value.code=='IDEMPOTENCY_CONFLICT'
    assert case[4].read(op['operation_id'])['status']=='APPLIED'


def test_active_pause_waits_for_owner_and_commits_wait_as_final_event(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    controls,scheduler=case[4],case[3]
    op=request(case,token.run_id,'pause')
    assert controls.apply_idle(token.run_id) is None
    with pytest.raises(BusinessError):
        request(case,token.run_id,'cancel')
    done=controls.apply_at_boundary(token)
    assert done['status']=='APPLIED' and done['state']=='PAUSED' and done['state_version']==2
    assert controls.completion_receipt(token)
    with connect(case[0]) as db:
        leases=db.execute('SELECT * FROM resource_leases').fetchall()
        assert all(l['resource_type']!='active_slot' and l['logical_hold']==1 for l in leases)
        assert {l['resource_type'] for l in leases}=={'site_identity','browser_context'}
        latest=db.execute('SELECT * FROM task_events ORDER BY event_id DESC LIMIT 1').fetchone()
        progress=db.execute('SELECT * FROM graph_progress ORDER BY progress_id DESC LIMIT 1').fetchone()
        assert latest['event_type']=='wait_registered' and progress['business_event_id']==latest['event_id']
        assert progress['wait_id']==done['wait_id'] and progress['phase']=='wait'
    assert GraphStore(case[0]).load_state(token.run_id)['route']=='wait'
    assert controls.apply_at_boundary(token) is None
    scheduler.start_worker('new-generation')
    assert not controls.completion_receipt(token)


@pytest.mark.parametrize('action',['pause','cancel'])
def test_request_survives_worker_revocation_before_safe_boundary(tmp_path,action):
    case=prepared(tmp_path);token,_=start(case)
    op=request(case,token.run_id,action)
    case[3].abandon(token)
    done=case[4].apply_idle(token.run_id)
    assert done['operation_id']==op['operation_id'] and done['status']=='APPLIED'
    assert done['state']==('PAUSED' if action=='pause' else 'CANCELLED')
    assert not case[4].completion_receipt(token)


def test_resume_enters_reconciliation_and_preserves_budget_and_quota(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    request(case,token.run_id,'pause');case[4].apply_at_boundary(token)
    before=case[3].budgets.status(token.run_id)
    request(case,token.run_id,'resume')
    done=case[4].apply_idle(token.run_id)
    assert done['state']=='RECONCILING' and done['state_version']==3
    fresh=case[3].claim('worker-1',token.worker_generation)
    assert fresh is not None and fresh.state_version==3
    after=case[3].budgets.status(token.run_id)
    for key in ('actions_used','model_calls_used','observations_used','recovery_counts'):
        assert after[key]==before[key]
    with connect(case[0]) as db:
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==1


def test_idle_settlement_of_observed_pause_cannot_apply_a_later_resume(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    pause=request(case,token.run_id,'pause')
    case[3].abandon(token)
    assert case[4].apply_idle(token.run_id,expected_operation_id=pause['operation_id'])['state']=='PAUSED'
    resume=request(case,token.run_id,'resume')
    assert case[4].apply_idle(token.run_id,expected_operation_id=pause['operation_id']) is None
    assert case[4].read(resume['operation_id'])['status']=='PENDING'
    with connect(case[0]) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?',(token.run_id,)).fetchone()[0]=='PAUSED'
    assert case[4].apply_idle(token.run_id,expected_operation_id=resume['operation_id'])['state']=='RECONCILING'


def test_queued_pause_is_rejected_and_cancel_does_not_deduct_quota(tmp_path):
    case=prepared(tmp_path);path,task,_,_,controls=case
    op=controls.request(task['task_id'],'start',ControlRequest(expected_state_version=task['state_version'],contract_version=1,settings_version=1),'start')['operation']
    controls.apply_idle(op['run_id'])
    with pytest.raises(BusinessError):
        request(case,op['run_id'],'pause')
    request(case,op['run_id'],'cancel')
    done=controls.apply_idle(op['run_id'])
    assert done['state']=='CANCELLED'
    with connect(path) as db:
        assert db.execute('SELECT started_at FROM runs').fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==0


@pytest.mark.parametrize('target',['WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED','RECONCILING'])
def test_cancel_accepts_every_durable_wait_and_retains_human_ownership(tmp_path,target):
    case=prepared(tmp_path);token,_=start(case)
    if target=='RECONCILING':
        case[3].abandon(token)
    else:
        case[3].defer(token,target,handoff_deadline=datetime.now(timezone.utc)+timedelta(minutes=5) if target=='WAITING_HANDOFF' else None,
            control_owner='human' if target=='WAITING_HANDOFF' else 'worker')
    request(case,token.run_id,'cancel')
    done=case[4].apply_idle(token.run_id)
    assert done['state']=='CANCELLED' and done['status']=='APPLIED'
    with connect(case[0]) as db:
        assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0]=='FINISHED'
        if target=='WAITING_HANDOFF':
            assert db.execute("SELECT count(*) FROM resource_leases WHERE control_owner='human'").fetchone()[0]>0


def test_resume_cannot_clear_human_control_or_unknown_write(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    request(case,token.run_id,'pause');case[4].apply_at_boundary(token)
    with connect(case[0]) as db,transaction(db):
        db.execute("UPDATE resource_leases SET control_owner='human',state_version=state_version+1 WHERE holder_run_id=?",(token.run_id,))
    with pytest.raises(BusinessError) as caught:
        request(case,token.run_id,'resume')
    assert caught.value.field=='human_control'
    with connect(case[0]) as db,transaction(db):
        db.execute("UPDATE resource_leases SET control_owner='none',state_version=state_version+1 WHERE holder_run_id=?",(token.run_id,))
    intent(case[0],token.run_id,status='UNKNOWN')
    with pytest.raises(BusinessError) as caught:
        request(case,token.run_id,'resume')
    assert caught.value.field=='unknown_write'


@pytest.mark.parametrize('status',['NOT_APPLIED','UNKNOWN'])
def test_retry_has_new_run_thread_and_inherits_real_old_side_effects(tmp_path,status):
    case=prepared(tmp_path);token,_=start(case)
    intent(case[0],token.run_id,status=status)
    request(case,token.run_id,'cancel');case[4].apply_at_boundary(token)
    with connect(case[0]) as db:
        version=db.execute('SELECT state_version FROM runs WHERE run_id=?',(token.run_id,)).fetchone()[0]
        old=dict(db.execute('SELECT * FROM runs WHERE run_id=?',(token.run_id,)).fetchone())
    op=case[4].request(case[1]['task_id'],'retry',ControlRequest(expected_state_version=version,contract_version=1,settings_version=1),'retry')['operation']
    assert op['run_id']!=token.run_id and op['parent_run_id']==token.run_id
    case[4].apply_idle(op['run_id'])
    claimed=case[3].claim('worker-1',token.worker_generation)
    assert (claimed is None)==(status=='UNKNOWN')
    with connect(case[0]) as db:
        assert dict(db.execute('SELECT * FROM runs WHERE run_id=?',(token.run_id,)).fetchone())==old
        assert db.execute('SELECT originating_run_id,recorded_status FROM run_retry_operations').fetchone()[:]==(token.run_id,status)
        assert db.execute('SELECT thread_id FROM runs WHERE run_id=?',(op['run_id'],)).fetchone()[0]==op['run_id']
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==(1 if status=='UNKNOWN' else 2)


def test_retry_after_contract_edit_retains_parent_and_blocks_old_unknown_with_new_resources(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    intent(case[0],token.run_id,status='UNKNOWN')
    request(case,token.run_id,'cancel');case[4].apply_at_boundary(token)
    changed={**deepcopy(FINANCE),'instruction':'Read with a different account','identity_ref':'another-user','contract_version':1}
    tasks.change(case[0],case[1]['task_id'],RevisionRequest.model_validate(changed),'revision',clarification=False)
    with connect(case[0]) as db:
        parent_version=db.execute('SELECT state_version FROM runs WHERE run_id=?',(token.run_id,)).fetchone()[0]
        assert db.execute('SELECT current_run_id FROM tasks').fetchone()[0] is None
    op=case[4].request(case[1]['task_id'],'retry',ControlRequest(expected_state_version=parent_version,contract_version=2,settings_version=1),'retry')['operation']
    assert op['parent_run_id']==token.run_id
    case[4].apply_idle(op['run_id'])
    assert case[3].claim('worker-1',token.worker_generation) is None


def test_terminal_resume_conflicts_and_later_accepted_pause_becomes_rejected(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    op=request(case,token.run_id,'pause')
    case[3].finish(token,'CANCELLED')
    done=case[4].apply_idle(token.run_id)
    assert done['operation_id']==op['operation_id'] and done['status']=='REJECTED' and done['reason']=='terminal_run'
    with pytest.raises(BusinessError):
        request(case,token.run_id,'resume')


def test_completed_history_and_side_effect_links_cannot_be_rewritten(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    op=request(case,token.run_id,'cancel');case[4].apply_at_boundary(token)
    with connect(case[0]) as db:
        for sql in ("UPDATE run_controls SET status='PENDING' WHERE operation_id=?",'DELETE FROM run_controls WHERE operation_id=?'):
            with pytest.raises(sqlite3.IntegrityError),transaction(db):
                db.execute(sql,(op['operation_id'],))
    rows=case[4].list_operations(token.run_id)
    assert rows[-1]['status']=='APPLIED' and len(rows)==2
    assert case[4].list_completed(after=rows[-1]['operation_seq'])==[]


def test_resume_authorization_is_one_specific_block_and_one_specific_claim(tmp_path):
    case=prepared(tmp_path);token,_=start(case)
    case[3].abandon(token)
    recovered=case[3].claim('worker-1',token.worker_generation)
    recovery=RecoveryStore(case[0],budgets=case[3].budgets)
    block=recovery.blocked(recovered,'proof_missing')
    case[3].abandon(recovered)
    request(case,token.run_id,'resume')
    case[4].apply_idle(token.run_id)
    with connect(case[0]) as db:
        assert ControlStore.resume_authorized_in_transaction(db,token.run_id,block['recovery_seq'])
    permitted=case[3].claim('worker-1',token.worker_generation)
    assert permitted is not None
    with connect(case[0]) as db:
        assert ControlStore.resume_authorized_in_transaction(db,token.run_id,block['recovery_seq'])
    second=recovery.blocked(permitted,'proof_missing')
    case[3].abandon(permitted)
    with connect(case[0]) as db:
        assert not ControlStore.resume_authorized_in_transaction(db,token.run_id,block['recovery_seq'])
        assert not ControlStore.resume_authorized_in_transaction(db,token.run_id,second['recovery_seq'])
    assert case[3].claim('worker-1',token.worker_generation) is None


def populated_v16(tmp_path):
    from webagent.db.repository import add_contract, create_task, create_run
    from unit.test_verification_rules import setup
    path=tmp_path/'business.sqlite3'
    migrate(path,target=16)
    contract=setup()[0]
    with connect(path) as db,transaction(db):
        create_task(db,task_id='task-1',instruction='Retain historical parent events',requested_fields=['contract'])
        add_contract(db,contract.model_dump(mode='json'))
        create_run(db,run_id='run-1',task_id='task-1',contract_version=1,
            graph_version=GRAPH_VERSION,graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256='a'*64,runtime_config_sha256='b'*64)
        db.execute("INSERT INTO run_budgets(budget_record_id,run_id) VALUES('budget-1','run-1')")
    scheduler=SchedulerStore(path,lease_seconds=300)
    scheduler.enqueue('run-1',[Resource.site_identity('local-fixture'),Resource.browser_context('run-1')],expected_state_version=0)
    generation=scheduler.start_worker('worker')
    token=scheduler.claim('worker',generation)
    recovery=RecoveryStore(path,budgets=scheduler.budgets)
    recovery.checkpoint_facts('run-1',token)
    GraphStore(path).record_progress('run-1','reconcile',expected_state_version=token.state_version,execution_token=token)
    scheduler.abandon(token)
    recovered=scheduler.claim('worker',generation)
    recovery.blocked(recovered,'proof_missing')
    return path,recovered,scheduler


def test_sql17_event_rebuild_preserves_populated_v16_refs_sequence_and_guards(tmp_path):
    path,token,scheduler=populated_v16(tmp_path)
    tables=('task_events','run_checkpoints','graph_progress','graph_recoveries')
    with connect(path) as db:
        before={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in tables}
        seq=db.execute("SELECT seq FROM sqlite_sequence WHERE name='task_events'").fetchone()[0]
    assert migrate(path, target=17)['schema_version']==17
    with connect(path) as db:
        assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
        for table in tables:
            assert [tuple(r) for r in db.execute('SELECT * FROM '+table)]==before[table]
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='task_events'").fetchone()[0]==seq
        for sql in ('DELETE FROM task_events','UPDATE task_events SET event_type=event_type'):
            with pytest.raises(sqlite3.IntegrityError),transaction(db):
                db.execute(sql)
    op=ControlStore(path,scheduler=scheduler).request('run-1','cancel',ControlRequest(expected_state_version=token.state_version,
        contract_version=1,settings_version=0),'cancel')['operation']
    assert op['requested_event_id']>seq
    assert ControlStore(path,scheduler=scheduler).apply_at_boundary(token)['state']=='CANCELLED'


def test_failed_sql17_rebuild_rolls_back_original_events_and_schema(tmp_path,monkeypatch):
    from webagent.db import migrations
    path,_,_=populated_v16(tmp_path)
    with connect(path) as db:
        before=[tuple(r) for r in db.execute('SELECT * FROM task_events')]
        schema=migrations.schema_digest(db)
    original=migrations.statements
    def broken(script):
        for statement in original(script):
            if 'CREATE TABLE run_controls (' in statement:
                raise migrations.MigrationError('Injected failure after event rebuild')
            yield statement
    monkeypatch.setattr(migrations,'statements',broken)
    with pytest.raises(migrations.MigrationError):
        migrate(path)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==16
        assert migrations.schema_digest(db)==schema
        assert [tuple(r) for r in db.execute('SELECT * FROM task_events')]==before
        assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None


@pytest.mark.parametrize('damage',['graph_version_mismatch','summary_mismatch'])
def test_cancel_of_blocked_damaged_graph_or_summary_commits_business_end(tmp_path,damage,monkeypatch):
    case=prepared(tmp_path)
    if damage=='graph_version_mismatch':
        # Model a Run created by an older binary; its binding is immutable.
        with monkeypatch.context() as patch:
            patch.setattr('webagent.controls.store.GRAPH_VERSION','unsupported')
            token,_=start(case)
    else:
        token,_=start(case)
    with connect(case[0]) as db,transaction(db):
        if damage=='summary_mismatch':
            transition_in_transaction(db,run_id=token.run_id,expected_state_version=token.state_version,target='VERIFYING')
            case[3]._change(db,case[3]._row(db,token.run_id),utc_text(),'heartbeat',run_state_version=2)
            run=db.execute('SELECT contract_sha256 FROM runs WHERE run_id=?',(token.run_id,)).fetchone()
            db.execute('INSERT INTO run_verifications VALUES(?,?,?,?,?,?,?,?,?)',('damaged-summary',token.run_id,2,
                run[0],'a'*64,'b'*64,'{"checks":[]}','c'*64,utc_text()))
            token=replace(token,state_version=2)
    case[3].abandon(token)
    reconciling=case[3].claim('worker-1',token.worker_generation)
    RecoveryStore(case[0],budgets=case[3].budgets).blocked(reconciling,damage)
    case[3].abandon(reconciling)
    op=request(case,token.run_id,'cancel')
    completed=case[4].apply_idle(token.run_id)
    assert completed['operation_id']==op['operation_id'] and completed['status']=='APPLIED'
    assert completed['state']=='CANCELLED'
    with connect(case[0]) as db:
        assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0]=='FINISHED'
        assert db.execute("SELECT count(*) FROM graph_progress WHERE phase='stopped'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='operation_completed' AND json_extract(payload_json,'$.action')='cancel'").fetchone()[0]==1
