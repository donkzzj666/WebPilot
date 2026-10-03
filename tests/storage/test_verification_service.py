"""SQLite/real-artifact invariants of the sole runtime result writer."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from webagent.db import connect, transaction, migrate
from webagent.db.repository import add_contract, canonical_json, create_run, create_task
from webagent.errors import BusinessError
from webagent.evidence.store import EvidenceStore
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.state import transition
from webagent.verification.service import VerificationService
from webagent.models.schema import ProposeResult
from unit.test_verification_rules import setup


def prepared(tmp_path, *, scheduled=False, raw_transform=None, scenario="finance"):
    path = tmp_path / 'business.sqlite3'
    migrate(path)
    contract, proposal, documents, bindings = setup(scenario)
    with connect(path) as db, transaction(db):
        create_task(db,task_id='task-1',instruction='Verify captured facts',requested_fields=['contract'])
        add_contract(db,contract.model_dump(mode='json'))
        create_run(db,run_id='run-1',task_id='task-1',contract_version=1,graph_version='v1',
                   graph_state_schema_version='v1',model_config_sha256='a'*64,runtime_config_sha256='b'*64)
        db.execute("INSERT INTO run_budgets(budget_record_id,run_id) VALUES('budget-1','run-1')")
    scheduler = SchedulerStore(path,lease_seconds=300)
    token = None
    if scheduled:
        scheduler.enqueue('run-1',[Resource.browser_context('run-1'),Resource.site_identity('local-fixture')],expected_state_version=0)
        generation = scheduler.start_worker('worker-1')
        token = scheduler.claim('worker-1',generation)
    else:
        transition(path,run_id='run-1',expected_state_version=0,target='RUNNING')
    original = documents[0]
    raw = canonical_json(original.content).encode()
    if raw_transform:
        raw = raw_transform(raw)
    item = EvidenceStore(tmp_path).publish('run-1',raw,evidence_id='e1',source_url=original.source_url,
        captured_at=original.captured_at,object_id=original.object_id,locator_or_page='json',query_scope='contract',
        execution_token=token,retain=True,artifact_kind=original.artifact_kind,
        commit_sha=original.commit_sha,test_run_id=original.test_run_id)
    service = VerificationService(tmp_path,scheduler=scheduler)
    token = service.begin('run-1',1,token)
    return service,proposal,bindings,token,item


def verify(case):
    service,proposal,bindings,token,_ = case
    return asyncio.run(service.verify('run-1',proposal,bindings,expected_state_version=2,execution_token=token))


def finalize(case, record):
    return case[0].finalize(record['verification_id'],expected_state_version=2,execution_token=case[3])


def assert_error(fn,code):
    with pytest.raises(BusinessError) as caught:
        fn()
    assert caught.value.code == code


@pytest.mark.parametrize('scheduled',[False,True])
def test_success_atomic_result_events_leases_and_restart(tmp_path,scheduled):
    case = prepared(tmp_path,scheduled=scheduled)
    record = verify(case)
    result = finalize(case,record)
    assert result.outcome == 'SUCCEEDED'
    assert finalize(case,record) == result
    assert VerificationService(tmp_path).read('run-1')['result'] == result.model_dump(mode='json')
    with connect(case[0].database) as db:
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='result_ready'").fetchone()[0] == 1
        assert db.execute('SELECT state,state_version FROM runs').fetchone()[:] == ('SUCCEEDED',3)
        assert db.execute('SELECT count(*) FROM resource_leases').fetchone()[0] == 0
        if scheduled:
            assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == 'FINISHED'


@pytest.mark.parametrize('stage',['before_transition','before_result_event'])
def test_result_and_terminal_state_rollback_together(tmp_path,stage):
    case = prepared(tmp_path,scheduled=True); record=verify(case)
    def fault(point):
        if point == stage:
            raise RuntimeError('interrupt local commit')
    case[0].fault_hook=fault
    with pytest.raises(RuntimeError): finalize(case,record)
    with connect(case[0].database) as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='result_ready'").fetchone()[0] == 0
        assert db.execute('SELECT state FROM runs').fetchone()[0] == 'VERIFYING'
        assert db.execute('SELECT status FROM scheduler_queue').fetchone()[0] == 'ACTIVE'
    case[0].fault_hook=None
    assert finalize(case,record).outcome == 'SUCCEEDED'


@pytest.mark.parametrize('target',['SUCCEEDED','PARTIAL'])
def test_direct_terminal_service_scheduler_and_sql_are_fenced(tmp_path,target):
    case=prepared(tmp_path,scheduled=True)
    assert_error(lambda: transition(case[0].database,run_id='run-1',expected_state_version=2,target=target),'INVALID_PARAMETER')
    assert_error(lambda: case[0].scheduler.finish(case[3],target),'INVALID_PARAMETER')
    with connect(case[0].database) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE runs SET state=?,state_version=3,ended_at=strftime('%Y-%m-%dT%H:%M:%f000Z','now')",(target,))


@pytest.mark.parametrize('mutation',['missing','changed','expired'])
def test_changed_evidence_cannot_commit_previously_passing_checks(tmp_path,mutation):
    case=prepared(tmp_path); record=verify(case)
    if mutation=='expired': case[0].evidence.expire('e1')
    else:
        path=tmp_path/case[4]['artifact_path']
        if mutation=='missing': path.unlink()
        else: path.write_bytes(b'changed bytes')
    assert_error(lambda:finalize(case,record),'STATE_CONFLICT')
    with connect(case[0].database) as db:
        assert db.execute('SELECT state FROM runs').fetchone()[0]=='VERIFYING'
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0]==0


def test_missing_index_is_insufficient_and_can_commit_failure(tmp_path):
    service,proposal,bindings,token,item=prepared(tmp_path)
    body=proposal.model_dump(mode='json');body['evidence_ids']=['missing'];body['items']['values'][0]['evidence_ids']=['missing']
    proposal=ProposeResult.model_validate_json(canonical_json(body))
    bindings=[b.model_copy(update={'evidence_id':'missing'}) for b in bindings]
    case=(service,proposal,bindings,token,item)
    record=verify(case)
    assert finalize(case,record).outcome=='FAILED'


@pytest.mark.parametrize('raw_transform',[
    lambda raw:raw.replace(b'"raw_value":"1.20"',b'"raw_value":"9","raw_value":"1.20"'),
    lambda raw:b'{"values":NaN}',lambda raw:b'{"values":',
])
def test_ambiguous_original_json_cannot_pass(tmp_path,raw_transform):
    case=prepared(tmp_path,raw_transform=raw_transform)
    assert finalize(case,verify(case)).outcome=='FAILED'


@pytest.mark.parametrize('mutation',['cancel','wrong-token','no-token'])
def test_qualification_is_rechecked_at_finalize(tmp_path,mutation):
    case=prepared(tmp_path,scheduled=True);record=verify(case)
    service,_,_,token,_=case
    if mutation=='cancel':
        service.scheduler.finish(token,'CANCELLED')
    if mutation=='no-token': token=None
    if mutation=='wrong-token':
        from dataclasses import replace
        token=replace(token,epoch=token.epoch+1)
    with pytest.raises(BusinessError):
        service.finalize(record['verification_id'],expected_state_version=2,execution_token=token)
    with connect(service.database) as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0]==0


def test_concurrent_finalization_is_one_result_and_one_event(tmp_path):
    case=prepared(tmp_path);record=verify(case)
    def finish(_):
        return VerificationService(tmp_path).finalize(record['verification_id'],expected_state_version=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(finish,range(2)))
    assert results[0]==results[1]
    with connect(case[0].database) as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='result_ready'").fetchone()[0]==1


@pytest.mark.parametrize('table',['run_results','run_verifications'])
@pytest.mark.parametrize('operation',['update','delete','replace'])
def test_verification_history_is_immutable(tmp_path,table,operation):
    case=prepared(tmp_path);finalize(case,verify(case))
    with connect(case[0].database) as db, pytest.raises(sqlite3.IntegrityError):
        sql=(f'UPDATE {table} SET run_id=run_id' if operation=='update' else f'DELETE FROM {table}' if operation=='delete'
             else f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}')
        db.execute(sql)


@pytest.mark.parametrize('valid',[True,False])
def test_confirmed_write_requires_original_receipt_content(tmp_path,valid):
    from webagent.db.repository import utc_text
    facts=dict(operation_id='operation-1',target=Resource.repository_write('fixture/demo').resource_key,
               receipt='observed-receipt',identity_ref='test-identity')
    def capture(raw):
        body=json.loads(raw)
        if valid: body.update(facts)
        return canonical_json(body).encode()
    case=prepared(tmp_path,scenario='code',raw_transform=capture)
    with connect(case[0].database) as db,transaction(db):
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,receipt,created_at,updated_at)
            VALUES(?,?,'task-1','run-1',?,'create_pr',?,'before','CONFIRMED',?,?,?)''',
            (facts['operation_id'],'key-1',facts['target'],facts['identity_ref'],facts['receipt'],utc_text(),utc_text()))
        db.execute("INSERT INTO write_intent_evidence VALUES('task-1','operation-1','run-1','e1')")
    result=finalize(case,verify(case))
    assert result.outcome==('SUCCEEDED' if valid else 'PARTIAL')
    assert result.side_effects[0].status=='CONFIRMED'
    assert ('side_effect_receipt_not_verified:operation-1' in result.unresolved) is not valid


def test_unknown_write_added_after_verify_is_not_hidden_by_candidate(tmp_path):
    from webagent.db.repository import utc_text
    case=prepared(tmp_path,scenario='code');record=verify(case)
    with connect(case[0].database) as db,transaction(db):
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('late-operation','late-key','task-1','run-1',?,'create_pr','test-identity','before','UNKNOWN',?,?)''',
            (Resource.repository_write('fixture/demo').resource_key,utc_text(),utc_text()))
    result=finalize(case,record)
    assert result.outcome=='PARTIAL'
    assert [e.operation_id for e in result.side_effects]==['late-operation']
    assert 'unresolved_write:late-operation' in result.unresolved


def test_disk_full_result_transaction_rolls_back_and_latches_evidence_domain(tmp_path):
    case=prepared(tmp_path);record=verify(case)
    def fault(stage):
        if stage=='before_result_event':
            error=sqlite3.OperationalError('database or disk is full')
            error.sqlite_errorcode=sqlite3.SQLITE_FULL
            raise error
    case[0].fault_hook=fault
    with pytest.raises(BusinessError): finalize(case,record)
    with connect(case[0].database) as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0]==0
        assert db.execute('SELECT state FROM runs').fetchone()[0]=='VERIFYING'
        assert db.execute('SELECT faulted FROM evidence_domain').fetchone()[0]==1
    with pytest.raises(BusinessError): EvidenceStore(tmp_path).assert_dispatch_allowed()


@pytest.mark.parametrize('phase',['verify','finalize'])
def test_lease_expiring_during_integrity_reads_cannot_publish(tmp_path,monkeypatch,phase):
    from datetime import timedelta
    import webagent.scheduler.store as scheduling
    case=prepared(tmp_path,scheduled=True)
    record=verify(case) if phase=='finalize' else None
    original=case[0]._documents
    calls=0
    def slow_read(*args):
        nonlocal calls
        result=original(*args);calls+=1
        # Advance wall time after the final integrity read, as slow disk I/O
        # would; no real sleep or token mutation is used.
        if phase=='finalize' or calls==2:
            future=datetime.now(timezone.utc)+timedelta(seconds=600)
            monkeypatch.setattr(scheduling,'_now',lambda value=None: future.isoformat(timespec='microseconds').replace('+00:00','Z'))
        return result
    monkeypatch.setattr(case[0],'_documents',slow_read)
    with pytest.raises(BusinessError):
        finalize(case,record) if record else verify(case)
    with connect(case[0].database) as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM run_verifications').fetchone()[0]==(1 if phase=='finalize' else 0)
