"""Read-only diagnostics expose references and known totals, never raw facts."""
import hashlib
import json

import pytest

from webagent.db import connect, migrate, transaction
from webagent.db.repository import canonical_json, utc_text
from webagent.errors import BusinessError
from webagent.observability.store import ObservabilityStore
from webagent.scheduler.models import Resource
from webagent.state import transition
from test_graph_store import setup_run
from test_gateway_store import fixture, observe, action
from test_budgets import setup as budget_setup, add_run


def persisted_model(path, request_id, *, run_id='run-1', usage=None, cost=None,
                    version=None, currency=None, status='VALID', model_id='deepseek-flash'):
    stamp = utc_text()
    with connect(path) as db, transaction(db):
        run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        call_id = 'call-' + request_id
        db.execute('INSERT INTO model_generations VALUES(?,?,?,?,?,?,?)',
            (call_id, run_id, run['model_config_sha256'], 'm1-05-model-v1', 0, run['state_version'], stamp))
        db.execute('INSERT INTO model_attempts(request_id,call_id,run_id,attempt_number,started_at) VALUES(?,?,?,?,?)',
            (request_id, call_id, run_id, 1, stamp))
        if status != 'STARTED':
            record = {'run_id': run_id, 'request_id': request_id, 'provider_request_id': 'synthetic-provider',
                'provider': 'deepseek', 'model_id': model_id, 'config_sha256': run['model_config_sha256'],
                'prompt_version': 'm1-05-model-v1', 'usage': usage or {
                    'input_tokens': None, 'output_tokens': None, 'image_units': None, 'provider_usage': {}},
                'duration_ms': 10, 'format_repairs': 0,
                'error_class': 'provider_error' if status == 'ERROR' else None,
                'price_version': version, 'estimated_cost': cost}
            if currency is not None:
                record['cost_currency'] = currency
            db.execute('UPDATE model_attempts SET status=?,finished_at=?,record_json=? WHERE request_id=?',
                (status, stamp, canonical_json(record), request_id))
    return request_id


def fingerprint(path):
    with connect(path) as db:
        return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()


def prepared(tmp_path):
    graph, scheduler, token, _, _, _ = setup_run(tmp_path)
    checkpoint = graph.checkpoint_observation('run-1', 'snapshot-1', expected_state_version=1)
    progress = graph.record_progress('run-1', 'observe', snapshot_id='snapshot-1', expected_state_version=1)
    return graph.path, checkpoint, progress


def test_event_graph_checkpoint_and_independent_model_cursor_are_persisted_references(tmp_path):
    path, checkpoint, progress = prepared(tmp_path)
    persisted_model(path, 'request-1', usage={'input_tokens': 12, 'output_tokens': 7, 'image_units': None})
    persisted_model(path, 'request-2', status='STARTED')
    store = ObservabilityStore(path)
    first = store.diagnostics('run-1', limit=1)
    assert first['run']['thread_id'] == first['run']['run_id'] == 'run-1'
    assert first['run']['graph_version'] == 'browser-loop-v1'
    assert first['events'][0]['graph'][0]['node'] == 'observe'
    assert first['events'][0]['graph'][0]['checkpoint_id'] == checkpoint.checkpoint_id
    assert first['events'][0]['graph'][0]['progress_id'] == progress['progress_id']
    assert first['model_attempts'][0]['request_id'] == 'request-1'
    assert first['models_truncated']
    second = store.diagnostics('run-1', after=first['next_event_cursor'],
        model_after=first['next_model_cursor'], limit=1)
    assert not second['events'] and second['model_attempts'][0]['request_id'] == 'request-2'
    assert second['model_attempts'][0]['usage']['input_tokens'] is None
    assert not second['model_attempts'][0]['usage_known'] and not second['models_truncated']
    assert first['model_summary']['usage']['input_tokens'] == {
        'known_total': 12, 'known_attempts': 1, 'complete': False}


def test_step_operation_and_error_category_are_safe_without_action_arguments(database):
    f = fixture(database, write=True)
    observe(f)
    f.store.prepare(f.token, action(f, kind='input', write=True), f.binding, external_write=True)
    f.store.finish(f.token, 'step-1', error_code='BROWSER_ACTION_FAILED')
    result = ObservabilityStore(database).diagnostics(f.token.run_id)
    events = [event for event in result['events'] if event['step_id']]
    assert events and all(event['operation_id'] == 'operation-step-1' for event in events)
    assert events[-1]['current_step_status'] == 'UNKNOWN'
    assert events[-1]['error_class'] == 'browser'
    text = json.dumps(result)
    assert all(value not in text for value in ('SECRET', '127.0.0.1', 'action_json', 'actual_result',
        'artifact_path', 'source_url', 'identity_ref', 'expected_change', 'visible_excerpt'))


