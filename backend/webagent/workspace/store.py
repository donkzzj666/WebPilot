"""One bounded SQLite snapshot; never execute, settle, migrate or recover a Run.

Business state remains authoritative. Graph phases, evidence references and
control receipts explain that state without exposing model messages or private
browser captures. Event IDs are global SQLite integers represented as decimal
text at this browser boundary.
"""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3

from pydantic import ValidationError

from ..budgets.store import BudgetStore
from ..controls.store import _public as control_receipt
from ..db.repository import utc_text
from ..errors import BusinessError
from ..state import STATES
from ..tasks.models import TaskContract

MAX_CURSOR = 2**63 - 1
MAX_EVENTS = 100
SAFE_REASONS = frozenset({
    'resource_conflict', 'context_capacity', 'worker_restarted', 'lease_expired',
    'scope_expanded', 'waiting', 'cancelled', 'finished', 'reconciled',
    'executor_interrupted', 'unknown_write', 'human_control', 'budget_exhausted',
    'active_time', 'ci_wait', 'handoff', 'site_wait_exceeds_budget', 'action_limit',
    'content_page_limit', 'recovery_limit', 'configuration_required',
    'identity_recheck_required', 'evidence_required', 'input_required',
    'verification_incomplete', 'invalid_model_output', 'model_failed',
    'page_changed', 'budget_exceeded', 'recovery_required', 'run_finished',
    'write_adapter_unavailable', 'graph_preparation_failed', 'version_conflict',
    'state_conflict', 'terminal_run', 'pause_unavailable', 'resume_unavailable',
    'rate_limit', 'site_blocked', 'deadline_exceeded', 'checkpoint_mismatch',
    'contract_mismatch', 'graph_ahead', 'event_missing', 'event_version_mismatch',
    'snapshot_missing', 'progress_mismatch', 'summary_mismatch', 'evidence_missing',
})
REQUIRED_TABLES = frozenset({
    'tasks', 'runs', 'contracts', 'task_events', 'run_config_snapshots',
    'run_checkpoints', 'graph_progress', 'gateway_page_heads',
    'gateway_observations', 'observations', 'observations_evidence',
    'evidence', 'evidence_artifacts', 'evidence_availability', 'scheduler_queue',
    'run_controls', 'run_budgets', 'budget_timers', 'budget_limits',
})
GRAPH_DIAGNOSTICS = frozenset({
    'evidence_required', 'input_required', 'verification_incomplete', 'invalid_model_output',
    'model_failed', 'page_changed', 'budget_exceeded', 'recovery_required', 'run_finished',
    'configuration_required', 'identity_recheck_required', 'write_adapter_unavailable',
    'graph_preparation_failed',
})


def _identifier(value):
    if (type(value) is not str or not 1 <= len(value) <= 200
            or value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise BusinessError('INVALID_PARAMETER', '工作台标识无效。')
    return value


def _reason(value):
    return value if value is None or value in SAFE_REASONS else 'unrecognized_reason'


def _enum(value, allowed, *, nullable=False):
    if nullable and value is None:
        return value
    if type(value) is not str or value not in allowed:
        raise ValueError('invalid_metadata_enum')
    return value


def _integer(value, *, nullable=False):
    if nullable and value is None:
        return value
    if type(value) is not int or not 0 <= value <= MAX_CURSOR:
        raise ValueError('invalid_metadata_integer')
    return value


def _id_field(value, *, nullable=False):
    if nullable and value is None:
        return value
    try:
        return _identifier(value)
    except BusinessError:
        raise ValueError('invalid_metadata_identifier') from None


@contextmanager
def _snapshot(path):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro',
                                     uri=True, isolation_level=None, timeout=.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            remaining = 5000
            def bounded():
                nonlocal remaining
                remaining -= 1
                return int(remaining < 0)
            db.set_progress_handler(bounded, 1000)
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if not REQUIRED_TABLES <= tables:
                raise ValueError('workspace_storage_not_ready')
            yield db
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OSError, ValidationError):
        raise BusinessError('STORAGE_UNAVAILABLE', '暂时无法读取执行工作台，请稍后刷新。', status=503) from None


