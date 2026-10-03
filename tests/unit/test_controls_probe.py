"""Falsify M1-18 probe assertions without launching owned services."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest


PROBE_PATH = Path(__file__).resolve().parents[2] / 'scripts/verification/verify_controls.py'
SPEC = importlib.util.spec_from_file_location('owned_controls_probe', PROBE_PATH)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def completion_pair():
    operation = {'operation_id':'owned-operation','action':'pause','status':'APPLIED','completed_event_id':9}
    value = {'task_events':[{'event_type':'operation_completed','event_id':9,
        'payload_json':json.dumps({'operation_id':'owned-operation','action':'pause','status':'APPLIED',
            'result_ref':'owned-operation'})}]}
    return value,operation


@pytest.mark.parametrize('mutation',('duplicate','missing','event_id','action','status','result_ref'))
def test_completion_guard_rejects_changed_or_duplicate_receipts(mutation):
    value,operation = completion_pair()
    PROBE.assert_completed_event(value,operation)
    if mutation=='duplicate':value['task_events']*=2
    elif mutation=='missing':value['task_events']=[]
    elif mutation=='event_id':value['task_events'][0]['event_id']=10
    else:
        payload = json.loads(value['task_events'][0]['payload_json'])
        payload[mutation]='changed-receipt'
        value['task_events'][0]['payload_json']=json.dumps(payload)
    with pytest.raises(AssertionError):PROBE.assert_completed_event(value,operation)


def ledger_pair():
    before = {'run':{'run_id':'owned-run','contract_sha256':'contract','graph_version':'graph',
        'graph_state_schema_version':'schema','model_config_sha256':'model'},
        'run_budgets':[{'budget_record_id':'original-budget','actions_used':2,
            'content_pages_used':1,'observations_used':3,'screenshots_used':1,'model_calls_used':1,
            'active_ms':3200,'ci_wait_ms':0,'recovery_counts_json':'{"existing-obstacle":1}'}],
        'quota_debits':[{'debit_id':'original-debit','amount':1}],
        'task_events':[{'event_id':1,'event_type':'wait_registered'}],
        'budget_attempts':[{'attempt_id':'original-attempt','epoch':1}],
        'steps':[{'step_id':'original-step','status':'COMPLETED'}],
        'run_results':[],'integrity':'ok','foreign_keys':[]}
    return before,deepcopy(before)


@pytest.mark.parametrize('mutation',('duplicate_budget','missing_attempt','reset_budget','changed_step','repeat_quota'))
def test_same_run_resume_guard_rejects_reset_or_replayed_original_work(mutation):
    before,after = ledger_pair()
    PROBE.assert_resume_preserved(before,after)
    if mutation=='duplicate_budget':after['run_budgets']*=2
    elif mutation=='missing_attempt':after['budget_attempts']=[]
    elif mutation=='reset_budget':after['run_budgets'][0]['actions_used']=0
    elif mutation=='changed_step':after['steps'][0]['status']='INTENT'
    elif mutation=='repeat_quota':after['quota_debits']*=2
    with pytest.raises(AssertionError):PROBE.assert_resume_preserved(before,after)


def test_fault_marker_is_durable_before_fixed_owned_os_stop(tmp_path,monkeypatch):
    with pytest.raises(ValueError):PROBE.FixedHook(tmp_path,'untrusted-code')
    stopped = []
    def stop(pid,sig):
        marker = json.loads((tmp_path/'boundary.json').read_text())
        assert marker=={'stage':'control_applied','run_id':'owned-run','pid':pid}
        stopped.append(sig)
    monkeypatch.setattr(PROBE.os,'kill',stop)
    hook = PROBE.FixedHook(tmp_path,'control_applied',kill=True)
    asyncio.run(hook.hook('model_before','owned-run'))
    assert not (tmp_path/'boundary.json').exists()
    asyncio.run(hook.hook('control_applied','owned-run'))
    asyncio.run(hook.hook('control_applied','owned-run'))
    assert stopped==[signal.SIGSTOP]


def test_owned_resource_factory_cannot_authorize_an_unowned_origin():
    origin = 'http://127.0.0.1:12345'
    contract = SimpleNamespace(sources=[SimpleNamespace(origin=origin,site_id='owned-http')],identity_ref=None)
    resources = PROBE.owned_resources(origin,contract,'owned-run')
    assert resources==[PROBE.Resource.site_identity('owned-http',realm='webarena'),
        PROBE.Resource.browser_context('owned-run')]
    contract.sources[0].origin = 'https://external.example'
    with pytest.raises(AssertionError):PROBE.owned_resources(origin,contract,'owned-run')


def test_trusted_provider_replacement_preserves_frozen_logical_config(monkeypatch):
    calls = []
    class Transport:
        def __init__(self,config,key,**kwargs):calls.append(('create',config,key,kwargs))
        async def complete(self,*args,**kwargs):calls.append(('complete',args,kwargs));return 'reply'
        async def complete_verification(self,*args,**kwargs):return 'verification'
        async def aclose(self):calls.append(('closed',))
    monkeypatch.setattr(PROBE,'DeepSeekTransport',Transport)
    logical = PROBE.ModelConfig(base_url='https://api.deepseek.com')
    provider = PROBE.OwnedProvider(logical,'http://127.0.0.1:12345')
    assert provider.config is logical
    assert calls[0][1].base_url=='http://127.0.0.1:12345' and calls[0][3]=={'allow_test_loopback':True}
    assert asyncio.run(provider.complete('current-input',input_sha256='a'*64))=='reply'
    assert calls[1]==('complete',('current-input',),{'input_sha256':'a'*64})
    asyncio.run(provider.aclose())
    assert calls[-1]==('closed',)


def test_dynamic_result_guard_rejects_a_fixed_answer_and_model_declared_success():
    run_id = 'owned-run'
    fixture = SimpleNamespace(disclosure=lambda run_id:{'values':[{'normalized_value':'visible-47'}]})
    result = {'generated_by':'business_aggregator','run_id':run_id,
        'items':{'values':[{'normalized_value':'visible-47'}]}}
    value = {'run':{'state':'SUCCEEDED'},'run_results':[{'result_json':json.dumps(result)}]}
    PROBE.assert_source_result(value,fixture,run_id)
    result['items']['values'][0]['normalized_value']='fixed-answer'
    value['run_results'][0]['result_json']=json.dumps(result)
    with pytest.raises(AssertionError):PROBE.assert_source_result(value,fixture,run_id)
    result['items']['values'][0]['normalized_value']='visible-47'
    result['generated_by']='model'
    value['run_results'][0]['result_json']=json.dumps(result)
    with pytest.raises(AssertionError):PROBE.assert_source_result(value,fixture,run_id)


def test_crash_snapshot_reaps_stopped_worker_before_reading_sqlite(tmp_path,monkeypatch):
    order = []
    domain = object.__new__(PROBE.Domain)
    domain.directory,domain.run_id = tmp_path,'owned-run'
    async def boundary():order.append('boundary');return {'pid':17,'run_id':'owned-run'}
    async def kill():order.append('reaped')
    def facts(directory,run_id):
        assert order==['boundary','reaped']
        order.append('sqlite-read')
        return {'run':{'state':'PAUSED'}}
    domain.boundary,domain.kill_worker = boundary,kill
    monkeypatch.setattr(PROBE,'facts',facts)
    _,before = asyncio.run(domain.crash_snapshot())
    artifact = json.loads((tmp_path/'before-crash.json').read_text())
    assert before==artifact['business'] and artifact['observed_after_sigkill'] is True
    assert order==['boundary','reaped','sqlite-read']


@pytest.mark.parametrize('fault',['permission','changed_group'])
def test_group_signal_failure_still_cleans_other_resources_and_remains_a_failure(tmp_path,monkeypatch,fault):
    calls = []
    class Process:
        def __init__(self,pid):self.pid,self.returncode = pid,None
        def kill(self):calls.append(('kill',self.pid));self.returncode=-signal.SIGKILL
        def terminate(self):calls.append(('terminate',self.pid));self.returncode=0
        async def wait(self):calls.append(('wait',self.pid));return self.returncode
    class Client:
        async def aclose(self):calls.append('client')
    class Resource:
        def __init__(self,name):self.name=name
        def close(self):calls.append(self.name)
    domain = object.__new__(PROBE.Domain)
    domain.directory,domain.run_id = tmp_path,'owned-run'
    domain.worker,domain.api = Process(17),Process(18)
    domain.worker_group,domain.worker_pids,domain.worker_kill_boundary = 17,[17],True
    domain.client,domain.listener = Client(),Resource('listener')
    domain.logs,domain.requests = [Resource('log')],[]
    monkeypatch.setattr(PROBE.os,'getpgid',lambda pid:17 if fault=='permission' else 999)
    def killpg(pid,sig):
        calls.append(('group',pid))
        raise PermissionError(1,'Owned group signal denied')
    monkeypatch.setattr(PROBE.os,'killpg',killpg)
    with pytest.raises(ExceptionGroup) as caught:asyncio.run(domain.close())
    assert ('kill',17) in calls and ('terminate',18) in calls
    assert all(item in calls for item in ('client','listener','log'))
    assert domain.worker is None and domain.api.returncode==0
    record = json.loads((tmp_path/'process-exit.json').read_text())
    assert record['cleanup_errors'] and record['api_returncode']==0
    details = PROBE.error_details(caught.value)
    assert details['exceptions']
    if fault=='permission':
        assert details['exceptions'][0]['exceptions'][0]['errno']==1
    else:
        assert not any(isinstance(item,tuple) and item[0]=='group' for item in calls)


def test_cleanup_error_does_not_replace_original_failure_or_its_cause(tmp_path,monkeypatch):
    original = RuntimeError('Original SQLite snapshot failed')
    class Fixture:
        def __init__(self,*args):
            self.requests,self.provider_requests,self.errors = [],[],[]
        async def start(self):return self
        async def close(self):return None
        def document(self):return {}
    class Domain:
        def __init__(self,directory,fixture,alias):
            self.directory,self.alias,self.worker_pids,self.api = directory,alias,[],None
        async def start(self,**kwargs):
            try:raise PermissionError(1,'Underlying read failed')
            except PermissionError as error:raise original from error
        async def close(self):raise PermissionError(1,'Cleanup also failed')
    monkeypatch.setattr(PROBE,'ControlsFixture',Fixture)
    monkeypatch.setattr(PROBE,'Domain',Domain)
    report = {'passed':False,'checks':{}}
    with pytest.raises(RuntimeError) as caught:asyncio.run(PROBE.verify(tmp_path,report))
    assert caught.value is original and not report['passed']
    assert report['primary_failure']['message']=='Original SQLite snapshot failed'
    assert report['primary_failure']['cause']['errno']==1
    assert report['primary_failure']['locations'][-1]['function']=='start'
    assert report['cleanup_errors'][0]['message']=='[Errno 1] Cleanup also failed'
    assert report['cleanup_errors'][0]['errno']==1
