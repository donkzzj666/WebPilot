"""Read-only projections of durable business facts, never execution authority.

Every response uses one SQLite read snapshot. No model, browser, credential,
artifact read, budget settlement, migration, or framework saver is invoked.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from ..db.repository import utc_text
from ..errors import BusinessError
from ..evidence.redaction import TextRedactor

MAX_PAGE = 1000
MAX_PRICE_GROUPS = 1000
_ID = re.compile(r'[A-Za-z0-9_-]{1,200}')
_VERSION = re.compile(r'[A-Za-z0-9_.-]{1,200}')
_PRICE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?')
_STATES = frozenset(('QUEUED', 'RUNNING', 'VERIFYING', 'WAITING_CI', 'WAITING_SITE',
    'WAITING_HANDOFF', 'PAUSED', 'RECONCILING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'))
_EVENTS = frozenset(('state_changed', 'action_recorded', 'wait_registered', 'result_ready',
    'operation_requested', 'operation_completed'))
_ACTIONS = frozenset(('navigate', 'click', 'input', 'keypress', 'select', 'scroll',
    'switch_tab', 'read_visible', 'screenshot', 'download_attachment'))
_PHASES = frozenset(('reconcile', 'observe', 'decide', 'dispatch', 'confirm', 'verify',
    'aggregate', 'wait', 'recover', 'stopped'))
_DIAGNOSTICS = frozenset(('evidence_required', 'input_required', 'verification_incomplete',
    'invalid_model_output', 'model_failed', 'page_changed', 'budget_exceeded', 'recovery_required',
    'run_finished', 'configuration_required', 'identity_recheck_required', 'write_adapter_unavailable',
    'graph_preparation_failed'))
_REASONS = frozenset(('resource_conflict', 'context_capacity', 'worker_restarted', 'lease_expired',
    'scope_expanded', 'waiting', 'cancelled', 'finished', 'reconciled', 'executor_interrupted',
    'graph_state_invalid', 'graph_version_mismatch', 'contract_mismatch', 'graph_ahead', 'event_missing',
    'event_version_mismatch', 'checkpoint_mismatch', 'snapshot_missing', 'progress_mismatch',
    'summary_mismatch', 'evidence_missing', 'evidence_corrupt', 'unknown_write', 'human_control',
    'object_mismatch', 'object_version_mismatch', 'identity_mismatch', 'budget_exhausted',
    'recovery_not_completed', 'session_unavailable', 'source_scope_mismatch', 'proof_missing',
    'active_time', 'ci_wait', 'handoff', 'site_wait_exceeds_budget', 'action_limit',
    'content_page_limit', 'recovery_limit'))
_ERRORS = {'TIMEOUT': 'timeout', 'BUDGET_EXCEEDED': 'budget', 'RESOURCE_CONFLICT': 'conflict',
    'STATE_CONFLICT': 'conflict', 'INVALID_PARAMETER': 'input', 'FORBIDDEN': 'boundary',
    'INPUT_BLOCKED': 'boundary', 'EVIDENCE_MISSING': 'evidence', 'EVIDENCE_CORRUPT': 'evidence',
    'EVIDENCE_UNAVAILABLE': 'evidence', 'EVIDENCE_EXPIRED': 'evidence',
    'BROWSER_ACTION_FAILED': 'browser', 'SERVICE_UNAVAILABLE': 'service',
    'STORAGE_FULL': 'storage', 'SESSION_LOST': 'browser', 'NETWORK_BLOCKED': 'boundary'}
_MODEL_ERRORS = frozenset(('timeout', 'rate_limit', 'invalid_credentials', 'invalid_output', 'provider_error'))
_REQUIRED = frozenset(('runs', 'contracts', 'task_events', 'run_budgets', 'budget_timers',
    'model_attempts', 'graph_progress', 'steps', 'gateway_attempts', 'scheduler_queue',
    'scheduler_workers', 'scheduler_events', 'budget_attempts', 'graph_recoveries',
    'write_intents', 'evidence', 'evidence_availability'))


def _unavailable():
    return BusinessError('SERVICE_UNAVAILABLE', 'Read-only diagnostics are unavailable',
                         status=503, field='observability_schema')


def _safe(value, *, version=False):
    if value is None:
        return None
    if (type(value) is str and (bool((_VERSION if version else _ID).fullmatch(value)))
            and not TextRedactor().contains_sensitive(value)):
        return value
    # Preserve correlatability without forwarding arbitrary identifiers as text.
    return 'redacted-' + hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]


def _enum(value, allowed):
    return value if type(value) is str and value in allowed else ('other' if value is not None else None)


def _counter(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if type(value) is str else None
        return parsed.astimezone(timezone.utc) if parsed is not None and parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _timestamp(value):
    parsed = _time(value)
    return utc_text(parsed) if parsed is not None else None


def _elapsed_ms(start, end):
    if start is None or end is None or end < start:
        return None
    delta = end - start
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def _duration_bucket():
    return {'count': 0, 'known_count': 0, 'unknown_count': 0, 'known_total_ms': 0, 'max_ms': None}


def _add_duration(bucket, value):
    bucket['count'] += 1
    if value is None:
        bucket['unknown_count'] += 1
        if not bucket['known_count']:
            bucket['known_total_ms'] = None
        return
    bucket['known_count'] += 1
    bucket['known_total_ms'] = (bucket['known_total_ms'] or 0) + value
    bucket['max_ms'] = value if bucket['max_ms'] is None else max(bucket['max_ms'], value)


def _page(after, limit):
    if (type(after) is not int or not 0 <= after < 2**63 - 1
            or type(limit) is not int or not 1 <= limit <= MAX_PAGE):
        raise BusinessError('INVALID_PARAMETER', 'Invalid diagnostic cursor or page size')


def _record(raw):
    try:
        value = json.loads(raw) if raw is not None and len(raw) <= 32768 else None
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _model(row):
    record = _record(row['record_json'])
    usage = record.get('usage') if isinstance(record.get('usage'), dict) else {}
    values = {key: _counter(usage.get(key)) for key in ('input_tokens', 'output_tokens', 'image_units')}
    price_version = record.get('price_version')
    cost = record.get('estimated_cost')
    cost = cost if (price_version is not None and type(cost) is str and len(cost) <= 100
                    and _PRICE.fullmatch(cost)) else None
    return {'model_cursor': row['model_cursor'], 'request_id': _safe(row['request_id']),
        'call_id': _safe(row['call_id']), 'started_at': _timestamp(row['started_at']),
        'finished_at': _timestamp(row['finished_at']),
        'status': _enum(row['status'], frozenset(('STARTED', 'VALID', 'INVALID', 'ERROR', 'CANCELLED'))),
        'provider': _enum(record.get('provider'), frozenset(('deepseek',))),
        'model_id': _safe(record.get('model_id'), version=True),
        'prompt_version': _safe(record.get('prompt_version'), version=True),
        'duration_ms': _counter(record.get('duration_ms')), 'attempt_number': row['attempt_number'],
        'error_class': _enum(record.get('error_class'), _MODEL_ERRORS),
        'usage': values, 'usage_known': values['input_tokens'] is not None or values['output_tokens'] is not None,
        'usage_partial': (values['input_tokens'] is None) != (values['output_tokens'] is None),
        'usage_complete': values['input_tokens'] is not None and values['output_tokens'] is not None,
        'price_version': _safe(price_version, version=True), 'estimated_cost': cost,
        'cost_currency': record.get('cost_currency') if record.get('cost_currency') in ('USD', 'CNY') else None,
        'cost_known': cost is not None}


class ObservabilityStore:
    def __init__(self, path: Path, *, clock=None):
        value = Path(path)
        self.path = value if value.suffix in ('.sqlite', '.sqlite3', '.db') else value / 'business.sqlite3'
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _snapshot(self, *, worker_only=False):
        db = None
        try:
            # mode=ro also refuses a missing database instead of creating it.
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None, timeout=.1)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            remaining = [5000]  # Bound VM work even for global aggregate queries.
            def progress():
                remaining[0] -= 1
                return int(remaining[0] < 0)
            db.set_progress_handler(progress, 1000)
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            required = frozenset(('scheduler_workers',)) if worker_only else _REQUIRED
            if not required <= tables:
                raise _unavailable()
            yield db
        except sqlite3.Error:
            raise _unavailable() from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _budget(db, run_id):
        row = db.execute('''SELECT b.actions_used,b.content_pages_used,b.active_ms,b.ci_wait_ms,
            b.model_calls_used,b.observations_used,b.screenshots_used,b.last_persisted_at,t.mode,t.stop_reason
            FROM run_budgets b LEFT JOIN budget_timers t USING(run_id) WHERE b.run_id=?''', (run_id,)).fetchone()
        if row is None:
            return {'available': False, 'timing': 'persisted_only'}
        result = dict(row)
        result['stop_reason'] = _enum(result['stop_reason'], _REASONS)
        result['last_persisted_at'] = _timestamp(result['last_persisted_at'])
        return {**result, 'available': True, 'timing': 'persisted_only'}

    @staticmethod
    def _model_summary(db, run_id=None):
        where, parameters = ('WHERE run_id=?', (run_id,)) if run_id is not None else ('', ())
        total = db.execute('SELECT count(*) FROM model_attempts ' + where, parameters).fetchone()[0]
        rows = db.execute('''SELECT status,
            json_extract(record_json,'$.usage.input_tokens') AS input_tokens,
            json_extract(record_json,'$.usage.output_tokens') AS output_tokens,
            json_extract(record_json,'$.usage.image_units') AS image_units,
            json_extract(record_json,'$.duration_ms') AS duration_ms,
            json_extract(record_json,'$.price_version') AS price_version,
            json_extract(record_json,'$.cost_currency') AS cost_currency,
            json_extract(record_json,'$.estimated_cost') AS estimated_cost
            FROM model_attempts ''' + where + ' ORDER BY rowid', parameters)
        known = {key: {'total': 0, 'attempts': 0} for key in
                 ('input_tokens', 'output_tokens', 'image_units', 'duration_ms')}
        price_groups, statuses, unknown, partial, scanned = {}, {}, 0, 0, 0
        # This is an explicit whole-ledger aggregate, streamed in fixed-size
        # batches. No float sum or unbounded list silently loses priced calls.
        with localcontext() as context:
            # 100 integer digits + <=19 SQLite row-count growth + 98 fraction
            # digits fit without losing tiny legacy amounts in the same group.
            context.prec = 256
            while batch := rows.fetchmany(256):
                for row in batch:
                    scanned += 1
                    status = _enum(row['status'], frozenset(('STARTED','VALID','INVALID','ERROR','CANCELLED')))
                    statuses[status] = statuses.get(status, 0) + 1
                    values = {key: _counter(row[key]) for key in known}
                    missing = sum(values[key] is None for key in ('input_tokens','output_tokens'))
                    if missing == 2:
                        unknown += 1
                    elif missing == 1:
                        partial += 1
                    for key, count in values.items():
                        if count is not None:
                            known[key]['total'] += count
                            known[key]['attempts'] += 1
                    version = _safe(row['price_version'], version=True)
                    currency = row['cost_currency'] if row['cost_currency'] in ('USD','CNY') else None
                    if (version, currency) not in price_groups and len(price_groups) >= MAX_PRICE_GROUPS:
                        raise _unavailable()
                    group = price_groups.setdefault((version, currency), {'price_version': version,
                        'cost_currency': currency, 'known_attempts': 0, 'unknown_attempts': 0, 'amount': Decimal(0)})
                    cost = row['estimated_cost']
                    if row['price_version'] is not None and type(cost) is str and len(cost) <= 100 and _PRICE.fullmatch(cost):
                        group['known_attempts'] += 1
                        group['amount'] += Decimal(cost)
                    else:
                        group['unknown_attempts'] += 1
        costs = [{**{key: value[key] for key in ('price_version', 'cost_currency', 'known_attempts', 'unknown_attempts')},
            'estimated_cost': format(value['amount'], 'f') if value['known_attempts'] else None,
            'complete': value['unknown_attempts'] == 0 and scanned == total}
            for value in price_groups.values()]
        return {'attempts': total, 'scanned_attempts': scanned, 'aggregation': 'streaming_exact_totals',
            'truncated': False, 'status_counts': statuses,
            'unknown_usage_attempts': unknown, 'partial_usage_attempts': partial, 'usage': {key: {
                'known_total': value['total'] if value['attempts'] else None,
                'known_attempts': value['attempts'], 'complete': value['attempts'] == total and total > 0}
                for key, value in known.items()}, 'costs_by_price_version': costs}

    def diagnostics(self, run_id, *, after=0, model_after=0, limit=100):
        if type(run_id) is not str or not _ID.fullmatch(run_id):
            raise BusinessError('INVALID_PARAMETER', 'Invalid Run identifier')
        _page(after, limit)
        _page(model_after, limit)
        with self._snapshot() as db:
            run = db.execute('''SELECT run_id,task_id,thread_id,graph_version,graph_state_schema_version,
                state,state_version,contract_version,blocked_reason,created_at,started_at,ended_at
                FROM runs WHERE run_id=?''', (run_id,)).fetchone()
            if run is None:
                raise BusinessError('NOT_FOUND', 'Run not found', status=404)
            events = []
            page = db.execute('''SELECT event_id,event_type,state_version,occurred_at,
                json_extract(payload_json,'$.step_id') AS step_id,
                json_extract(payload_json,'$.operation_id') AS operation_id,
                json_extract(payload_json,'$.previous_state') AS previous_state,
                json_extract(payload_json,'$.current_state') AS current_state,
                json_extract(payload_json,'$.attempt_status') AS attempt_status,
                json_extract(payload_json,'$.action_type') AS action_type
                FROM task_events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?''',
                (run_id, after, limit)).fetchall()
            links, graph_truncated = {}, False
            if page:
                ids = [row['event_id'] for row in page]
                linked = db.execute('''SELECT progress_id,business_event_id,phase,checkpoint_id,diagnostic,
                    iteration FROM graph_progress WHERE run_id=? AND business_event_id IN (''' +
                    ','.join('?' for _ in ids) + ') ORDER BY progress_id LIMIT ?',
                    (run_id, *ids, min(limit * 4, 4000) + 1)).fetchall()
                graph_truncated = len(linked) > min(limit * 4, 4000)
                for row in linked[:min(limit * 4, 4000)]:
                    links.setdefault(row['business_event_id'], []).append({
                        'progress_id': row['progress_id'], 'node': _enum(row['phase'], _PHASES),
                        'checkpoint_id': _safe(row['checkpoint_id']), 'iteration': row['iteration'],
                        'diagnostic': _enum(row['diagnostic'], _DIAGNOSTICS)})
            for row in page:
                step = db.execute('''SELECT s.sequence,s.status,s.error_code,g.operation_id,g.action_kind
                    FROM steps s LEFT JOIN gateway_attempts g USING(step_id,run_id)
                    WHERE s.run_id=? AND s.step_id=?''', (run_id, row['step_id'])).fetchone() if row['step_id'] else None
                code = step['error_code'] if step else None
                events.append({'event_id': row['event_id'], 'event_type': _enum(row['event_type'], _EVENTS),
                    'state_version': row['state_version'], 'occurred_at': _timestamp(row['occurred_at']),
                    'previous_state': _enum(row['previous_state'], _STATES),
                    'current_state': _enum(row['current_state'], _STATES),
                    'step_id': _safe(row['step_id']), 'step_sequence': step['sequence'] if step else None,
                    'attempt_status': _enum(row['attempt_status'], frozenset(('INTENT','COMPLETED','FAILED','UNKNOWN'))),
                    'current_step_status': step['status'] if step else None,
                    'operation_id': _safe(step['operation_id'] if step else row['operation_id']),
                    'action_type': _enum(row['action_type'] or (step['action_kind'] if step else None), _ACTIONS),
                    'error_code': code if code in _ERRORS else None,
                    'error_class': _ERRORS.get(code, 'other' if code is not None else None),
                    'graph': links.get(row['event_id'], [])})
            models = [_model(row) for row in db.execute('''SELECT rowid AS model_cursor,
                request_id,call_id,started_at,finished_at,status,attempt_number,
                substr(record_json,1,32769) AS record_json FROM model_attempts
                WHERE run_id=? AND rowid>? ORDER BY rowid LIMIT ?''', (run_id, model_after, limit))]
            value = dict(run)
            for key in ('run_id', 'task_id', 'thread_id'):
                value[key] = _safe(value[key])
            for key in ('graph_version', 'graph_state_schema_version'):
                value[key] = _safe(value[key], version=True)
            value['blocked_reason'] = _enum(value['blocked_reason'], _REASONS)
            for key in ('created_at', 'started_at', 'ended_at'):
                value[key] = _timestamp(value[key])
            return {'schema_version': 'm1-19-diagnostics-v1', 'as_of': utc_text(self.clock()),
                'source': 'persistent_business_ledgers', 'run': value,
                'budget': self._budget(db, run_id), 'events': events, 'model_attempts': models,
                'model_summary': self._model_summary(db, run_id), 'graph_links_truncated': graph_truncated,
                'next_event_cursor': events[-1]['event_id'] if events else after,
                'next_model_cursor': models[-1]['model_cursor'] if models else model_after,
                'events_truncated': bool(page and db.execute('SELECT 1 FROM task_events WHERE run_id=? AND event_id>? LIMIT 1',
                    (run_id, page[-1]['event_id'])).fetchone()),
                'models_truncated': bool(models and db.execute('SELECT 1 FROM model_attempts WHERE run_id=? AND rowid>? LIMIT 1',
                    (run_id, models[-1]['model_cursor'])).fetchone())}

    @staticmethod
    def _counts(db, sql, allowed):
        result = {}
        for row in db.execute(sql):
            key = _enum(row[0], allowed)
            result[key] = result.get(key, 0) + row[1]
        return result

    @staticmethod
    def _queue_waits(db, now):
        pending = {key: _duration_bucket() for key in ('QUEUED', 'RECOVERY')}
        separate = {key: _duration_bucket() for key in
                    ('WAITING_CI', 'WAITING_SITE', 'WAITING_HANDOFF', 'PAUSED')}
        rows = db.execute('''SELECT q.status,q.updated_at,r.state FROM scheduler_queue q
            JOIN runs r USING(run_id) WHERE q.status IN ('QUEUED','RECOVERY')
            OR r.state IN ('WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED')''')
        while batch := rows.fetchmany(256):
            for row in batch:
                bucket = separate.get(row['state']) or pending.get(row['status'])
                if bucket is not None:
                    _add_duration(bucket, _elapsed_ms(_time(row['updated_at']), now))
        completed = _duration_bucket()
        rows = db.execute('''SELECT
            min(CASE WHEN event_type='enqueued' THEN occurred_at END) AS enqueued_at,
            min(CASE WHEN event_type='claimed' THEN occurred_at END) AS first_claimed_at
            FROM scheduler_events WHERE event_type IN ('enqueued','claimed') GROUP BY run_id
            HAVING first_claimed_at IS NOT NULL''')
        while batch := rows.fetchmany(256):
            for row in batch:
                _add_duration(completed, _elapsed_ms(_time(row['enqueued_at']), _time(row['first_claimed_at'])))
        return {'timing': 'wall_clock_snapshot', 'current_wait_basis': 'scheduler_queue_updated_at',
            'current_pending_wait_age_ms': pending, 'wait_state_revision_age_ms': separate,
            'completed_enqueue_to_first_claim_ms': completed,
            'completed_wait_source': 'scheduler_events_first_enqueued_to_first_claimed'}

    @staticmethod
    def _recovery_counts(db):
        row = db.execute('''SELECT count(*) AS counter_groups,
            COALESCE(sum(CASE WHEN j.type='integer' AND j.value>=0 THEN j.value ELSE 0 END),0) AS known_total,
            COALESCE(sum(CASE WHEN j.type='integer' AND j.value>=0 THEN 0 ELSE 1 END),0) AS unknown_groups
            FROM run_budgets b,json_each(b.recovery_counts_json) j''').fetchone()
        return {'budget_attempt_count': db.execute(
            "SELECT count(*) FROM budget_attempts WHERE kind='recovery'").fetchone()[0],
            'budget_counter_total': row['known_total'] if not row['unknown_groups'] else None,
            'known_budget_counter_total': row['known_total'], 'counter_groups': row['counter_groups'],
            'unknown_counter_groups': row['unknown_groups'],
            'count_sources': ['budget_attempts_recovery', 'run_budgets_recovery_counts'],
            'phase_counts_are_receipts': True}

    def metrics(self):
        now = self.clock()
        with self._snapshot() as db:
            budgets = dict(db.execute('''SELECT count(*) AS records,
                COALESCE(sum(actions_used),0) AS actions_used,COALESCE(sum(active_ms),0) AS active_ms,
                COALESCE(sum(content_pages_used),0) AS content_pages_used,
                COALESCE(sum(ci_wait_ms),0) AS ci_wait_ms,COALESCE(sum(model_calls_used),0) AS model_calls_used,
                COALESCE(sum(observations_used),0) AS observations_used,
                COALESCE(sum(screenshots_used),0) AS screenshots_used FROM run_budgets''').fetchone())
            scenarios = [{'scenario': row['scenario'], 'state': row['state'], 'count': row['count']}
                for row in db.execute('''SELECT c.scenario,r.state,count(*) AS count FROM runs r JOIN contracts c
                    ON c.task_id=r.task_id AND c.contract_version=r.contract_version
                    GROUP BY c.scenario,r.state ORDER BY c.scenario,r.state''')]
            actions = [{'action_type': _enum(row['action_kind'], _ACTIONS), 'status': row['status'],
                'count': row['count']} for row in db.execute('''SELECT g.action_kind,s.status,count(*) AS count
                    FROM gateway_attempts g JOIN steps s USING(step_id,run_id)
                    GROUP BY g.action_kind,s.status ORDER BY g.action_kind,s.status''')]
            return {'schema_version': 'm1-19-metrics-v1', 'as_of': utc_text(now),
                'source': 'persistent_business_ledgers', 'aggregation': 'sql_and_streaming_ledger_totals',
                'run_state_counts': self._counts(db, 'SELECT state,count(*) FROM runs GROUP BY state', _STATES),
                'scenarios': scenarios, 'budget': {**budgets, 'timing': 'persisted_only'}, 'actions': actions,
                'queue': {**self._queue_waits(db, now),
                    'status_counts': self._counts(db, 'SELECT status,count(*) FROM scheduler_queue GROUP BY status',
                    frozenset(('QUEUED','ACTIVE','WAITING','RECOVERY','FINISHED'))),
                    'reason_counts': self._counts(db, 'SELECT reason,count(*) FROM scheduler_queue WHERE reason IS NOT NULL GROUP BY reason', _REASONS)},
                'recovery': {**self._recovery_counts(db),
                    'phase_counts': self._counts(db, 'SELECT phase,count(*) FROM graph_recoveries GROUP BY phase',
                    frozenset(('BEGIN','BLOCKED','COMPLETE'))), 'reason_counts': self._counts(db,
                    'SELECT reason,count(*) FROM graph_recoveries WHERE reason IS NOT NULL GROUP BY reason', _REASONS)},
                'writes': {'status_counts': self._counts(db, 'SELECT status,count(*) FROM write_intents GROUP BY status',
                    frozenset(('INTENT','UNKNOWN','NOT_APPLIED','CONFIRMED'))),
                    'quarantines': db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0]},
                'evidence': {'capture_counts': self._counts(db, 'SELECT capture_status,count(*) FROM evidence GROUP BY capture_status',
                    frozenset(('COMPLETE','INCOMPLETE','CORRUPT','MISSING'))), 'availability_counts': self._counts(db,
                    'SELECT status,count(*) FROM evidence_availability GROUP BY status',
                    frozenset(('AVAILABLE','MISSING','CORRUPT','EXPIRED')))},
                'model': self._model_summary(db), 'unavailable_metrics': [
                    {'name': name, 'reason': 'not_implemented'} for name in
                    ('business_quality_rate', 'false_success_rate', 'unauthorized_write_rate',
                     'login_success_rate', 'handoff_success_rate', 'flow_hit_rate', 'flow_invalidations',
                     'monitoring_start_deviation', 'monitoring_gaps', 'benchmark_recovery_failures')]}

    def worker_health(self):
        now = self.clock()
        with self._snapshot(worker_only=True) as db:
            row = db.execute('''SELECT worker_id,generation,state,heartbeat_at,expires_at
                FROM scheduler_workers ORDER BY generation DESC LIMIT 1''').fetchone()
            heartbeat, expiry = (_time(row['heartbeat_at']), _time(row['expires_at'])) if row else (None, None)
            ready = bool(row and row['state'] == 'ACTIVE' and heartbeat is not None and expiry is not None
                         and heartbeat <= now < expiry)
            status = 'ready' if ready else ('not_started' if row is None else
                     'stopped' if row['state'] == 'STOPPED' else 'stale')
            return {'schema_version': 'm1-19-worker-health-v1', 'service': 'worker',
                'source': 'persisted_scheduler_heartbeat', 'as_of': utc_text(now), 'ready': ready,
                'status': status, 'worker_id': _safe(row['worker_id']) if row else None,
                'generation': row['generation'] if row else None, 'state': row['state'] if row else None,
                'heartbeat_at': utc_text(heartbeat) if heartbeat else None,
                'expires_at': utc_text(expiry) if expiry else None,
                'heartbeat_age_ms': max(0, int((now - heartbeat).total_seconds() * 1000)) if heartbeat else None,
                'tasks_success_implied': False}
