"""Durable user intent, committed only at qualified local safe boundaries.

The HTTP acceptance receipt never grants execution authority. OS credentials
are inspected before the short start transaction; browser/model I/O is absent.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ..db import connect, transaction
from ..db.repository import canonical_json, create_run, utc_text
from ..errors import BusinessError
from ..events import OperationRequestedEvent, OperationCompletedEvent, WaitingEvent, append_event
from ..graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from ..graph.store import GraphStore
from ..scheduler.models import ExecutionToken, Resource
from ..scheduler.store import SchedulerStore, validate_in_transaction
from ..settings import service as settings_service
from ..state import TERMINAL_STATES, transition_in_transaction
from ..tasks.models import TaskContract
from .models import ControlRequest


def _error(reason='state_conflict', run=None):
    return BusinessError('STATE_CONFLICT', 'Control preconditions no longer hold', status=409,
        field=reason, current_state_version=run['state_version'] if run is not None else None,
        current_contract_version=run['contract_version'] if run is not None else None)


def _id(value):
    if (type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip()
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise BusinessError('INVALID_PARAMETER', 'Invalid control identifier')
    return value


def _public(row):
    result = {key: row[key] for key in ('operation_seq','operation_id','task_id','run_id',
        'parent_run_id','action','status','requested_state_version','accepted_run_state_version',
        'contract_version','settings_version','requested_event_id','completed_event_id',
        'reason','created_at','completed_at')}
    result['result'] = json.loads(row['result_json']) if row['result_json'] is not None else None
    for key in ('state','state_version','wait_id'):
        result[key] = (result['result'] or {}).get(key)
    return result


class ControlStore:
    def __init__(self, path: Path, *, secret_store=None, scheduler=None, resource_factory=None):
        value = Path(path)
        self.path = value if value.suffix in ('.sqlite','.sqlite3','.db') else value / 'business.sqlite3'
        self.secret_store = secret_store
        self.scheduler = scheduler or SchedulerStore(self.path)
        # Trusted composition seam for explicitly owned evaluation contexts;
        # HTTP/model content cannot select realms or resource factories.
        self.resource_factory = resource_factory

    @staticmethod
    def _run(db, run_id):
        row = db.execute('SELECT * FROM runs WHERE run_id=?', (_id(run_id),)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Run not found', status=404)
        return row

    @staticmethod
    def pending_in_transaction(db, run_id):
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='run_controls'").fetchone():
            return None
        row = db.execute("SELECT * FROM run_controls WHERE run_id=? AND status='PENDING'", (run_id,)).fetchone()
        return _public(row) if row is not None else None

    def pending(self, run_id):
        with connect(self.path) as db:
            self._run(db, run_id)
            return self.pending_in_transaction(db, run_id)

    def read(self, operation_id):
        with connect(self.path) as db:
            row = db.execute('SELECT * FROM run_controls WHERE operation_id=?', (_id(operation_id),)).fetchone()
            if row is None:
                raise BusinessError('NOT_FOUND', 'Control operation not found', status=404)
            return _public(row)

    def list_pending(self, *, limit=100):
        self._page(0, limit)
        with connect(self.path) as db:
            return [_public(row) for row in db.execute("SELECT * FROM run_controls WHERE status='PENDING' ORDER BY operation_seq LIMIT ?", (limit,))]

    @staticmethod
    def _page(after, limit):
        if type(after) is not int or not 0 <= after < 2**63-1 or type(limit) is not int or not 1 <= limit <= 1000:
            raise BusinessError('INVALID_PARAMETER', 'Invalid operation cursor or page size')

    def list_operations(self, run_id, *, after=0, limit=100):
        self._page(after, limit)
        with connect(self.path) as db:
            self._run(db, run_id)
            return [_public(row) for row in db.execute('SELECT * FROM run_controls WHERE run_id=? AND operation_seq>? ORDER BY operation_seq LIMIT ?', (run_id, after, limit))]

    def list_completed(self, *, after=0, limit=100):
        self._page(after, limit)
        with connect(self.path) as db:
            return [_public(row) for row in db.execute('''SELECT c.* FROM run_controls c JOIN runs r USING(run_id)
                WHERE c.status='APPLIED' AND c.action IN ('pause','cancel')
                AND c.operation_seq>?
                AND json_extract(c.result_json,'$.state_version')=r.state_version
                AND json_extract(c.result_json,'$.state')=r.state
                ORDER BY c.operation_seq LIMIT ?''', (after,limit))]

    @staticmethod
    def _replay(db, scope, key, digest):
        row = db.execute('SELECT * FROM run_controls WHERE request_scope=? AND idempotency_key=?', (scope, key)).fetchone()
        if row is None:
            return None
        if row['request_sha256'] != digest:
            raise BusinessError('IDEMPOTENCY_CONFLICT', 'Idempotency key was used with different content', status=409)
        return json.loads(row['accepted_json'])

    @staticmethod
    def _settings_version(db, run):
        row = db.execute('SELECT * FROM run_config_snapshots WHERE run_id=?', (run['run_id'],)).fetchone()
        if row is None:
            return 0
        if row['model_config_sha256'] != run['model_config_sha256'] or row['runtime_config_sha256'] != run['runtime_config_sha256']:
            raise _error('settings_mismatch', run)
        return row['settings_version']

    @staticmethod
    def _unsafe_resume(db, run):
        if db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'", (run['run_id'],)).fetchone():
            return 'human_control'
        pending = (db.execute("SELECT 1 FROM write_intents WHERE task_id=? AND status IN ('INTENT','UNKNOWN')", (run['task_id'],)).fetchone()
                   or db.execute('''SELECT 1 FROM resource_quarantines q JOIN write_intents w USING(operation_id)
                       WHERE w.task_id=?''', (run['task_id'],)).fetchone())
        if pending and not SchedulerStore.write_recheck_available(db, run['run_id']):
            return 'unknown_write'
        timer = db.execute('SELECT stop_reason FROM budget_timers WHERE run_id=?', (run['run_id'],)).fetchone()
        return 'budget_exhausted' if timer and timer['stop_reason'] else None

    def request(self, target_id, action, body: ControlRequest, key):
        _id(target_id); _id(key)
        try:
            body = ControlRequest.model_validate(body.model_dump())
        except (ValidationError, AttributeError):
            raise BusinessError('INVALID_PARAMETER', 'Invalid control request') from None
        if action not in ('start','retry','pause','resume','cancel') or body.idempotency_key not in (None, key):
            raise BusinessError('INVALID_PARAMETER', 'Invalid action or idempotency key')
        scope = f'{action}:{target_id}'
        digest = hashlib.sha256(canonical_json(body.model_dump(mode='json', exclude={'idempotency_key'})).encode()).hexdigest()
        with connect(self.path) as db:
            replay = self._replay(db, scope, key, digest)
            snapshot = settings_service._latest(db) if action in ('start','retry') else None
        if replay is not None:
            return replay
        if action in ('start','retry'):
            settings_service._expect(snapshot, body.settings_version)
            if snapshot is None or self.secret_store is None:
                raise BusinessError('CONFIG_NOT_READY', 'Model settings and credentials are required', status=409)
            settings_service._snapshot(snapshot)
            settings_service._ready_secret(snapshot, self.secret_store)
        with connect(self.path) as db, transaction(db):
            replay = self._replay(db, scope, key, digest)
            if replay is not None:
                return replay
            parent = None
            if action in ('start','retry'):
                settings_service._expect(settings_service._latest(db), body.settings_version)
                run, parent = self._create(db, target_id, action, body, snapshot)
            else:
                run = self._run(db, target_id)
                parent = run['parent_run_id']
                if run['state_version'] != body.expected_state_version or run['contract_version'] != body.contract_version:
                    raise _error('version_conflict', run)
                if self._settings_version(db, run) != body.settings_version:
                    raise _error('settings_version_conflict', run)
                if run['state'] in TERMINAL_STATES:
                    raise _error('terminal_run', run)
                if action == 'pause' and run['state'] not in ('RUNNING','VERIFYING','RECONCILING','PAUSED'):
                    raise _error('pause_unavailable', run)
                if action == 'resume':
                    q = db.execute('SELECT * FROM scheduler_queue WHERE run_id=?', (target_id,)).fetchone()
                    if run['state'] not in ('PAUSED','RECONCILING') or q is None or q['status'] not in ('WAITING','RECOVERY'):
                        raise _error('resume_unavailable', run)
                    reason = self._unsafe_resume(db, run)
                    if reason:
                        raise _error(reason, run)
            if self.pending_in_transaction(db, run['run_id']) is not None:
                raise _error('control_pending', run)
            op_id, now = 'control-' + uuid4().hex, utc_text()
            event = append_event(db, run_id=run['run_id'], expected_state_version=run['state_version'],
                payload=OperationRequestedEvent(operation_id=op_id, action=action))
            # Allocate the sequence before serializing the immutable acceptance.
            seq = db.execute('SELECT COALESCE(MAX(operation_seq),0)+1 FROM run_controls').fetchone()[0]
            public = dict(operation_seq=seq, operation_id=op_id, task_id=run['task_id'],run_id=run['run_id'],
                parent_run_id=parent, action=action,status='PENDING',requested_state_version=body.expected_state_version,
                accepted_run_state_version=run['state_version'],contract_version=body.contract_version,
                settings_version=body.settings_version,requested_event_id=event['event_id'],completed_event_id=None,
                reason=None,created_at=now,completed_at=None,result=None,state=None,state_version=None,wait_id=None)
            accepted = {'operation': public}
            db.execute('''INSERT INTO run_controls(operation_seq,operation_id,task_id,run_id,parent_run_id,action,
                requested_state_version,accepted_run_state_version,contract_version,settings_version,request_scope,
                idempotency_key,request_sha256,accepted_json,requested_event_id,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (seq,op_id,run['task_id'],run['run_id'],parent,action,
                body.expected_state_version,run['state_version'],body.contract_version,body.settings_version,
                scope,key,digest,canonical_json(accepted),event['event_id'],now))
            return accepted

    def _create(self, db, task_id, action, body, snapshot):
        task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if task is None:
            raise BusinessError('NOT_FOUND', 'Task not found', status=404)
        if task['preparation_status'] != 'READY' or task['current_contract_version'] != body.contract_version:
            raise _error('contract_not_ready')
        if db.execute("SELECT 1 FROM task_compilations WHERE task_id=? AND status='STARTED'", (task_id,)).fetchone():
            raise _error('compilation_pending')
        parent = task['current_run_id']
        if action == 'start':
            if parent is not None or task['state_version'] != body.expected_state_version or db.execute('SELECT 1 FROM runs WHERE task_id=?',(task_id,)).fetchone():
                raise _error('task_version_conflict')
        else:
            if parent is None:
                historical=db.execute('SELECT run_id FROM runs WHERE task_id=? ORDER BY created_at DESC,run_id DESC LIMIT 1',(task_id,)).fetchone()
                parent=historical['run_id'] if historical else None
            if parent is None:
                raise _error('retry_requires_parent')
            old = self._run(db, parent)
            if old['state'] not in TERMINAL_STATES or old['state_version'] != body.expected_state_version:
                raise _error('retry_parent_not_finished', old)
        if db.execute("SELECT 1 FROM runs WHERE task_id=? AND state NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')", (task_id,)).fetchone():
            raise _error('unfinished_run')
        contract_row = db.execute('SELECT * FROM contracts WHERE task_id=? AND contract_version=?', (task_id, body.contract_version)).fetchone()
        if contract_row is None or hashlib.sha256(contract_row['content_json'].encode()).hexdigest() != contract_row['contract_sha256']:
            raise _error('contract_mismatch')
        try:
            contract = TaskContract.model_validate_json(contract_row['content_json'])
        except ValidationError:
            raise _error('contract_not_ready') from None
        run_id, now = 'run-' + uuid4().hex, utc_text()
        create_run(db, run_id=run_id,task_id=task_id,contract_version=body.contract_version,
            graph_version=GRAPH_VERSION,graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256=snapshot['model_config_sha256'],runtime_config_sha256=snapshot['runtime_config_sha256'],
            parent_run_id=parent if action == 'retry' else None)
        db.execute('INSERT INTO run_config_snapshots VALUES(?,?,?,?,?)', (run_id,snapshot['version'],snapshot['model_config_sha256'],snapshot['runtime_config_sha256'],now))
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES(?,?)', ('budget-'+uuid4().hex,run_id))
        db.execute('UPDATE tasks SET current_run_id=?,state_version=state_version+1 WHERE task_id=?', (run_id,task_id))
        if self.resource_factory is not None:
            resources = self.resource_factory(contract, run_id)
        else:
            resources = [Resource.site_identity(source.site_id, contract.identity_ref) for source in contract.sources]
            resources.append(Resource.browser_context(run_id))
            if contract.action_policy.mode == 'repository_write':
                resources.append(Resource.repository_write(contract.action_policy.repository))
        resources = self.scheduler._resources(resources)
        if sum(r.resource_type=='browser_context' for r in resources)!=1 or Resource.browser_context(run_id).resource_key not in {r.resource_key for r in resources}:
            raise BusinessError('INVALID_PARAMETER','Exactly this Run context must be reserved',field='resources')
        db.execute('''INSERT INTO scheduler_queue(run_id,queue_class,status,run_state_version,available_at,created_at,updated_at)
            VALUES(?,?,'QUEUED',0,?,?,?)''', (run_id,'monitoring' if contract.scenario == 'monitoring' else 'ordinary',now,now,now))
        db.executemany('INSERT INTO scheduler_requirements VALUES(?,?,?,?)', [(run_id,r.resource_key,r.resource_type,int(r.logical_hold)) for r in resources])
        self.scheduler._event(db,self.scheduler._row(db,run_id),'enqueued',now)
        if action == 'retry':
            db.execute('''INSERT INTO run_retry_operations SELECT ?,operation_id,originating_run_id,status
                FROM write_intents WHERE task_id=?''', (run_id,task_id))
            if self.scheduler.write_recheck_available(db, run_id) and self._run(db, parent)['contract_sha256'] == self._run(db, run_id)['contract_sha256']:
                # The retry is admitted only as a query-only reconciliation.
                # Reusing old effects never grants a fresh mutation permission.
                self.scheduler.register_write_reconciliation(db, run_id, parent)
        return self._run(db,run_id), parent if action == 'retry' else None

    def _finish(self, db, op, *, token=None, reason=None, wait_id=None):
        run = self._run(db, op['run_id'])
        queue = db.execute('SELECT * FROM scheduler_queue WHERE run_id=?', (run['run_id'],)).fetchone()
        blocked = db.execute("SELECT recovery_seq FROM graph_recoveries WHERE run_id=? AND phase='BLOCKED' ORDER BY recovery_seq DESC LIMIT 1", (run['run_id'],)).fetchone()
        result = dict(run_id=run['run_id'],state=run['state'],state_version=run['state_version'],wait_id=wait_id,
            epoch=queue['epoch'] if queue else None,queue_revision=queue['revision'] if queue else None,
            recovery_blocked_seq=blocked['recovery_seq'] if blocked and op['action']=='resume' else None,
            side_effects=[dict(row) for row in db.execute('SELECT operation_id,originating_run_id,status FROM write_intents WHERE task_id=? ORDER BY operation_id', (run['task_id'],))])
        status = 'REJECTED' if reason else 'APPLIED'
        event = append_event(db,run_id=run['run_id'],expected_state_version=run['state_version'],
            payload=OperationCompletedEvent(operation_id=op['operation_id'],action=op['action'],status=status,result_ref=op['operation_id']))
        db.execute('''UPDATE run_controls SET status=?,completed_event_id=?,result_json=?,reason=?,completed_at=?,
            completion_worker_id=?,completion_worker_generation=?,completion_epoch=?,completion_input_state_version=?
            WHERE operation_id=? AND status='PENDING' ''', (status,event['event_id'],canonical_json(result),reason,utc_text(),
            token.worker_id if token else None,token.worker_generation if token else None,
            token.epoch if token else None,token.state_version if token else None,op['operation_id']))
        return _public(db.execute('SELECT * FROM run_controls WHERE operation_id=?', (op['operation_id'],)).fetchone())

    def _graph_progress(self, db, run_id, phase, *, wait_id=None, event=None):
        run = self._run(db, run_id)
        if run['graph_version']==GRAPH_VERSION and run['graph_state_schema_version']==STATE_SCHEMA_VERSION:
            graph = GraphStore(self.path)
            if phase=='stopped':
                # Damaged old refs may block a framework repair, but cannot
                # undo an explicit cancellation. Storage errors still abort
                # the entire control transaction.
                db.execute('SAVEPOINT optional_control_progress')
                try:
                    graph._record(db,graph._run(db,run_id),phase,diagnostic='run_finished',event=event)
                except BusinessError:
                    db.execute('ROLLBACK TO optional_control_progress')
                db.execute('RELEASE optional_control_progress')
            else:
                graph._record(db,graph._run(db,run_id),phase,wait_id=wait_id,
                    diagnostic='input_required',event=event)

    def _apply(self, db, op, token=None):
        run = self._run(db, op['run_id'])
        if run['contract_version'] != op['contract_version'] or self._settings_version(db, run) != op['settings_version']:
            return self._finish(db,op,token=token,reason='version_conflict')
        # In-flight actions and crash revocation can advance this same Run.
        # Acceptance already checked its version; pause/cancel remain durable
        # intent after revocation. Resume and new-run creation match exactly.
        if token is None and (run['state_version'] < op['accepted_run_state_version']
                or op['action'] in ('start','retry','resume') and run['state_version'] != op['accepted_run_state_version']):
            return self._finish(db,op,reason='state_conflict')
        action, now = op['action'], self.scheduler._time()[0]
        q = db.execute('SELECT * FROM scheduler_queue WHERE run_id=?', (run['run_id'],)).fetchone()
        if action in ('start','retry'):
            query_only = (action == 'retry' and run['state'] == 'RECONCILING' and q
                          and q['status'] == 'RECOVERY' and self.scheduler._write_check_run(db, run['run_id']))
            return self._finish(db,op,reason=None if query_only or run['state']=='QUEUED' and q and q['status']=='QUEUED' else 'state_conflict')
        if run['state'] in TERMINAL_STATES:
            return self._finish(db,op,token=token,reason='terminal_run')
        if action=='pause':
            if token is not None and run['state'] in ('RUNNING','VERIFYING','RECONCILING'):
                self.scheduler._defer_in_transaction(db,token,'PAUSED',now,now)
            elif token is None and run['state']=='RECONCILING' and q is not None and q['status']=='RECOVERY' and q['run_state_version']==run['state_version']:
                if db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'",(run['run_id'],)).fetchone():
                    return self._finish(db,op,reason='human_control')
                self.scheduler.budgets.on_transition(db,run['run_id'],'PAUSED')
                transition_in_transaction(db,run_id=run['run_id'],expected_state_version=run['state_version'],target='PAUSED',blocked_reason='waiting')
                db.execute("UPDATE resource_leases SET logical_hold=1,state_version=state_version+1 WHERE holder_run_id=?",(run['run_id'],))
                fresh=self.scheduler._row(db,run['run_id'])
                self.scheduler._change(db,fresh,now,'waiting',reason='waiting',status='WAITING',epoch=q['epoch']+1,
                    worker_id=None,worker_generation=None,expires_at=None,run_state_version=fresh['run_state_version'],available_at=now)
            elif run['state'] != 'PAUSED' or q is None or q['status'] != 'WAITING':
                return self._finish(db,op,token=token,reason='pause_unavailable')
            wait_id = 'control-wait-' + op['operation_id']
            result = self._finish(db,op,token=token,wait_id=wait_id)
            fresh = self._run(db,run['run_id'])
            waiting = append_event(db,run_id=run['run_id'],expected_state_version=fresh['state_version'],
                payload=WaitingEvent(wait_id=wait_id,reason='pause'))
            # The wait is deliberately the final business event at this head.
            self._graph_progress(db,run['run_id'],'wait',wait_id=wait_id,event=waiting)
            return result
        if action=='resume':
            reason = self._unsafe_resume(db,run)
            if reason or q is None or q['status'] not in ('WAITING','RECOVERY') or run['state'] not in ('PAUSED','RECONCILING'):
                return self._finish(db,op,reason=reason or 'resume_unavailable')
            if run['state']=='PAUSED':
                self.scheduler.budgets.on_transition(db,run['run_id'],'RECONCILING')
                transition_in_transaction(db,run_id=run['run_id'],expected_state_version=run['state_version'],target='RECONCILING')
            db.execute("UPDATE resource_leases SET control_owner='none',state_version=state_version+1 WHERE holder_run_id=? AND control_owner<>'human'", (run['run_id'],))
            fresh = self.scheduler._row(db,run['run_id'])
            self.scheduler._change(db,fresh,now,'resumed',status='RECOVERY',epoch=q['epoch']+1,
                worker_id=None,worker_generation=None,expires_at=None,run_state_version=fresh['run_state_version'],available_at=now)
            return self._finish(db,op)
        # Cancellation retains ambiguity and isolation instead of rolling back
        # a dispatched external action. It is legal for every nonterminal state.
        db.execute("""UPDATE write_intents SET status='UNKNOWN',updated_at=MAX(updated_at,?)
            WHERE status='INTENT' AND (originating_run_id=? OR operation_id IN (
                SELECT a.operation_id FROM write_intent_attempts a JOIN steps s USING(run_id,step_id)
                WHERE a.run_id=? AND s.status='INTENT'))""", (now,run['run_id'],run['run_id']))
        operations = db.execute("SELECT operation_id FROM write_intents WHERE task_id=? AND status='UNKNOWN'", (run['task_id'],)).fetchall()
        keys = [r[0] for r in db.execute("SELECT resource_key FROM resource_leases WHERE holder_run_id=? AND resource_type IN ('site_identity','repository_write','webarena_environment')", (run['run_id'],))]
        db.executemany('INSERT OR IGNORE INTO resource_quarantines VALUES(?,?,?)', [(key,o['operation_id'],now) for o in operations for key in keys])
        db.execute("UPDATE steps SET status='UNKNOWN',ended_at=MAX(started_at,?),error_code='CONTROL_CANCELLED' WHERE run_id=? AND step_kind='atomic_action' AND status='INTENT'", (now,run['run_id']))
        self.scheduler.budgets.on_transition(db,run['run_id'],'CANCELLED')
        transition_in_transaction(db,run_id=run['run_id'],expected_state_version=run['state_version'],target='CANCELLED',blocked_reason='cancelled')
        self.scheduler._release_safe(db,run['run_id'],preserve_context=False,preserve_logical=False)
        db.execute("UPDATE resource_leases SET control_owner=CASE WHEN control_owner='human' THEN 'human' ELSE 'none' END,logical_hold=1,state_version=state_version+1 WHERE holder_run_id=?", (run['run_id'],))
        if q is not None:
            fresh=self.scheduler._row(db,run['run_id'])
            self.scheduler._change(db,fresh,now,'finished',reason='cancelled',status='FINISHED',epoch=q['epoch']+1,
                worker_id=None,worker_generation=None,expires_at=None,run_state_version=fresh['run_state_version'])
        result=self._finish(db,op,token=token)
        self._graph_progress(db,run['run_id'],'stopped')
        return result

    def apply_at_boundary(self, token):
        if not isinstance(token, ExecutionToken):
            raise BusinessError('RESOURCE_CONFLICT','A current execution token is required',status=409)
        with connect(self.path) as db, transaction(db):
            pending=self.pending_in_transaction(db,token.run_id)
            if pending is None:
                return None
            validate_in_transaction(db,token,allow_reconciling=True)
            op=db.execute('SELECT * FROM run_controls WHERE operation_id=?',(pending['operation_id'],)).fetchone()
            return self._apply(db,op,token)

    apply_pending=apply_at_boundary

    def apply_idle(self, run_id, *, expected_operation_id=None):
        with connect(self.path) as db, transaction(db):
            self._run(db,run_id)
            q=db.execute('SELECT * FROM scheduler_queue WHERE run_id=?',(run_id,)).fetchone()
            if q is not None and q['status']=='ACTIVE':
                return None
            op=db.execute("SELECT * FROM run_controls WHERE run_id=? AND status='PENDING'",(run_id,)).fetchone()
            if expected_operation_id is not None and (op is None or op['operation_id']!=expected_operation_id):
                return None
            return self._apply(db,op) if op is not None else None

    def apply_idle_pending(self, *, exclude_run_ids=(), limit=100):
        excluded=set(exclude_run_ids)
        result=[]
        for op in self.list_pending(limit=limit):
            if op['run_id'] not in excluded:
                completed=self.apply_idle(op['run_id'])
                if completed is not None:
                    result.append(completed)
        return result

    @staticmethod
    def resume_authorized_in_transaction(db,run_id,blocked_seq):
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='run_controls'").fetchone():
            return False
        op=db.execute("SELECT * FROM run_controls WHERE run_id=? AND action='resume' AND status='APPLIED' ORDER BY operation_seq DESC LIMIT 1",(run_id,)).fetchone()
        q=db.execute('SELECT q.*,r.state,r.state_version,r.contract_version FROM scheduler_queue q JOIN runs r USING(run_id) WHERE q.run_id=?',(run_id,)).fetchone()
        latest=db.execute("SELECT recovery_seq FROM graph_recoveries WHERE run_id=? AND phase='BLOCKED' ORDER BY recovery_seq DESC LIMIT 1",(run_id,)).fetchone()
        if op is None or q is None or latest is None:
            return False
        proof=json.loads(op['result_json'])
        receipt_epoch=proof.get('epoch')
        return (type(blocked_seq) is int and latest[0]==blocked_seq==proof.get('recovery_blocked_seq')
            and q['state']=='RECONCILING' and q['state_version']==proof.get('state_version')
            and q['contract_version']==op['contract_version'] and q['status'] in ('RECOVERY','ACTIVE')
            and type(receipt_epoch) is int
            and q['epoch']==receipt_epoch+(1 if q['status']=='ACTIVE' else 0)
            and q['run_state_version']==q['state_version'])

    def completion_receipt(self,token):
        if not isinstance(token,ExecutionToken):
            return False
        now=self.scheduler._time()[0]
        with connect(self.path) as db:
            db.execute('BEGIN')
            worker=db.execute('SELECT * FROM scheduler_workers WHERE worker_id=?',(token.worker_id,)).fetchone()
            op=db.execute("SELECT * FROM run_controls WHERE run_id=? AND status='APPLIED' AND action IN ('pause','cancel') ORDER BY operation_seq DESC LIMIT 1",(token.run_id,)).fetchone()
            q=db.execute('SELECT q.*,r.state,r.state_version,r.contract_version FROM scheduler_queue q JOIN runs r USING(run_id) WHERE q.run_id=?',(token.run_id,)).fetchone()
            event=db.execute('SELECT * FROM scheduler_events WHERE run_id=? ORDER BY event_id DESC LIMIT 1',(token.run_id,)).fetchone()
            if worker is None or op is None or q is None or event is None:
                return False
            proof=json.loads(op['result_json'])
            return (worker['state']=='ACTIVE' and worker['generation']==token.worker_generation
                and worker['expires_at']>now and token.expires_at>now
                and op['completion_worker_id']==token.worker_id and op['completion_worker_generation']==token.worker_generation
                and op['completion_epoch']==token.epoch and token.state_version<=op['completion_input_state_version']
                and q['epoch']==token.epoch+1==proof.get('epoch')
                and q['state']==proof.get('state') and q['state_version']==proof.get('state_version')
                and q['contract_version']==op['contract_version'] and q['run_state_version']==q['state_version']
                and q['worker_id'] is None and q['worker_generation'] is None and q['expires_at'] is None
                and q['status']==('WAITING' if op['action']=='pause' else 'FINISHED')
                and event['epoch']==q['epoch'] and event['revision']==q['revision']==proof.get('queue_revision')
                and event['event_type']==('waiting' if op['action']=='pause' else 'finished'))
