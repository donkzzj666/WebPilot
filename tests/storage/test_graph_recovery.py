"""Recovery proves immutable facts without replaying steps or resetting limits."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from webagent.db import connect, migrate, transaction
from webagent.db.repository import canonical_json, utc_text
from webagent.errors import BusinessError
from webagent.gateway.store import GatewayStore
from webagent.graph.recovery import RecoveryStore
from webagent.scheduler.models import ExecutionToken
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry
from storage.test_graph_store import check_error, setup_run


def recovery_case(tmp_path, *, with_session=True, close_session=False, initial_text=None):
    """Exposed real SQLite fixture for runtime wrappers; no browser/network I/O."""
    text=initial_text if initial_text is not None else canonical_json({'object_id':'company','object_version':'2025','values':[]})
    graph,scheduler,old,evidence,view,_=setup_run(tmp_path,scheduled=True,text=text)
    recovery=RecoveryStore(tmp_path,budgets=scheduler.budgets)
    registry=SessionRegistry(graph.path)
    session=None
    if with_session:
        session=registry.reserve('manager-1',SessionOwner('run','run-1','local-fixture'),execution_token=old)
        session=registry.opened(session.session_id,'manager-1',execution_token=old)
    checkpoint=recovery.checkpoint_facts('run-1',old,'snapshot-1')
    graph.record_progress('run-1','observe',expected_state_version=old.state_version,execution_token=old,snapshot_id='snapshot-1')
    saved=graph.load_state('run-1')
    if close_session:
        registry.closing(session.session_id,'manager-1');registry.closed(session.session_id,'manager-1')
    scheduler.abandon(old)
    generation=scheduler.start_worker('worker-2')
    token=scheduler.claim('worker-2',generation)
    assert token.state_version==2
    return recovery,scheduler,token,saved,session,registry,evidence


def capture(case, *, object_id='company', object_version='2025', identity_ref=None, account=None, snapshot_id='snapshot-recovery'):
    recovery,scheduler,token,_,session,registry,evidence=case
    if session is None or registry.get(session.session_id,session.owner).state!='OPEN':
        session=registry.reserve('manager-2',SessionOwner('run','run-1','local-fixture'),execution_token=token)
        session=registry.opened(session.session_id,'manager-2',execution_token=token)
    binding=dict(session_id=session.session_id,manager_id=session.manager_id,session_generation=session.generation,
        tab_id='tab-recovery',frame_id='frame-recovery',page_version='version-recovery',width=100,height=100)
    gateway=GatewayStore(recovery.path,budgets=scheduler.budgets)
    observation=gateway.record_observation(token,binding,dict(snapshot_id=snapshot_id,
        source_url=recovery.graph.load_run(token.run_id)['contract'].start_urls[0],screenshot_sha256=None),attempt_id='capture-'+snapshot_id)
    document=dict(object_id=object_id,object_version=object_version)
    if identity_ref is not None:document.update(identity_ref=identity_ref,normalized_account=account)
    evidence.publish_observation(observation,dict(title='Real current JSON',text=canonical_json(document)),execution_token=token)
    return recovery.observed_facts(token,snapshot_id)


def test_begin_and_complete_are_idempotent_and_budget_survives(tmp_path):
    case=recovery_case(tmp_path)
    recovery,scheduler,token,saved,*_=case
    before=scheduler.budgets.status('run-1')
    plan=recovery.begin(token,saved)
    assert plan['allowed'] and plan['graph_status']=='behind'
    assert recovery.begin(token,saved)['recovery_id']==plan['recovery_id']
    facts=capture(case)
    receipt=recovery.complete(token,'snapshot-recovery',**facts)
    assert recovery.complete(token,'snapshot-recovery',**facts)['recovery_id']==receipt['recovery_id']
    fresh=scheduler.reconcile('run-1',token.state_version)
    assert recovery.require_completed(fresh)['recovery_id']==receipt['recovery_id']
    after=scheduler.budgets.status('run-1')
    assert after['actions_used']==before['actions_used']+1
    assert list(after['recovery_counts'].values())==[1]
    assert after['model_calls_used']==before['model_calls_used']
    assert after['observations_used']==before['observations_used']+1
    with connect(recovery.path) as db:
        assert db.execute("SELECT count(*) FROM graph_recoveries WHERE phase='BEGIN'").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM graph_recoveries WHERE phase='COMPLETE'").fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==1


@pytest.mark.parametrize('field,value,reason',[
    ('graph_version','unknown','graph_version_mismatch'),('state_schema_version','unknown','graph_version_mismatch'),
    ('run_id','another-run','contract_mismatch'),('contract_version',2,'contract_mismatch'),
    ('state_version',999,'graph_ahead'),('business_event_id',999999,'event_missing'),
    ('business_event_id',0,'event_missing'),('business_checkpoint_id','missing','checkpoint_mismatch'),
    ('snapshot_id','missing','snapshot_missing'),('progress_id',9999,'progress_mismatch'),
    ('verified_summary_refs',['missing'],'summary_mismatch'),('evidence_ids',['missing'],'evidence_missing')])
def test_graph_reference_mismatch_blocks_without_modifying_facts(tmp_path,field,value,reason):
    recovery,scheduler,token,saved,*_=recovery_case(tmp_path)
    changed={**saved,field:value}
    before=scheduler.budgets.status('run-1')
    plan=recovery.begin(token,changed)
    assert not plan['allowed'] and plan['reason']==reason
    assert scheduler.budgets.status('run-1')['actions_used']==before['actions_used']
    assert recovery.graph.load_run('run-1')['state']=='RECONCILING'


def test_additional_raw_graph_content_is_rejected(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    saved['model_reply']='unsafe raw content'
    assert recovery.begin(token,saved)['reason']=='graph_state_invalid'


def test_event_version_mismatch_is_not_repaired_from_graph(tmp_path):
    recovery,_,_,saved,*_=recovery_case(tmp_path)
    changed={**saved,'state_version':2}
    assert recovery.inspect('run-1',changed)['reason']=='event_version_mismatch'


@pytest.mark.parametrize('mutation,reason',[('missing','evidence_missing'),('corrupt','evidence_corrupt'),('expired','evidence_missing')])
def test_original_evidence_integrity_is_required_for_recovery(tmp_path,mutation,reason):
    recovery,_,token,saved,_,_,evidence=recovery_case(tmp_path)
    display=evidence.store.metadata(saved['evidence_ids'][0])
    original=evidence.store.metadata(display['original_evidence_id'])
    if mutation=='expired':evidence.store.expire(original['evidence_id'])
    elif mutation=='missing':(tmp_path/original['artifact_path']).unlink()
    else:(tmp_path/original['artifact_path']).write_bytes(b'changed original')
    assert recovery.begin(token,saved)['reason']==reason


@pytest.mark.parametrize('field,value,reason',[('object_id','other-company','object_mismatch'),
    ('object_version','2024','object_version_mismatch'),('identity_ref','unexpected-account','identity_mismatch')])
def test_current_page_object_version_and_identity_changes_block(tmp_path,field,value,reason):
    case=recovery_case(tmp_path)
    recovery,_,token,saved,*_=case
    recovery.begin(token,saved)
    facts=capture(case,**{field:value})
    with pytest.raises(BusinessError) as caught:recovery.complete(token,'snapshot-recovery',**facts)
    assert caught.value.field==reason
    with connect(recovery.path) as db:
        assert db.execute("SELECT phase,reason FROM graph_recoveries ORDER BY recovery_seq DESC LIMIT 1").fetchone()[:]==('BLOCKED',reason)


def test_caller_proof_cannot_replace_actual_original(tmp_path):
    case=recovery_case(tmp_path)
    recovery,_,token,saved,*_=case
    recovery.begin(token,saved)
    facts=capture(case,object_id='other-company')
    facts['object_id']='company'
    with pytest.raises(BusinessError) as caught:recovery.complete(token,'snapshot-recovery',**facts)
    assert caught.value.field=='object_mismatch'


def test_reservation_for_closed_context_is_reset_but_open_is_preserved(tmp_path):
    case=recovery_case(tmp_path,close_session=True)
    recovery,_,token,saved,session,*_=case
    recovery.begin(token,saved)
    with connect(recovery.path) as db:
        reservation=db.execute('SELECT * FROM scheduler_context_reservations').fetchone()
        assert reservation['session_id'] is None and reservation['worker_id']==token.worker_id
        assert db.execute('SELECT state FROM browser_sessions WHERE session_id=?',(session.session_id,)).fetchone()[0]=='CLOSED'
    capture(case)
    another=tmp_path/'other';another.mkdir()
    recovery2,_,token2,saved2,session2,*_=recovery_case(another)
    recovery2.begin(token2,saved2)
    with connect(recovery2.path) as db:
        assert db.execute('SELECT session_id FROM scheduler_context_reservations').fetchone()[0]==session2.session_id


def test_valid_block_receipt_requires_current_worker_and_safe_revoke(tmp_path):
    recovery,scheduler,token,*_=recovery_case(tmp_path)
    receipt=recovery.blocked(token,'object_mismatch')
    assert recovery.blocked(token,'object_mismatch')['recovery_id']==receipt['recovery_id']
    assert not recovery.blocked_receipt(token)
    scheduler.abandon(token)
    assert recovery.blocked_receipt(token)
    generation=scheduler.start_worker('worker-3')
    assert not recovery.blocked_receipt(token)
    # The production claim filter is owned by SchedulerStore.
    with connect(recovery.path) as db:
        assert not db.execute('SELECT 1 FROM scheduler_queue q WHERE '+recovery.claim_blocked_sql()).fetchone()


def test_without_completed_proof_scheduler_transition_is_not_graph_authority(tmp_path):
    recovery,scheduler,token,*_=recovery_case(tmp_path)
    fresh=scheduler.reconcile('run-1',token.state_version)
    with pytest.raises(BusinessError) as caught:recovery.require_completed(fresh)
    assert caught.value.field=='recovery_not_completed'


def test_checkpoint_facts_reads_actual_version_and_keeps_business_refs(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    checkpoint=recovery.graph._checkpoint
    with connect(recovery.path) as db:
        cp=checkpoint(db,'run-1')
    assert cp.current_object_id=='company' and cp.current_object_version=='2025'
    assert cp.budget_record_ref=='budget-1' and cp.pending_item_ids
    fresh=recovery.checkpoint_facts('run-1',token)
    assert fresh.current_object_version=='2025'
    assert fresh.current_snapshot_id=='snapshot-1' and fresh.evidence_ids==saved['evidence_ids']
    assert recovery.checkpoint_facts('run-1',token)==fresh


def test_recovery_and_navigation_history_is_append_only(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    recovery.begin(token,saved)
    with connect(recovery.path) as db:
        for sql in ('UPDATE graph_recoveries SET phase=\'COMPLETE\'','DELETE FROM graph_recoveries'):
            with pytest.raises(sqlite3.IntegrityError):db.execute(sql)


def test_navigation_intent_charges_once_and_never_redispatches(tmp_path):
    recovery,scheduler,token,saved,*_=recovery_case(tmp_path)
    plan=recovery.begin(token,saved)
    before=scheduler.budgets.status('run-1')
    url=plan['restore_url']
    first=recovery.navigation_intent(token,plan['recovery_id'],url,'read-nav',site_id='local-fixture')
    assert first['dispatch_allowed']
    assert not recovery.navigation_intent(token,plan['recovery_id'],url,'read-nav',site_id='local-fixture')['dispatch_allowed']
    recovery.navigation_complete(token,plan['recovery_id'],url,'read-nav')
    assert recovery.navigation_complete(token,plan['recovery_id'],url,'read-nav')['status']=='COMPLETED'
    after=scheduler.budgets.status('run-1')
    assert after['actions_used']==before['actions_used']+1
    assert after['content_pages_used']==before['content_pages_used']+1
    with connect(recovery.path) as db:
        assert db.execute('SELECT count(*) FROM graph_recovery_navigations').fetchone()[0]==2
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM graph_recovery_navigations')


def test_invalid_navigation_cannot_escape_scope(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    plan=recovery.begin(token,saved)
    with pytest.raises(BusinessError):recovery.navigation_intent(token,plan['recovery_id'],'https://evil.example','outside',site_id='local-fixture')


def add_step(recovery, step_id, sequence, status, *, effect='read', epoch=1):
    now=utc_text()
    with connect(recovery.path) as db,transaction(db):
        db.execute('''INSERT INTO steps(step_id,run_id,sequence,step_kind,epoch,input_snapshot_id,
            action_json,actual_result_json,status,error_code,started_at,ended_at)
            VALUES(?,'run-1',?,'atomic_action',?,'snapshot-1',?,?,?, ?,?,?)''',
            (step_id,sequence,epoch,canonical_json({'expected_effect':effect}),
             canonical_json({'source_url':'http://127.0.0.1:8765/next'}),status,
             'ORIGINAL_FAILURE' if status=='FAILED' else None,now,None if status=='INTENT' else now))


def test_read_reconciliation_keeps_failed_and_uncertain_history_without_replay(tmp_path):
    case=recovery_case(tmp_path)
    recovery,scheduler,token,saved,*_=case
    add_step(recovery,'completed',1,'COMPLETED')
    add_step(recovery,'failed',2,'FAILED')
    add_step(recovery,'uncertain',3,'UNKNOWN')
    plan=recovery.begin(token,saved)
    assert plan['completed_read_step_ids']==['completed'] and plan['failed_step_ids']==['failed']
    assert plan['uncertain_read_step_ids']==['uncertain']
    assert plan['restore_url']=='http://127.0.0.1:8765/next'
    facts=capture(case)
    recovery.complete(token,'snapshot-recovery',**facts)
    fresh=scheduler.reconcile('run-1',token.state_version)
    recovery.require_completed(fresh)
    with connect(recovery.path) as db:
        assert recovery.resolved_read_steps(db,'run-1')=={'uncertain'}
        assert not recovery.has_unresolved_steps(db,'run-1')
        assert db.execute("SELECT status,error_code FROM steps WHERE step_id='failed'").fetchone()[:]==('FAILED','ORIGINAL_FAILURE')
        assert db.execute("SELECT status FROM steps WHERE step_id='uncertain'").fetchone()[0]=='UNKNOWN'
        assert db.execute('SELECT count(*) FROM steps').fetchone()[0]==3
    add_step(recovery,'new-intent',4,'INTENT',epoch=fresh.epoch)
    with connect(recovery.path) as db:assert recovery.has_unresolved_steps(db,'run-1')


def test_unknown_write_cannot_be_resolved_as_read_observation(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    add_step(recovery,'unknown-write',1,'UNKNOWN',effect='write')
    plan=recovery.begin(token,saved)
    assert not plan['allowed'] and plan['reason']=='unknown_write'
    with connect(recovery.path) as db:
        assert recovery.has_unresolved_steps(db,'run-1')
        assert db.execute("SELECT status FROM steps WHERE step_id='unknown-write'").fetchone()[0]=='UNKNOWN'


def test_task_write_intent_protection_survives_blocked_receipt(tmp_path):
    recovery,scheduler,token,saved,*_=recovery_case(tmp_path)
    with connect(recovery.path) as db,transaction(db):
        now=utc_text()
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,target,
            expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('write-1','write-key','task-1','run-1','repo','change','account','v1','UNKNOWN',?,?)''',(now,now))
    plan=recovery.begin(token,saved)
    assert not plan['allowed'] and plan['reason']=='unknown_write'
    assert plan['pending_operation_ids']==['write-1']
    recovery.blocked(token,'unknown_write')
    scheduler.abandon(token)
    assert recovery.blocked_receipt(token)
    with connect(recovery.path) as db:
        assert db.execute("SELECT status FROM write_intents WHERE operation_id='write-1'").fetchone()[0]=='UNKNOWN'
        assert db.execute("SELECT count(*) FROM resource_leases WHERE holder_run_id='run-1' AND resource_type<>'active_slot'").fetchone()[0]>0


def test_human_control_only_allows_recording_a_block(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    with connect(recovery.path) as db,transaction(db):
        db.execute("UPDATE resource_leases SET control_owner='human',state_version=state_version+1 WHERE resource_type='browser_context'")
    assert recovery.inspect('run-1',saved)['reason']=='human_control'
    receipt=recovery.blocked(token,'human_control')
    assert receipt['phase']=='BLOCKED'
    check_error(lambda:recovery.require_active(token,receipt['recovery_id']),'RESOURCE_CONFLICT')
    with connect(recovery.path) as db:
        assert db.execute("SELECT control_owner FROM resource_leases WHERE resource_type='browser_context'").fetchone()[0]=='human'


def test_evidence_lost_after_complete_still_blocks_fresh_execution(tmp_path):
    case=recovery_case(tmp_path)
    recovery,scheduler,token,saved,_,_,evidence=case
    recovery.begin(token,saved)
    facts=capture(case)
    recovery.complete(token,'snapshot-recovery',**facts)
    fresh=scheduler.reconcile('run-1',token.state_version)
    display=evidence.store.metadata(facts['proof_evidence_ids'][0])
    original=evidence.store.metadata(display['original_evidence_id'])
    (tmp_path/original['artifact_path']).unlink()
    with pytest.raises(BusinessError) as caught:recovery.require_completed(fresh)
    assert caught.value.field=='evidence_missing'


def test_terminal_checkpoint_does_not_create_a_success_or_change_budget(tmp_path):
    recovery,scheduler,token,*_=recovery_case(tmp_path)
    scheduler.finish(token,'FAILED')
    before=scheduler.budgets.status('run-1')
    cp=recovery.checkpoint_facts('run-1',None)
    assert cp.current_object_version=='2025'
    plan=recovery.inspect('run-1')
    assert plan['terminal'] and plan['allowed']
    assert scheduler.budgets.status('run-1')==before
    with connect(recovery.path) as db:assert db.execute('SELECT count(*) FROM run_results').fetchone()[0]==0


def test_migration_v15_history_survives_v16_upgrade(tmp_path):
    path=tmp_path/'old.sqlite3'
    migrate(path,target=15)
    with connect(path) as db:before=[tuple(r) for r in db.execute('SELECT * FROM schema_migrations')]
    assert migrate(path,target=16)['schema_version']==16
    with connect(path) as db:assert [tuple(r) for r in db.execute('SELECT * FROM schema_migrations WHERE version<=15')]==before
    assert migrate(path,target=16)['applied']==0


def test_incompatible_run_metadata_is_durably_blocked_without_graph_parsing(tmp_path):
    graph,scheduler,old,*_=setup_run(tmp_path,scheduled=True,graph_version='unsupported-graph')
    scheduler.abandon(old)
    token=scheduler.claim('worker-1',old.worker_generation)
    recovery=RecoveryStore(tmp_path,budgets=scheduler.budgets)
    plan=recovery.begin(token)
    assert not plan['allowed'] and plan['reason']=='graph_version_mismatch'
    recovery.blocked(token,'graph_version_mismatch')
    scheduler.abandon(token)
    assert recovery.blocked_receipt(token)
    assert scheduler.claim('worker-1',old.worker_generation) is None


def test_corrupt_contract_hash_can_be_blocked_without_loading_bad_contract(tmp_path):
    recovery,scheduler,token,*_=recovery_case(tmp_path)
    with connect(recovery.path) as db,transaction(db):
        # Controlled corruption of a disposable database, never live project data.
        db.execute('DROP TRIGGER contracts_immutable')
        db.execute("UPDATE contracts SET content_json=json_set(content_json,'$.original_instruction','Changed frozen instruction')")
    assert recovery.inspect('run-1')['reason']=='contract_mismatch'
    recovery.blocked(token,'contract_mismatch')
    scheduler.abandon(token)
    assert recovery.blocked_receipt(token)
    assert scheduler.claim(token.worker_id,token.worker_generation) is None


def test_checkpoint_read_and_commit_do_not_mix_changed_page_refs(tmp_path,monkeypatch):
    case=recovery_case(tmp_path)
    recovery,_,token,saved,*_=case
    recovery.begin(token,saved)
    capture(case)
    boundary=recovery._boundary
    calls=[]
    def changed_after_read(db,run,snapshot_id):
        result=boundary(db,run,snapshot_id)
        calls.append(snapshot_id)
        if len(calls)==1:
            with connect(recovery.path) as other,transaction(other):
                other.execute("UPDATE gateway_page_heads SET valid=0 WHERE run_id='run-1'")
        return result
    monkeypatch.setattr(recovery,'_boundary',changed_after_read)
    with pytest.raises(BusinessError) as caught:
        recovery.checkpoint_facts('run-1',token,'snapshot-recovery')
    assert caught.value.field=='checkpoint_mismatch'
    with connect(recovery.path) as db:
        assert not db.execute("SELECT 1 FROM run_checkpoints WHERE current_snapshot_id='snapshot-recovery'").fetchone()


def test_complete_rechecks_page_after_checkpoint_is_committed(tmp_path,monkeypatch):
    case=recovery_case(tmp_path)
    recovery,_,token,saved,*_=case
    recovery.begin(token,saved)
    facts=capture(case)
    checkpoint=recovery.checkpoint_facts
    def invalidate(*args,**kwargs):
        result=checkpoint(*args,**kwargs)
        with connect(recovery.path) as db,transaction(db):
            db.execute("UPDATE gateway_page_heads SET valid=0 WHERE run_id='run-1'")
        return result
    monkeypatch.setattr(recovery,'checkpoint_facts',invalidate)
    with pytest.raises(BusinessError) as caught:
        recovery.complete(token,'snapshot-recovery',**facts)
    assert caught.value.field=='session_unavailable'
    with connect(recovery.path) as db:
        assert not db.execute("SELECT 1 FROM graph_recoveries WHERE phase='COMPLETE'").fetchone()


@pytest.mark.parametrize('mutation',['page_invalid','missing_original','human_control'])
def test_completed_receipt_cannot_bypass_later_invalid_evidence_or_control(tmp_path,mutation):
    case=recovery_case(tmp_path)
    recovery,scheduler,token,saved,_,_,evidence=case
    recovery.begin(token,saved)
    facts=capture(case)
    recovery.complete(token,'snapshot-recovery',**facts)
    fresh=scheduler.reconcile('run-1',token.state_version)
    if mutation=='missing_original':
        display=evidence.store.metadata(facts['proof_evidence_ids'][0])
        original=evidence.store.metadata(display['original_evidence_id'])
        (tmp_path/original['artifact_path']).unlink()
    else:
        with connect(recovery.path) as db,transaction(db):
            if mutation=='page_invalid':db.execute("UPDATE gateway_page_heads SET valid=0 WHERE run_id='run-1'")
            else:db.execute("UPDATE resource_leases SET control_owner='human' WHERE holder_run_id='run-1' AND resource_type='browser_context'")
    with pytest.raises(BusinessError):recovery.require_completed(fresh)


def test_first_recovery_version_must_match_frozen_contract_without_old_version(tmp_path):
    case=recovery_case(tmp_path,initial_text='No original report has been captured')
    recovery,_,token,saved,*_=case
    recovery.begin(token,saved)
    facts=capture(case,object_version='2026')
    with pytest.raises(BusinessError) as caught:recovery.complete(token,'snapshot-recovery',**facts)
    assert caught.value.field=='object_version_mismatch'
    with connect(recovery.path) as db:
        assert not db.execute("SELECT 1 FROM graph_recoveries WHERE phase='COMPLETE'").fetchone()


def test_fourth_crash_recovery_keeps_original_budget_and_is_blocked(tmp_path):
    case=recovery_case(tmp_path)
    recovery,scheduler,token,saved,session,registry,evidence=case
    for number in range(1,4):
        plan=recovery.begin(token,saved)
        assert plan['allowed']
        assert recovery.begin(token,saved)['recovery_id']==plan['recovery_id']
        current=(recovery,scheduler,token,saved,session,registry,evidence)
        snapshot_id='snapshot-recovery-'+str(number)
        facts=capture(current,snapshot_id=snapshot_id)
        recovery.complete(token,snapshot_id,**facts)
        fresh=scheduler.reconcile('run-1',token.state_version)
        recovery.require_completed(fresh)
        scheduler.abandon(fresh)
        token=scheduler.claim(token.worker_id,token.worker_generation)
        assert token is not None
    plan=recovery.begin(token,saved)
    assert not plan['allowed'] and plan['reason']=='budget_exhausted'
    status=scheduler.budgets.status('run-1')
    assert status['reason']=='recovery_limit' and status['actions_used']==3
    assert list(status['recovery_counts'].values())==[3]
    with connect(recovery.path) as db:
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==1
        assert db.execute("SELECT phase,reason FROM graph_recoveries ORDER BY recovery_seq DESC LIMIT 1").fetchone()[:]==('BLOCKED','budget_exhausted')


def test_legacy_schema_pending_read_stays_unresolved_without_recovery_tables(tmp_path):
    path=tmp_path/'pre-m17.sqlite3'
    with connect(path) as db:
        db.execute('CREATE TABLE steps(run_id TEXT,step_id TEXT,status TEXT)')
        db.execute("INSERT INTO steps VALUES('legacy','read-intent','INTENT')")
        # The helper never initializes or migrates an older business database.
        recovery=object.__new__(RecoveryStore)
        assert recovery.has_unresolved_steps(db,'legacy')
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='graph_recoveries'").fetchone()


def test_saved_wait_id_must_reference_its_actual_registered_business_wait(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    changed={**saved,'wait_id':'invented-wait-id','route':'wait'}
    plan=recovery.begin(token,changed)
    assert not plan['allowed'] and plan['reason']=='progress_mismatch'


def test_changed_saved_input_cannot_clear_same_epoch_durable_block(tmp_path):
    recovery,_,token,saved,*_=recovery_case(tmp_path)
    recovery.begin(token,saved)
    recovery.blocked(token,'object_mismatch')
    changed=recovery.begin(token,None)
    assert not changed['allowed'] and changed['reason']=='object_mismatch'
    with connect(recovery.path) as db:
        assert db.execute("SELECT count(*) FROM graph_recoveries WHERE phase='BEGIN'").fetchone()[0]==1
        assert db.execute('SELECT phase FROM graph_recoveries ORDER BY recovery_seq DESC LIMIT 1').fetchone()[0]=='BLOCKED'


@pytest.mark.parametrize('version,allowed',[('v2',False),('v1',True),(None,False)])
def test_dashboard_known_version_survives_incomplete_capture_and_crash(tmp_path,version,allowed):
    text=canonical_json({'dashboard_id':'dashboard','object_version':'v1'})
    graph,scheduler,old,evidence,_,_=setup_run(tmp_path,scheduled=True,scenario='grafana',text=text)
    recovery=RecoveryStore(tmp_path,budgets=scheduler.budgets)
    registry=SessionRegistry(graph.path)
    session=registry.reserve('manager-1',SessionOwner('run','run-1','local-fixture'),execution_token=old)
    session=registry.opened(session.session_id,'manager-1',execution_token=old)
    assert recovery.checkpoint_facts('run-1',old,'snapshot-1').current_object_version=='v1'
    incomplete=canonical_json({'dashboard_id':'dashboard'})
    observation=dict(snapshot_id='incomplete-dashboard',run_id='run-1',captured_at=utc_text(),
        source_url=graph.load_run('run-1')['contract'].start_urls[0],title='Current page',
        tab_id='tab',frame_id='frame',page_version='page',width=100,height=100,
        visible_excerpt=incomplete,evidence_ids=[],redaction_status='FILTERED')
    evidence.publish_observation(observation,dict(title='Current page',text=incomplete),execution_token=old)
    legacy=graph.checkpoint_observation('run-1','incomplete-dashboard',expected_state_version=old.state_version,execution_token=old)
    assert legacy.current_object_version is None
    current=recovery.checkpoint_facts('run-1',old,'incomplete-dashboard')
    assert current.current_object_version=='v1'
    graph.record_progress('run-1','observe',expected_state_version=old.state_version,execution_token=old,snapshot_id='incomplete-dashboard')
    saved=graph.load_state('run-1')
    scheduler.abandon(old)
    generation=scheduler.start_worker('worker-2')
    token=scheduler.claim('worker-2',generation)
    case=(recovery,scheduler,token,saved,session,registry,evidence)
    assert recovery.begin(token,saved)['allowed']
    facts=capture(case,object_id='dashboard',object_version=version)
    if allowed:
        recovery.complete(token,'snapshot-recovery',**facts)
        fresh=scheduler.reconcile('run-1',token.state_version)
        assert recovery.require_completed(fresh)['phase']=='COMPLETE'
    else:
        with pytest.raises(BusinessError) as caught:recovery.complete(token,'snapshot-recovery',**facts)
        assert caught.value.field==('proof_missing' if version is None else 'object_version_mismatch')
        with connect(recovery.path) as db:
            assert not db.execute("SELECT 1 FROM graph_recoveries WHERE phase='COMPLETE'").fetchone()


def test_known_version_never_carries_to_another_object(tmp_path):
    recovery,_,token,*_=recovery_case(tmp_path)
    with connect(recovery.path) as db:
        run=recovery.graph._run(db,'run-1')
        assert recovery._known_version(db,run,'company')=='2025'
        assert recovery._known_version(db,run,'another-company') is None


def test_recovery_checkpoint_cannot_replace_known_constraint_before_complete(tmp_path):
    case=recovery_case(tmp_path)
    recovery,_,token,saved,*_=case
    recovery.begin(token,saved)
    capture(case,object_version='2026')
    with pytest.raises(BusinessError) as caught:recovery.checkpoint_facts('run-1',token,'snapshot-recovery')
    assert caught.value.field=='object_version_mismatch'
    with connect(recovery.path) as db:
        assert recovery._known_version(db,recovery.graph._run(db,'run-1'),'company')=='2025'
        assert not db.execute("SELECT 1 FROM run_checkpoints WHERE current_snapshot_id='snapshot-recovery'").fetchone()
