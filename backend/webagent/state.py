"""Trusted run state service; callers enforce permissions and business prerequisites.

The database owns the legal matrix and atomically appends each state event.
No HTTP endpoint exposes arbitrary transitions. Scheduler, verifier and pause /
resume orchestration will call this service after their own checks.
"""
from datetime import datetime
from pathlib import Path

from .db import connect, transaction
from .db.repository import utc_text, writing
from .errors import BusinessError
from .events import event_dict

TERMINAL_STATES = frozenset({'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'})
STATES = frozenset({'QUEUED', 'RUNNING', 'VERIFYING', 'WAITING_CI', 'WAITING_SITE',
                    'WAITING_HANDOFF', 'PAUSED', 'RECONCILING'}) | TERMINAL_STATES
MAX_VERSION = 2**63 - 1


def transition_in_transaction(db, *, run_id: str, expected_state_version: int,
                              target: str, blocked_reason: str | None = None,
                              handoff_deadline: datetime | None = None) -> dict:
    """Compose with local writes; result is provisional until caller commits.

    Never perform network/browser/model I/O inside the caller's transaction.
    A retry must reread state and decide again, not blindly repeat an action.
    """
    writing(db)
    if type(expected_state_version) is not int or not 0 <= expected_state_version < MAX_VERSION:
        raise BusinessError('INVALID_PARAMETER', 'Expected version must be a nonnegative integer',
                            field='expected_state_version')
    row = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        raise BusinessError('NOT_FOUND', 'Run not found', status=404)
    context = dict(current_state_version=row['state_version'],
                   current_contract_version=row['contract_version'])
    if row['state_version'] != expected_state_version:
        raise BusinessError('STATE_CONFLICT', 'Run state has changed; reload before retrying',
                            status=409, **context)
    if not isinstance(target, str) or target not in STATES:
        raise BusinessError('INVALID_PARAMETER', 'Unknown run state', field='target', **context)
    if not db.execute('SELECT 1 FROM run_transitions WHERE previous_state=? AND current_state=?',
                      (row['state'], target)).fetchone():
        raise BusinessError('INVALID_PARAMETER', f'Illegal transition: {row["state"]} -> {target}',
                            field='target', **context)
    if blocked_reason is not None and (not isinstance(blocked_reason, str) or not blocked_reason.strip()):
        raise BusinessError('INVALID_PARAMETER', 'Blocked reason must be nonempty text',
                            field='blocked_reason', **context)
    deadline = None
    if target == 'WAITING_HANDOFF':
        if not isinstance(handoff_deadline, datetime):
            raise BusinessError('INVALID_PARAMETER', 'Handoff requires an aware deadline',
                                field='handoff_deadline', **context)
        try:
            deadline = utc_text(handoff_deadline)
        except ValueError as error:
            raise BusinessError('INVALID_PARAMETER', str(error), field='handoff_deadline', **context) from error
    elif handoff_deadline is not None:
        raise BusinessError('INVALID_PARAMETER', 'Deadline only applies to WAITING_HANDOFF',
                            field='handoff_deadline', **context)
    if target == 'SUCCEEDED' and db.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_run_guards'").fetchone():
        tracked = db.execute('SELECT 1 FROM evidence_run_guards WHERE run_id=?', (run_id,)).fetchone()
        gateway = db.execute('SELECT 1 FROM gateway_observations WHERE run_id=?', (run_id,)).fetchone()
        indexed = db.execute('SELECT 1 FROM evidence WHERE run_id=?', (run_id,)).fetchone()
        scheduled = db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone()
        if tracked or gateway or indexed or scheduled:
            if not indexed:
                raise BusinessError('EVIDENCE_MISSING', 'Run has no published evidence', status=409)
            # Bounded local integrity reads are a terminal prerequisite, not a
            # browser/model call. Use the same DB snapshot as the state update.
            from .evidence.store import EvidenceStore
            path = Path(db.execute('PRAGMA database_list').fetchone()['file'])
            EvidenceStore(path.parent, initialize=False).assert_run_ready(run_id, db=db)
    if target in ('SUCCEEDED', 'PARTIAL') and db.execute("SELECT 1 FROM sqlite_master WHERE name='run_results'").fetchone():
        compiled = db.execute("SELECT 1 FROM contracts WHERE task_id=? AND contract_version=? AND json_type(content_json,'$.acceptance_criteria')='array'",
                              (row['task_id'],row['contract_version'])).fetchone()
        if compiled and not db.execute('SELECT 1 FROM run_results WHERE run_id=? AND state_version=? AND outcome=?',
                                       (run_id,expected_state_version+1,target)).fetchone():
            raise BusinessError('INVALID_PARAMETER', 'Terminal output requires business aggregator', status=409)
    now = utc_text()
    changed = db.execute('''UPDATE runs SET state=?,state_version=state_version+1,
        blocked_reason=?,started_at=COALESCE(started_at,?),ended_at=?,
        handoff_deadline=?,next_eligible_at=NULL WHERE run_id=? AND state_version=?''',
        (target, blocked_reason, now if target != 'CANCELLED' else None,
         now if target in TERMINAL_STATES else None, deadline, run_id, expected_state_version))
    if changed.rowcount != 1:
        raise BusinessError('STATE_CONFLICT', 'Run state changed', status=409, **context)
    event = db.execute('''SELECT * FROM task_events WHERE run_id=? AND state_version=?
                         AND event_type='state_changed' ORDER BY event_id DESC LIMIT 1''',
                       (run_id, expected_state_version + 1)).fetchone()
    if event is None:
        raise RuntimeError('State-event invariant failed')
    return event_dict(event)


def transition(path: Path, **arguments) -> dict:
    """Return only after both state and its event commit successfully."""
    with connect(path) as db, transaction(db):
        event = transition_in_transaction(db, **arguments)
    return event
