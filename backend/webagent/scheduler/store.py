"""SQLite is the scheduling authority; no I/O or awaited work inside transactions.

An expiry revokes execution, it does not establish that an external write failed.
Recovery qualifications are therefore explicitly distinct from action permission.
The application must hold its machine-local Worker lock before start_worker().
"""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

from ..db import connect, transaction
from ..db.repository import utc_text
from ..errors import BusinessError
from ..state import TERMINAL_STATES, transition_in_transaction
from .models import ExecutionToken, Resource, canonical_site, resource_site, ordered_resources


def _error(message='Execution qualification is stale or unavailable', row=None):
    raise BusinessError('RESOURCE_CONFLICT', message, status=409,
                        current_state_version=row['run_state_version'] if row is not None else None)


def _text(value, field):
    if type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip() or any(ord(c) < 32 for c in value):
        raise BusinessError('INVALID_PARAMETER', 'Invalid scheduler identifier', field=field)
    return value


def _version(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise BusinessError('INVALID_PARAMETER', 'Invalid state version', field='expected_state_version')


def _now(value=None):
    return utc_text(value)


def validate_in_transaction(db, token, resource=None, *, now=None, allow_reconciling=False):
    """Fresh DB check for every gateway dispatch, composed with local metadata writes.

This does not reserve an external action: browser I/O is performed after the
transaction. Already dispatched actions cannot be undone by epoch revocation.
"""
    if not isinstance(token, ExecutionToken):
        _error()
    current = _now(now)
    row = db.execute('''SELECT r.state_version AS run_state_version,q.*,r.state,
         q.run_state_version AS qualification_state_version,
         w.state AS worker_state,w.generation AS generation,w.expires_at AS worker_expires
         FROM scheduler_queue q JOIN runs r USING(run_id)
         LEFT JOIN scheduler_workers w ON w.worker_id=q.worker_id WHERE q.run_id=?''', (token.run_id,)).fetchone()
    if (row is None or row['status'] != 'ACTIVE' or row['worker_id'] != token.worker_id
            or row['worker_generation'] != token.worker_generation or row['generation'] != token.worker_generation
            or row['worker_state'] != 'ACTIVE' or row['worker_expires'] <= current
            or row['epoch'] != token.epoch or row['run_state_version'] != token.state_version
            or row['qualification_state_version'] != token.state_version
            or row['expires_at'] <= current or row['state'] not in ('RUNNING','VERIFYING','RECONCILING')
            or (row['state'] == 'RECONCILING' and not allow_reconciling)):
        _error(row=row)
    leases = db.execute('SELECT * FROM resource_leases WHERE holder_run_id=? ORDER BY resource_key', (token.run_id,)).fetchall()
    actual = tuple(item['resource_key'] for item in leases)
    requirements = {r[0] for r in db.execute('SELECT resource_key FROM scheduler_requirements WHERE run_id=?', (token.run_id,))}
    if (actual != tuple(sorted(token.resources)) or sum(item['resource_type'] == 'active_slot' for item in leases) != 1
            or requirements != {item['resource_key'] for item in leases if item['resource_type'] != 'active_slot'}):
        _error(row=row)
    if any(item['worker_id'] != token.worker_id or item['epoch'] != token.epoch
           or item['expires_at'] <= current or item['control_owner'] != 'worker' for item in leases):
        _error(row=row)
    if resource is not None:
        key = resource.resource_key if isinstance(resource, Resource) else resource
        if key not in actual:
            _error('The requested resource is outside this qualification', row)
    # Normal actions are forbidden while any unresolved operation on this Run exists.
    if not allow_reconciling and (db.execute('''SELECT 1 FROM write_intents w JOIN runs r USING(task_id)
          WHERE r.run_id=? AND w.status IN ('INTENT','UNKNOWN') LIMIT 1''', (token.run_id,)).fetchone()
            or db.execute('''SELECT 1 FROM resource_quarantines q
          JOIN write_intents w USING(operation_id) JOIN runs r ON r.task_id=w.task_id
          WHERE r.run_id=? LIMIT 1''', (token.run_id,)).fetchone()):
        _error('Unresolved write requires reconciliation', row)
    if not allow_reconciling and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget_timers'").fetchone():
        stopped = db.execute('SELECT stop_reason FROM budget_timers WHERE run_id=?', (token.run_id,)).fetchone()
        if stopped and stopped['stop_reason']:
            raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409, field=stopped['stop_reason'])
    return row