def _event(row, *, task_id, run_id):
    if row['task_id'] != task_id or row['run_id'] != run_id:
        raise ValueError('event_binding_mismatch')
    result = {key: row[key] for key in ('task_id', 'run_id', 'event_type', 'state_version', 'occurred_at')}
    result['event_id'] = str(row['event_id'])
    if len(row['payload_json']) > 65536:
        raise ValueError('unbounded_event_payload')
    payload = json.loads(row['payload_json'])
    if type(payload) is not dict:
        raise ValueError('invalid_event_payload')
    fields = {
        'state_changed': ('previous_state', 'current_state', 'blocked_reason'),
        'action_recorded': ('step_id', 'action_type', 'attempt_status', 'evidence_ids'),
        'wait_registered': ('wait_id', 'reason', 'deadline'),
        'result_ready': ('result_ref', 'outcome'),
        'operation_requested': ('operation_id', 'action'),
        'operation_completed': ('operation_id', 'action', 'status', 'result_ref'),
    }[row['event_type']]
    result['payload'] = {'event_type': row['event_type'], **{key: payload[key] for key in fields if key in payload}}
    content = result['payload']
    if row['event_type'] == 'state_changed':
        _enum(content.get('previous_state'), STATES)
        _enum(content.get('current_state'), STATES)
    elif row['event_type'] == 'action_recorded':
        _id_field(content.get('step_id'))
        _enum(content.get('action_type'), {'navigate', 'click', 'input', 'keypress', 'select',
            'scroll', 'switch_tab', 'read_visible', 'screenshot', 'download_attachment'})
        _enum(content.get('attempt_status'), {'INTENT', 'COMPLETED', 'FAILED', 'UNKNOWN'})
    elif row['event_type'] == 'wait_registered':
        _id_field(content.get('wait_id'))
        _enum(content.get('reason'), {'ci', 'site', 'handoff', 'pause'})
        if content.get('deadline') is not None and not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z', content['deadline']):
            raise ValueError('invalid_wait_deadline')
    elif row['event_type'] == 'result_ready':
        _id_field(content.get('result_ref'))
        _enum(content.get('outcome'), {'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'})
    else:
        _id_field(content.get('operation_id'))
        _enum(content.get('action'), {'start', 'retry', 'pause', 'resume', 'cancel'})
        if row['event_type'] == 'operation_completed':
            _enum(content.get('status'), {'APPLIED', 'REJECTED'})
            _id_field(content.get('result_ref'))
    if 'blocked_reason' in result['payload']:
        result['payload']['blocked_reason'] = _reason(result['payload']['blocked_reason'])
    if 'evidence_ids' in result['payload']:
        ids = result['payload']['evidence_ids']
        if type(ids) is not list or len(ids) > 100 or any(type(value) is not str or len(value) > 200 for value in ids):
            raise ValueError('unbounded_event_evidence')
    return result


def _watermarks(db, run_id):
    global_high = db.execute('SELECT COALESCE(MAX(event_id),0) FROM task_events').fetchone()[0]
    run_high = (db.execute('SELECT COALESCE(MAX(event_id),0) FROM task_events WHERE run_id=?',
                          (run_id,)).fetchone()[0] if run_id else 0)
    return run_high, global_high


def _criteria(db, task_id, version):
    if version is None:
        return []
    row = db.execute('SELECT content_json FROM contracts WHERE task_id=? AND contract_version=?',
                     (task_id, version)).fetchone()
    if row is None or len(row[0]) > 1_048_576:
        raise ValueError('missing_or_unbounded_contract')
    contract = TaskContract.model_validate_json(row[0])
    return [item.model_dump(mode='json') for item in contract.acceptance_criteria]


def _checkpoint(db, task_id, run_id, version, criterion_ids):
    row = db.execute('''SELECT checkpoint_id,current_subgoal,verified_item_ids_json,
        pending_item_ids_json,action_sequence,saved_at FROM run_checkpoints
        WHERE run_id=? AND task_id=? AND contract_version=?
        ORDER BY saved_at DESC,rowid DESC LIMIT 1''', (run_id, task_id, version)).fetchone()
    if row is None:
        return None, None
    result = {key: row[key] for key in ('checkpoint_id', 'action_sequence', 'saved_at')}
    for field in ('verified_item_ids', 'pending_item_ids'):
        raw = row[field + '_json']
        if len(raw) > 65536:
            raise ValueError('unbounded_checkpoint')
        values = json.loads(raw)
        if type(values) is not list or any(type(value) is not str for value in values):
            raise ValueError('invalid_checkpoint')
        result[field] = list(dict.fromkeys(value for value in values if value in criterion_ids))
    subgoal = row['current_subgoal']
    return result, subgoal if subgoal == 'aggregate' or subgoal in criterion_ids else None


