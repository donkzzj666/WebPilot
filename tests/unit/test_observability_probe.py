"""Falsify acceptance guards without starting a process or browser."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


PATH = Path(__file__).resolve().parents[2] / 'scripts/verification/verify_observability.py'
SPEC = importlib.util.spec_from_file_location('owned_observability_probe', PATH)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def model(*, priced=True, known=True):
    return {'usage_known': known, 'usage_complete': known,
        'usage': {'input_tokens': 23 if known else None, 'output_tokens': 17 if known else None, 'image_units': None},
        'price_version': 'price-owned-fixture' if priced else None,
        'estimated_cost': '0.000097' if priced and known else None,
        'cost_currency': 'USD' if priced and known else None,
        'cost_known': priced and known, 'error_class': None if known else 'provider_error'}


@pytest.mark.parametrize('mutation', ['none', 'missing_price', 'fabricated_unknown_cost', 'unknown_as_zero',
                                    'wrong_amount', 'unpriced_amount', 'missing_unknown_usage'])
def test_cost_guard_distinguishes_versioned_unpriced_and_unknown_usage(mutation):
    success = {'model_attempts': [model()]}
    failed = {'model_attempts': [model(priced=False), model(priced=False, known=False)],
              'model_summary': {'unknown_usage_attempts': 1}}
    if mutation == 'missing_price':
        success['model_attempts'][0]['price_version'] = None
    elif mutation == 'fabricated_unknown_cost':
        failed['model_attempts'][1].update(estimated_cost='0', cost_known=True)
    elif mutation == 'unknown_as_zero':
        failed['model_attempts'][1].update(usage_known=True, usage_complete=True)
    elif mutation == 'wrong_amount':
        success['model_attempts'][0]['estimated_cost'] = '0.5'
    elif mutation == 'unpriced_amount':
        failed['model_attempts'][0].update(estimated_cost='0.000097', cost_known=True)
    elif mutation == 'missing_unknown_usage':
        failed['model_summary']['unknown_usage_attempts'] = 0
    if mutation == 'none':
        PROBE.assert_costs(success, failed)
    else:
        with pytest.raises((AssertionError, AttributeError)):
            PROBE.assert_costs(success, failed)


@pytest.mark.parametrize('category', list(PROBE.CANARIES))
def test_log_scan_detects_each_sensitive_category(tmp_path, category):
    path = tmp_path / 'worker.jsonl'
    path.write_text(json.dumps({'event': 'graph_node_completed', 'run_id': PROBE.CANARIES[category]}) + '\n')
    with pytest.raises(AssertionError):
        PROBE.scan_logs([path])


def test_empty_or_raw_framework_logs_cannot_prove_safety(tmp_path):
    path = tmp_path / 'worker.jsonl'
    path.write_text('')
    with pytest.raises(AssertionError):
        PROBE.scan_logs([path])
    path.write_text(json.dumps({'event': 'graph_node_completed', 'payload': 'arbitrary framework output'}) + '\n')
    with pytest.raises(AssertionError):
        PROBE.scan_logs([path])
    path.write_text(json.dumps({'event': 'graph_node_completed', 'run_id': 'owned-run'}) + '\n')
    assert PROBE.scan_logs([path])['structured_records'] == 1


def network():
    return ([{'event': 'socket.connect', 'host': '127.0.0.1', 'port': 9000, 'loopback': True}],
        [{'event': 'audit_loaded'}], {'known_loopback_connect_observed': True, 'non_loopback': [], 'event_count': 42},
        [{'host': '127.0.0.1', 'outcome': 'allowed'}, {'host': 'background.example', 'outcome': 'denied'}])


@pytest.mark.parametrize('mutation', ['none', 'python_external', 'node_external', 'node_udp',
                                    'chromium_external', 'trace_attempt', 'empty_python', 'missing_node', 'empty_proxy'])
def test_network_guard_checks_actual_audits_and_preserves_blocked_background_attempts(mutation):
    python, node, chromium, proxy = network()
    if mutation == 'python_external':
        python.append({'event': 'socket.connect', 'loopback': False})
    elif mutation == 'node_external':
        node.append({'event': 'net.connect', 'transport': 'tcp', 'host': 'outside.example'})
    elif mutation == 'node_udp':
        node.append({'event': 'dgram.send', 'arguments': [80, 'outside.example']})
    elif mutation == 'chromium_external':
        chromium['non_loopback'].append({'destination': '192.0.2.1'})
    elif mutation == 'trace_attempt':
        proxy.append({'host': 'api.smith.langchain.com', 'outcome': 'denied'})
    elif mutation == 'empty_python':
        python.clear()
    elif mutation == 'missing_node':
        node.clear()
    elif mutation == 'empty_proxy':
        proxy.clear()
    if mutation == 'none':
        result = PROBE.assert_network_audits(python, node, chromium, proxy)
        assert result['proxy_allowed'] == result['proxy_denied'] == 1
    else:
        with pytest.raises(AssertionError):
            PROBE.assert_network_audits(python, node, chromium, proxy)


def diagnostics():
    original = {'run': {'run_id': 'owned-run', 'task_id': 'owned-task', 'thread_id': 'owned-run',
        'graph_version': 'owned-graph', 'graph_state_schema_version': 'owned-schema', 'state': 'FAILED', 'state_version': 2},
        'events': [{'event_id': 1, 'event_type': 'action_recorded', 'state_version': 1,
            'payload_json': json.dumps({'step_id': 'owned-step'})}],
        'progress': [{'progress_id': 1, 'business_event_id': 1, 'phase': 'observe', 'checkpoint_id': 'owned-checkpoint'}],
        'steps': [{'step_id': 'owned-step', 'status': 'COMPLETED'}], 'models': [],
        'budget': {key: 1 for key in ('actions_used', 'content_pages_used', 'active_ms', 'model_calls_used', 'observations_used', 'screenshots_used')}}
    value = {'source': 'persistent_business_ledgers', 'run': deepcopy(original['run']),
        'events': [{'event_id': 1, 'event_type': 'action_recorded', 'state_version': 1, 'step_id': 'owned-step',
            'current_step_status': 'COMPLETED', 'graph': [{'progress_id': 1, 'node': 'observe', 'checkpoint_id': 'owned-checkpoint'}]}],
        'model_attempts': [], 'budget': deepcopy(original['budget'])}
    return value, original


@pytest.mark.parametrize('mutation', ['none', 'invented_event', 'missing_progress', 'wrong_node',
                                    'wrong_checkpoint', 'false_success', 'changed_counter'])
def test_diagnostic_guard_requires_exact_committed_correlations(mutation):
    value, original = diagnostics()
    if mutation == 'invented_event':
        value['events'][0]['event_id'] = 100
    elif mutation == 'missing_progress':
        value['events'][0]['graph'].clear()
    elif mutation == 'wrong_node':
        value['events'][0]['graph'][0]['node'] = 'aggregate'
    elif mutation == 'wrong_checkpoint':
        value['events'][0]['graph'][0]['checkpoint_id'] = 'framework-guessed-checkpoint'
    elif mutation == 'false_success':
        value['run']['state'] = 'SUCCEEDED'
    elif mutation == 'changed_counter':
        value['budget']['actions_used'] = 0
    if mutation == 'none':
        PROBE.assert_diagnostics(value, original)
    else:
        with pytest.raises(AssertionError):
            PROBE.assert_diagnostics(value, original)


def test_cleanup_never_signals_an_exited_or_reused_historical_process_group(monkeypatch):
    owner = object.__new__(PROBE.OwnedProcess)
    owner.pgid = 9000
    owner.proc = SimpleNamespace(pid=9000, poll=lambda: 0)
    monkeypatch.setattr(PROBE.os, 'killpg', lambda *args: pytest.fail('Exited child group must not be signaled'))
    owner.kill_owned_group()
    owner.proc = SimpleNamespace(pid=9000, poll=lambda: None)
    monkeypatch.setattr(PROBE.os, 'getpgid', lambda _: 9999)
    with pytest.raises(AssertionError):
        owner.kill_owned_group()


@pytest.mark.parametrize('mutation', ['none', 'missing_checkpoint', 'wrong_thread', 'wrong_task',
                                     'wrong_event', 'wrong_progress', 'wrong_checkpoint', 'missing_failure'])
def test_safe_graph_log_guard_binds_actual_business_references(tmp_path, mutation):
    originals, records = {}, []
    for run_id in PROBE.RUNS:
        _, original = diagnostics()
        original['run'].update(run_id=run_id, task_id='task-' + run_id)
        originals[run_id] = original
        records.append({'event': 'graph_checkpoint_saved', 'run_id': run_id, 'thread_id': run_id,
            'task_id': 'task-' + run_id, 'graph_version': 'owned-graph', 'state_version': 1,
            'event_id': 1, 'progress_id': 1, 'business_checkpoint_id': 'owned-checkpoint'})
        records.append({**records[-1], 'event': 'graph_node_completed',
            'node': 'verify' if run_id == 'obs-success' else 'recover',
            **({'error_class': 'provider'} if run_id == 'obs-failed' else {})})
    if mutation == 'missing_checkpoint':
        records[0]['event'] = 'graph_node_completed'
    elif mutation == 'wrong_thread':
        records[0]['thread_id'] = 'another-run'
    elif mutation == 'wrong_task':
        records[0]['task_id'] = 'another-task'
    elif mutation == 'wrong_event':
        records[0]['event_id'] = 2
    elif mutation == 'wrong_progress':
        records[0]['progress_id'] = 2
    elif mutation == 'wrong_checkpoint':
        records[0]['business_checkpoint_id'] = 'arbitrary-framework-reference'
    elif mutation == 'missing_failure':
        records[-1].pop('error_class')
    path = tmp_path / 'worker.jsonl'
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))
    if mutation == 'none':
        PROBE.assert_log_correlations([path], originals)
    else:
        with pytest.raises((AssertionError, StopIteration)):
            PROBE.assert_log_correlations([path], originals)


@pytest.mark.parametrize('mutation', ['none', 'false_zero_wait', 'budget_as_wait', 'invented_completed_wait'])
def test_queue_timing_guard_uses_actual_as_of_and_first_claim_events(mutation):
    import sqlite3
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        db.executescript('''CREATE TABLE scheduler_queue(run_id TEXT,status TEXT,updated_at TEXT);
            CREATE TABLE runs(run_id TEXT,state TEXT);
            CREATE TABLE scheduler_events(run_id TEXT,event_type TEXT,occurred_at TEXT);
            INSERT INTO runs VALUES('owned-run','RECONCILING');
            INSERT INTO scheduler_queue VALUES('owned-run','RECOVERY','2026-10-02T00:00:00.000Z');
            INSERT INTO scheduler_events VALUES('owned-run','enqueued','2026-10-01T23:59:58.000Z');
            INSERT INTO scheduler_events VALUES('owned-run','claimed','2026-10-01T23:59:59.250Z');
            INSERT INTO scheduler_events VALUES('owned-run','claimed','2026-10-02T00:00:00.000Z');''')
        empty = {'count': 0, 'known_count': 0, 'unknown_count': 0, 'known_total_ms': 0, 'max_ms': None}
        summary = lambda value: {'count': 1, 'known_count': 1, 'unknown_count': 0, 'known_total_ms': value, 'max_ms': value}
        value = {'timing': 'wall_clock_snapshot', 'current_wait_basis': 'scheduler_queue_updated_at',
            'completed_wait_source': 'scheduler_events_first_enqueued_to_first_claimed',
            'current_pending_wait_age_ms': {'QUEUED': empty, 'RECOVERY': summary(1500)},
            'wait_state_revision_age_ms': {state: empty for state in ('WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED')},
            'completed_enqueue_to_first_claim_ms': summary(1250)}
        if mutation == 'false_zero_wait':
            value['current_pending_wait_age_ms']['RECOVERY'] = summary(0)
        elif mutation == 'budget_as_wait':
            value['timing'] = 'persisted_only'
        elif mutation == 'invented_completed_wait':
            value['completed_enqueue_to_first_claim_ms'] = summary(2000)
        if mutation == 'none':
            PROBE.assert_queue_timings(value, db, '2026-10-02T00:00:01.500Z')
        else:
            with pytest.raises(AssertionError):
                PROBE.assert_queue_timings(value, db, '2026-10-02T00:00:01.500Z')


@pytest.mark.parametrize('mutation', ['none', 'missing_transport', 'untrusted_request_id', 'non_uuid4'])
def test_http_response_ids_require_both_trusted_uuid4_headers(mutation):
    headers = {'x-request-id': str(uuid4()), 'x-transport-request-id': str(uuid4())}
    if mutation == 'missing_transport':
        headers.pop('x-transport-request-id')
    elif mutation == 'untrusted_request_id':
        headers['x-request-id'] = PROBE.CANARIES['framework_payload']
    elif mutation == 'non_uuid4':
        headers['x-request-id'] = '00000000-0000-1000-8000-000000000000'
    response = SimpleNamespace(status_code=202, headers=headers)
    if mutation == 'none':
        assert PROBE.response_ids(response, operation_id='owned-operation')['operation_id'] == 'owned-operation'
    else:
        with pytest.raises((AssertionError, KeyError, ValueError)):
            PROBE.response_ids(response)


@pytest.mark.parametrize('mutation', ['none', 'reused_transport', 'wrong_business_id', 'wrong_operation', 'wrong_event', 'missing_response'])
def test_http_log_guard_requires_committed_control_bindings_and_unique_attempts(tmp_path, mutation):
    business, first, second = str(uuid4()), str(uuid4()), str(uuid4())
    expected = [{'request_id': business, 'transport_request_id': attempt, 'http_status': 202,
        'method': 'POST', 'task_id': 'owned-task', 'run_id': 'owned-run',
        'operation_id': 'owned-operation', 'event_id': 4} for attempt in (first, second)]
    records = [{'event': 'api_request', **value} for value in expected]
    if mutation == 'reused_transport':
        records[1]['transport_request_id'] = first
    elif mutation == 'wrong_business_id':
        records[1]['request_id'] = str(uuid4())
    elif mutation == 'wrong_operation':
        records[1]['operation_id'] = 'another-operation'
    elif mutation == 'wrong_event':
        records[1]['event_id'] = 5
    elif mutation == 'missing_response':
        records.pop()
    path = tmp_path / 'api.jsonl'
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))
    if mutation == 'none':
        PROBE.assert_http_log_bindings([path], expected)
    else:
        with pytest.raises(AssertionError):
            PROBE.assert_http_log_bindings([path], expected)


def test_artifact_manifest_survives_private_input_removal_by_clean_install_copy(tmp_path):
    import hashlib
    import shutil
    origin, copied = tmp_path / 'original', tmp_path / 'copied'
    origin.mkdir()
    for directory in ('.security', '.private', 'evidence'):
        (origin / directory).mkdir()
        (origin / directory / 'owned-input').write_text(PROBE.CANARIES['api_token'])
    (origin / 'report.json').write_text('unhashed manifest')
    hashes = PROBE.artifact_hashes(origin)
    assert list(hashes) == ['evidence/owned-input']
    shutil.copytree(origin, copied, ignore=shutil.ignore_patterns('.security', '.private'))
    assert all(hashlib.sha256((copied / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())


def invocation_outcome():
    records, originals = {}, {}
    for run_id, state in (('obs-success', 'SUCCEEDED'), ('obs-failed', 'FAILED')):
        cancelled = run_id == 'obs-failed'
        checkpoint = {'checkpoint_id': str(uuid4()), 'state_version': 1,
            'business_event_id': 1, 'progress_id': 1, 'completed': not cancelled}
        records[run_id] = {'finally_exited': True, 'durable_state': state, 'state_version': 2,
            'queue_status': 'FINISHED', 'blocked_reason': 'recovery_limit' if cancelled else None,
            'returned': not cancelled, 'exception': {'type': 'CancelledError', 'code': None,
                'status': None, 'cancelled': True} if cancelled else None,
            'framework_checkpoint': checkpoint, 'settlement_error': None}
        _, original = diagnostics()
        original['progress'][0]['state_version'] = 1
        originals[run_id] = original
    return {'error': None, 'cleanup_errors': [], 'worker_failed': False,
            'returned': ['obs-success'], 'invocations': records}, originals


@pytest.mark.parametrize('mutation', ['none', 'normal_failed_return', 'unsettled_business', 'arbitrary_error',
                                     'success_cancelled', 'false_normal_return', 'missing_finally',
                                     'fabricated_terminal_saver', 'wrong_saver_event', 'queue_still_active'])
def test_failure_wait_guard_records_actual_budget_cancellation_without_fabricating_return_or_saver(mutation):
    outcome, originals = invocation_outcome()
    failed, success = outcome['invocations']['obs-failed'], outcome['invocations']['obs-success']
    if mutation == 'normal_failed_return':
        failed.update(returned=True, exception=None)
        outcome['returned'].append('obs-failed')
    elif mutation == 'unsettled_business':
        failed['durable_state'] = 'RUNNING'
    elif mutation == 'arbitrary_error':
        failed['exception'] = {'type': 'RuntimeError', 'code': None, 'status': None, 'cancelled': False}
    elif mutation == 'success_cancelled':
        success.update(returned=False, exception=deepcopy(failed['exception']))
        outcome['returned'].clear()
    elif mutation == 'false_normal_return':
        failed['returned'] = True
        outcome['returned'].append('obs-failed')
    elif mutation == 'missing_finally':
        failed['finally_exited'] = False
    elif mutation == 'fabricated_terminal_saver':
        failed['framework_checkpoint'].update(state_version=2, completed=True)
    elif mutation == 'wrong_saver_event':
        failed['framework_checkpoint']['business_event_id'] = 2
    elif mutation == 'queue_still_active':
        failed['queue_status'] = 'ACTIVE'
    if mutation in ('none', 'normal_failed_return'):
        PROBE.assert_invocation_outcomes(outcome)
        PROBE.assert_framework_refs(outcome['invocations'], originals)
    else:
        with pytest.raises((AssertionError, KeyError)):
            PROBE.assert_invocation_outcomes(outcome)
            PROBE.assert_framework_refs(outcome['invocations'], originals)


def framework_log_history():
    history, logs = {}, []
    for run_id in PROBE.RUNS:
        for version in (1, 2):
            checkpoint_id = str(uuid4())
            saved = {'thread_id': run_id, 'run_id': run_id, 'checkpoint_ns': '', 'checkpoint_id': checkpoint_id,
                'graph_version': 'owned-graph', 'state_schema_version': 'owned-schema',
                'state_version': version, 'business_event_id': version, 'progress_id': version,
                'business_checkpoint_id': 'owned-business-' + str(version), 'completed': version == 2,
                'verified_summary_refs': []}
            history[(run_id, checkpoint_id)] = saved
            if version == 1:
                # The newer saved row deliberately differs; a legitimate older
                # log must be compared with its own ID, never with the head.
                logs.append({'event': 'graph_checkpoint_saved', 'thread_id': run_id, 'run_id': run_id,
                    'checkpoint_id': checkpoint_id, 'graph_version': 'owned-graph', 'state_schema_version': 'owned-schema',
                    'state_version': version, 'event_id': version, 'progress_id': version,
                    'business_checkpoint_id': 'owned-business-' + str(version)})
    return logs, history


@pytest.mark.parametrize('mutation', ['none', 'invented_id', 'wrong_run', 'stored_wrong_run', 'wrong_namespace',
                                     'wrong_version', 'wrong_event', 'wrong_progress', 'wrong_business_checkpoint',
                                     'wrong_graph', 'wrong_schema'])
def test_checkpoint_log_guard_matches_its_exact_historical_saver_row(tmp_path, mutation):
    logs, history = framework_log_history()
    if mutation == 'invented_id':
        logs[0]['checkpoint_id'] = str(uuid4())
    elif mutation == 'wrong_run':
        logs[0]['thread_id'] = 'obs-failed'
    elif mutation == 'stored_wrong_run':
        history[(logs[0]['run_id'], logs[0]['checkpoint_id'])]['run_id'] = 'obs-failed'
    elif mutation == 'wrong_namespace':
        history[(logs[0]['run_id'], logs[0]['checkpoint_id'])]['checkpoint_ns'] = 'another-subgraph'
    elif mutation == 'wrong_version':
        logs[0]['state_version'] = 2
    elif mutation == 'wrong_event':
        logs[0]['event_id'] = 2
    elif mutation == 'wrong_progress':
        logs[0]['progress_id'] = 2
    elif mutation == 'wrong_business_checkpoint':
        logs[0]['business_checkpoint_id'] = 'owned-business-2'
    elif mutation == 'wrong_graph':
        logs[0]['graph_version'] = 'another-graph'
    elif mutation == 'wrong_schema':
        logs[0]['state_schema_version'] = 'another-schema'
    path = tmp_path / 'worker.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in logs))
    if mutation == 'none':
        assert PROBE.assert_logged_framework_refs([path], history)['stored_checkpoints'] == 4
    else:
        with pytest.raises((AssertionError, KeyError)):
            PROBE.assert_logged_framework_refs([path], history)


def test_framework_history_reader_does_not_create_a_missing_database(tmp_path):
    import sqlite3
    path = tmp_path / 'absent-graph.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        PROBE.read_framework_history(path)
    assert not path.exists()