class SchedulerStore:
    def __init__(self, path: Path, *, clock=None, lease_seconds=30, budgets=None):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError('lease_seconds must be an integer between 1 and 300')
        self.path, self.clock, self.lease_seconds = Path(path), clock or (lambda: datetime.now(timezone.utc)), lease_seconds
        from ..budgets.store import BudgetStore
        self.budgets = budgets if budgets is not None else BudgetStore(self.path)

    @staticmethod
    def _budget_enabled(db):
        # Historical v10 fixtures can still be inspected; normal application
        # startup always applies the latest migration before any scheduling.
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget_timers'").fetchone() is not None

    def _time(self):
        value = self.clock()
        return _now(value), _now(value + timedelta(seconds=self.lease_seconds))

    @staticmethod
    def _row(db, run_id):
        _text(run_id, 'run_id')
        row = db.execute('''SELECT r.state_version AS run_state_version,q.*,r.state FROM scheduler_queue q
          JOIN runs r USING(run_id) WHERE q.run_id=?''', (run_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Run is not in the durable queue', status=404)
        return row

    @staticmethod
    def _event(db, row, event_type, now, reason=None):
        db.execute('INSERT INTO scheduler_events(run_id,epoch,revision,event_type,reason,occurred_at) VALUES(?,?,?,?,?,?)',
                   (row['run_id'], row['epoch'], row['revision'], event_type, reason, now))

    def _change(self, db, row, now, event_type, *, reason=None, **values):
        columns = dict(updated_at=now, reason=reason, **values)
        db.execute('UPDATE scheduler_queue SET ' + ','.join(key + '=?' for key in columns)
                   + ',revision=revision+1 WHERE run_id=?', (*columns.values(), row['run_id']))
        changed = self._row(db, row['run_id'])
        self._event(db, changed, event_type, now, reason)
        return changed

    @staticmethod
    def _resources(resources):
        if not isinstance(resources, (tuple,list)) or not 1 <= len(resources) <= 32:
            raise BusinessError('INVALID_PARAMETER', 'Provide a bounded complete resource set', field='resources')
        result = {}
        for item in resources:
            if not isinstance(item, Resource) or item.resource_type == 'active_slot':
                raise BusinessError('INVALID_PARAMETER', 'Active slots are assigned by the scheduler', field='resources')
            if item.resource_key in result:
                if result[item.resource_key] != item:
                    raise BusinessError('INVALID_PARAMETER', 'Conflicting resource definitions', field='resources')
            result[item.resource_key] = item
        return ordered_resources(tuple(result.values()))

    @staticmethod
    def _worker(db, worker_id, generation, now):
        _text(worker_id, 'worker_id')
        if type(generation) is not int or generation <= 0:
            _error()
        row = db.execute('SELECT * FROM scheduler_workers WHERE worker_id=?', (worker_id,)).fetchone()
        if row is None or row['generation'] != generation or row['state'] != 'ACTIVE' or row['expires_at'] <= now:
            _error('Worker generation has expired or changed')
        return row

    @staticmethod
    def _unresolved(db, run_id):
        return bool(db.execute('''SELECT 1 FROM write_intents w JOIN runs r USING(task_id)
            WHERE r.run_id=? AND w.status IN ('INTENT','UNKNOWN') LIMIT 1''', (run_id,)).fetchone())

    @staticmethod
    def _write_check_run(db, run_id):
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='write_reconciliation_runs'").fetchone():
            return None
        return db.execute('SELECT * FROM write_reconciliation_runs WHERE run_id=?', (run_id,)).fetchone()

    @classmethod
    def _write_check_scope(cls, db, run_id, other_run_id):
        """Only an explicit same-contract retry may inspect terminal effects."""
        link = cls._write_check_run(db, run_id)
        if link is None:
            return False
        row = db.execute('''SELECT r.task_id,r.contract_sha256,o.task_id AS old_task,
            o.contract_sha256 AS old_contract,o.state FROM runs r JOIN runs o ON o.run_id=?
            WHERE r.run_id=?''', (other_run_id, run_id)).fetchone()
        return bool(row and row['task_id'] == row['old_task']
                    and row['contract_sha256'] == row['old_contract'] and row['state'] in TERMINAL_STATES)

    def register_write_reconciliation(self, db, run_id, source_run_id):
        """Compose a trusted retry's query-only mode in its creation transaction."""
        run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        source = db.execute('SELECT * FROM runs WHERE run_id=?', (source_run_id,)).fetchone()
        if (run is None or source is None or run['state'] != 'QUEUED'
                or run['task_id'] != source['task_id'] or run['contract_sha256'] != source['contract_sha256']
                or source['state'] not in TERMINAL_STATES or not self._unresolved(db, run_id)
                or not self.write_recheck_available(db, run_id)):
            _error('Write reconciliation requires a same-contract retry of terminal effects')
        db.execute('INSERT INTO write_reconciliation_runs(run_id,source_run_id,created_at) VALUES(?,?,?)',
                   (run_id, source_run_id, utc_text()))
        transition_in_transaction(db, run_id=run_id, expected_state_version=run['state_version'],
                                  target='RECONCILING', blocked_reason='unknown_write')
        row = self._row(db, run_id)
        now, _ = self._time()
        return self._change(db, row, now, 'revoked', reason='resource_conflict', status='RECOVERY',
                            run_state_version=row['run_state_version'])

    @staticmethod
    def write_recheck_available(db, run_id):
        """Legacy/mixed or changed-contract effects keep their original fence."""
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='write_protocol_claims'").fetchone():
            return False
        rows = db.execute('''SELECT w.operation_id,p.operation_id AS claimed,
            r.contract_sha256,o.contract_sha256 AS original_contract
            FROM runs r JOIN write_intents w USING(task_id)
            JOIN runs o ON o.run_id=w.originating_run_id
            LEFT JOIN write_protocol_claims p USING(operation_id)
            WHERE r.run_id=? AND (w.status IN ('INTENT','UNKNOWN') OR EXISTS(
                SELECT 1 FROM resource_quarantines q WHERE q.operation_id=w.operation_id))''', (run_id,)).fetchall()
        return bool(rows) and all(row['claimed'] is not None
                                 and row['contract_sha256'] == row['original_contract'] for row in rows)

    def enqueue_write_reconciliation(self, run_id, expected_state_version, resources, *, source_run_id):
        """Trusted local entry; it cannot grant normal action authority."""
        self.enqueue(run_id, resources, expected_state_version=expected_state_version)
        with connect(self.path) as db, transaction(db):
            run = db.execute('SELECT state_version FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if run is None or run['state_version'] != expected_state_version:
                _error()
            return dict(self.register_write_reconciliation(db, run_id, source_run_id))

    @staticmethod
    def _protected(db, key):
        return bool(db.execute('SELECT 1 FROM resource_quarantines WHERE resource_key=? LIMIT 1', (key,)).fetchone())

    def _release_safe(self, db, run_id, *, preserve_context=True, preserve_logical=True):
        unresolved = self._unresolved(db, run_id)
        for lease in db.execute('SELECT * FROM resource_leases WHERE holder_run_id=?', (run_id,)).fetchall():
            keep = ((preserve_context and lease['resource_type'] == 'browser_context')
                    or lease['control_owner'] == 'human'
                    or (preserve_logical and lease['logical_hold']) or self._protected(db, lease['resource_key'])
                    or (unresolved and lease['resource_type'] != 'active_slot'))
            if not keep or lease['resource_type'] == 'active_slot':
                db.execute('DELETE FROM resource_leases WHERE resource_key=?', (lease['resource_key'],))
        reservation = db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?', (run_id,)).fetchone()
        if reservation is not None and not preserve_context:
            live = reservation['session_id'] and db.execute("SELECT 1 FROM browser_sessions WHERE session_id=? AND state IN ('OPENING','OPEN','CLOSING')", (reservation['session_id'],)).fetchone()
            if not live:
                db.execute('DELETE FROM scheduler_context_reservations WHERE run_id=?', (run_id,))
                db.execute("DELETE FROM resource_leases WHERE holder_run_id=? AND resource_type='browser_context'", (run_id,))

    def _revoke(self, db, row, now, reason):
        if self._budget_enabled(db):
            self.budgets.on_transition(db, row['run_id'], 'RECONCILING', recover=True)
        if row['state'] in ('RUNNING','VERIFYING'):
            transition_in_transaction(db, run_id=row['run_id'], expected_state_version=row['run_state_version'],
                                      target='RECONCILING', blocked_reason=reason)
        db.execute("UPDATE resource_leases SET logical_hold=1,control_owner=CASE WHEN control_owner='human' THEN 'human' ELSE 'none' END,state_version=state_version+1 WHERE holder_run_id=? AND resource_type<>'active_slot'", (row['run_id'],))
        db.execute("DELETE FROM resource_leases WHERE holder_run_id=? AND resource_type='active_slot'", (row['run_id'],))
        fresh = self._row(db, row['run_id'])
        return self._change(db, fresh, now, 'revoked', reason=reason, status='RECOVERY', epoch=row['epoch'] + 1,
                            worker_id=None, worker_generation=None, expires_at=None,
                            run_state_version=fresh['run_state_version'], available_at=now)

    def _expire(self, db, now):
        rows = db.execute('''SELECT r.state_version AS run_state_version,q.*,r.state FROM scheduler_queue q JOIN runs r USING(run_id)
          WHERE q.status='ACTIVE' AND (q.expires_at<=? OR NOT EXISTS(SELECT 1 FROM scheduler_workers w
           WHERE w.worker_id=q.worker_id AND w.generation=q.worker_generation AND w.state='ACTIVE' AND w.expires_at>?))''', (now,now)).fetchall()
        for row in rows:
            self._revoke(db, row, now, 'lease_expired')
        return len(rows)

    def start_worker(self, worker_id):
        _text(worker_id, 'worker_id')
        now, expiry = self._time()
        with connect(self.path) as db, transaction(db):
            for row in db.execute("SELECT r.state_version AS run_state_version,q.*,r.state FROM scheduler_queue q JOIN runs r USING(run_id) WHERE q.status='ACTIVE'").fetchall():
                self._revoke(db, row, now, 'worker_restarted')
            db.execute("UPDATE scheduler_workers SET state='STOPPED' WHERE state='ACTIVE'")
            generation = db.execute('INSERT INTO scheduler_generations(worker_id,created_at) VALUES(?,?) RETURNING generation', (worker_id,now)).fetchone()[0]
            db.execute('''INSERT INTO scheduler_workers(worker_id,generation,state,started_at,heartbeat_at,expires_at)
              VALUES(?,?,'ACTIVE',?,?,?) ON CONFLICT(worker_id) DO UPDATE SET generation=excluded.generation,
              state='ACTIVE',started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at''', (worker_id,generation,now,now,expiry))
        return generation

    def heartbeat_worker(self, worker_id, generation):
        now, expiry = self._time()
        with connect(self.path) as db, transaction(db):
            self._worker(db, worker_id, generation, now)
            db.execute('UPDATE scheduler_workers SET heartbeat_at=?,expires_at=? WHERE worker_id=?', (now,expiry,worker_id))
        return expiry

    def stop_worker(self, worker_id, generation):
        now, _ = self._time()
        with connect(self.path) as db, transaction(db):
            worker = db.execute('SELECT * FROM scheduler_workers WHERE worker_id=?', (worker_id,)).fetchone()
            if worker is None or worker['generation'] != generation:
                _error()
            for row in db.execute("SELECT r.state_version AS run_state_version,q.*,r.state FROM scheduler_queue q JOIN runs r USING(run_id) WHERE q.status='ACTIVE' AND q.worker_id=?", (worker_id,)).fetchall():
                self._revoke(db, row, now, 'worker_restarted')
            db.execute("UPDATE scheduler_workers SET state='STOPPED' WHERE worker_id=?", (worker_id,))

    def enqueue(self, run_id, resources, *, expected_state_version, queue_class='ordinary', available_at=None):
        _text(run_id, 'run_id'); _version(expected_state_version)
        resources = self._resources(resources)
        if queue_class not in ('ordinary','monitoring','webarena'):
            raise BusinessError('INVALID_PARAMETER', 'Unknown queue class', field='queue_class')
        if sum(item.resource_type == 'browser_context' for item in resources) != 1:
            raise BusinessError('INVALID_PARAMETER', 'Exactly one browser context is required', field='resources')
        if not any(item.resource_type == 'browser_context' and item.resource_key == Resource.browser_context(run_id).resource_key for item in resources):
            raise BusinessError('INVALID_PARAMETER', 'Browser context must belong to this Run', field='resources')
        now, _ = self._time(); due = _now(available_at) if available_at is not None else now
        with connect(self.path) as db, transaction(db):
            run = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if run is None:
                raise BusinessError('NOT_FOUND', 'Run not found', status=404)
            if run['state_version'] != expected_state_version or run['state'] != 'QUEUED':
                _error('Only the current QUEUED Run can be enqueued')
            existing = db.execute('SELECT * FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone()
            if existing:
                requirements = tuple((item['resource_type'],item['resource_key'],bool(item['logical_hold'])) for item in db.execute('SELECT * FROM scheduler_requirements WHERE run_id=? ORDER BY resource_key', (run_id,)))
                wanted = tuple((item.resource_type,item.resource_key,item.logical_hold) for item in sorted(resources,key=lambda r:r.resource_key))
                if requirements != wanted or existing['queue_class'] != queue_class or (available_at is not None and existing['available_at'] != due):
                    _error('Run is already queued with another resource set')
                return dict(self._row(db,run_id))
            db.execute('''INSERT INTO scheduler_queue(run_id,queue_class,status,run_state_version,available_at,created_at,updated_at)
              VALUES(?,?,'QUEUED',?,?,?,?)''', (run_id,queue_class,expected_state_version,due,now,now))
            db.executemany('INSERT INTO scheduler_requirements VALUES(?,?,?,?)', ((run_id,item.resource_key,item.resource_type,int(item.logical_hold)) for item in resources))
            row = self._row(db,run_id); self._event(db,row,'enqueued',now)
            return dict(row)

    def _blocked(self, db, row, requirements, now):
        if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='run_controls'").fetchone():
            # A new Run cannot escape an unresolved effect from the same Task
            # by choosing a different identity, repository or contract scope.
            unresolved = db.execute('''SELECT 1 FROM runs r JOIN write_intents w ON w.task_id=r.task_id
                WHERE r.run_id=? AND w.originating_run_id<>r.run_id AND (
                    w.status IN ('INTENT','UNKNOWN') OR EXISTS(
                        SELECT 1 FROM resource_quarantines q WHERE q.operation_id=w.operation_id)) LIMIT 1''',
                (row['run_id'],)).fetchone()
            if unresolved is not None and self._write_check_run(db, row['run_id']) is None:
                return 'resource_conflict'
        for item in requirements:
            held = db.execute('SELECT * FROM resource_leases WHERE resource_key=?', (item['resource_key'],)).fetchone()
            if held and (held['holder_run_id'] != row['run_id'] or held['control_owner'] == 'human'):
                # Never steal a live context or human control. A terminal
                # predecessor's logical hold may transfer to a query-only Run.
                if (held['control_owner'] == 'human'
                        or not self._write_check_scope(db, row['run_id'], held['holder_run_id'])
                        or db.execute("SELECT 1 FROM browser_sessions WHERE run_id=? AND state IN ('OPENING','OPEN','CLOSING')", (held['holder_run_id'],)).fetchone()
                        or db.execute("SELECT 1 FROM scheduler_queue WHERE run_id=? AND status='ACTIVE'", (held['holder_run_id'],)).fetchone()):
                    return 'resource_conflict'
            quarantines = db.execute('''SELECT w.originating_run_id FROM resource_quarantines q JOIN write_intents w USING(operation_id) WHERE q.resource_key=?''', (item['resource_key'],)).fetchall()
            if quarantines and (row['state'] != 'RECONCILING' or any(
                    q['originating_run_id'] != row['run_id'] and not self._write_check_scope(
                        db, row['run_id'], q['originating_run_id']) for q in quarantines)):
                return 'resource_conflict'
            scope = resource_site(Resource(item['resource_type'],item['resource_key'],bool(item['logical_hold'])))
            if scope:
                gates = db.execute('SELECT state,next_eligible_at FROM site_gates WHERE site_id IN (?,?)',
                                   (':'.join(scope), scope[1])).fetchall()
                if any(gate['state'] == 'BLOCKED' or (gate['state'] == 'COOLDOWN' and gate['next_eligible_at'] and gate['next_eligible_at'] > now) for gate in gates):
                    return 'resource_conflict'
            if scope and any((session['realm'],canonical_site(session['site_id'])) == scope for session in db.execute("SELECT site_id,realm FROM browser_sessions WHERE owner_kind='login' AND state IN ('OPENING','OPEN','CLOSING')")):
                return 'resource_conflict'
        reservation = db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?', (row['run_id'],)).fetchone()
        if reservation and reservation['session_id']:
            live = db.execute("SELECT 1 FROM browser_sessions WHERE session_id=? AND state IN ('OPENING','OPEN','CLOSING')", (reservation['session_id'],)).fetchone()
            if not live:
                db.execute('DELETE FROM scheduler_context_reservations WHERE run_id=?', (row['run_id'],))
                reservation = None
        if reservation is None:
            count = db.execute("SELECT (SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING'))+(SELECT count(*) FROM scheduler_context_reservations WHERE session_id IS NULL)").fetchone()[0]
            if count >= 4:
                return 'context_capacity'
        return None

    def claim(self, worker_id, generation, *, before_claim=None):
        """Acquire the entire ordered resource set or leave it untouched.

        before_claim is a trusted, local transaction hook for M1-11's atomic
        first debit. No external I/O is permitted and no hook is enabled by default.
        """
        now, expiry = self._time()
        with connect(self.path) as db, transaction(db):
            self._worker(db,worker_id,generation,now); self._expire(db,now)
            slot = next((Resource.active_slot(i) for i in (0,1) if not db.execute('SELECT 1 FROM resource_leases WHERE resource_key=?', (Resource.active_slot(i).resource_key,)).fetchone()), None)
            if slot is None:
                return None
            candidates = db.execute('''SELECT r.state_version AS run_state_version,q.*,r.state FROM scheduler_queue q JOIN runs r USING(run_id)
              WHERE q.status IN ('QUEUED','RECOVERY') AND q.available_at<=? ORDER BY CASE q.queue_class WHEN 'monitoring' THEN 0 ELSE 1 END,q.available_at,q.queue_id''', (now,)).fetchall()
            for row in candidates:
                controls_enabled = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='run_controls'").fetchone()
                if controls_enabled:
                    from ..controls.store import ControlStore
                    if ControlStore.pending_in_transaction(db, row['run_id']) is not None:
                        continue
                # A durable failed reconciliation is a task-level wait for
                # trusted correction, not an automatic retry on every tick.
                if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='graph_recoveries'").fetchone():
                    recovery = db.execute('''SELECT phase,recovery_seq FROM graph_recoveries WHERE run_id=?
                        ORDER BY recovery_seq DESC LIMIT 1''', (row['run_id'],)).fetchone()
                    if recovery is not None and recovery['phase'] == 'BLOCKED':
                        if not controls_enabled or not ControlStore.resume_authorized_in_transaction(
                                db, row['run_id'], recovery['recovery_seq']):
                            continue
                if row['state'] not in ('QUEUED','RECONCILING'):
                    continue
                requirements = db.execute('SELECT * FROM scheduler_requirements WHERE run_id=? ORDER BY resource_key', (row['run_id'],)).fetchall()
                reason = self._blocked(db,row,requirements,now)
                if reason:
                    if row['reason'] != reason:
                        self._change(db,row,now,'revoked',reason=reason)
                    continue
                if self._budget_enabled(db):
                    db.execute('SAVEPOINT budget_candidate')
                    try:
                        self.budgets.before_claim(db, dict(row))
                    except BusinessError as error:
                        if error.code in ('DAILY_QUOTA_EXCEEDED', 'BUDGET_EXCEEDED', 'SITE_THROTTLED'):
                            db.execute('ROLLBACK TO budget_candidate')
                            db.execute('RELEASE budget_candidate')
                            continue
                        raise
                    db.execute('RELEASE budget_candidate')
                if before_claim is not None:
                    before_claim(db,dict(row))
                if row['state'] == 'QUEUED':
                    transition_in_transaction(db,run_id=row['run_id'],expected_state_version=row['run_state_version'],target='RUNNING')
                epoch = row['epoch'] + 1
                keys = []
                for item in sorted([dict(resource_key=slot.resource_key,resource_type='active_slot',logical_hold=0), *map(dict,requirements)], key=lambda x:(['active_slot','site_identity','repository_write','webarena_environment','browser_context'].index(x['resource_type']),x['resource_key'])):
                    keys.append(item['resource_key'])
                    db.execute('''INSERT INTO resource_leases(resource_key,resource_type,holder_run_id,worker_id,epoch,expires_at,heartbeat_at,logical_hold)
                       VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET holder_run_id=excluded.holder_run_id,worker_id=excluded.worker_id,epoch=excluded.epoch,
                       expires_at=excluded.expires_at,heartbeat_at=excluded.heartbeat_at,control_owner='worker',logical_hold=excluded.logical_hold,state_version=resource_leases.state_version+1''',
                       (item['resource_key'],item['resource_type'],row['run_id'],worker_id,epoch,expiry,now,item['logical_hold']))
                reservation = db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?', (row['run_id'],)).fetchone()
                if reservation is None:
                    ordinal = next((i for i in range(1,5) if not db.execute('SELECT 1 FROM scheduler_context_reservations WHERE context_ordinal=?', (i,)).fetchone()), 1)
                    db.execute('INSERT INTO scheduler_context_reservations(run_id,context_ordinal,worker_id,worker_generation,epoch,created_at) VALUES(?,?,?,?,?,?)', (row['run_id'],ordinal,worker_id,generation,epoch,now))
                else:
                    db.execute('UPDATE scheduler_context_reservations SET worker_id=?,worker_generation=?,epoch=? WHERE run_id=?', (worker_id,generation,epoch,row['run_id']))
                fresh = self._row(db,row['run_id'])
                self._change(db,fresh,now,'claimed',status='ACTIVE',epoch=epoch,worker_id=worker_id,worker_generation=generation,
                             expires_at=expiry,run_state_version=fresh['run_state_version'])
                token = ExecutionToken(row['run_id'],worker_id,generation,epoch,fresh['run_state_version'],expiry,tuple(keys))
                if self._budget_enabled(db):
                    self.budgets.on_claim(db, token)
                return token
            return None

    def validate(self, token, *, resource_key=None, allow_reconciling=False):
        now,_ = self._time()
        with connect(self.path) as db:
            return dict(validate_in_transaction(db,token,resource_key,now=datetime.fromisoformat(now.replace('Z','+00:00')),allow_reconciling=allow_reconciling))

    def heartbeat(self, token):
        now,expiry = self._time()
        with connect(self.path) as db, transaction(db):
            return self._heartbeat_in_transaction(db, token, now, expiry)

    def _heartbeat_in_transaction(self, db, token, now, expiry):
        row = validate_in_transaction(db,token,now=datetime.fromisoformat(now.replace('Z','+00:00')),allow_reconciling=True)
        if self._budget_enabled(db):
            self.budgets.flush_in_transaction(db, token.run_id)
        db.execute('UPDATE resource_leases SET expires_at=?,heartbeat_at=?,state_version=state_version+1 WHERE holder_run_id=?', (expiry,now,token.run_id))
        db.execute('UPDATE scheduler_workers SET expires_at=?,heartbeat_at=? WHERE worker_id=?', (expiry,now,token.worker_id))
        self._change(db,row,now,'heartbeat',expires_at=expiry)
        return ExecutionToken(token.run_id,token.worker_id,token.worker_generation,token.epoch,token.state_version,expiry,token.resources)

    def _runtime_token(self, db, token, now):
        if not isinstance(token, ExecutionToken):
            _error()
        row = self._row(db, token.run_id)
        if (row['status'] != 'ACTIVE' or row['worker_id'] != token.worker_id
                or row['worker_generation'] != token.worker_generation or row['epoch'] != token.epoch):
            _error(row=row)
        rebound = ExecutionToken(token.run_id, token.worker_id, token.worker_generation,
                                 token.epoch, row['run_state_version'], row['expires_at'], token.resources)
        validate_in_transaction(db, rebound, now=datetime.fromisoformat(now.replace('Z', '+00:00')),
                                allow_reconciling=True)
        return rebound

    def refresh_qualification(self, token):
        """Rebind only the runtime guard to a trusted state transition.

        Action gateways must still validate the token they received. This may
        follow reconcile/verify within one owner, never a revoked epoch, changed
        resource set, expired lease, or transferred browser control.
        """
        now, _ = self._time()
        with connect(self.path) as db:
            db.execute('BEGIN')
            return self._runtime_token(db, token, now)

    def runtime_budget(self, token):
        """Guard-only rebind and budget settlement share one writer snapshot."""
        now, _ = self._time()
        with connect(self.path) as db, transaction(db):
            token = self._runtime_token(db, token, now)
            status = self.budgets.flush_in_transaction(db, token.run_id)
            if status is None:
                _error('Run budget is not initialized')
            return token, status

    def runtime_heartbeat(self, token):
        """Guard-only rebind and renewal cannot race a trusted state change."""
        now, expiry = self._time()
        with connect(self.path) as db, transaction(db):
            token = self._runtime_token(db, token, now)
            return self._heartbeat_in_transaction(db, token, now, expiry)

    def settlement(self, token):
        """Prove an explicit release for bounded local cleanup, never dispatch.

        The next epoch has no active qualification. This read-only receipt may
        let the old coroutine save its framework checkpoint and close clients;
        it cannot renew leases or return a replacement execution token.
        """
        if not isinstance(token, ExecutionToken):
            return False
        now, _ = self._time()
        with connect(self.path) as db:
            db.execute('BEGIN')
            worker = db.execute('SELECT * FROM scheduler_workers WHERE worker_id=?',
                                (token.worker_id,)).fetchone()
            row = db.execute('''SELECT q.*,r.state,r.state_version AS current_state_version
                FROM scheduler_queue q JOIN runs r USING(run_id) WHERE q.run_id=?''',
                (token.run_id,)).fetchone()
            event = db.execute('''SELECT * FROM scheduler_events WHERE run_id=?
                ORDER BY event_id DESC LIMIT 1''', (token.run_id,)).fetchone()
            if (worker is None or worker['generation'] != token.worker_generation
                    or worker['state'] != 'ACTIVE' or worker['expires_at'] <= now
                    or token.expires_at <= now or row is None or event is None
                    or row['epoch'] != token.epoch + 1
                    or row['worker_id'] is not None or row['worker_generation'] is not None
                    or row['expires_at'] is not None
                    or row['run_state_version'] != row['current_state_version']
                    or token.state_version >= row['current_state_version']
                    or event['epoch'] != row['epoch'] or event['revision'] != row['revision']):
                return False
            return ((row['status'] == 'WAITING' and row['state'] in
                     ('PAUSED', 'WAITING_CI', 'WAITING_SITE', 'WAITING_HANDOFF')
                     and event['event_type'] == 'waiting')
                    or (row['status'] == 'FINISHED' and row['state'] in TERMINAL_STATES
                        and event['event_type'] == 'finished'))

    def recovery_blocked_receipt(self, token):
        """A task-level recovery block never grants new execution authority."""
        with connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='graph_recoveries'").fetchone():
                return False
        from ..graph.recovery import RecoveryStore
        return RecoveryStore(self.path).blocked_receipt(token)

    def abandon(self, token, *, reason='executor_interrupted'):
        if reason not in ('worker_restarted','lease_expired','executor_interrupted'):
            reason='executor_interrupted'
        now,_ = self._time()
        with connect(self.path) as db, transaction(db):
            if not isinstance(token, ExecutionToken):
                _error()
            row=self._row(db,token.run_id)
            if row['status']=='RECOVERY' and row['epoch']==token.epoch+1:
                return dict(row)
            # Cleanup revokes a matching persisted owner even after its deadline.
            if (row['status']!='ACTIVE' or row['epoch']!=token.epoch
                    or row['worker_id']!=token.worker_id or row['worker_generation']!=token.worker_generation):
                _error(row=row)
            return dict(self._revoke(db,row,now,reason))

    def defer(self, token, target, *, next_eligible_at=None, handoff_deadline=None, control_owner='worker'):
        if target not in ('PAUSED','WAITING_CI','WAITING_SITE','WAITING_HANDOFF') or control_owner not in ('worker','human'):
            raise BusinessError('INVALID_PARAMETER','Invalid durable wait')
        if control_owner == 'human' and target != 'WAITING_HANDOFF':
            raise BusinessError('INVALID_PARAMETER','Human control requires a handoff')
        now,_=self._time(); due=_now(next_eligible_at) if next_eligible_at is not None else now
        with connect(self.path) as db, transaction(db):
            return self._defer_in_transaction(db, token, target, now, due,
                next_eligible_at=next_eligible_at, handoff_deadline=handoff_deadline, control_owner=control_owner)

    def _defer_in_transaction(self, db, token, target, now, due, *,
                              next_eligible_at=None, handoff_deadline=None, control_owner='worker'):
        row=validate_in_transaction(db,token,now=datetime.fromisoformat(now.replace('Z','+00:00')),allow_reconciling=True)
        if self._budget_enabled(db):
            if target == 'WAITING_HANDOFF':
                handoff_deadline = self.budgets.handoff_deadline(db, token.run_id, handoff_deadline)
            self.budgets.on_transition(db, token.run_id, target)
        transition_in_transaction(db,run_id=token.run_id,expected_state_version=token.state_version,target=target,blocked_reason='waiting',handoff_deadline=handoff_deadline)
        db.execute('UPDATE runs SET next_eligible_at=? WHERE run_id=?',(due if next_eligible_at is not None else None,token.run_id))
        db.execute("DELETE FROM resource_leases WHERE holder_run_id=? AND resource_type='active_slot'",(token.run_id,))
        db.execute('UPDATE resource_leases SET logical_hold=1,control_owner=?,state_version=state_version+1 WHERE holder_run_id=?',(control_owner,token.run_id))
        fresh=self._row(db,token.run_id)
        return dict(self._change(db,fresh,now,'waiting',reason='waiting',status='WAITING',epoch=token.epoch+1,worker_id=None,worker_generation=None,expires_at=None,run_state_version=fresh['run_state_version'],available_at=due))

    def resume(self, run_id, expected_state_version):
        _version(expected_state_version); now,_=self._time()
        with connect(self.path) as db, transaction(db):
            row=self._row(db,run_id)
            if row['run_state_version'] != expected_state_version or row['status'] != 'WAITING' or row['state'] not in ('PAUSED','WAITING_CI','WAITING_SITE','WAITING_HANDOFF'):
                _error(row=row)
            # Calling resume is the trusted orchestration's explicit control return.
            if self._budget_enabled(db):
                self.budgets.on_transition(db, run_id, 'RECONCILING')
            transition_in_transaction(db,run_id=run_id,expected_state_version=expected_state_version,target='RECONCILING')
            db.execute("UPDATE resource_leases SET control_owner='none',state_version=state_version+1 WHERE holder_run_id=?",(run_id,))
            fresh=self._row(db,run_id)
            return dict(self._change(db,fresh,now,'resumed',status='RECOVERY',run_state_version=fresh['run_state_version']))

    def wait_site(self, token, site_id, retry_after_seconds, *, reason='rate_limit'):
        """Persist a shared site cooldown without sleeping inside an executor."""
        if (type(retry_after_seconds) not in (int, float) or not math.isfinite(retry_after_seconds)
                or not 0 <= retry_after_seconds <= 604800 or reason not in ('rate_limit', 'site_policy')):
            raise BusinessError('INVALID_PARAMETER', 'Invalid site cooldown', field='retry_after_seconds')
        if not isinstance(token, ExecutionToken):
            _error()
        key = self.budgets._site(token, site_id)
        stamp = self.clock()
        now = _now(stamp)
        due = _now(stamp + timedelta(seconds=retry_after_seconds))
        stop = None
        with connect(self.path) as db, transaction(db):
            validate_in_transaction(db, token, now=stamp, allow_reconciling=True)
            existing = db.execute('SELECT next_eligible_at FROM site_gates WHERE site_id=?', (key,)).fetchone()
            if existing and existing['next_eligible_at']:
                due = max(due, existing['next_eligible_at'])
            db.execute('''INSERT INTO site_gates(site_id,state,blocked_reason,next_eligible_at,updated_at)
                VALUES(?,'COOLDOWN',?,?,?) ON CONFLICT(site_id) DO UPDATE SET
                state=CASE WHEN site_gates.state='BLOCKED' THEN 'BLOCKED' ELSE 'COOLDOWN' END,
                blocked_reason=excluded.blocked_reason,next_eligible_at=excluded.next_eligible_at,
                updated_at=excluded.updated_at,state_version=site_gates.state_version+1''', (key, reason, due, now))
            result = self._defer_in_transaction(db, token, 'WAITING_SITE', now, due,
                next_eligible_at=datetime.fromisoformat(due.replace('Z', '+00:00')))
            if self._budget_enabled(db):
                status = self.budgets.flush_in_transaction(db, token.run_id)
                wait_ms = math.ceil((datetime.fromisoformat(due.replace('Z', '+00:00')) - stamp).total_seconds() * 1000)
                if wait_ms > status['remaining_active_ms']:
                    stop = 'site_wait_exceeds_budget'
                    self.budgets.stop_in_transaction(db, token.run_id, stop)
        return self.expire_budget(token.run_id, stop) if stop else result

    @staticmethod
    def _expansion_checkpoint(db, token, checkpoint_id, now):
        """Require committed business progress before revoking its execution scope.

        Saving graph checkpoints belongs to the graph adapter. This boundary
        only accepts an immutable business checkpoint in the current Run epoch,
        bound to its current state event and all already-persisted step progress.
        """
        _text(checkpoint_id, 'checkpoint_id')
        checkpoint = db.execute('SELECT * FROM run_checkpoints WHERE checkpoint_id=?', (checkpoint_id,)).fetchone()
        run = db.execute('SELECT task_id,contract_version,state_version FROM runs WHERE run_id=?', (token.run_id,)).fetchone()
        event = db.execute("SELECT event_id,occurred_at FROM task_events WHERE run_id=? AND state_version=? AND event_type='state_changed' ORDER BY event_id DESC LIMIT 1", (token.run_id,token.state_version)).fetchone()
        claim = db.execute("SELECT occurred_at FROM scheduler_events WHERE run_id=? AND epoch=? AND event_type='claimed' ORDER BY event_id DESC LIMIT 1", (token.run_id,token.epoch)).fetchone()
        sequence = db.execute('SELECT COALESCE(MAX(sequence),0) FROM steps WHERE run_id=?', (token.run_id,)).fetchone()[0]
        if (checkpoint is None or run is None or event is None or claim is None
                or checkpoint['run_id'] != token.run_id or checkpoint['task_id'] != run['task_id']
                or checkpoint['contract_version'] != run['contract_version']
                or checkpoint['epoch'] != token.epoch or checkpoint['business_event_id'] != event['event_id']
                or checkpoint['action_sequence'] < sequence
                or checkpoint['saved_at'] < claim['occurred_at'] or checkpoint['saved_at'] < event['occurred_at']
                or checkpoint['saved_at'] > now):
            raise BusinessError('STATE_CONFLICT', 'Save a current business checkpoint before changing execution resources',
                                status=409, current_state_version=token.state_version, field='checkpoint_id')
        return checkpoint

    def expand(self, token, resources, *, checkpoint_id):
        """Requeue a whole expanded resource set after committed business progress."""
        resources=self._resources(resources); now,_=self._time()
        with connect(self.path) as db, transaction(db):
            row=validate_in_transaction(db,token,now=datetime.fromisoformat(now.replace('Z','+00:00')),allow_reconciling=True)
            self._expansion_checkpoint(db,token,checkpoint_id,now)
            if self._budget_enabled(db):
                self.budgets.on_transition(db, token.run_id, 'RECONCILING')
            for item in resources:
                if item.resource_type == 'browser_context' and not db.execute('SELECT 1 FROM scheduler_requirements WHERE run_id=? AND resource_key=? AND resource_type=?',(token.run_id,item.resource_key,'browser_context')).fetchone():
                    raise BusinessError('INVALID_PARAMETER','The browser context ownership cannot be expanded')
                db.execute('INSERT INTO scheduler_requirements VALUES(?,?,?,?) ON CONFLICT(run_id,resource_key) DO UPDATE SET logical_hold=MAX(logical_hold,excluded.logical_hold)',(token.run_id,item.resource_key,item.resource_type,int(item.logical_hold)))
            if row['state'] in ('RUNNING','VERIFYING'):
                transition_in_transaction(db,run_id=token.run_id,expected_state_version=token.state_version,target='RECONCILING',blocked_reason='scope_expanded')
            self._release_safe(db,token.run_id,preserve_context=True,preserve_logical=False)
            db.execute("UPDATE resource_leases SET control_owner='none',state_version=state_version+1 WHERE holder_run_id=?",(token.run_id,))
            fresh=self._row(db,token.run_id)
            return dict(self._change(db,fresh,now,'expanded',reason='scope_expanded',status='RECOVERY',epoch=token.epoch+1,worker_id=None,worker_generation=None,expires_at=None,run_state_version=fresh['run_state_version']))

    def reconcile(self, run_id, expected_state_version, *, release_resources=False):
        """Trusted caller reports its external reconciliation completed; never automatic."""
        _version(expected_state_version); now,_=self._time()
        with connect(self.path) as db, transaction(db):
            row=self._row(db,run_id)
            if row['state'] != 'RECONCILING' or row['run_state_version'] != expected_state_version or row['status'] not in ('ACTIVE','RECOVERY'):
                _error(row=row)
            if self._unresolved(db,run_id) or db.execute('''SELECT 1 FROM resource_quarantines q
                JOIN write_intents w USING(operation_id) JOIN runs r ON r.task_id=w.task_id
                WHERE r.run_id=?''',(run_id,)).fetchone():
                _error('Unresolved writes still require reconciliation',row)
            if db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'",(run_id,)).fetchone():
                _error('Human control has not been returned',row)
            if release_resources:
                if row['status'] == 'ACTIVE':
                    _error('Revoke execution before releasing recovery resources',row)
                self._release_safe(db,run_id,preserve_context=False,preserve_logical=False)
                return dict(self._change(db,row,now,'reconciled',reason='reconciled'))
            if row['status'] != 'ACTIVE':
                _error('Acquire a recovery qualification before resuming execution',row)
            keys=tuple(item.resource_key for item in ordered_resources(tuple(Resource(r['resource_type'],r['resource_key']) for r in db.execute('SELECT resource_type,resource_key FROM resource_leases WHERE holder_run_id=?',(run_id,)))))
            qualification=ExecutionToken(run_id,row['worker_id'],row['worker_generation'],row['epoch'],row['run_state_version'],row['expires_at'],keys)
            validate_in_transaction(db,qualification,now=datetime.fromisoformat(now.replace('Z','+00:00')),allow_reconciling=True)
            transition_in_transaction(db,run_id=run_id,expected_state_version=expected_state_version,target='RUNNING')
            db.execute('UPDATE resource_leases SET logical_hold=0,state_version=state_version+1 WHERE holder_run_id=? AND resource_type<>?',(run_id,'browser_context'))
            fresh=self._row(db,run_id)
            changed=self._change(db,fresh,now,'reconciled',reason='reconciled',run_state_version=fresh['run_state_version'])
            keys=tuple(item.resource_key for item in ordered_resources(tuple(Resource(r['resource_type'],r['resource_key']) for r in db.execute('SELECT resource_type,resource_key FROM resource_leases WHERE holder_run_id=?',(run_id,)))))
            return ExecutionToken(run_id,changed['worker_id'],changed['worker_generation'],changed['epoch'],fresh['run_state_version'],changed['expires_at'],keys)

    def finish(self, token, target='CANCELLED'):
        if target not in TERMINAL_STATES:
            raise BusinessError('INVALID_PARAMETER','Finish requires a legal terminal state')
        now,_=self._time()
        with connect(self.path) as db, transaction(db):
            row=validate_in_transaction(db,token,now=datetime.fromisoformat(now.replace('Z','+00:00')),allow_reconciling=target not in ('SUCCEEDED', 'PARTIAL'))
            if self._budget_enabled(db):
                budget = self.budgets.on_transition(db, token.run_id, target)
                if budget['exhausted'] and target in ('SUCCEEDED', 'PARTIAL'):
                    raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409, field=budget['reason'])
            transition_in_transaction(db,run_id=token.run_id,expected_state_version=token.state_version,target=target)
            self._release_safe(db,token.run_id,preserve_context=False,preserve_logical=False)
            if self._unresolved(db,token.run_id):
                db.execute("UPDATE resource_leases SET logical_hold=1,control_owner='none',state_version=state_version+1 WHERE holder_run_id=?",(token.run_id,))
            fresh=self._row(db,token.run_id)
            return dict(self._change(db,fresh,now,'finished',reason='cancelled' if target == 'CANCELLED' else 'finished',status='FINISHED',epoch=token.epoch+1,worker_id=None,worker_generation=None,expires_at=None,run_state_version=fresh['run_state_version']))

    def sweep_expired(self):
        now,_=self._time()
        with connect(self.path) as db, transaction(db):
            return self._expire(db,now)

    def expire_budget(self, run_id, reason):
        """Fence a due Run before cancelling external work; never invent success."""
        _text(run_id, 'run_id')
        _text(reason, 'reason')
        now, _ = self._time()
        with connect(self.path) as db, transaction(db):
            row = self._row(db, run_id)
            if row['state'] in TERMINAL_STATES:
                return dict(row)
            if not self._budget_enabled(db):
                _error('Run has no unified budget', row)
            status = self.budgets.flush_in_transaction(db, run_id)
            if not status['exhausted'] or status['reason'] != reason:
                _error('Budget deadline is not due', row)
            # Unconfirmed dispatched writes retain quarantine through failure.
            db.execute("""UPDATE write_intents SET status='UNKNOWN',updated_at=MAX(updated_at,?)
                WHERE status='INTENT' AND (originating_run_id=? OR operation_id IN (
                    SELECT a.operation_id FROM write_intent_attempts a JOIN steps s USING(run_id,step_id)
                    WHERE a.run_id=? AND s.status='INTENT'))""", (now,run_id,run_id))
            operations = db.execute('''SELECT w.operation_id FROM write_intents w JOIN runs r USING(task_id)
                WHERE r.run_id=? AND w.status='UNKNOWN' ''', (run_id,)).fetchall()
            keys = [r[0] for r in db.execute("SELECT resource_key FROM resource_leases WHERE holder_run_id=? AND resource_type IN ('site_identity','repository_write','webarena_environment')", (run_id,))]
            db.executemany('INSERT OR IGNORE INTO resource_quarantines VALUES(?,?,?)',
                           ((key, op['operation_id'], now) for op in operations for key in keys))
            db.execute("UPDATE steps SET status='UNKNOWN',ended_at=MAX(started_at,?),error_code='BUDGET_EXCEEDED' WHERE run_id=? AND step_kind='atomic_action' AND status='INTENT'", (now, run_id))
            self.budgets.on_transition(db, run_id, 'FAILED')
            transition_in_transaction(db, run_id=run_id, expected_state_version=row['run_state_version'],
                                      target='FAILED', blocked_reason=reason)
            self._release_safe(db, run_id, preserve_context=False, preserve_logical=False)
            db.execute("UPDATE resource_leases SET control_owner=CASE WHEN control_owner='human' THEN 'human' ELSE 'none' END,logical_hold=1,state_version=state_version+1 WHERE holder_run_id=?", (run_id,))
            fresh = self._row(db, run_id)
            return dict(self._change(db, fresh, now, 'finished', reason='finished', status='FINISHED',
                        epoch=row['epoch'] + 1, worker_id=None, worker_generation=None, expires_at=None,
                        run_state_version=fresh['run_state_version']))

    def sweep_budget_due(self):
        due = self.budgets.sweep_due()
        for item in due:
            try:
                self.expire_budget(item['run_id'], item['reason'])
            except BusinessError as error:
                if error.status != 409:
                    raise
        return due

    def snapshot(self):
        with connect(self.path) as db:
            return dict(queue=[dict(row) for row in db.execute('SELECT r.state_version AS run_state_version,q.*,r.state FROM scheduler_queue q JOIN runs r USING(run_id) ORDER BY queue_id')],
                        leases=[dict(row) for row in db.execute('SELECT * FROM resource_leases ORDER BY resource_key')],
                        context_reservations=[dict(row) for row in db.execute('SELECT * FROM scheduler_context_reservations ORDER BY run_id')],
                        workers=[dict(row) for row in db.execute('SELECT * FROM scheduler_workers ORDER BY generation')])