def _progress(db, run_id):
    rows = db.execute('''SELECT progress_id,phase,state_version,business_event_id,iteration,
        diagnostic,occurred_at,checkpoint_id,snapshot_id,verification_id,wait_id
        FROM graph_progress WHERE run_id=? ORDER BY progress_id DESC LIMIT 20''', (run_id,)).fetchall()
    values = []
    for row in reversed(rows):
        item = dict(row)
        _enum(item['diagnostic'], GRAPH_DIAGNOSTICS, nullable=True)
        for key in ('progress_id', 'business_event_id'):
            item[key] = str(item[key])
        values.append(item)
    return values


def _observation(db, run_id):
    row = db.execute('''SELECT g.snapshot_id,g.state_version,g.page_version,
        g.screenshot_evidence_id,h.valid,o.captured_at FROM gateway_page_heads h
        JOIN gateway_observations g ON g.run_id=h.run_id AND g.snapshot_id=h.snapshot_id
        JOIN observations o ON o.run_id=g.run_id AND o.snapshot_id=g.snapshot_id
        WHERE h.run_id=?''', (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['valid'] = bool(result['valid'])
    # The gateway screenshot is usually private; only a FILTERED derivative is
    # displayable. Return metadata for both, without disclosing filesystem paths.
    rows = db.execute('''SELECT DISTINCT e.evidence_id,e.artifact_kind,e.captured_at,
        v.status AS availability,a.redaction_status,a.mime_type
        FROM evidence e LEFT JOIN evidence_artifacts a USING(evidence_id)
        LEFT JOIN evidence_availability v USING(evidence_id)
        WHERE e.run_id=? AND (e.evidence_id IN (
            SELECT evidence_id FROM observations_evidence WHERE run_id=? AND snapshot_id=?)
            OR e.evidence_id=?) ORDER BY e.evidence_id LIMIT 100''',
        (run_id, run_id, row['snapshot_id'], row['screenshot_evidence_id'])).fetchall()
    result['evidence'] = [dict(value) for value in rows]
    for item in result['evidence']:
        _enum(item['artifact_kind'], {'screenshot', 'text', 'pdf', 'ci', 'har', 'diff'})
        _enum(item['availability'], {'AVAILABLE', 'MISSING', 'CORRUPT', 'EXPIRED'}, nullable=True)
        _enum(item['redaction_status'], {'FILTERED', 'BLOCKED'}, nullable=True)
        _enum(item['mime_type'], {'image/png', 'text/plain; charset=utf-8', 'application/pdf',
                                 'application/json'}, nullable=True)
    return result


def _controls(db, run_id):
    rows = db.execute('SELECT * FROM run_controls WHERE run_id=? ORDER BY operation_seq DESC LIMIT 20',
                      (run_id,)).fetchall()
    values = []
    for row in reversed(rows):
        if row['result_json'] is not None and len(row['result_json']) > 65536:
            raise ValueError('unbounded_control_result')
        if row['result_json'] is not None and type(json.loads(row['result_json'])) is not dict:
            raise ValueError('invalid_control_result')
        item = control_receipt(row)
        item['reason'] = _reason(item['reason'])
        if item['result'] is not None:
            value = item['result']
            if type(value) is not dict or value.get('run_id') != row['run_id']:
                raise ValueError('invalid_control_result')
            _enum(value.get('state'), STATES)
            _integer(value.get('state_version'))
            _id_field(value.get('wait_id'), nullable=True)
            for field in ('epoch', 'queue_revision', 'recovery_blocked_seq'):
                _integer(value.get(field), nullable=True)
            effects = value.get('side_effects', [])
            if type(effects) is not list or len(effects) > 100:
                raise ValueError('unbounded_control_effects')
            item['result'] = {key: value[key] for key in ('run_id', 'state', 'state_version',
                'wait_id', 'epoch', 'queue_revision', 'recovery_blocked_seq') if key in value}
            item['result']['side_effects'] = []
            for effect in effects:
                if type(effect) is not dict:
                    raise ValueError('invalid_control_effect')
                _id_field(effect.get('operation_id'))
                _id_field(effect.get('originating_run_id'))
                _enum(effect.get('status'), {'INTENT', 'CONFIRMED', 'NOT_APPLIED', 'UNKNOWN'})
                item['result']['side_effects'].append({key: effect[key] for key in (
                    'operation_id', 'originating_run_id', 'status')})
        for key in ('operation_seq', 'requested_event_id', 'completed_event_id'):
            if item[key] is not None:
                item[key] = str(item[key])
        values.append(item)
    return values


def workspace(path: Path, task_id: str, *, run_id: str | None = None) -> dict:
    task_id = _identifier(task_id)
    if run_id is not None:
        run_id = _identifier(run_id)
    with _snapshot(path) as db:
        task = db.execute('''SELECT task_id,preparation_status,state_version,
            current_contract_version,current_run_id FROM tasks WHERE task_id=?''', (task_id,)).fetchone()
        if task is None:
            raise BusinessError('NOT_FOUND', '任务不存在。', status=404)
        selected = run_id or task['current_run_id']
        row = None
        if selected:
            row = db.execute('''SELECT run_id,task_id,contract_version,state,state_version,blocked_reason,
                parent_run_id,created_at,started_at,ended_at,handoff_deadline
                FROM runs WHERE run_id=? AND task_id=?''', (selected, task_id)).fetchone()
            if row is None:
                raise BusinessError('NOT_FOUND', '任务下的运行记录不存在。', status=404)
        version = row['contract_version'] if row else task['current_contract_version']
        criteria = _criteria(db, task_id, version)
        criterion_ids = {item['criterion_id'] for item in criteria}
        checkpoint, subgoal = (_checkpoint(db, task_id, selected, version, criterion_ids)
                               if row else (None, None))
        verified = set(checkpoint['verified_item_ids']) if checkpoint else set()
        result = {'task': dict(task), 'run': None, 'is_current_run': bool(row and selected == task['current_run_id']),
                  'criteria_contract_version': version,
                  'criteria': [{**item, 'verified': item['criterion_id'] in verified} for item in criteria],
                  'current_subgoal': subgoal, 'checkpoint': checkpoint, 'graph_progress': [],
                  'observation': None, 'budget': None, 'queue': None, 'controls': [], 'events': []}
        high, global_high = _watermarks(db, selected)
        result.update(event_cursor=str(high), event_high_water=str(high),
                      global_event_high_water=str(global_high), has_earlier_events=False, as_of=utc_text())
        if row:
            result['run'] = dict(row)
            result['run']['blocked_reason'] = _reason(row['blocked_reason'])
            config = db.execute('SELECT settings_version FROM run_config_snapshots WHERE run_id=?',
                                (selected,)).fetchone()
            result['run']['settings_version'] = config[0] if config else 0
            result['graph_progress'] = _progress(db, selected)
            result['observation'] = _observation(db, selected)
            result['budget'] = BudgetStore(path)._status(db, selected)
            # Historical compatibility keys can contain arbitrary text. Runtime
            # obstacle identities are digests; retain only those exact digests.
            if 'recovery_counts' in result['budget']:
                recovery = result['budget']['recovery_counts']
                result['budget']['recovery_counts'] = {key: value for key, value in recovery.items()
                    if re.fullmatch('[0-9a-f]{64}', key)}
            queue = db.execute('''SELECT status,queue_class,reason,available_at,updated_at,epoch,revision
                FROM scheduler_queue WHERE run_id=?''', (selected,)).fetchone()
            result['queue'] = dict(queue) if queue else None
            if result['queue']:
                result['queue']['reason'] = _reason(result['queue']['reason'])
            result['controls'] = _controls(db, selected)
            events = db.execute('SELECT * FROM task_events WHERE run_id=? ORDER BY event_id DESC LIMIT 101',
                                (selected,)).fetchall()
            result['events'] = [_event(value, task_id=task_id, run_id=selected) for value in reversed(events[:100])]
            result['has_earlier_events'] = len(events) > 100
        return result


def run_events(path: Path, run_id: str, *, after: int = 0, limit: int = MAX_EVENTS) -> dict:
    run_id = _identifier(run_id)
    if (type(after) is not int or not 0 <= after <= MAX_CURSOR
            or type(limit) is not int or not 1 <= limit <= MAX_EVENTS):
        raise BusinessError('INVALID_PARAMETER', '事件分页参数无效。')
    with _snapshot(path) as db:
        run = db.execute('SELECT task_id FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            raise BusinessError('NOT_FOUND', '运行记录不存在。', status=404)
        high, global_high = _watermarks(db, run_id)
        # A global cursor from another selected stream remains valid. The
        # run-specific maximum may be lower; that is not a missing event.
        if after > global_high:
            raise BusinessError('INVALID_PARAMETER', '事件游标超出已存储的历史。')
        rows = db.execute('SELECT * FROM task_events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?',
                          (run_id, after, limit + 1)).fetchall()
        values = [_event(row, task_id=run[0], run_id=run_id) for row in rows[:limit]]
        return {'task_id': run[0], 'run_id': run_id, 'events': values,
                'cursor': values[-1]['event_id'] if values else str(after),
                'high_water': str(high), 'global_high_water': str(global_high),
                'has_more': len(rows) > limit, 'as_of': utc_text()}
