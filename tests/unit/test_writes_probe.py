"""Falsify FR-03 probe guards without spawning browsers or services."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import signal

import pytest

PROBE_PATH = Path(__file__).resolve().parents[2] / 'scripts/verification/verify_writes.py'
SPEC = importlib.util.spec_from_file_location('owned_writes_probe', PROBE_PATH)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def test_owned_external_write_persists_content_and_deduplicates_exact_business_key(tmp_path):
    fixture = PROBE.WriteFixture(tmp_path)
    key = fixture.bind('owned-case', 'owned-task', 'owned-identity')
    assert fixture.facts('owned-case')['outcome'] == 'NOT_APPLIED'
    first = fixture.apply('owned-case', {'business_key': [key]})
    second = fixture.apply('owned-case', {'business_key': [key]})
    assert first == second and fixture.counts('owned-case') == {'posts': 2, 'applied': 1}
    assert fixture.content('owned-case') == 'Owned M1-23 synthetic content v1'
    facts = fixture.facts('owned-case')
    assert facts['outcome'] == 'APPLIED' and facts['receipt'] == first
    fixture.change('owned-case', 'unknown')
    assert fixture.facts('owned-case')['outcome'] == 'UNKNOWN'
    assert fixture.content('owned-case') == 'Owned M1-23 synthetic content v1'
    assert fixture.counts('owned-case') == {'posts': 2, 'applied': 1}


@pytest.mark.parametrize('fields', ({}, {'business_key': ['wrong']}, {'business_key': ['wrong'], 'extra': ['value']}))
def test_external_source_rejects_unbound_submission_before_mutating(tmp_path, fields):
    fixture = PROBE.WriteFixture(tmp_path)
    fixture.bind('owned-case', 'owned-task', 'owned-identity')
    with pytest.raises(ValueError):
        fixture.apply('owned-case', fields)
    assert fixture.counts('owned-case') == {'posts': 0, 'applied': 0}
    assert fixture.facts('owned-case')['outcome'] == 'NOT_APPLIED'


def test_fault_marker_identifies_fixed_owned_process_before_stopping(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        PROBE.Fault(tmp_path, 'A', 'owned-run', 'untrusted-stage')
    observed = []
    def stop(pid, sig):
        observed.append(json.loads((tmp_path / 'marker-A.json').read_text()))
        assert sig == signal.SIGSTOP
        assert observed[-1] == {'pid': pid, 'run_id': 'owned-run', 'stage': 'intent_committed'}
    monkeypatch.setattr(PROBE.os, 'kill', stop)
    fault = PROBE.Fault(tmp_path, 'A', 'owned-run', 'intent_committed')
    fault.stop('physical_applied')
    assert not observed
    fault.stop('intent_committed')
    fault.stop('intent_committed')
    assert len(observed) == 1


def ledger_pair():
    value = {'run': {key: key for key in ('run_id', 'task_id', 'contract_sha256', 'graph_version',
        'graph_state_schema_version', 'model_config_sha256', 'runtime_config_sha256')},
        'budget': {'budget_record_id': 'original-budget', **{key: 1 for key in (
            'actions_used', 'content_pages_used', 'observations_used', 'screenshots_used',
            'model_calls_used', 'active_ms', 'ci_wait_ms')}},
        'tables': {'steps': [{'step_id': 'old-write', 'status': 'UNKNOWN', 'error_code': 'TIMEOUT'}],
            'task_events': [{'event_id': 1}], 'budget_attempts': [{'attempt_id': 'old-debit'}],
            'gateway_attempts': [{'step_id': 'old-write', 'epoch': 1}],
            'write_protocol_claims': [{'operation_id': 'stable-write', 'claim_sha256': 'old'}],
            'write_protocol_dispatches': [{'step_id': 'old-write'}], 'write_protocol_checks': [],
            'write_intents': [{'operation_id': 'stable-write', **{key: key for key in (
                'business_key', 'task_id', 'originating_run_id', 'target', 'expected_change',
                'identity_ref', 'precondition_version')}}]}, 'integrity': 'ok', 'foreign_keys': []}
    return value, deepcopy(value)


@pytest.mark.parametrize('mutation', ('budget', 'run', 'missing_event', 'changed_attempt',
    'changed_intent', 'changed_claim', 'changed_failure', 'integrity', 'foreign_key'))
def test_recovery_guard_rejects_ledger_rewrites_and_budget_resets(mutation):
    before, after = ledger_pair()
    PROBE.preserve(before, after)
    if mutation == 'budget':
        after['budget']['actions_used'] = 0
    elif mutation == 'run':
        after['run']['run_id'] = 'another-run'
    elif mutation == 'missing_event':
        after['tables']['task_events'] = []
    elif mutation == 'changed_attempt':
        after['tables']['gateway_attempts'][0]['epoch'] = 2
    elif mutation == 'changed_intent':
        after['tables']['write_intents'][0]['identity_ref'] = 'another-identity'
    elif mutation == 'changed_claim':
        after['tables']['write_protocol_claims'][0]['claim_sha256'] = 'changed'
    elif mutation == 'changed_failure':
        after['tables']['steps'][0]['error_code'] = None
    elif mutation == 'integrity':
        after['integrity'] = 'corrupt'
    else:
        after['foreign_keys'] = [['broken-reference']]
    with pytest.raises(AssertionError):
        PROBE.preserve(before, after)


@pytest.mark.parametrize('mutation', ('none', 'missing_debit', 'wrong_epoch', 'missing_dispatch', 'reset_counter'))
def test_each_write_dispatch_requires_one_separate_budget_attempt(mutation):
    step, run = 'owned-step', 'owned-run'
    attempt_id = 'gateway-' + hashlib.sha256(PROBE.canonical_json([run, step]).encode()).hexdigest()
    value = {'run': {'run_id': run}, 'budget': {'actions_used': 1}, 'tables': {
        'gateway_attempts': [{'step_id': step, 'run_id': run, 'external_write': 1, 'epoch': 1}],
        'write_protocol_dispatches': [{'step_id': step}],
        'budget_attempts': [{'run_id': run, 'attempt_id': attempt_id, 'kind': 'action', 'actions': 1, 'epoch': 1}]}}
    if mutation == 'missing_debit':
        value['tables']['budget_attempts'] = []
    elif mutation == 'wrong_epoch':
        value['tables']['budget_attempts'][0]['epoch'] = 2
    elif mutation == 'missing_dispatch':
        value['tables']['write_protocol_dispatches'] = []
    elif mutation == 'reset_counter':
        value['budget']['actions_used'] = 0
    if mutation == 'none':
        assert PROBE.assert_dispatch_budgets(value) == 1
    else:
        with pytest.raises((AssertionError, KeyError)):
            PROBE.assert_dispatch_budgets(value)
