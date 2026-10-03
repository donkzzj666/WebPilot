"""Reconcile graph hints against immutable business facts before fresh execution.

No method dispatches a browser action, retries a step, clears a failed outcome,
resets a budget, changes a contract, or grants RUNNING authority. Receipts only
let the trusted scheduler know what was checked under its current qualification.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from pydantic import ValidationError

from ..budgets.store import BudgetStore
from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..evidence.store import EvidenceStore
from ..models.schema import RunCheckpoint
from ..scheduler.models import ExecutionToken
from ..scheduler.store import validate_in_transaction
from ..state import TERMINAL_STATES
from ..tasks.models import TaskContract
from ..verification.rules import _resolve, _MISSING
from ..verification.service import VerificationService
from .models import GRAPH_VERSION, STATE_SCHEMA_VERSION, validate_graph_state
from .store import GraphStore, _identifier

REASONS = frozenset(('graph_state_invalid','graph_version_mismatch','contract_mismatch','graph_ahead',
    'event_missing','event_version_mismatch','checkpoint_mismatch','snapshot_missing','progress_mismatch',
    'summary_mismatch','evidence_missing','evidence_corrupt','unknown_write','human_control',
    'object_mismatch','object_version_mismatch','identity_mismatch','budget_exhausted',
    'recovery_not_completed','session_unavailable','source_scope_mismatch','proof_missing'))


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _deny(reason):
    return BusinessError('STATE_CONFLICT','Recovery blocked: ' + reason,status=409,field=reason)


class RecoveryStore:
    def __init__(self, path: Path, *, budgets=None):
        value = Path(path)
        self.path = value if value.suffix in ('.sqlite','.sqlite3','.db') else value / 'business.sqlite3'
        self.data_dir = self.path.parent
        self.graph = GraphStore(self.path)
        self.evidence = EvidenceStore(self.data_dir)
        self.verifier = VerificationService(self.data_dir)
        self.budgets = budgets if budgets is not None else BudgetStore(self.path)

    @staticmethod
    def _raw_run(db, run_id):
        """Minimal bookkeeping facts remain available for an invalid graph."""
        _identifier(run_id,'run_id')
        row=db.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
        if row is None:raise BusinessError('NOT_FOUND','Run not found',status=404)
        return dict(row)

    @staticmethod
    def _compatibility(db, run):
        if (run['graph_version']!=GRAPH_VERSION or run['graph_state_schema_version']!=STATE_SCHEMA_VERSION
                or run['thread_id']!=run['run_id']):return 'graph_version_mismatch'
        row=db.execute('SELECT content_json FROM contracts WHERE task_id=? AND contract_version=?',
                       (run['task_id'],run['contract_version'])).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest()!=run['contract_sha256']:
            return 'contract_mismatch'
        try:TaskContract.model_validate_json(row[0])
        except ValidationError:return 'contract_mismatch'
        return None

    @staticmethod
    def _known_version(db, run, object_id):
        """An incomplete later capture cannot erase a known object constraint."""
        row=db.execute('''SELECT current_object_version FROM run_checkpoints
            WHERE run_id=? AND task_id=? AND contract_version=? AND current_object_id=?
            AND identity_ref IS ? AND current_object_version IS NOT NULL
            ORDER BY saved_at DESC,checkpoint_id DESC LIMIT 1''',
            (run['run_id'],run['task_id'],run['contract_version'],object_id,run['contract'].identity_ref)).fetchone()
        return row[0] if row is not None else None

    def _boundary(self, db, run, snapshot_id):
        """Hash SQL refs read with the original, before the short writer phase.

        No artifact or browser I/O occurs while a writer transaction is held.
        Heartbeat and timer bookkeeping do not change these business refs.
        """
        run_id=run['run_id']
        checkpoint=self.graph._checkpoint(db,run_id)
        event=self.graph._event(db,run)
        verification=db.execute('SELECT verification_id,content_sha256,state_version FROM run_verifications '
            'WHERE run_id=? ORDER BY created_at DESC,verification_id DESC LIMIT 1',(run_id,)).fetchone()
        steps=[dict(row) for row in db.execute('SELECT step_id,sequence,status,epoch,action_json,actual_result_json,error_code '
            'FROM steps WHERE run_id=? ORDER BY sequence',(run_id,))]
        operations=[dict(row) for row in db.execute('SELECT operation_id,status FROM write_intents '
            'WHERE task_id=? ORDER BY operation_id',(run['task_id'],))]
        head=db.execute('SELECT * FROM gateway_page_heads WHERE run_id=?',(run_id,)).fetchone()
        reservation=db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?',(run_id,)).fetchone()
        session=None
        if reservation and reservation['session_id']:
            session=db.execute('SELECT * FROM browser_sessions WHERE session_id=?',(reservation['session_id'],)).fetchone()
        identity=None
        if run['contract'].identity_ref:
            identity=db.execute('SELECT * FROM identities WHERE identity_ref=?',(run['contract'].identity_ref,)).fetchone()
        view=self.evidence.filtered_observation_row(db,snapshot_id,run_id) if snapshot_id else None
        evidence=[self.evidence._metadata(db,row[0],run_id) for row in db.execute(
            'SELECT evidence_id FROM evidence WHERE run_id=? ORDER BY evidence_id',(run_id,))]
        return _hash(dict(version=run['state_version'],state=run['state'],contract=run['contract_sha256'],
            event=event['event_id'],checkpoint=checkpoint.checkpoint_id if checkpoint else None,
            verification=dict(verification) if verification else None,steps=steps,operations=operations,
            head=dict(head) if head else None,reservation=dict(reservation) if reservation else None,
            session=dict(session) if session else None,identity=dict(identity) if identity else None,
            snapshot=view,evidence=evidence))

    @staticmethod
    def claim_blocked_sql(alias='q'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',alias):
            raise ValueError('A SQL identifier is required')
        return ("NOT EXISTS(SELECT 1 FROM graph_recoveries gr WHERE gr.run_id=" + alias
            + ".run_id AND gr.phase='BLOCKED' AND gr.recovery_seq=(SELECT MAX(g2.recovery_seq)"
              " FROM graph_recoveries g2 WHERE g2.run_id=" + alias + '.run_id))')

    @staticmethod
    def _qualified(db, token, *, state='RECONCILING'):
        if not isinstance(token,ExecutionToken):
            raise BusinessError('RESOURCE_CONFLICT','Current recovery qualification is required',status=409)
        row = validate_in_transaction(db,token,allow_reconciling=True)
        if row['state'] != state:
            raise _deny('recovery_not_completed')
        return row

    @staticmethod
    def _row(row):
        result = dict(row)
        if hashlib.sha256(result['facts_json'].encode()).hexdigest() != result['facts_sha256']:
            raise _deny('checkpoint_mismatch')
        result['facts'] = json.loads(result.pop('facts_json'))
        return result

    def _save(self, db, token, run, phase, facts, reason=None, *, input_sha=None):
        event = self.graph._event(db,run)
        key = input_sha or _hash([phase,reason])
        old = db.execute('''SELECT * FROM graph_recoveries WHERE run_id=? AND epoch=?
            AND phase=? AND input_sha256=?''',(token.run_id,token.epoch,phase,key)).fetchone()
        if old is not None:
            if phase=='COMPLETE' and old['facts_sha256']!=_hash(facts):
                raise _deny('recovery_not_completed')
            return self._row(old)
        payload = canonical_json(facts)
        identifier = 'recovery-' + _hash([token.run_id,token.epoch,phase,key])
        db.execute('''INSERT INTO graph_recoveries(recovery_id,run_id,epoch,state_version,contract_sha256,
            business_event_id,phase,reason,input_sha256,facts_json,facts_sha256,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (identifier,token.run_id,token.epoch,token.state_version,
            run['contract_sha256'],event['event_id'],phase,reason,key,payload,
            hashlib.sha256(payload.encode()).hexdigest(),utc_text()))
        return self._row(db.execute('SELECT * FROM graph_recoveries WHERE recovery_id=?',(identifier,)).fetchone())

    def _evidence_reason(self, db, run_id):
        if not db.execute('SELECT 1 FROM evidence WHERE run_id=?',(run_id,)).fetchone():
            # A process killed before its first capture may legitimately have
            # no page evidence yet. A recorded partial capture cannot vanish.
            if db.execute("SELECT 1 FROM gateway_observations g JOIN observations o USING(snapshot_id) WHERE g.run_id=? AND o.source_url<>'about:blank'",(run_id,)).fetchone():
                return 'evidence_missing'
            return None
        try:
            self.evidence.assert_run_ready(run_id,db)
        except BusinessError as error:
            return 'evidence_corrupt' if error.code == 'EVIDENCE_CORRUPT' else 'evidence_missing'
        return None

    def _inspect(self, db, run, saved_values):
        run_id = run['run_id']
        checkpoint = self.graph._checkpoint(db,run_id)
        verified,summaries = self.graph._verification(db,run)
        operations = [dict(r) for r in db.execute('''SELECT operation_id,status,originating_run_id
            FROM write_intents WHERE task_id=? ORDER BY operation_id''',(run['task_id'],))]
        steps = []
        restore_url = None
        for row in db.execute('''SELECT s.*,a.external_write FROM steps s LEFT JOIN gateway_attempts a
            USING(run_id,step_id) WHERE s.run_id=? ORDER BY s.sequence''',(run_id,)):
            action = json.loads(row['action_json']) if row['action_json'] else {}
            external = bool(row['external_write']) if row['external_write'] is not None else action.get('expected_effect') == 'write'
            step = {key:row[key] for key in ('step_id','sequence','status','error_code')}
            step.update(external_write=external,result_sha256=hashlib.sha256(row['actual_result_json'].encode()).hexdigest())
            steps.append(step)
            actual = json.loads(row['actual_result_json'])
            if row['status']=='COMPLETED' and not external and isinstance(actual,dict):
                url = actual.get('source_url')
                if isinstance(url,str) and any(s.permits(url) for s in run['contract'].sources):
                    restore_url = url
        if restore_url is None and checkpoint and checkpoint.current_snapshot_id:
            row = db.execute('SELECT source_url FROM observations WHERE run_id=? AND snapshot_id=?',
                             (run_id,checkpoint.current_snapshot_id)).fetchone()
            restore_url = row[0] if row is not None else None
        budget = db.execute('SELECT * FROM run_budgets WHERE run_id=?',(run_id,)).fetchone()
        timer = db.execute('SELECT stop_reason FROM budget_timers WHERE run_id=?',(run_id,)).fetchone()
        event = self.graph._event(db,run)
        plan = dict(allowed=True,reason=None,run_id=run_id,run_state=run['state'],state_version=run['state_version'],
            contract_sha256=run['contract_sha256'],business_event_id=event['event_id'],
            checkpoint_id=checkpoint.checkpoint_id if checkpoint else None,
            snapshot_id=checkpoint.current_snapshot_id if checkpoint else None,graph_status='absent',
            verified_item_ids=verified,pending_item_ids=sorted(c.criterion_id for c in run['contract'].acceptance_criteria
                if c.criterion_id not in verified),verified_summary_refs=summaries,
            budget_record_ref=budget['budget_record_id'] if budget else None,
            budget={key:budget[key] for key in ('actions_used','content_pages_used','active_ms','model_calls_used',
                'observations_used','screenshots_used','recovery_counts_json')} if budget else {},
            operation_refs=operations,pending_operation_ids=[o['operation_id'] for o in operations if o['status'] in ('INTENT','UNKNOWN')],
            steps=steps,completed_read_step_ids=[s['step_id'] for s in steps if not s['external_write'] and s['status']=='COMPLETED'],
            failed_step_ids=[s['step_id'] for s in steps if s['status']=='FAILED'],
            uncertain_read_step_ids=[s['step_id'] for s in steps if not s['external_write'] and s['status'] in ('INTENT','UNKNOWN')],
            restore_url=restore_url or run['contract'].start_urls[0],terminal=run['state'] in TERMINAL_STATES)
        reason = None
        if budget is None:
            reason = 'checkpoint_mismatch'
        if saved_values:
            try:
                saved = validate_graph_state(saved_values)
            except (ValueError,TypeError):
                saved = None
                reason = ('graph_version_mismatch' if isinstance(saved_values,dict) and
                    (saved_values.get('graph_version')!=GRAPH_VERSION or saved_values.get('state_schema_version')!=STATE_SCHEMA_VERSION)
                    else 'graph_state_invalid')
            if saved is not None:
                plan['graph_status'] = 'aligned' if saved['state_version']==run['state_version'] else 'behind'
                if saved['run_id']!=run_id or saved['contract_version']!=run['contract_version']:
                    reason = 'contract_mismatch'
                elif saved['state_version']>run['state_version']:
                    reason = 'graph_ahead'
                else:
                    saved_event = db.execute('SELECT * FROM task_events WHERE run_id=? AND event_id=?',
                                            (run_id,saved['business_event_id'])).fetchone()
                    if saved_event is None:
                        reason = 'event_missing'
                    elif saved_event['state_version']!=saved['state_version']:
                        reason = 'event_version_mismatch'
                if reason is None and saved['business_checkpoint_id']:
                    cp = self.graph._checkpoint(db,run_id,saved['business_checkpoint_id'])
                    if (cp is None or cp.contract_version!=run['contract_version']
                            or cp.business_event_id>saved['business_event_id']):
                        reason = 'checkpoint_mismatch'
                if reason is None and saved['snapshot_id']:
                    try:self.evidence.filtered_observation_row(db,saved['snapshot_id'],run_id)
                    except BusinessError:reason = 'snapshot_missing'
                if reason is None and saved['progress_id']:
                    p = db.execute('SELECT * FROM graph_progress WHERE run_id=? AND progress_id=?',
                                   (run_id,saved['progress_id'])).fetchone()
                    if (p is None or p['state_version']!=saved['state_version']
                            or p['business_event_id']!=saved['business_event_id']
                            or p['checkpoint_id']!=saved['business_checkpoint_id']
                            or p['wait_id']!=saved['wait_id']
                            or p['snapshot_id'] is not None and p['snapshot_id']!=saved['snapshot_id']):
                        reason = 'progress_mismatch'
                if reason is None and saved['wait_id']:
                    wait=db.execute('''SELECT p.* FROM graph_progress p JOIN task_events e
                        ON e.run_id=p.run_id AND e.event_id=p.business_event_id
                        WHERE p.run_id=? AND p.progress_id=? AND p.phase='wait' AND p.wait_id=?
                        AND e.event_type='wait_registered' AND e.state_version=?
                        AND json_extract(e.payload_json,'$.wait_id')=p.wait_id''',
                        (run_id,saved['progress_id'],saved['wait_id'],saved['state_version'])).fetchone()
                    if (wait is None or wait['business_event_id']!=saved['business_event_id']
                            or saved['route']!='wait' or saved['completed']):reason='progress_mismatch'
                if reason is None:
                    for ref in saved['verified_summary_refs']:
                        item = db.execute('SELECT * FROM run_verifications WHERE run_id=? AND verification_id=?',(run_id,ref)).fetchone()
                        if (item is None or item['contract_sha256']!=run['contract_sha256']
                                or item['state_version']>saved['state_version']
                                or hashlib.sha256(item['content_json'].encode()).hexdigest()!=item['content_sha256']):
                            reason = 'summary_mismatch';break
                if reason is None:
                    for ref in saved['evidence_ids']:
                        try:self.evidence._metadata(db,ref,run_id)
                        except BusinessError:reason = 'evidence_missing';break
        if reason is None and (plan['pending_operation_ids'] or db.execute('''SELECT 1 FROM resource_quarantines q
            JOIN write_intents w USING(operation_id) WHERE w.task_id=?''',(run['task_id'],)).fetchone()
            or any(s['external_write'] and s['status'] in ('INTENT','UNKNOWN')
                   and not self._resolved_write_step(db, s['step_id']) for s in steps)):
            reason = 'unknown_write'
        if reason is None and db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'",(run_id,)).fetchone():
            reason = 'human_control'
        if reason is None:
            reason = self._evidence_reason(db,run_id)
        if reason is None and not plan['terminal'] and timer and timer['stop_reason']:
            reason = 'budget_exhausted'
        if reason is None and not plan['terminal']:
            blocked=db.execute('SELECT * FROM graph_recoveries WHERE run_id=? ORDER BY recovery_seq DESC LIMIT 1',
                               (run_id,)).fetchone()
            if blocked is not None and blocked['phase']=='BLOCKED':
                self._row(blocked)
                authorized = False
                if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='run_controls'").fetchone():
                    from ..controls.store import ControlStore
                    authorized = ControlStore.resume_authorized_in_transaction(db, run_id, blocked['recovery_seq'])
                if not authorized:
                    reason=blocked['reason']
        if reason is None and plan['terminal']:
            result = db.execute('SELECT * FROM run_results WHERE run_id=?',(run_id,)).fetchone()
            if result is not None:
                if (result['state_version']!=run['state_version'] or result['outcome']!=run['state']
                        or hashlib.sha256(result['result_json'].encode()).hexdigest()!=result['result_sha256']):
                    reason = 'checkpoint_mismatch'
                else:
                    body = json.loads(result['result_json'])
                    if body.get('generated_by')!='business_aggregator' or body.get('contract_version')!=run['contract_version']:
                        reason = 'contract_mismatch'
                    plan['result_ref']=result['verification_id']
                    plan['result_sha256']=result['result_sha256']
            elif run['state'] in ('SUCCEEDED','PARTIAL'):
                reason = 'checkpoint_mismatch'
        if reason:
            plan.update(allowed=False,reason=reason,graph_status='invalid' if saved_values else 'absent')
        if plan['allowed']:plan['ledger_sha256']=self._boundary(db,run,plan['snapshot_id'])
        plan['facts_sha256']=_hash(plan)
        return plan

    def inspect(self, run_id, saved_values=None):
        with self.evidence.files.locked(),connect(self.path) as db:
            db.execute('BEGIN')
            raw=self._raw_run(db,run_id)
            reason=self._compatibility(db,raw)
            if reason:
                event=self.graph._event(db,raw)
                plan=dict(allowed=False,reason=reason,run_id=run_id,run_state=raw['state'],
                    state_version=raw['state_version'],contract_sha256=raw['contract_sha256'],
                    business_event_id=event['event_id'],terminal=raw['state'] in TERMINAL_STATES,
                    graph_status='invalid',checkpoint_id=None,snapshot_id=None,restore_url=None)
                plan['facts_sha256']=_hash(plan)
                return plan
            return self._inspect(db,self.graph._run(db,run_id),saved_values)

    def begin(self, token, saved_values=None):
        plan = self.inspect(token.run_id,saved_values)
        with connect(self.path) as db,transaction(db):
            self._qualified(db,token)
            run = self._raw_run(db,token.run_id)
            if plan['state_version']!=run['state_version'] or plan['contract_sha256']!=run['contract_sha256']:
                raise _deny('contract_mismatch')
            if plan['allowed']:
                run=self.graph._run(db,token.run_id)
                if plan['ledger_sha256']!=self._boundary(db,run,plan['snapshot_id']):raise _deny('checkpoint_mismatch')
                if run['blocked_reason'] in ('worker_restarted','lease_expired','executor_interrupted'):
                    source=next((s for s in run['contract'].sources if s.permits(plan['restore_url'])),None)
                    if source is None:raise _deny('source_scope_mismatch')
                    checkpoint=self.graph._checkpoint(db,token.run_id)
                    subgoal=checkpoint.current_subgoal if checkpoint else run['contract'].acceptance_criteria[0].criterion_id
                    _,error=self.budgets.consume_in_transaction(db,token,kind='recovery',
                        attempt_id='worker-recovery-'+_hash([token.run_id,token.epoch]),site_id=source.site_id,
                        subgoal=subgoal,obstacle_type='worker_interrupted',allow_reconciling=True)
                    if error:
                        plan.update(allowed=False,reason='budget_exhausted')
                        plan['facts_sha256']=_hash({key:value for key,value in plan.items() if key!='facts_sha256'})
            record = self._save(db,token,run,'BEGIN' if plan['allowed'] else 'BLOCKED',plan,
                reason=plan['reason'],input_sha=_hash(saved_values))
            # A dead browser cannot keep its materialized reservation forever.
            # Keep the same pool ordinal, and never reset an OPEN session.
            if plan['allowed']:
                reservation = db.execute('''SELECT r.*,s.state FROM scheduler_context_reservations r
                    LEFT JOIN browser_sessions s USING(session_id) WHERE r.run_id=?''',(token.run_id,)).fetchone()
                if reservation and reservation['session_id'] and reservation['state'] in ('CLOSED','LOST'):
                    db.execute('DELETE FROM scheduler_context_reservations WHERE run_id=?',(token.run_id,))
                    db.execute('''INSERT INTO scheduler_context_reservations(run_id,context_ordinal,worker_id,
                        worker_generation,epoch,created_at) VALUES(?,?,?,?,?,?)''',
                        (token.run_id,reservation['context_ordinal'],token.worker_id,token.worker_generation,token.epoch,utc_text()))
        return {**plan,'recovery_id':record['recovery_id']}

    def blocked(self, token, reason):
        if reason not in REASONS:
            raise BusinessError('INVALID_PARAMETER','Unknown recovery diagnosis')
        with connect(self.path) as db,transaction(db):
            try:self._qualified(db,token)
            except BusinessError:
                # Recording a block is bookkeeping, never browser permission.
                # Human control may prevent the normal qualification gate;
                # every other part of the owner/epoch/expiry binding remains.
                if reason!='human_control' or not isinstance(token,ExecutionToken):raise
                q=db.execute('SELECT q.*,r.state,r.state_version FROM scheduler_queue q JOIN runs r USING(run_id) WHERE q.run_id=?',(token.run_id,)).fetchone()
                worker=db.execute('SELECT * FROM scheduler_workers WHERE worker_id=?',(token.worker_id,)).fetchone()
                leases=db.execute('SELECT * FROM resource_leases WHERE holder_run_id=?',(token.run_id,)).fetchall()
                now=utc_text()
                if (q is None or q['state']!='RECONCILING' or q['state_version']!=token.state_version
                        or q['status']!='ACTIVE' or q['epoch']!=token.epoch or q['worker_id']!=token.worker_id
                        or q['worker_generation']!=token.worker_generation or q['expires_at']<=now
                        or q['run_state_version']!=token.state_version or token.expires_at<=now
                        or worker is None or worker['state']!='ACTIVE' or worker['generation']!=token.worker_generation
                        or worker['expires_at']<=now or {r['resource_key'] for r in leases}!=set(token.resources)
                        or not any(r['control_owner']=='human' for r in leases)
                        or any(r['worker_id']!=token.worker_id or r['epoch']!=token.epoch or r['expires_at']<=now for r in leases)):
                    raise _deny('human_control')
            run = self._raw_run(db,token.run_id)
            return self._save(db,token,run,'BLOCKED',{'reason':reason},reason=reason)

    def blocked_receipt(self, token):
        if not isinstance(token,ExecutionToken):return False
        with connect(self.path) as db:
            db.execute('BEGIN')
            run = self._raw_run(db,token.run_id)
            latest = db.execute('SELECT * FROM graph_recoveries WHERE run_id=? ORDER BY recovery_seq DESC LIMIT 1',(token.run_id,)).fetchone()
            queue = db.execute('SELECT * FROM scheduler_queue WHERE run_id=?',(token.run_id,)).fetchone()
            worker = db.execute('SELECT * FROM scheduler_workers WHERE worker_id=?',(token.worker_id,)).fetchone()
            event=db.execute('SELECT * FROM scheduler_events WHERE run_id=? ORDER BY event_id DESC LIMIT 1',
                             (token.run_id,)).fetchone()
            now=utc_text()
            if latest is not None:self._row(latest)
            return bool(latest and queue and latest['phase']=='BLOCKED' and latest['epoch']==token.epoch
                and latest['state_version']==token.state_version==run['state_version']
                and latest['contract_sha256']==run['contract_sha256'] and queue['status']=='RECOVERY'
                and queue['epoch']==token.epoch+1 and queue['worker_id'] is None and queue['worker_generation'] is None
                and queue['expires_at'] is None and queue['run_state_version']==run['state_version']
                and event and event['epoch']==queue['epoch'] and event['revision']==queue['revision']
                and event['event_type']=='revoked'
                and run['state']=='RECONCILING' and worker and worker['state']=='ACTIVE'
                and worker['generation']==token.worker_generation and worker['expires_at']>now
                and token.expires_at>now)

    def _active(self, db, token, recovery_id):
        self._qualified(db,token)
        run = self.graph._run(db,token.run_id)
        row = db.execute('SELECT * FROM graph_recoveries WHERE recovery_id=? AND run_id=?',(recovery_id,token.run_id)).fetchone()
        last = db.execute('SELECT phase FROM graph_recoveries WHERE run_id=? AND epoch=? ORDER BY recovery_seq DESC LIMIT 1',(token.run_id,token.epoch)).fetchone()
        if (row is None or row['phase']!='BEGIN' or row['epoch']!=token.epoch
                or row['state_version']!=token.state_version or row['contract_sha256']!=run['contract_sha256']
                or last is None or last['phase']!='BEGIN'):
            raise _deny('recovery_not_completed')
        return self._row(row)

    def require_active(self, token, recovery_id):
        with connect(self.path) as db:return self._active(db,token,recovery_id)

    @staticmethod
    def _fresh_capture(db, run, snapshot_id, token, *, observation_version=None):
        """SQL-only validation usable at the final commit boundary."""
        gateway=db.execute('''SELECT g.*,s.state AS session_state,s.identity_ref FROM gateway_observations g
            JOIN browser_sessions s USING(session_id) WHERE g.run_id=? AND g.snapshot_id=?''',
            (run['run_id'],snapshot_id)).fetchone()
        version=token.state_version if observation_version is None else observation_version
        if (gateway is None or gateway['epoch']!=token.epoch or gateway['state_version']!=version
                or gateway['session_state']!='OPEN' or gateway['identity_ref']!=run['contract'].identity_ref):
            raise _deny('session_unavailable')
        reservation=db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?',(token.run_id,)).fetchone()
        head=db.execute('SELECT * FROM gateway_page_heads WHERE run_id=?',(token.run_id,)).fetchone()
        begin=db.execute("SELECT created_at FROM graph_recoveries WHERE run_id=? AND epoch=? AND phase='BEGIN' ORDER BY recovery_seq DESC LIMIT 1",(token.run_id,token.epoch)).fetchone()
        observed=db.execute('SELECT captured_at FROM observations WHERE snapshot_id=?',(snapshot_id,)).fetchone()
        if (reservation is None or reservation['session_id']!=gateway['session_id']
                or reservation['worker_id']!=token.worker_id or reservation['worker_generation']!=token.worker_generation
                or reservation['epoch']!=token.epoch or head is None or head['snapshot_id']!=snapshot_id
                or not head['valid'] or begin is None or observed is None or observed['captured_at']<begin['created_at']):
            raise _deny('session_unavailable')

    def _facts(self, db, run, snapshot_id, token=None):
        view = self.evidence.filtered_observation_row(db,snapshot_id,run['run_id'])
        if token is not None:self._fresh_capture(db,run,snapshot_id,token)
        if not any(s.permits(view['content']['source_url']) for s in run['contract'].sources):
            raise _deny('source_scope_mismatch')
        docs,_ = self.verifier._documents(db,run['run_id'],view['content']['evidence_ids'])
        for doc in docs:
            if not doc.readable or not isinstance(doc.content,dict):continue
            prefix = '/parsed_text' if 'parsed_text' in doc.content else ''
            parsed = doc.content.get('parsed_text',doc.content)
            if not isinstance(parsed,dict):continue
            context = parsed.get('recovery_context',parsed)
            base = prefix + ('/recovery_context' if 'recovery_context' in parsed else '')
            paths = {}
            if isinstance(context,dict) and context.get('object_id') is not None:
                object_id,version = context['object_id'],context.get('object_version')
                paths.update(object_id=base+'/object_id',object_version=base+'/object_version' if version is not None else None)
            elif run['contract'].scenario=='finance' and parsed.get('values'):
                values=parsed['values']
                ids={v.get('entity_id') for v in values if isinstance(v,dict)}
                versions={v.get('report_version') for v in values if isinstance(v,dict)}
                if len(ids)!=1 or len(versions)!=1:raise _deny('object_mismatch')
                object_id,version=next(iter(ids)),next(iter(versions))
                paths.update(object_id=prefix+'/values/0/entity_id',object_version=prefix+'/values/0/report_version')
            elif parsed.get('dashboard_id'):
                object_id,version=parsed['dashboard_id'],parsed.get('object_version')
                paths.update(object_id=prefix+'/dashboard_id',object_version=prefix+'/object_version' if version is not None else None)
            elif parsed.get('source_id'):
                object_id,version=parsed['source_id'],parsed.get('verified_boundary')
                paths.update(object_id=prefix+'/source_id',object_version=prefix+'/verified_boundary' if version is not None else None)
            elif len(parsed.get('publications',[]))==1:
                object_id,version=parsed['publications'][0].get('canonical_id'),parsed['publications'][0].get('version')
                paths.update(object_id=prefix+'/publications/0/canonical_id',object_version=prefix+'/publications/0/version')
            else:continue
            if type(object_id) is not str or not object_id or version is not None and type(version) is not str:
                raise _deny('proof_missing')
            identity_ref=context.get('identity_ref') if isinstance(context,dict) else None
            account=context.get('normalized_account') if isinstance(context,dict) else None
            if identity_ref is not None:paths['identity_ref']=base+'/identity_ref'
            if account is not None:paths['normalized_account']=base+'/normalized_account'
            return dict(object_id=object_id,object_version=version,identity_ref=identity_ref,
                normalized_account=account,proof_evidence_ids=[doc.evidence_id],
                proof_bindings={key:value for key,value in paths.items() if value is not None})
        raise _deny('proof_missing')

    def observed_facts(self, token, snapshot_id):
        with self.evidence.files.locked(),connect(self.path) as db:
            db.execute('BEGIN')
            self._qualified(db,token)
            return self._facts(db,self.graph._run(db,token.run_id),snapshot_id,token)

    @staticmethod
    def _identity_scope(db, run, snapshot_id, identity_ref, account_hash):
        if identity_ref is None:return
        identity=db.execute('SELECT * FROM identities WHERE identity_ref=?',(identity_ref,)).fetchone()
        scoped=db.execute('''SELECT s.site_id,s.realm,s.identity_ref,o.source_url FROM gateway_observations g
            JOIN browser_sessions s USING(session_id) JOIN observations o USING(snapshot_id)
            WHERE g.run_id=? AND g.snapshot_id=?''',(run['run_id'],snapshot_id)).fetchone()
        source=next((s for s in run['contract'].sources if scoped and s.permits(scoped['source_url'])),None)
        if (identity is None or scoped is None or source is None or identity['state']!='VERIFIED'
                or identity_ref!=run['contract'].identity_ref or scoped['identity_ref']!=identity_ref
                or identity['site_id']!=scoped['site_id'] or identity['site_id']!=source.site_id
                or identity['realm']!=scoped['realm'] or identity['origin'].rstrip('/')!=source.origin.rstrip('/')
                or any(s.site_id!=identity['site_id'] or s.origin.rstrip('/')!=identity['origin'].rstrip('/')
                    for s in run['contract'].sources)
                or _hash(identity['normalized_account'])!=account_hash):raise _deny('identity_mismatch')

    def checkpoint_facts(self, run_id, token, snapshot_id=None, *, object_id=None, object_version=None):
        facts = None
        with self.evidence.files.locked():
            with connect(self.path) as db:
                db.execute('BEGIN')
                run = self.graph._run(db,run_id)
                if token is not None:
                    validate_in_transaction(db,token,allow_reconciling=True)
                    if token.run_id!=run_id:raise _deny('contract_mismatch')
                elif run['state'] not in TERMINAL_STATES:
                    raise _deny('recovery_not_completed')
                old = self.graph._checkpoint(db,run_id)
                selected = snapshot_id or (old.current_snapshot_id if old else None)
                if selected:
                    try:facts=self._facts(db,run,selected)
                    except BusinessError as error:
                        if error.field!='proof_missing':raise
                boundary=self._boundary(db,run,selected)
            with connect(self.path) as db,transaction(db):
                run = self.graph._run(db,run_id)
                if token is not None:validate_in_transaction(db,token,allow_reconciling=True)
                elif run['state'] not in TERMINAL_STATES:raise _deny('recovery_not_completed')
                if boundary!=self._boundary(db,run,selected):raise _deny('checkpoint_mismatch')
                old = self.graph._checkpoint(db,run_id)
                event=self.graph._event(db,run)
                verified,summaries=self.graph._verification(db,run)
                pending=sorted(c.criterion_id for c in run['contract'].acceptance_criteria if c.criterion_id not in verified)
                budget=db.execute('SELECT budget_record_id FROM run_budgets WHERE run_id=?',(run_id,)).fetchone()
                if budget is None:raise _deny('checkpoint_mismatch')
                refs=[]
                if selected:refs=self.evidence.filtered_observation_row(db,selected,run_id)['content']['evidence_ids']
                actual_id=facts['object_id'] if facts else old.current_object_id if old else run['contract'].targets[0].object_id
                actual_version=facts['object_version'] if facts else old.current_object_version if old else None
                known_version=self._known_version(db,run,actual_id)
                if (run['state']=='RECONCILING' and actual_version is not None
                        and known_version is not None and actual_version!=known_version):
                    raise _deny('object_version_mismatch')
                if actual_version is None:actual_version=known_version
                if object_id is not None and object_id!=actual_id or object_version is not None and object_version!=actual_version:
                    raise _deny('object_mismatch')
                seq=db.execute('SELECT COALESCE(MAX(sequence),0) FROM steps WHERE run_id=?',(run_id,)).fetchone()[0]
                operations=[r[0] for r in db.execute("SELECT operation_id FROM write_intents WHERE task_id=? AND status IN ('INTENT','UNKNOWN') ORDER BY operation_id",(run['task_id'],))]
                epoch=token.epoch if token is not None else old.epoch if old else max(1,db.execute('SELECT COALESCE(MAX(epoch),1) FROM scheduler_queue WHERE run_id=?',(run_id,)).fetchone()[0])
                identifier='ledger-checkpoint-'+_hash([run_id,epoch,event['event_id'],seq,selected,actual_id,actual_version,summaries,operations])
                existing=self.graph._checkpoint(db,run_id,identifier)
                if existing:return existing
                body=dict(checkpoint_id=identifier,task_id=run['task_id'],run_id=run_id,contract_version=run['contract_version'],
                    current_subgoal=pending[0] if pending else 'aggregate',verified_item_ids=verified,pending_item_ids=pending,
                    current_object_id=actual_id,current_object_version=actual_version,current_snapshot_id=selected,
                    flow_version=old.flow_version if old else None,action_sequence=seq,business_event_id=event['event_id'],
                    budget_record_ref=budget[0],identity_ref=run['contract'].identity_ref,pending_operation_ids=operations,
                    epoch=epoch,evidence_ids=sorted(refs),saved_at=utc_text())
                cp=RunCheckpoint.model_validate_json(canonical_json(body))
                db.execute('''INSERT INTO run_checkpoints(checkpoint_id,task_id,run_id,contract_version,current_subgoal,
                    verified_item_ids_json,pending_item_ids_json,current_object_id,current_object_version,current_snapshot_id,
                    flow_version,action_sequence,business_event_id,budget_record_ref,identity_ref,epoch,saved_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(identifier,run['task_id'],run_id,run['contract_version'],body['current_subgoal'],
                    canonical_json(verified),canonical_json(pending),actual_id,actual_version,selected,body['flow_version'],seq,
                    event['event_id'],budget[0],run['contract'].identity_ref,epoch,body['saved_at']))
                db.executemany('INSERT INTO run_checkpoints_evidence VALUES(?,?,?)',[(run_id,identifier,ref) for ref in refs])
                db.executemany('INSERT INTO checkpoint_operations VALUES(?,?,?,?)',[(run['task_id'],run_id,identifier,op) for op in operations])
                return cp

    def complete(self, token, snapshot_id, *, object_id, object_version=None, identity_ref=None,
                 proof_evidence_ids=(), normalized_account=None, proof_bindings=None):
        prior = self.inspect(token.run_id)
        try:
            if not prior['allowed']:raise _deny(prior['reason'])
            actual=self.observed_facts(token,snapshot_id)
            if object_id!=actual['object_id'] or object_id not in {t.object_id for t in self.graph.load_run(token.run_id)['contract'].targets}:
                raise _deny('object_mismatch')
            if object_version!=actual['object_version']:raise _deny('object_version_mismatch')
            if actual['object_version'] is None:raise _deny('proof_missing')
            if proof_bindings is not None and proof_bindings!=actual['proof_bindings']:raise _deny('proof_missing')
            if proof_evidence_ids and set(proof_evidence_ids)!=set(actual['proof_evidence_ids']):raise _deny('proof_missing')
            with connect(self.path) as db:
                run=self.graph._run(db,token.run_id)
                if run['contract'].scenario=='finance' and object_version!=run['contract'].parameters.report_version:
                    raise _deny('object_version_mismatch')
                known_version=self._known_version(db,run,object_id)
                if known_version is not None and known_version!=object_version:
                    raise _deny('object_version_mismatch')
                if identity_ref!=actual['identity_ref'] or normalized_account!=actual['normalized_account'] or identity_ref!=run['contract'].identity_ref:
                    raise _deny('identity_mismatch')
                self._identity_scope(db,run,snapshot_id,identity_ref,_hash(normalized_account))
            cp=self.checkpoint_facts(token.run_id,token,snapshot_id,object_id=object_id,object_version=object_version)
            with self.evidence.files.locked():
                with connect(self.path) as db:
                    db.execute('BEGIN')
                    self._qualified(db,token)
                    run=self.graph._run(db,token.run_id)
                    current=self._inspect(db,run,None)
                    if not current['allowed']:raise _deny(current['reason'])
                    if actual!=self._facts(db,run,snapshot_id,token):raise _deny('proof_missing')
                    boundary=self._boundary(db,run,snapshot_id)
                with connect(self.path) as db,transaction(db):
                    self._qualified(db,token)
                    run=self.graph._run(db,token.run_id)
                    self._fresh_capture(db,run,snapshot_id,token)
                    if boundary!=self._boundary(db,run,snapshot_id):raise _deny('checkpoint_mismatch')
                    begin=db.execute("SELECT * FROM graph_recoveries WHERE run_id=? AND epoch=? AND phase='BEGIN' ORDER BY recovery_seq DESC LIMIT 1",(token.run_id,token.epoch)).fetchone()
                    if begin is None:raise _deny('recovery_not_completed')
                    last=db.execute('SELECT * FROM graph_recoveries WHERE run_id=? AND epoch=? ORDER BY recovery_seq DESC LIMIT 1',(token.run_id,token.epoch)).fetchone()
                    if last['phase']=='BLOCKED':raise _deny('recovery_not_completed')
                    facts=dict(snapshot_id=snapshot_id,checkpoint_id=cp.checkpoint_id,object_id=object_id,object_version=object_version,
                        identity_ref=identity_ref,account_sha256=_hash(normalized_account),proof_evidence_ids=actual['proof_evidence_ids'],
                        proof_bindings=actual['proof_bindings'],parent_recovery_id=begin['recovery_id'],
                        reconciled_read_step_ids=self._row(begin)['facts']['uncertain_read_step_ids'],
                        read_reconciliation='read_observed_no_replay')
                    return self._save(db,token,run,'COMPLETE',facts)
        except BusinessError as error:
            if error.field in REASONS:self.blocked(token,error.field)
            raise

    def require_completed(self, token):
        with self.evidence.files.locked(),connect(self.path) as db:
            db.execute('BEGIN')
            self._qualified(db,token,state='RUNNING')
            run=self.graph._run(db,token.run_id)
            plan=self._inspect(db,run,None)
            if not plan['allowed']:raise _deny(plan['reason'])
            row=db.execute('SELECT * FROM graph_recoveries WHERE run_id=? AND epoch=? ORDER BY recovery_seq DESC LIMIT 1',(token.run_id,token.epoch)).fetchone()
            transition=db.execute("SELECT payload_json FROM task_events WHERE run_id=? AND state_version=? AND event_type='state_changed'",(token.run_id,token.state_version)).fetchone()
            payload=json.loads(transition[0]) if transition else {}
            if (row is None or row['phase']!='COMPLETE' or row['state_version']+1!=token.state_version
                    or row['contract_sha256']!=run['contract_sha256'] or payload.get('previous_state')!='RECONCILING'
                    or payload.get('current_state')!='RUNNING'):
                raise _deny('recovery_not_completed')
            record=self._row(row)
            if not db.execute('SELECT 1 FROM task_events WHERE run_id=? AND event_id=? AND state_version=?',
                (token.run_id,row['business_event_id'],row['state_version'])).fetchone():raise _deny('event_missing')
            facts=record['facts']
            self._fresh_capture(db,run,facts['snapshot_id'],token,observation_version=row['state_version'])
            actual=self._facts(db,run,facts['snapshot_id'])
            if (actual['object_id']!=facts['object_id'] or actual['object_version']!=facts['object_version']
                    or actual['identity_ref']!=facts['identity_ref'] or _hash(actual['normalized_account'])!=facts['account_sha256']
                    or actual['proof_evidence_ids']!=facts['proof_evidence_ids']
                    or actual['proof_bindings']!=facts['proof_bindings']):raise _deny('proof_missing')
            cp=self.graph._checkpoint(db,token.run_id,facts['checkpoint_id'])
            parent=db.execute('SELECT * FROM graph_recoveries WHERE run_id=? AND recovery_id=?',
                (token.run_id,facts['parent_recovery_id'])).fetchone()
            if (cp is None or cp.epoch!=token.epoch or cp.current_snapshot_id!=facts['snapshot_id']
                    or cp.current_object_id!=facts['object_id'] or cp.current_object_version!=facts['object_version']
                    or cp.business_event_id>row['business_event_id'] or cp.pending_operation_ids
                    or parent is None or parent['phase']!='BEGIN' or parent['epoch']!=token.epoch
                    or parent['contract_sha256']!=run['contract_sha256']
                    or not self._row(parent)['facts'].get('allowed')):raise _deny('checkpoint_mismatch')
            self._identity_scope(db,run,facts['snapshot_id'],facts['identity_ref'],facts['account_sha256'])
            return record

    def resolved_read_steps(self, db, run_id):
        """Only actual read attempts covered by an immutable completed check.

        This does not rewrite their original INTENT/UNKNOWN outcomes. New
        attempts absent from the proof remain unresolved, and a write can
        never use an observation as its receipt.
        """
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='graph_recoveries'").fetchone():
            return set()
        rows=db.execute("SELECT * FROM graph_recoveries WHERE run_id=? AND phase='COMPLETE'",(run_id,)).fetchall()
        if not rows:return set()
        run=self.graph._run(db,run_id)
        queue=db.execute('SELECT * FROM scheduler_queue WHERE run_id=?',(run_id,)).fetchone()
        if queue is None or run['state']=='RECONCILING':return set()
        resolved=set()
        for row in rows:
            if row['contract_sha256']!=run['contract_sha256'] or row['state_version']>=run['state_version']:continue
            if not ((queue['status']=='ACTIVE' and queue['epoch']==row['epoch'])
                    or (queue['status']=='FINISHED' and run['state'] in TERMINAL_STATES
                        and queue['epoch']==row['epoch']+1)):continue
            event=db.execute("SELECT payload_json FROM task_events WHERE run_id=? AND state_version=? AND event_type='state_changed'",(run_id,row['state_version']+1)).fetchone()
            transition=json.loads(event[0]) if event else {}
            if transition.get('previous_state')!='RECONCILING' or transition.get('current_state')!='RUNNING':continue
            proof=self._row(row)['facts']
            if proof.get('read_reconciliation')!='read_observed_no_replay':continue
            parent=db.execute('SELECT * FROM graph_recoveries WHERE run_id=? AND recovery_id=?',
                (run_id,proof.get('parent_recovery_id'))).fetchone()
            if (parent is None or parent['phase']!='BEGIN' or parent['epoch']!=row['epoch']
                    or parent['state_version']!=row['state_version'] or parent['contract_sha256']!=row['contract_sha256']
                    or parent['recovery_seq']>=row['recovery_seq']):continue
            beginning=self._row(parent)['facts']
            if (not beginning.get('allowed') or proof.get('reconciled_read_step_ids')!=beginning.get('uncertain_read_step_ids')
                    or beginning.get('pending_operation_ids')):continue
            cp=self.graph._checkpoint(db,run_id,proof.get('checkpoint_id'))
            view=self.evidence.filtered_observation_row(db,proof['snapshot_id'],run_id)
            if (cp is None or cp.epoch!=row['epoch'] or cp.contract_version!=run['contract_version']
                    or cp.current_snapshot_id!=proof['snapshot_id'] or cp.business_event_id>row['business_event_id']
                    or cp.current_object_id!=proof['object_id'] or cp.current_object_version!=proof['object_version']
                    or cp.identity_ref!=proof['identity_ref'] or cp.pending_operation_ids
                    or proof['identity_ref']!=run['contract'].identity_ref
                    or proof['object_id'] not in {t.object_id for t in run['contract'].targets}
                    or not set(proof['proof_evidence_ids']).issubset(cp.evidence_ids)
                    or not set(proof['proof_evidence_ids']).issubset(view['content']['evidence_ids'])):continue
            for evidence_id in proof['proof_evidence_ids']:
                display=self.evidence._metadata(db,evidence_id,run_id)
                original=self.evidence._metadata(db,display['original_evidence_id'],run_id)
                if any(item['availability']!='AVAILABLE' or item['capture_status']!='COMPLETE'
                        or item['expires_at'] is not None and item['expires_at']<=utc_text()
                        or item['snapshot_id']!=proof['snapshot_id'] for item in (display,original)):
                    raise _deny('evidence_missing')
            for step_id in proof.get('reconciled_read_step_ids',[]):
                step=db.execute('''SELECT s.*,g.external_write FROM steps s LEFT JOIN gateway_attempts g
                    USING(run_id,step_id) WHERE s.run_id=? AND s.step_id=?''',(run_id,step_id)).fetchone()
                if step is None or step['status'] not in ('INTENT','UNKNOWN') or step['external_write']:continue
                action=json.loads(step['action_json']) if step['action_json'] else {}
                if action.get('expected_effect')=='read' and step['epoch']<row['epoch']:resolved.add(step_id)
        return resolved

    def has_unresolved_steps(self, db, run_id):
        pending=db.execute("SELECT step_id FROM steps WHERE run_id=? AND status IN ('INTENT','UNKNOWN')",(run_id,)).fetchall()
        if not pending:return False
        resolved=self.resolved_read_steps(db,run_id)
        return any(r[0] not in resolved and not self._resolved_write_step(db, r[0]) for r in pending)

    @staticmethod
    def _resolved_write_step(db, step_id):
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='write_protocol_claims'").fetchone():
            return False
        from ..writes.store import WriteProtocolStore
        return WriteProtocolStore.is_resolved_step(db, step_id)

    def _navigation(self, db, token, recovery_id, url, attempt_id, status):
        self._active(db,token,recovery_id)
        run=self.graph._run(db,token.run_id)
        _identifier(attempt_id,'attempt_id')
        if status not in ('INTENT','COMPLETED','FAILED','UNKNOWN') or not any(s.permits(url) for s in run['contract'].sources):
            raise _deny('source_scope_mismatch')
        old=db.execute('SELECT * FROM graph_recovery_navigations WHERE run_id=? AND attempt_id=? AND status=?',(token.run_id,attempt_id,status)).fetchone()
        if old:
            if old['source_url']!=url or old['recovery_id']!=recovery_id:raise _deny('source_scope_mismatch')
            return {**dict(old),'dispatch_allowed':False}
        debit=db.execute("SELECT 1 FROM budget_attempts WHERE run_id=? AND attempt_id=? AND epoch=? AND kind='action'",(token.run_id,attempt_id,token.epoch)).fetchone()
        if debit is None:raise _deny('recovery_not_completed')
        db.execute('''INSERT INTO graph_recovery_navigations(run_id,recovery_id,epoch,attempt_id,source_url,status,created_at)
            VALUES(?,?,?,?,?,?,?)''',(token.run_id,recovery_id,token.epoch,attempt_id,url,status,utc_text()))
        return {**dict(db.execute('SELECT * FROM graph_recovery_navigations WHERE run_id=? AND attempt_id=? AND status=?',(token.run_id,attempt_id,status)).fetchone()),'dispatch_allowed':status=='INTENT'}

    def record_navigation(self, token, recovery_id, url, attempt_id, status):
        with connect(self.path) as db,transaction(db):return self._navigation(db,token,recovery_id,url,attempt_id,status)

    def navigation_intent(self, token, recovery_id, url, attempt_id, *, site_id):
        with connect(self.path) as db,transaction(db):
            self._active(db,token,recovery_id)
            budget,error=self.budgets.consume_in_transaction(db,token,kind='action',attempt_id=attempt_id,
                site_id=site_id,navigation=True,content_page=True,allow_reconciling=True)
            result=None if error else self._navigation(db,token,recovery_id,url,attempt_id,'INTENT')
            if result is not None and budget['duplicate']:result['dispatch_allowed']=False
        if error:raise error
        return result

    def navigation_complete(self, token, recovery_id, url, attempt_id, status='COMPLETED'):
        return self.record_navigation(token,recovery_id,url,attempt_id,status)