def test_decimal_costs_are_exact_versioned_and_never_mix_currencies(tmp_path):
    path, _, _ = prepared(tmp_path)
    usage = {'input_tokens': 10, 'output_tokens': 2, 'image_units': None}
    persisted_model(path, 'request-1', usage=usage, cost='0.1000000000000001', version='price-one', currency='USD')
    persisted_model(path, 'request-2', usage=usage, cost='0.2000000000000002', version='price-one', currency='USD')
    persisted_model(path, 'request-3', usage=usage, cost='1.1', version='price-one', currency='CNY')
    persisted_model(path, 'request-4', usage=usage)
    persisted_model(path, 'request-5', status='ERROR')
    summary = ObservabilityStore(path).metrics()['model']
    groups = {(row['price_version'], row['cost_currency']): row for row in summary['costs_by_price_version']}
    assert groups[('price-one', 'USD')]['estimated_cost'] == '0.3000000000000003'
    assert groups[('price-one', 'CNY')]['estimated_cost'] == '1.1'
    assert groups[(None, None)]['estimated_cost'] is None
    assert groups[(None, None)]['unknown_attempts'] == 2
    assert summary['unknown_usage_attempts'] == 1
    assert summary['usage']['input_tokens'] == {'known_total': 40, 'known_attempts': 4, 'complete': False}


@pytest.mark.parametrize('tiny_first', [False, True])
def test_legacy_cost_extremes_preserve_smallest_amount_in_exact_group_total(tmp_path, tiny_first):
    path, _, _ = prepared(tmp_path)
    large, tiny = '9' * 100, '0.' + '0' * 97 + '1'
    amounts = (tiny, large) if tiny_first else (large, tiny)
    for index, amount in enumerate(amounts):
        persisted_model(path, 'precision-' + str(index), cost=amount,
                        version='price-legacy-boundary', currency='USD')
    store = ObservabilityStore(path)
    metrics = store.metrics()['model']
    diagnostics = store.diagnostics('run-1', limit=1)['model_summary']
    for summary in (metrics, diagnostics):
        assert summary['aggregation'] == 'streaming_exact_totals'
        assert summary['costs_by_price_version'] == [{
            'price_version': 'price-legacy-boundary', 'cost_currency': 'USD',
            'known_attempts': 2, 'unknown_attempts': 0, 'complete': True,
            'estimated_cost': large + '.' + '0' * 97 + '1'}]


def test_unknown_usage_cost_and_inflight_attempts_are_not_zero(tmp_path):
    path, _, _ = prepared(tmp_path)
    persisted_model(path, 'request-unknown', status='ERROR')
    persisted_model(path, 'request-inflight', status='STARTED')
    summary = ObservabilityStore(path).metrics()['model']
    assert summary['attempts'] == 2 and summary['unknown_usage_attempts'] == 2
    assert summary['status_counts'] == {'ERROR': 1, 'STARTED': 1}
    assert summary['usage']['input_tokens']['known_total'] is None
    assert summary['costs_by_price_version'][0]['estimated_cost'] is None


def test_queries_do_not_settle_budget_revoke_lease_or_clear_unknown(database):
    f = fixture(database, write=True)
    observe(f)
    f.store.prepare(f.token, action(f, kind='input', write=True), f.binding, external_write=True)
    f.store.finish(f.token, 'step-1')
    f.clock.advance(31)
    before = fingerprint(database)
    store = ObservabilityStore(database, clock=f.clock.utcnow)
    assert store.diagnostics(f.token.run_id)['budget']['timing'] == 'persisted_only'
    metrics = store.metrics()
    assert metrics['writes']['status_counts'] == {'UNKNOWN': 1}
    assert metrics['writes']['quarantines'] == 2
    assert metrics['queue']['status_counts'] == {'ACTIVE': 1}
    assert store.worker_health()['status'] == 'stale'
    assert fingerprint(database) == before


def test_worker_lease_health_is_independent_of_task_outcome(database):
    f = fixture(database)
    store = ObservabilityStore(database, clock=f.clock.utcnow)
    ready = store.worker_health()
    assert ready['ready'] and ready['source'] == 'persisted_scheduler_heartbeat'
    assert not ready['tasks_success_implied']
    f.scheduler.finish(f.token, 'FAILED')
    assert store.worker_health()['ready']
    f.scheduler.stop_worker(f.token.worker_id, f.token.worker_generation)
    stopped = store.worker_health()
    assert not stopped['ready'] and stopped['status'] == 'stopped'


