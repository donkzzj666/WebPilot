"""Short SQLite transactions for session ownership, lifecycle and recheck events.

The manager owns the process lock before orphan recovery. This registry never
loads browser state, contacts Keychain, or keeps a transaction across an await.
"""
from dataclasses import asdict
from pathlib import Path
import re
from uuid import UUID, uuid4

from ..db import connect, transaction
from ..db.repository import utc_text
from ..errors import BusinessError
from .models import LOSS_REASONS, SessionInfo, SessionOwner, identifier

LIVE_STATES = ('OPENING', 'OPEN', 'CLOSING')


def _owner(owner):
    if not isinstance(owner, SessionOwner):
        raise BusinessError('INVALID_PARAMETER', 'SessionOwner is required', field='owner')
    return SessionOwner(**asdict(owner))


def _uuid(value):
    try:
        parsed = UUID(value) if type(value) is str else None
        if parsed is None or parsed.version != 4 or str(parsed) != value:
            raise ValueError
    except (ValueError, TypeError):
        raise BusinessError('INVALID_PARAMETER', 'Invalid authentication reference', field='auth_ref') from None
    return value


class SessionRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _row(self, db, session_id):
        row = db.execute('''SELECT s.*,a.sha256 AS auth_sha256 FROM browser_sessions s
            LEFT JOIN browser_auth_snapshots a ON a.auth_ref=s.auth_ref WHERE s.session_id=?''', (session_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Managed session not found', status=404)
        return row

    @staticmethod
    def _info(row):
        return SessionInfo(session_id=row['session_id'],
            owner=SessionOwner(row['owner_kind'], row['owner_id'], row['site_id'], row['identity_ref'], row['realm']),
            **{key: row[key] for key in ('manager_id', 'state', 'generation', 'state_version', 'auth_ref',
                                        'auth_sha256', 'restored_from_session_id', 'created_at', 'closed_at', 'loss_reason')},
            requires_identity_check=bool(row['requires_identity_check']),
            requires_business_check=bool(row['requires_business_check']))

    def _owned(self, db, session_id, owner):
        row = self._row(db, session_id)
        if self._info(row).owner != _owner(owner):
            raise BusinessError('FORBIDDEN', 'Session belongs to a different owner or scope', status=403)
        return row

    def _managed(self, db, session_id, manager_id):
        row = self._row(db, session_id)
        if row['manager_id'] != identifier(manager_id, 'manager_id'):
            raise BusinessError('STATE_CONFLICT', 'Session belongs to another manager generation', status=409)
        return row

    @staticmethod
    def _event(db, row, kind, reason=None):
        db.execute('''INSERT INTO browser_session_events(session_id,state_version,event_type,reason,occurred_at)
            VALUES (?,?,?,?,?)''', (row['session_id'], row['state_version'], kind, reason, utc_text()))

    def get(self, session_id, owner):
        with connect(self.path) as db:
            return self._info(self._owned(db, session_id, owner))

    def list_owned(self, manager_id):
        identifier(manager_id, 'manager_id')
        with connect(self.path) as db:
            return [self._info(row) for row in db.execute('''SELECT s.*,a.sha256 AS auth_sha256
                FROM browser_sessions s LEFT JOIN browser_auth_snapshots a ON a.auth_ref=s.auth_ref
                WHERE s.manager_id=? ORDER BY s.created_at,s.session_id''', (manager_id,))]

    @staticmethod
    def _execution(db, owner, execution_token, *, allow_reconciling=False):
        """Fresh durable authorization for scheduled Runs; fixtures remain local."""
        if owner.kind != 'run':
            if execution_token is not None:
                raise BusinessError('INVALID_PARAMETER', 'Only a Run can use execution qualification',
                                    field='execution_token')
            return None
        scheduler_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_queue'").fetchone()
        if not scheduler_exists or db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (owner.owner_id,)).fetchone() is None:
            if execution_token is not None:
                raise BusinessError('STATE_CONFLICT', 'Run has no durable scheduling qualification', status=409)
            return None
        from ..scheduler.models import ExecutionToken, Resource
        from ..scheduler.store import validate_in_transaction
        if not isinstance(execution_token, ExecutionToken) or execution_token.run_id != owner.owner_id:
            raise BusinessError('STATE_CONFLICT', 'Current execution qualification is required', status=409)
        scope = Resource.site_identity(owner.site_id, owner.identity_ref, realm=owner.realm).resource_key
        validate_in_transaction(db, execution_token, resource=scope, allow_reconciling=allow_reconciling)
        validate_in_transaction(db, execution_token, resource=Resource.browser_context(owner.owner_id).resource_key,
                                allow_reconciling=allow_reconciling)
        return execution_token

    def validate_execution(self, owner, execution_token=None, *, allow_reconciling=False):
        owner = _owner(owner)
        with connect(self.path) as db, transaction(db):
            return self._execution(db, owner, execution_token, allow_reconciling=allow_reconciling)

    @staticmethod
    def _confirmed_gateway_history(db, token, step_id):
        """Resolve a fenced historical write, without granting cached reuse."""
        from ..writes.store import WriteProtocolStore
        old = db.execute('''SELECT g.operation_id,g.external_write,g.epoch,s.epoch AS step_epoch,
            s.status,s.started_at,w.status AS operation_status FROM gateway_attempts g
            JOIN steps s USING(step_id,run_id) JOIN write_intents w USING(operation_id)
            WHERE g.step_id=? AND g.run_id=?''', (step_id, token.run_id)).fetchone()
        if (old is None or old['external_write'] != 1 or old['status'] != 'INTENT'
                or old['operation_status'] != 'CONFIRMED' or old['epoch'] >= token.epoch
                or old['step_epoch'] != old['epoch']):
            return None
        check = WriteProtocolStore.verified_check_for_run(db, old['operation_id'], token.run_id)
        if (check is None or any(check[key] != getattr(token, key) for key in
                ('worker_id', 'worker_generation', 'epoch'))
                or not WriteProtocolStore._version_current(db, token, check)
                or old['started_at'] > check['created_at']
                or not WriteProtocolStore.is_resolved_step(db, step_id)):
            return None
        # The effect is already proven and its old executor is fenced. A new
        # target observation or elapsed cache lifetime cannot make it in-flight
        # again; ordinary cached operation reuse retains its separate limits.
        return check

    def validate_gateway_dispatch(self, session_id, owner, *, execution_token, step_id, operation_id):
        """Authorize only the current, already-journaled write dispatch.

        Normal qualification rejects unresolved INTENTs. The private gateway
        needs one narrower exception for the very intent it committed before
        issuing that action. This check creates no permission or intent, renews
        no token, and never permits reconciliation or another pending operation.
        Its caller must separately confine the permission to one active dispatch.
        """
        from ..scheduler.models import ExecutionToken, canonical_site
        owner = _owner(owner)
        identifier(session_id, 'session_id')
        identifier(step_id, 'step_id')
        identifier(operation_id, 'operation_id')
        if (owner.kind != 'run' or not isinstance(execution_token, ExecutionToken)
                or execution_token.run_id != owner.owner_id):
            raise BusinessError('RESOURCE_CONFLICT', 'Current gateway dispatch qualification is required', status=409)

        def denied():
            raise BusinessError('RESOURCE_CONFLICT', 'Gateway intent no longer authorizes this dispatch', status=409)

        # Original bytes are checked before taking the short writer lock. The
        # transaction below rechecks the same immutable proof and live token.
        confirmed_history = {}
        with connect(self.path) as db:
            self._execution(db, owner, execution_token, allow_reconciling=True)
            protocol_available = bool(db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='write_protocol_claims'").fetchone())
            if protocol_available:
                previous = db.execute('''SELECT g.step_id FROM gateway_attempts g JOIN steps s USING(step_id,run_id)
                    WHERE g.run_id=? AND g.external_write=1 AND g.epoch<? AND s.status='INTENT' ''',
                    (execution_token.run_id, execution_token.epoch)).fetchall()
                for row in previous:
                    check = self._confirmed_gateway_history(db, execution_token, row['step_id'])
                    if check is not None:
                        confirmed_history[row['step_id']] = (check['operation_id'], check['check_id'])
        if confirmed_history:
            from ..writes.store import WriteProtocolStore
            protocol = WriteProtocolStore(self.path)
            for historical_operation, check_id in set(confirmed_history.values()):
                proof = protocol.assert_proof_integrity(historical_operation, run_id=execution_token.run_id)
                if proof is None or proof['check_id'] != check_id:
                    denied()

        with connect(self.path) as db, transaction(db):
            token = self._execution(db, owner, execution_token, allow_reconciling=True)
            session = self._owned(db, session_id, owner)
            attempt = db.execute('''SELECT g.*,s.status AS step_status,s.epoch AS step_epoch,
                s.step_kind,s.input_snapshot_id,r.state AS run_state,w.status AS operation_status,
                w.originating_run_id,w.identity_ref AS operation_identity,w.target AS operation_resource
                FROM gateway_attempts g JOIN steps s USING(step_id,run_id)
                JOIN runs r USING(run_id) LEFT JOIN write_intents w USING(operation_id)
                WHERE g.step_id=?''', (step_id,)).fetchone()
            if (token is None or session['state'] != 'OPEN' or attempt is None
                    or attempt['run_state'] not in ('RUNNING', 'VERIFYING')
                    or attempt['run_id'] != token.run_id or attempt['session_id'] != session_id
                    or attempt['manager_id'] != session['manager_id']
                    or attempt['session_generation'] != session['generation']
                    or attempt['worker_id'] != token.worker_id
                    or attempt['worker_generation'] != token.worker_generation
                    or attempt['epoch'] != token.epoch or attempt['state_version'] != token.state_version
                    or attempt['step_epoch'] != token.epoch or attempt['step_kind'] != 'atomic_action'
                    or attempt['input_snapshot_id'] != attempt['snapshot_id']
                    or attempt['step_status'] != 'INTENT' or attempt['external_write'] != 1
                    or attempt['operation_id'] != operation_id or attempt['operation_status'] != 'INTENT'
                    or not db.execute('''SELECT 1 FROM write_intent_attempts
                        WHERE operation_id=? AND run_id=? AND step_id=?''',
                        (operation_id, token.run_id, step_id)).fetchone()
                    or attempt['operation_identity'] != session['identity_ref']
                    or attempt['operation_resource'] not in token.resources):
                denied()
            pending = db.execute('''SELECT w.operation_id FROM write_intents w JOIN runs r USING(task_id)
                WHERE r.run_id=? AND w.status IN ('INTENT','UNKNOWN')''', (token.run_id,)).fetchall()
            if len(pending) != 1 or pending[0]['operation_id'] != operation_id:
                denied()
            pending_steps = db.execute('''SELECT g.step_id FROM gateway_attempts g
                JOIN steps s USING(step_id,run_id) WHERE g.run_id=? AND s.status='INTENT' ''',
                                       (token.run_id,)).fetchall()
            if not any(row['step_id'] == step_id for row in pending_steps):
                denied()
            from ..graph.recovery import RecoveryStore
            resolved_reads = RecoveryStore(self.path.parent).resolved_read_steps(db, token.run_id)
            for previous in pending_steps:
                if previous['step_id'] == step_id or previous['step_id'] in resolved_reads:
                    continue
                historical = confirmed_history.get(previous['step_id'])
                if historical is not None:
                    check = self._confirmed_gateway_history(db, token, previous['step_id'])
                    if check is not None and (check['operation_id'], check['check_id']) == historical:
                        continue
                if not protocol_available:
                    denied()
                # The historical INTENT survives a crash. Only a separately
                # consumed absence proof can qualify its replacement, and the
                # old epoch must already be fenced. Do not ignore a live call.
                proof = db.execute('''SELECT 1 FROM write_protocol_dispatches d
                    JOIN write_protocol_checks c USING(check_id)
                    JOIN gateway_attempts old ON old.step_id=?
                    JOIN steps s ON s.step_id=old.step_id AND s.run_id=old.run_id
                    WHERE d.step_id=? AND d.operation_id=? AND d.run_id=?
                    AND d.worker_id=? AND d.worker_generation=? AND d.epoch=?
                    AND d.state_version=? AND c.effective_status='NOT_APPLIED'
                    AND c.operation_id=d.operation_id AND c.run_id=d.run_id
                    AND c.epoch=d.epoch AND old.operation_id=d.operation_id
                    AND old.external_write=1 AND old.epoch<d.epoch AND s.started_at<=c.created_at''',
                    (previous['step_id'], step_id, operation_id, token.run_id, token.worker_id,
                     token.worker_generation, token.epoch, token.state_version)).fetchone()
                if proof is None:
                    denied()
            reservation = db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?',
                                     (token.run_id,)).fetchone()
            if (reservation is None or reservation['session_id'] != session_id
                    or reservation['worker_id'] != token.worker_id
                    or reservation['worker_generation'] != token.worker_generation
                    or reservation['epoch'] != token.epoch):
                denied()
            identity = db.execute('SELECT * FROM identities WHERE identity_ref=?',
                                  (session['identity_ref'],)).fetchone()
            if (identity is None or identity['state'] != 'VERIFIED' or identity['realm'] != session['realm']
                    or canonical_site(identity['site_id']) != canonical_site(session['site_id'])):
                denied()
            if db.execute('''SELECT 1 FROM resource_quarantines q JOIN resource_leases l USING(resource_key)
                    WHERE l.holder_run_id=? LIMIT 1''', (token.run_id,)).fetchone():
                denied()
            stopped = db.execute('SELECT stop_reason FROM budget_timers WHERE run_id=?', (token.run_id,)).fetchone()
            if stopped is None:
                denied()
            if stopped['stop_reason'] is not None:
                raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409, field=stopped['stop_reason'])
            return token

    @staticmethod
    def _login_scope(db, owner):
        if owner.kind != 'login':
            return
        from ..scheduler.models import canonical_site, resource_site
        scope = owner.realm, canonical_site(owner.site_id)
        for lease in db.execute("SELECT resource_key FROM resource_leases WHERE resource_type='site_identity'"):
            try:
                leased_scope = resource_site(lease['resource_key'])
            except BusinessError:
                # Historical pre-scheduler lease names are not guessed into a
                # website scope. New scheduler acquisitions use normalized keys.
                continue
            if leased_scope == scope:
                raise BusinessError('RESOURCE_CONFLICT', 'This site is reserved by a task Run', status=409)

    def reserve(self, manager_id, owner, *, auth_ref=None, replaces=None, execution_token=None):
        identifier(manager_id, 'manager_id')
        owner = _owner(owner)
        if auth_ref is not None:
            _uuid(auth_ref)
        with connect(self.path) as db, transaction(db):
            if owner.kind == 'run':
                run = db.execute('SELECT state FROM runs WHERE run_id=?', (owner.owner_id,)).fetchone()
                if run is None:
                    raise BusinessError('NOT_FOUND', 'Session Run does not exist', status=404)
                if run['state'] in ('SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'):
                    raise BusinessError('STATE_CONFLICT', 'A terminal Run cannot create a new execution session', status=409)
            token = self._execution(db, owner, execution_token, allow_reconciling=True)
            self._login_scope(db, owner)
            capacity = db.execute("SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING')").fetchone()[0]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_context_reservations'").fetchone():
                capacity += db.execute('SELECT count(*) FROM scheduler_context_reservations WHERE session_id IS NULL').fetchone()[0]
            if (capacity >= 4 and token is None) or capacity > 4:
                raise BusinessError('RESOURCE_CONFLICT', 'Four managed browser contexts are already reserved', status=409)
            generation = 1
            if replaces is not None:
                previous = self._owned(db, replaces, owner)
                if previous['state'] not in ('CLOSED', 'LOST'):
                    raise BusinessError('STATE_CONFLICT', 'Close the previous session before replacing it', status=409)
                generation = previous['generation'] + 1
            if auth_ref is not None:
                snapshot = db.execute('''SELECT 1 FROM browser_auth_snapshots WHERE auth_ref=? AND
                    site_id=? AND identity_ref IS ? AND realm=?''',
                    (auth_ref, owner.site_id, owner.identity_ref, owner.realm)).fetchone()
                if snapshot is None:
                    raise BusinessError('FORBIDDEN', 'Authentication reference is not registered for this scope', status=403)
            session_id = 'context-' + uuid4().hex
            if token is not None:
                # The deferred FK permits binding before browser INSERT. Both
                # writes commit together and turn a reservation into one live
                # context instead of briefly charging two places in the pool.
                changed = db.execute('''UPDATE scheduler_context_reservations SET session_id=?
                    WHERE run_id=? AND worker_id=? AND worker_generation=? AND epoch=? AND session_id IS NULL''',
                    (session_id, token.run_id, token.worker_id, token.worker_generation, token.epoch)).rowcount
                if changed != 1:
                    raise BusinessError('RESOURCE_CONFLICT', 'Run context reservation is already consumed or stale',
                                        status=409)
            db.execute('''INSERT INTO browser_sessions(session_id,owner_kind,owner_id,run_id,site_id,identity_ref,
                realm,manager_id,state,generation,state_version,auth_ref,restored_from_session_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,'OPENING',?,0,?,?,?)''',
                (session_id, owner.kind, owner.owner_id, owner.owner_id if owner.kind == 'run' else None,
                 owner.site_id, owner.identity_ref, owner.realm, manager_id, generation, auth_ref, replaces, utc_text()))
            row = self._row(db, session_id)
            self._event(db, row, 'reserved')
            self._event(db, row, 'recheck_required', 'new_context')
            return self._info(row)

    def _transition(self, db, row, state, kind, reason=None):
        terminal = state in ('CLOSED', 'LOST')
        db.execute('''UPDATE browser_sessions SET state=?,state_version=state_version+1,
            closed_at=?,loss_reason=?,requires_identity_check=1,requires_business_check=1 WHERE session_id=?''',
            (state, utc_text() if terminal else None, reason if state == 'LOST' else None, row['session_id']))
        updated = self._row(db, row['session_id'])
        self._event(db, updated, kind, reason)
        if state == 'LOST':
            self._event(db, updated, 'recheck_required', reason)
        return self._info(updated)

    def opened(self, session_id, manager_id, *, execution_token=None):
        with connect(self.path) as db, transaction(db):
            row = self._managed(db, session_id, manager_id)
            self._execution(db, self._info(row).owner, execution_token, allow_reconciling=True)
            if row['state'] == 'OPEN':
                return self._info(row)
            if row['state'] != 'OPENING':
                raise BusinessError('STATE_CONFLICT', 'Session is no longer opening', status=409)
            return self._transition(db, row, 'OPEN', 'opened')

    def closing(self, session_id, manager_id):
        with connect(self.path) as db, transaction(db):
            row = self._managed(db, session_id, manager_id)
            if row['state'] in ('CLOSING', 'CLOSED', 'LOST'):
                return self._info(row)
            return self._transition(db, row, 'CLOSING', 'closing')

    def closing_preparation(self, session_id, manager_id, owner, *, execution_token,
                            paused_settlement=False):
        """Commit preparation cleanup with its last ownership check.

        BEGIN IMMEDIATE excludes another SQLite writer until CLOSING commits.
        Browser disposal starts only after this durable intent; a refused
        qualification must never enter the manager's destructive cleanup path.
        """
        owner = _owner(owner)
        from ..scheduler.models import ExecutionToken
        if (owner.kind != 'run' or not isinstance(execution_token, ExecutionToken)
                or execution_token.run_id != owner.owner_id):
            raise BusinessError('RESOURCE_CONFLICT', 'Cleanup belongs to another Run', status=409)
        with connect(self.path) as db, transaction(db):
            row = self._managed(db, session_id, manager_id)
            self._owned(db, session_id, owner)
            if paused_settlement:
                from ..scheduler.store import SchedulerStore
                # This read-only receipt uses the same committed state while
                # our writer lock prevents a generation/control handoff.
                allowed = SchedulerStore(self.path).settlement(execution_token)
                run = db.execute('SELECT state FROM runs WHERE run_id=?', (owner.owner_id,)).fetchone()
                human = db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'",
                                   (owner.owner_id,)).fetchone()
                if not allowed or run is None or run['state'] != 'PAUSED' or human is not None:
                    raise BusinessError('RESOURCE_CONFLICT', 'Preparation cleanup ownership changed', status=409)
            else:
                self._execution(db, owner, execution_token)
            if row['state'] in ('CLOSING', 'CLOSED', 'LOST'):
                return self._info(row)
            return self._transition(db, row, 'CLOSING', 'closing')

    def closing_terminal(self, session_id, manager_id, owner):
        """Commit automatic terminal cleanup without taking human control."""
        from ..state import TERMINAL_STATES
        owner = _owner(owner)
        with connect(self.path) as db, transaction(db):
            row = self._managed(db, session_id, manager_id)
            self._owned(db, session_id, owner)
            run = db.execute('SELECT state FROM runs WHERE run_id=?', (owner.owner_id,)).fetchone()
            human = db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'",
                               (owner.owner_id,)).fetchone()
            if owner.kind != 'run' or run is None or run['state'] not in TERMINAL_STATES or human is not None:
                raise BusinessError('RESOURCE_CONFLICT', 'Terminal cleanup ownership changed', status=409)
            if row['state'] in ('CLOSING', 'CLOSED', 'LOST'):
                return self._info(row)
            return self._transition(db, row, 'CLOSING', 'closing')

    def closed(self, session_id, manager_id):
        with connect(self.path) as db, transaction(db):
            row = self._managed(db, session_id, manager_id)
            if row['state'] in ('CLOSED', 'LOST'):
                return self._info(row)
            if row['state'] != 'CLOSING':
                raise BusinessError('STATE_CONFLICT', 'Normal close must be requested before it is confirmed', status=409)
            return self._transition(db, row, 'CLOSED', 'closed')

    def lost(self, session_id, manager_id, reason='unknown'):
        reason = reason if type(reason) is str and reason in LOSS_REASONS else 'unknown'
        with connect(self.path) as db, transaction(db):
            row = self._managed(db, session_id, manager_id)
            if row['state'] in ('CLOSED', 'LOST'):
                return self._info(row)
            return self._transition(db, row, 'LOST', 'lost', reason)

    def recover_orphans(self, manager_id):
        """Call only after obtaining the exclusive session manager process lock."""
        identifier(manager_id, 'manager_id')
        with connect(self.path) as db, transaction(db):
            rows = db.execute("SELECT * FROM browser_sessions WHERE manager_id<>? AND state IN ('OPENING','OPEN','CLOSING')",
                              (manager_id,)).fetchall()
            return [self._transition(db, row, 'LOST', 'lost', 'manager_restarted') for row in rows]

    def bind_auth(self, session_id, manager_id, ref, sha256, *, execution_token=None):
        _uuid(ref)
        if type(sha256) is not str or re.fullmatch('[0-9a-f]{64}', sha256) is None:
            raise BusinessError('INVALID_PARAMETER', 'Invalid authentication digest', field='auth_ref')
        with connect(self.path) as db, transaction(db):
            row = self._managed(db, session_id, manager_id)
            self._execution(db, self._info(row).owner, execution_token)
            if row['state'] != 'OPEN':
                raise BusinessError('STATE_CONFLICT', 'Only an open session can publish authentication state', status=409)
            # A saved file is immutable. Failure leaves a safe unreferenced encrypted
            # file/key; do not guess whether an uncertain commit needs deletion.
            db.execute('INSERT INTO browser_auth_snapshots VALUES (?,?,?,?,?,?)',
                       (ref, row['site_id'], row['identity_ref'], row['realm'], sha256, utc_text()))
            db.execute('UPDATE browser_sessions SET auth_ref=?,state_version=state_version+1 WHERE session_id=?',
                       (ref, session_id))
            updated = self._row(db, session_id)
            self._event(db, updated, 'auth_saved')
            return self._info(updated)
