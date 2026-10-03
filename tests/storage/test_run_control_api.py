"""Public controls accept requests; only the trusted boundary applies them."""
from copy import deepcopy

import pytest
from pydantic import SecretStr

from api_support import AuthenticatedTestClient, create_test_app
from webagent.config import Settings
from webagent.controls.store import ControlStore
from webagent.db import connect
from webagent.scheduler.store import SchedulerStore
from webagent.settings.models import ModelConnection, ModelSettingsRequest
from webagent.settings.service import update_model
from storage.test_run_controls import FINANCE, Secrets


@pytest.fixture
def case(database):
    secrets=Secrets()
    update_model(database,secrets,ModelSettingsRequest(expected_version=0,model=ModelConnection(),
        accept_data_sharing=True,api_key=SecretStr('SYNTHETIC_API_CONTROL_KEY')))
    app=create_test_app(Settings(database.parent),secret_store=secrets)
    with AuthenticatedTestClient(app) as client:
        reply=client.post('/v1/tasks',json=deepcopy(FINANCE),headers={'Idempotency-Key':'prepare'})
        assert reply.status_code==201,reply.text
        yield client,reply.json()['task'],database,secrets


def post(case,path,version, *, key='control',settings=1,contract=1):
    return case[0].post(path,json={'expected_state_version':version,'contract_version':contract,
        'settings_version':settings},headers={'Idempotency-Key':key})


def start(case, *, active=False):
    response=post(case,'/v1/tasks/'+case[1]['task_id']+'/start',case[1]['state_version'],key='start')
    assert response.status_code==202,response.text
    operation=response.json()['operation']
    controls=ControlStore(case[2],secret_store=case[3])
    assert controls.apply_idle(operation['run_id'])['status']=='APPLIED'
    if active:
        scheduler=SchedulerStore(case[2],lease_seconds=300)
        generation=scheduler.start_worker('api-worker')
        return operation,scheduler.claim('api-worker',generation),scheduler
    return operation,None,None


def test_start_and_poll_distinguish_acceptance_from_completion(case):
    response=post(case,'/v1/tasks/'+case[1]['task_id']+'/start',case[1]['state_version'],key='start')
    assert response.status_code==202 and response.json()['operation']['status']=='PENDING'
    operation=response.json()['operation']
    assert response.headers['location']=='/v1/operations/'+operation['operation_id']
    assert case[0].get(response.headers['location']).json()['operation']['status']=='PENDING'
    done=ControlStore(case[2]).apply_idle(operation['run_id'])
    assert done['status']=='APPLIED'
    assert case[0].get(response.headers['location']).json()['operation']['status']=='APPLIED'
    replay=post(case,'/v1/tasks/'+case[1]['task_id']+'/start',case[1]['state_version'],key='start')
    assert replay.status_code==202 and replay.json()==response.json()
    conflict=post(case,'/v1/tasks/'+case[1]['task_id']+'/start',case[1]['state_version']+1,key='start')
    assert conflict.status_code==409
    with connect(case[2]) as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==1


def test_public_pause_then_resume_then_cancel_receipts(case):
    op,token,scheduler=start(case,active=True)
    controls=ControlStore(case[2],scheduler=scheduler)
    pause=post(case,'/v1/runs/'+op['run_id']+'/pause',1,key='pause')
    assert pause.status_code==202 and pause.json()['operation']['status']=='PENDING'
    controls.apply_at_boundary(token)
    polled=case[0].get('/v1/operations/'+pause.json()['operation']['operation_id']).json()['operation']
    assert polled['status']=='APPLIED' and polled['state']=='PAUSED'
    waiting=case[0].get('/v1/runs/'+op['run_id']+'/progress').json()
    assert waiting['progress'][-1]['phase']=='wait'
    resume=post(case,'/v1/runs/'+op['run_id']+'/resume',2,key='resume')
    assert resume.status_code==202
    controls.apply_idle(op['run_id'])
    resumed=case[0].get('/v1/operations/'+resume.json()['operation']['operation_id']).json()['operation']
    assert resumed['state']=='RECONCILING'
    cancel=post(case,'/v1/runs/'+op['run_id']+'/cancel',3,key='cancel')
    assert cancel.status_code==202
    controls.apply_idle(op['run_id'])
    final=case[0].get('/v1/operations/'+cancel.json()['operation']['operation_id']).json()['operation']
    assert final['state']=='CANCELLED' and final['result']['side_effects']==[]
    assert post(case,'/v1/runs/'+op['run_id']+'/resume',4,key='terminal-resume').status_code==409