def test_old_or_missing_schema_is_typed_unavailable_without_migration_or_creation(tmp_path):
    old = tmp_path / 'old.sqlite3'
    migrate(old, target=9)
    before = fingerprint(old)
    for method in ('metrics', 'worker_health'):
        with pytest.raises(BusinessError) as error:
            getattr(ObservabilityStore(old), method)()
        assert error.value.code == 'SERVICE_UNAVAILABLE' and error.value.status == 503
    assert fingerprint(old) == before
    missing = tmp_path / 'missing.sqlite3'
    with pytest.raises(BusinessError):
        ObservabilityStore(missing).metrics()
    assert not missing.exists()


def test_malicious_free_text_metadata_is_not_forwarded(tmp_path):
    path, _, _ = prepared(tmp_path)
    persisted_model(path, 'sk-privatevalue12345', model_id='https://user:password@invalid/token')
    transition(path, run_id='run-1', expected_state_version=1, target='FAILED',
               blocked_reason='Bearer sk-privatevalue12345')
    result = ObservabilityStore(path).diagnostics('run-1')
    text = json.dumps(result)
    assert 'sk-privatevalue12345' not in text and 'https://' not in text and 'password' not in text
    assert result['run']['blocked_reason'] == 'other'
    assert result['model_attempts'][0]['request_id'].startswith('redacted-')


def test_metrics_report_unimplemented_quality_as_unavailable(tmp_path):
    path, _, _ = prepared(tmp_path)
    metrics = ObservabilityStore(path).metrics()
    assert metrics['scenarios'] == [{'scenario': 'finance', 'state': 'RUNNING', 'count': 1}]
    assert metrics['budget']['model_calls_used'] == 0
    assert metrics['unavailable_metrics']
    assert all(row['reason'] == 'not_implemented' for row in metrics['unavailable_metrics'])
    assert not any(key in metrics for key in ('success_rate', 'quality_rate', 'login_rate'))
    assert {'false_success_rate','flow_hit_rate','flow_invalidations','monitoring_start_deviation',
        'monitoring_gaps','benchmark_recovery_failures'} <= {row['name'] for row in metrics['unavailable_metrics']}


def test_model_summary_streams_multiple_batches_without_losing_cost(tmp_path):
    path, _, _ = prepared(tmp_path)
    for index in range(257):
        persisted_model(path, 'request-' + str(index), usage={'input_tokens': 1, 'output_tokens': 0},
                        cost='0.01', version='price-many', currency='USD')
    summary = ObservabilityStore(path).metrics()['model']
    assert summary['attempts'] == summary['scanned_attempts'] == 257
    assert summary['usage']['input_tokens']['known_total'] == 257
    assert summary['costs_by_price_version'][0]['estimated_cost'] == '2.57'
    assert not summary['truncated']


def test_diagnostics_use_one_snapshot_while_a_writer_commits(tmp_path, monkeypatch):
    path, _, _ = prepared(tmp_path)
    original = ObservabilityStore._model_summary
    def concurrent_writer(db, run_id=None):
        transition(path, run_id='run-1', expected_state_version=1, target='FAILED')
        persisted_model(path, 'concurrently-finished', status='ERROR')
        return original(db, run_id)
    monkeypatch.setattr(ObservabilityStore, '_model_summary', staticmethod(concurrent_writer))
    result = ObservabilityStore(path).diagnostics('run-1')
    assert result['run']['state'] == 'RUNNING'
    assert result['model_attempts'] == [] and result['model_summary']['attempts'] == 0
    assert result['events'][-1]['current_state'] == 'RUNNING'
    with connect(path) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id="run-1"').fetchone()[0] == 'FAILED'
        assert db.execute('SELECT count(*) FROM model_attempts').fetchone()[0] == 1


def test_price_group_bound_fails_closed_instead_of_returning_partial_cost(tmp_path, monkeypatch):
    path, _, _ = prepared(tmp_path)
    persisted_model(path, 'request-1', cost='1', version='price-one', currency='USD')
    persisted_model(path, 'request-2', cost='2', version='price-two', currency='USD')
    monkeypatch.setattr('webagent.observability.store.MAX_PRICE_GROUPS', 1)
    with pytest.raises(BusinessError) as error:
        ObservabilityStore(path).metrics()
    assert error.value.status == 503


