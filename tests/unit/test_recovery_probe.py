"""Falsify the FR-02 acceptance assertions without launching a browser."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import signal

import pytest


PROBE_PATH = Path(__file__).resolve().parents[2] / 'scripts/verification/verify_recovery.py'
SPEC = importlib.util.spec_from_file_location('owned_recovery_probe', PROBE_PATH)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def audit_pair():
    before = {'run':{'run_id':'owned-run','contract_sha256':'fixed-contract','graph_version':'fixed-graph',
        'graph_state_schema_version':'fixed-schema','model_config_sha256':'fixed-model'},
        'budget': {'budget_record_id':'original-budget', 'actions_used':2,
        'content_pages_used':2, 'observations_used':3, 'screenshots_used':1,'model_calls_used':1,
        'active_ms':3200,'ci_wait_ms':100,'recovery_counts_json':'{"original-obstacle":1}'},
        'quota_debits':[{'debit_id':'original-debit','amount':1}],
        'task_events':[{'event_id':1,'event_type':'state_changed','state_version':1}],
        'budget_attempts':[{'attempt_id':'original-attempt','epoch':1,'actions':1}],
        'steps':[{'step_id':'original-uncertain-read','status':'UNKNOWN','error_code':'SERVICE_UNAVAILABLE'}],
        'run_results':[], 'integrity':'ok','foreign_keys':[]}
    return before, deepcopy(before)


def test_ledger_guard_accepts_monotonic_new_work_without_rewriting_originals():
    before, after = audit_pair()
    after['budget']['actions_used'] += 1
    after['budget']['active_ms'] += 100
    after['task_events'].append({'event_id':2,'event_type':'state_changed','state_version':2})
    after['budget_attempts'].append({'attempt_id':'fresh-attempt','epoch':3,'actions':1})
    PROBE.assert_preserved(before,after)


@pytest.mark.parametrize('field',('actions_used','content_pages_used','observations_used','screenshots_used','model_calls_used','active_ms','ci_wait_ms'))
def test_ledger_guard_rejects_reset_original_budget(field):
    before, after = audit_pair()
    after['budget'][field] = 0
    with pytest.raises(AssertionError):
        PROBE.assert_preserved(before,after)


@pytest.mark.parametrize('mutation',('budget_identity','duplicate_debit','changed_debit',
    'missing_event','changed_event','missing_attempt','changed_attempt','lost_failure','rewritten_failure',
    'integrity','foreign_key','duplicate_result_event'))
def test_ledger_guard_rejects_replayed_or_changed_durable_records(mutation):
    before, after = audit_pair()
    if mutation=='budget_identity':after['budget']['budget_record_id']='fresh-budget'
    elif mutation=='duplicate_debit':after['quota_debits'] *= 2
    elif mutation=='changed_debit':after['quota_debits'][0]['amount']=0
    elif mutation=='missing_event':after['task_events']=[]
    elif mutation=='changed_event':after['task_events'][0]['state_version']=2
    elif mutation=='missing_attempt':after['budget_attempts']=[]
    elif mutation=='changed_attempt':after['budget_attempts'][0]['epoch']=3
    elif mutation=='lost_failure':after['steps']=[]
    elif mutation=='rewritten_failure':after['steps'][0]['status']='COMPLETED'
    elif mutation=='integrity':after['integrity']='corrupt'
    elif mutation=='foreign_key':after['foreign_keys']=[['broken-reference']]
    elif mutation=='duplicate_result_event':
        after['run_results']=[{'run_id':'original-run'}]
        after['task_events'] += [{'event_id':2,'event_type':'result_ready'},{'event_id':3,'event_type':'result_ready'}]
    with pytest.raises(AssertionError):
        PROBE.assert_preserved(before,after)


def test_fault_is_fixed_constructor_dependency_and_marker_precedes_os_stop(tmp_path,monkeypatch):
    with pytest.raises(ValueError):
        PROBE.FaultController(tmp_path,'owned-run','user-controlled-code')
    calls=[]
    def stop(pid,sig):
        marker=json.loads((tmp_path/'fault-marker.json').read_text())
        assert marker=={'run_id':'owned-run','stage':'action_after','pid':pid}
        calls.append(sig)
    monkeypatch.setattr(PROBE.os,'kill',stop)
    fault=PROBE.FaultController(tmp_path,'owned-run','action_after')
    fault.stop('action_before')
    assert not (tmp_path/'fault-marker.json').exists()
    fault.stop('action_after')
    fault.stop('action_after')
    assert calls==[signal.SIGSTOP]


def test_saver_before_and_after_wrap_exactly_one_real_commit(tmp_path):
    calls=[]
    class Saver:
        async def aput(self,*args):
            calls.append(('commit',args))
            return 'durable-config'
    fault=PROBE.FaultController(tmp_path,'owned-run')
    fault.stop=lambda stage:calls.append((stage,None))
    saver=Saver()
    PROBE.install_saver_fault(saver,fault)
    args=({'thread':'owned-run'},{'channel_values':{'route':'confirm'}},{'step':1},{'route':'2'})
    result=asyncio.run(saver.aput(*args))
    assert result=='durable-config'
    assert calls==[('saver_before',None),('commit',args),('saver_after',None)]


def test_saver_failure_occurs_before_any_real_commit_and_only_once(tmp_path):
    class Saver:
        calls=0
        async def aput(self,*args):
            self.calls+=1
            return 'durable-config'
    saver=Saver()
    PROBE.install_saver_fault(saver,PROBE.FaultController(tmp_path,'owned-run','saver_error'))
    args=({}, {'channel_values':{'route':'confirm'}}, {}, {})
    with pytest.raises(PROBE.sqlite3.OperationalError):asyncio.run(saver.aput(*args))
    assert saver.calls==0
    assert asyncio.run(saver.aput(*args))=='durable-config' and saver.calls==1


def test_provider_result_uses_transferred_visible_bytes_not_fixture_secret(tmp_path):
    fixture=PROBE.RecoveryFixture(tmp_path)
    fixture.phase['owned-run']='B'
    visible=fixture.document()
    visible['values'][0]['normalized_value']='transferred-visible-number'
    observation={'source_url':'http://owned.example/data/owned-run','snapshot_id':'fresh-snapshot',
        'visible_excerpt':json.dumps(visible),'evidence_ids':['current-filtered-evidence'],'redaction_status':'FILTERED'}
    payload={'run_id':'owned-run','contract':{},'observation':observation,'verified_checkpoint':{'epoch':3},
        'image_evidence_ids':[],'allowed_action_schema_ref':'fixed-schema','selected_flow_versions':[]}
    request={'messages':[{'role':'system','content':'owned-schema'}, {'role':'user','content':json.dumps(payload)}]}
    reply=asyncio.run(fixture.model_reply(request))
    proposal=json.loads(reply['choices'][0]['message']['content'])
    assert proposal['items']['values'][0]['normalized_value']=='transferred-visible-number'
    assert proposal['evidence_ids']==['current-filtered-evidence']
    assert fixture.document()['values'][0]['normalized_value']!='transferred-visible-number'


def test_live_gateway_rpc_uses_production_action_argument_name():
    class Manager:
        calls=[]
        async def rpc(self,method,**args):self.calls.append((method,args));return {'status':'COMPLETED'}
    proxy=object.__new__(PROBE.LiveGatewayProxy)
    proxy.manager=Manager()
    result=asyncio.run(proxy.dispatch('fresh-token',{'step_id':'fresh-step'}))
    assert result=={'status':'COMPLETED'}
    assert proxy.manager.calls==[('dispatch',{'token':'fresh-token','action':{'step_id':'fresh-step'}})]


def test_current_qualification_reconstruction_uses_scheduler_resource_order(tmp_path):
    config=PROBE.ModelConfig(base_url='http://127.0.0.1:12345')
    PROBE.seed(tmp_path,'http://127.0.0.1:12345','owned-run',config)
    store=PROBE.SchedulerStore(tmp_path/'business.sqlite3')
    generation=store.start_worker('owned-test-worker')
    original=store.claim('owned-test-worker',generation)
    assert PROBE.current_token(tmp_path,'owned-run')==original


@pytest.mark.parametrize('field',('contract_sha256','graph_version','graph_state_schema_version','model_config_sha256'))
def test_old_framework_cannot_rewrite_frozen_business_identity(field):
    before,after=audit_pair()
    after['run'][field]='rewritten'
    with pytest.raises(AssertionError):PROBE.assert_preserved(before,after)


def test_recovery_counter_cannot_be_forgotten_on_restart():
    before,after=audit_pair()
    after['budget']['recovery_counts_json']='{}'
    with pytest.raises(AssertionError):PROBE.assert_preserved(before,after)