def test_config_update_does_not_change_old_run_control_preconditions(case):
    op,token,scheduler=start(case,active=True)
    update_model(case[2],case[3],ModelSettingsRequest(expected_version=1,model=ModelConnection(),
        accept_data_sharing=True,api_key=SecretStr('SYNTHETIC_API_ROTATED_KEY')))
    history=case[0].get('/v1/tasks/'+case[1]['task_id']+'/runs').json()['runs']
    assert history[0]['settings_version']==1
    assert post(case,'/v1/runs/'+op['run_id']+'/pause',1,key='new-config',settings=2).status_code==409
    accepted=post(case,'/v1/runs/'+op['run_id']+'/pause',1,key='frozen-config',settings=1)
    assert accepted.status_code==202
    ControlStore(case[2],scheduler=scheduler).apply_at_boundary(token)
    assert post(case,'/v1/runs/'+op['run_id']+'/cancel',2,key='new-config-cancel',settings=2).status_code==409
    accepted=post(case,'/v1/runs/'+op['run_id']+'/cancel',2,key='frozen-config-cancel',settings=1)
    assert accepted.status_code==202
    assert ControlStore(case[2]).apply_idle(op['run_id'])['state']=='CANCELLED'


def test_public_retry_creates_new_thread_without_rewriting_parent(case):
    op,_,_=start(case)
    cancelled=post(case,'/v1/runs/'+op['run_id']+'/cancel',0,key='cancel')
    assert cancelled.status_code==202
    ControlStore(case[2]).apply_idle(op['run_id'])
    retry=post(case,'/v1/tasks/'+case[1]['task_id']+'/retry',1,key='retry')
    assert retry.status_code==202,retry.text
    new=retry.json()['operation']
    assert new['parent_run_id']==op['run_id'] and new['run_id']!=op['run_id']
    with connect(case[2]) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?',(op['run_id'],)).fetchone()[0]=='CANCELLED'
        assert db.execute('SELECT thread_id FROM runs WHERE run_id=?',(new['run_id'],)).fetchone()[0]==new['run_id']
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0]==0


@pytest.mark.parametrize('body',[
    {'expected_state_version':True,'contract_version':1,'settings_version':1},
    {'expected_state_version':-1,'contract_version':1,'settings_version':1},
    {'expected_state_version':1,'contract_version':1},
    {'expected_state_version':1,'contract_version':1,'settings_version':1,'realm':'webarena'},
])
def test_request_shape_is_strict_before_acceptance(case,body):
    response=case[0].post('/v1/tasks/'+case[1]['task_id']+'/start',json=body,headers={'Idempotency-Key':'invalid'})
    assert response.status_code==422
    with connect(case[2]) as db:
        assert db.execute('SELECT count(*) FROM run_controls').fetchone()[0]==0


def test_idempotency_header_and_body_must_match(case):
    body={'expected_state_version':case[1]['state_version'],'contract_version':1,'settings_version':1}
    path='/v1/tasks/'+case[1]['task_id']+'/start'
    assert case[0].post(path,json=body).status_code==422
    body['idempotency_key']='different'
    assert case[0].post(path,json=body,headers={'Idempotency-Key':'header'}).status_code==422


def test_operation_cursor_is_bounded_and_reads_expose_no_secrets(case):
    op,_,_=start(case)
    path='/v1/runs/'+op['run_id']+'/operations'
    response=case[0].get(path+'?after=0&limit=1')
    assert response.status_code==200
    assert response.json()['next_after']==op['operation_seq']
    for query in ('?after=-1','?limit=0','?after=0&after=1','?unknown=1','?after=9223372036854775808'):
        assert case[0].get(path+query).status_code==422
    for hidden in ('credential_ref','api_key','request_sha256','worker_generation','SYNTHETIC_API_CONTROL_KEY'):
        assert hidden not in response.text
    assert case[0].get('/v1/operations/missing').status_code==404
    assert case[0].get('/v1/operations/'+op['operation_id'],headers={'Authorization':''}).status_code==401