def test_queue_pending_age_and_completed_first_claim_wait_are_separate_exact_totals(database):
    clock, _, scheduler, generation, _ = budget_setup(database)
    add_run(database, 'second')
    scheduler.enqueue('second', [Resource.site_identity('local-fixture','second'),
        Resource.browser_context('second')], expected_state_version=0)
    clock.advance(2.5009)
    store = ObservabilityStore(database, clock=clock.utcnow)
    first = store.metrics()['queue']
    assert first['timing'] == 'wall_clock_snapshot'
    assert first['current_pending_wait_age_ms']['QUEUED'] == {
        'count': 1, 'known_count': 1, 'unknown_count': 0, 'known_total_ms': 2500, 'max_ms': 2500}
    assert first['completed_enqueue_to_first_claim_ms']['count'] == 1
    assert first['completed_enqueue_to_first_claim_ms']['known_total_ms'] == 0
    clock.advance(.6241)
    assert scheduler.claim('worker', generation).run_id == 'second'
    last = store.metrics()['queue']
    assert last['current_pending_wait_age_ms']['QUEUED']['count'] == 0
    assert last['completed_enqueue_to_first_claim_ms'] == {
        'count': 2, 'known_count': 2, 'unknown_count': 0, 'known_total_ms': 3125, 'max_ms': 3125}


@pytest.mark.parametrize('target', ['WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED'])
def test_business_wait_states_are_not_mixed_into_scheduler_queue_age(database, target):
    clock, _, scheduler, _, token = budget_setup(database)
    scheduler.defer(token, target)
    clock.advance(1.0005)
    before = fingerprint(database)
    queue = ObservabilityStore(database, clock=clock.utcnow).metrics()['queue']
    assert queue['current_pending_wait_age_ms']['QUEUED']['count'] == 0
    assert queue['current_pending_wait_age_ms']['RECOVERY']['count'] == 0
    assert queue['wait_state_revision_age_ms'][target]['known_total_ms'] == 1000
    assert queue['wait_state_revision_age_ms'][target]['count'] == 1
    assert fingerprint(database) == before


def test_recovery_attempt_and_counter_metrics_use_budget_authority_not_receipt_phases(database):
    clock, budgets, _, _, token = budget_setup(database)
    for index in range(3):
        budgets.consume(token, kind='recovery', attempt_id='recovery-' + str(index),
                        site_id='local-fixture', subgoal='bounded-goal', obstacle_type='locator_changed')
    budgets.consume(token, kind='action', attempt_id='content-open', content_page=True)
    before = fingerprint(database)
    metrics = ObservabilityStore(database, clock=clock.utcnow).metrics()
    assert metrics['recovery']['budget_attempt_count'] == 3
    assert metrics['recovery']['budget_counter_total'] == 3
    assert metrics['recovery']['counter_groups'] == 1
    assert metrics['recovery']['phase_counts'] == {}
    assert metrics['recovery']['phase_counts_are_receipts']
    assert metrics['budget']['content_pages_used'] == 1
    assert fingerprint(database) == before


def test_current_recovery_pending_age_uses_revision_without_recharging_budget(database):
    clock, _, scheduler, _, token = budget_setup(database)
    scheduler.abandon(token)
    clock.advance(2.001)
    before = fingerprint(database)
    queue = ObservabilityStore(database, clock=clock.utcnow).metrics()['queue']
    assert queue['current_pending_wait_age_ms']['RECOVERY'] == {
        'count': 1, 'known_count': 1, 'unknown_count': 0, 'known_total_ms': 2001, 'max_ms': 2001}
    assert queue['current_pending_wait_age_ms']['QUEUED']['count'] == 0
    assert fingerprint(database) == before


def test_partial_and_fully_unknown_token_usage_have_separate_counts(tmp_path):
    path, _, _ = prepared(tmp_path)
    persisted_model(path, 'request-partial', usage={'input_tokens': 5, 'output_tokens': None})
    persisted_model(path, 'request-image-only', usage={'input_tokens': None, 'output_tokens': None, 'image_units': 2})
    result = ObservabilityStore(path).diagnostics('run-1')
    summary = result['model_summary']
    assert summary['partial_usage_attempts'] == 1 and summary['unknown_usage_attempts'] == 1
    assert summary['usage']['input_tokens']['known_total'] == 5
    assert summary['usage']['output_tokens']['known_total'] is None
    assert result['model_attempts'][0]['usage_partial'] and result['model_attempts'][0]['usage_known']
    assert not result['model_attempts'][1]['usage_known']


def test_metrics_obtain_as_of_once_for_the_entire_read_snapshot(database):
    clock, _, _, _, _ = budget_setup(database)
    calls = []
    def once():
        calls.append(1)
        value = clock.utcnow()
        clock.advance(1)
        return value
    expected = utc_text(clock.utcnow())
    result = ObservabilityStore(database, clock=once).metrics()
    assert result['as_of'] == expected and calls == [1]
