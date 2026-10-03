"""Graph bookkeeping cannot replace business state, qualification or evidence."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task
from webagent.errors import BusinessError
from webagent.models.schema import ProposeResult
from webagent.events import WaitingEvent, append_event
from webagent.evidence.service import EvidenceService
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.graph.store import GraphStore
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.state import transition
from webagent.verification.service import VerificationService
from unit.test_verification_rules import setup


def setup_run(tmp_path, *, scheduled=False, text='Current bounded page', graph_version=GRAPH_VERSION, scenario='finance'):
    path = tmp_path / 'business.sqlite3'
    migrate(path)
    contract, proposal, documents, bindings = setup(scenario)
    with connect(path) as db, transaction(db):
        create_task(db,task_id='task-1',instruction='Read actual original facts',requested_fields=['contract'])
        add_contract(db,contract.model_dump(mode='json'))
        create_run(db,run_id='run-1',task_id='task-1',contract_version=1,
            graph_version=graph_version,graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256='a'*64,runtime_config_sha256='b'*64)
        db.execute("INSERT INTO run_budgets(budget_record_id,run_id) VALUES('budget-1','run-1')")
    scheduler = SchedulerStore(path,lease_seconds=300)
    token = None
    if scheduled:
        scheduler.enqueue('run-1',[Resource.browser_context('run-1'),Resource.site_identity('local-fixture')],expected_state_version=0)
        generation = scheduler.start_worker('worker-1')
        token = scheduler.claim('worker-1',generation)
    else:
        transition(path,run_id='run-1',expected_state_version=0,target='RUNNING')
    observation = dict(snapshot_id='snapshot-1',run_id='run-1',captured_at='2026-10-01T00:00:00Z',
        source_url=contract.start_urls[0],title='Current page',tab_id='tab',frame_id='frame',page_version='page',
        width=100,height=100,visible_excerpt=text,evidence_ids=[],redaction_status='FILTERED')
    evidence = EvidenceService(tmp_path)
    view = evidence.publish_observation(observation,dict(title='Current page',text=text),execution_token=token)
    return GraphStore(path),scheduler,token,evidence,view,(contract,proposal,documents,bindings)


def check_error(fn, code):
    with pytest.raises(BusinessError) as caught:
        fn()
    assert caught.value.code == code


@pytest.mark.parametrize('scheduled',[False,True])
def test_observation_checkpoint_is_qualified_filtered_and_idempotent(tmp_path,scheduled):
    store,_,token,evidence,view,_=setup_run(tmp_path,scheduled=scheduled)
    checkpoint=store.checkpoint_observation('run-1','snapshot-1',expected_state_version=1,execution_token=token)
    assert checkpoint==store.checkpoint_observation('run-1','snapshot-1',expected_state_version=1,execution_token=token)
    assert checkpoint.evidence_ids==view['evidence_ids']
    assert checkpoint.verified_item_ids==[] and checkpoint.pending_item_ids
    assert checkpoint.budget_record_ref=='budget-1' and checkpoint.epoch==1
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM run_checkpoints').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==2
    assert all(evidence.store.metadata(ref)['redaction_status']=='FILTERED' for ref in checkpoint.evidence_ids)


def test_progress_is_committed_business_reference_and_reconstructable(tmp_path):
    store,_,token,_,_,_=setup_run(tmp_path,scheduled=True)
    checkpoint=store.checkpoint_observation('run-1','snapshot-1',expected_state_version=1,execution_token=token)
    first=store.record_progress('run-1','observe',snapshot_id='snapshot-1',expected_state_version=1,execution_token=token)
    repeat=store.record_progress('run-1','observe',snapshot_id='snapshot-1',expected_state_version=1,execution_token=token)
    assert first==repeat and first['observations']==1
    assert first['business_checkpoint_id']==checkpoint.checkpoint_id
    second=store.record_progress('run-1','decide',expected_state_version=1,execution_token=token,iteration=1)
    assert second['decisions']==1 and second['iteration']==1
    assert GraphStore(store.path).load_state('run-1')==second
    page=store.list_progress('run-1',after=first['progress_id'])
    assert [row['phase'] for row in page]==['decide']
    with connect(store.path) as db:
        for row in store.list_progress('run-1'):
            event=db.execute('SELECT state_version FROM task_events WHERE event_id=?',(row['business_event_id'],)).fetchone()
            assert event[0]==row['state_version']


def test_concurrent_checkpoint_and_progress_have_one_identity(tmp_path):
    store,_,token,_,_,_=setup_run(tmp_path,scheduled=True)
    def save(_):
        cp=store.checkpoint_observation('run-1','snapshot-1',expected_state_version=1,execution_token=token)
        state=store.record_progress('run-1','observe',snapshot_id='snapshot-1',expected_state_version=1,execution_token=token)
        return cp.checkpoint_id,state['progress_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities=list(pool.map(save,range(8)))
    assert len(set(identities))==1


def test_scheduled_checkpoint_and_progress_reject_missing_or_stale_token(tmp_path):
    store,scheduler,token,_,_,_=setup_run(tmp_path,scheduled=True)
    check_error(lambda:store.checkpoint_observation('run-1','snapshot-1',expected_state_version=1),'RESOURCE_CONFLICT')
    check_error(lambda:store.record_progress('run-1','decide',expected_state_version=1),'RESOURCE_CONFLICT')
    check_error(lambda:store.record_progress('run-1','decide',expected_state_version=0,execution_token=token),'STATE_CONFLICT')
    scheduler.abandon(token,reason='probe')
    check_error(lambda:store.checkpoint_observation('run-1','snapshot-1',expected_state_version=store.load_run('run-1')['state_version'],execution_token=token),'RESOURCE_CONFLICT')
    assert store.list_progress('run-1')==[]


@pytest.mark.parametrize('phase,diagnostic,iteration',[
    ('raw-secret',None,0),('decide','sensitive narrative',0),('decide',None,-1),('decide',None,True)])
def test_only_bounded_typed_progress_metadata_is_accepted(tmp_path,phase,diagnostic,iteration):
    store,_,_,_,_,_=setup_run(tmp_path)
    check_error(lambda:store.record_progress('run-1',phase,expected_state_version=1,
        diagnostic=diagnostic,iteration=iteration),'INVALID_PARAMETER')
    assert store.list_progress('run-1')==[]


def test_missing_foreign_snapshot_and_verification_fail_before_progress(tmp_path):
    store,_,_,_,_,_=setup_run(tmp_path)
    check_error(lambda:store.record_progress('run-1','observe',expected_state_version=1,snapshot_id='missing'),'NOT_FOUND')
    check_error(lambda:store.record_progress('run-1','verify',expected_state_version=1,verification_id='missing'),'STATE_CONFLICT')
    assert store.list_progress('run-1')==[]


def test_wait_requires_real_same_version_wait_event_and_queue(tmp_path):
    store,scheduler,token,_,_,_=setup_run(tmp_path,scheduled=True)
    check_error(lambda:store.record_wait_progress('run-1','wait-1',expected_state_version=1),'STATE_CONFLICT')
    now,_=scheduler._time()
    with connect(store.path) as db,transaction(db):
        scheduler._defer_in_transaction(db,token,'PAUSED',now,now)
    check_error(lambda:store.record_wait_progress('run-1','wait-1',expected_state_version=2),'STATE_CONFLICT')
    with connect(store.path) as db,transaction(db):
        append_event(db,run_id='run-1',expected_state_version=2,payload=WaitingEvent(wait_id='wait-1',reason='pause'))
    state=store.record_wait_progress('run-1','wait-1',expected_state_version=2,diagnostic='input_required')
    assert state['route']=='wait' and state['wait_id']=='wait-1' and not state['completed']
    assert store.record_wait_progress('run-1','wait-1',expected_state_version=2,diagnostic='input_required')==state
    check_error(lambda:store.record_wait_progress('run-1','wait-2',expected_state_version=2),'STATE_CONFLICT')
    check_error(lambda:store.record_progress('run-1','dispatch',expected_state_version=2,execution_token=token),'RESOURCE_CONFLICT')


def test_graph_end_does_not_publish_business_success(tmp_path):
    store,_,_,_,_,_=setup_run(tmp_path)
    check_error(lambda:store.record_progress('run-1','end',expected_state_version=1),'INVALID_PARAMETER')
    state=store.load_state('run-1')
    assert not state['completed'] and state['route']=='reconcile'
    assert store.load_run('run-1')['state']=='RUNNING'
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0]==0


def test_terminal_progress_can_only_reference_existing_terminal_fact(tmp_path):
    store,scheduler,token,_,_,_=setup_run(tmp_path,scheduled=True)
    scheduler.finish(token,'FAILED')
    state=store.record_progress('run-1','stopped',expected_state_version=2,diagnostic='budget_exceeded')
    assert state['completed'] and state['state_version']==2
    check_error(lambda:store.record_progress('run-1','dispatch',expected_state_version=2),'STATE_CONFLICT')


def test_progress_append_only_sql_and_current_binding(tmp_path):
    store,_,_,_,_,_=setup_run(tmp_path)
    store.record_progress('run-1','decide',expected_state_version=1)
    with connect(store.path) as db:
        for sql in ('UPDATE graph_progress SET iteration=10','DELETE FROM graph_progress'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('''INSERT INTO graph_progress(run_id,state_version,contract_sha256,graph_version,state_schema_version,
                phase,business_event_id,iteration,idempotency_key,occurred_at)
                SELECT run_id,0,contract_sha256,graph_version,state_schema_version,phase,business_event_id,
                iteration,?,occurred_at FROM graph_progress''',('b'*64,))


@pytest.mark.parametrize('operation',[lambda s:s.load_run('run-1'),lambda s:s.load_state('run-1'),
    lambda s:s.record_progress('run-1','decide',expected_state_version=1)])
def test_incompatible_graph_version_is_never_loaded(tmp_path,operation):
    store,_,_,_,_,_=setup_run(tmp_path,graph_version='unverified-version')
    check_error(lambda:operation(store),'STATE_CONFLICT')


def test_verified_summary_references_real_latest_pass_capsule(tmp_path):
    store,_,_,evidence,_,case=setup_run(tmp_path)
    contract,proposal,documents,bindings=case
    doc=documents[0]
    evidence.store.publish('run-1',canonical_json(doc.content).encode(),evidence_id='e1',
        source_url=doc.source_url,captured_at=doc.captured_at,object_id=doc.object_id,
        query_scope='original',locator_or_page='json')
    verifier=VerificationService(tmp_path)
    verifier.begin('run-1',1)
    record=asyncio.run(verifier.verify('run-1',proposal,bindings,expected_state_version=2))
    cp=store.checkpoint_observation('run-1','snapshot-1',expected_state_version=2)
    expected={check['criterion_id'] for check in record['checks'] if check['verdict']=='PASS'}
    assert set(cp.verified_item_ids)==expected and expected
    state=store.load_state('run-1')
    assert state['verified_summary_refs']==[record['verification_id']]
    assert record['verification_id'] not in state['evidence_ids']
    assert 'e1' not in cp.evidence_ids  # restricted original is never a model ref


def test_later_verification_replaces_previous_progress_without_promoting_proposal(tmp_path):
    store,_,_,evidence,_,case=setup_run(tmp_path)
    _,proposal,documents,bindings=case
    doc=documents[0]
    evidence.store.publish('run-1',canonical_json(doc.content).encode(),evidence_id='e1',
        source_url=doc.source_url,captured_at=doc.captured_at,object_id=doc.object_id,
        query_scope='original',locator_or_page='json')
    verifier=VerificationService(tmp_path)
    verifier.begin('run-1',1)
    first=asyncio.run(verifier.verify('run-1',proposal,bindings,expected_state_version=2))
    first_cp=store.checkpoint_observation('run-1','snapshot-1',expected_state_version=2)
    body=proposal.model_dump(mode='json')
    body['items']['values'][0]['entity_id']='wrong-company'
    wrong=ProposeResult.model_validate_json(canonical_json(body))
    later=asyncio.run(verifier.verify('run-1',wrong,bindings,expected_state_version=2))
    current=store.checkpoint_observation('run-1','snapshot-1',expected_state_version=2)
    expected={c['criterion_id'] for c in later['checks'] if c['verdict']=='PASS'}
    assert set(current.verified_item_ids)==expected
    assert set(current.pending_item_ids).isdisjoint(expected)
    assert set(current.verified_item_ids) != set(first_cp.verified_item_ids)
    assert first['verification_id'] not in store.load_state('run-1')['verified_summary_refs']


@pytest.mark.parametrize('after,limit',[(-1,100),(True,100),(0,0),(0,1001),(0,True)])
def test_progress_page_arguments_are_bounded(tmp_path,after,limit):
    store,_,_,_,_,_=setup_run(tmp_path)
    check_error(lambda:store.list_progress('run-1',after=after,limit=limit),'INVALID_PARAMETER')
