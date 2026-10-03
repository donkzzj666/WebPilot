"""Business-qualified progress and reconstructable checkpoints for the graph.

LangGraph saves only references to these committed facts. It cannot publish
authority, model messages, or a proposed success through this store.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..evidence.service import POLICY_VERSION
from ..evidence.store import EvidenceStore
from ..models.schema import RunCheckpoint
from ..scheduler.models import ExecutionToken
from ..scheduler.store import validate_in_transaction
from ..state import TERMINAL_STATES
from ..tasks.models import TaskContract
from ..verification.models import Check, Verdict
from .models import (Diagnostic, GRAPH_VERSION, GraphSnapshot, GraphState, Phase,
                     STATE_SCHEMA_VERSION, validate_graph_state)

_WAITING = frozenset(('PAUSED', 'WAITING_CI', 'WAITING_SITE', 'WAITING_HANDOFF'))


def _conflict(message='Graph references or business state changed'):
    return BusinessError('STATE_CONFLICT', message, status=409)


def _identifier(value, field):
    if (type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BusinessError('INVALID_PARAMETER', 'Invalid graph reference', field=field)
    return value


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class GraphStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _run(db, run_id):
        _identifier(run_id, 'run_id')
        row = db.execute('''SELECT r.*,c.content_json FROM runs r JOIN contracts c
            ON c.task_id=r.task_id AND c.contract_version=r.contract_version
            WHERE r.run_id=?''', (run_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Run not found', status=404)
        if (row['graph_version'] != GRAPH_VERSION
                or row['graph_state_schema_version'] != STATE_SCHEMA_VERSION
                or row['thread_id'] != run_id):
            raise _conflict('Graph or state schema version is incompatible')
        if hashlib.sha256(row['content_json'].encode()).hexdigest() != row['contract_sha256']:
            raise _conflict('Frozen contract integrity mismatch')
        try:
            contract = TaskContract.model_validate_json(row['content_json'])
        except ValidationError:
            raise BusinessError('INVALID_PARAMETER', 'Graph requires a complete frozen contract') from None
        result = dict(row)
        result.pop('content_json')
        result['contract'] = contract
        return result

    def load_run(self, run_id):
        with connect(self.path) as db:
            return self._run(db, run_id)

    @staticmethod
    def _qualify(db, run, expected_state_version, execution_token):
        if type(expected_state_version) is not int or expected_state_version != run['state_version']:
            raise _conflict()
        if execution_token is not None:
            if not isinstance(execution_token, ExecutionToken) or execution_token.run_id != run['run_id']:
                raise BusinessError('RESOURCE_CONFLICT', 'Graph qualification belongs to another Run', status=409)
            validate_in_transaction(db, execution_token, allow_reconciling=True)
        elif db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (run['run_id'],)).fetchone():
            raise BusinessError('RESOURCE_CONFLICT', 'Graph requires current execution qualification', status=409)
        if run['state'] not in ('RUNNING', 'VERIFYING', 'RECONCILING'):
            raise _conflict('Run is not active')

    @staticmethod
    def _event(db, run):
        row = db.execute('''SELECT * FROM task_events WHERE run_id=? AND state_version=?
            ORDER BY event_id DESC LIMIT 1''', (run['run_id'], run['state_version'])).fetchone()
        if row is None:
            raise _conflict('Current business event is unavailable')
        return row

    @staticmethod
    def _verification(db, run):
        """Use only the latest actual verifier capsule, never executor summaries.

        Older PASS records must not hide a later FAIL or CONFLICT. Referenced
        bytes remain evidence-service responsibilities; these IDs are progress
        summaries only and are never added to the evidence collection.
        """
        row = db.execute('''SELECT * FROM run_verifications WHERE run_id=?
            ORDER BY created_at DESC,verification_id DESC LIMIT 1''', (run['run_id'],)).fetchone()
        if row is None:
            return [], []
        if (row['contract_sha256'] != run['contract_sha256']
                or hashlib.sha256(row['content_json'].encode()).hexdigest() != row['content_sha256']
                or row['state_version'] > run['state_version']):
            raise _conflict('Verification summary integrity mismatch')
        try:
            body = json.loads(row['content_json'])
            checks = [Check.model_validate_json(canonical_json(item)) for item in body['checks']]
        except (KeyError, TypeError, ValueError):
            raise _conflict('Verification summary is unavailable') from None
        declared = {criterion.criterion_id: criterion.expected_rule for criterion in run['contract'].acceptance_criteria}
        if len(checks) != len(declared) or {(c.criterion_id, c.expected_rule) for c in checks} != set(declared.items()):
            raise _conflict('Verification summary contract mismatch')
        verified = sorted(c.criterion_id for c in checks if c.verdict == Verdict.PASS)
        return verified, [row['verification_id']] if verified else []

    @staticmethod
    def _checkpoint(db, run_id, checkpoint_id=None):
        if checkpoint_id is None:
            row = db.execute('''SELECT * FROM run_checkpoints WHERE run_id=?
                ORDER BY saved_at DESC,checkpoint_id DESC LIMIT 1''', (run_id,)).fetchone()
        else:
            row = db.execute('SELECT * FROM run_checkpoints WHERE run_id=? AND checkpoint_id=?',
                             (run_id, checkpoint_id)).fetchone()
        if row is None:
            return None
        body = dict(row)
        body['verified_item_ids'] = json.loads(body.pop('verified_item_ids_json'))
        body['pending_item_ids'] = json.loads(body.pop('pending_item_ids_json'))
        body['evidence_ids'] = [r[0] for r in db.execute('''SELECT evidence_id FROM run_checkpoints_evidence
            WHERE run_id=? AND checkpoint_id=? ORDER BY evidence_id''', (run_id, row['checkpoint_id']))]
        body['pending_operation_ids'] = [r[0] for r in db.execute('''SELECT operation_id FROM checkpoint_operations
            WHERE run_id=? AND checkpoint_id=? ORDER BY operation_id''', (run_id, row['checkpoint_id']))]
        return RunCheckpoint.model_validate_json(canonical_json(body))

    def checkpoint_observation(self, run_id, snapshot_id, *, expected_state_version, execution_token=None):
        _identifier(snapshot_id, 'snapshot_id')
        with connect(self.path) as db, transaction(db):
            run = self._run(db, run_id)
            self._qualify(db, run, expected_state_version, execution_token)
            view = EvidenceStore.filtered_observation_row(db, snapshot_id, run_id)
            if view['policy_version'] != POLICY_VERSION:
                raise _conflict('Observation publication policy is incompatible')
            observation = view['content']
            if not any(source.permits(observation['source_url']) for source in run['contract'].sources):
                raise BusinessError('FORBIDDEN', 'Observation is outside the frozen sources', status=403)
            for ref in observation['evidence_ids']:
                row = db.execute('''SELECT a.redaction_status,a.snapshot_id,a.policy_version
                    FROM evidence_artifacts a JOIN evidence e USING(evidence_id)
                    WHERE a.evidence_id=? AND e.run_id=?''', (ref, run_id)).fetchone()
                if (row is None or row['redaction_status'] != 'FILTERED' or row['snapshot_id'] != snapshot_id
                        or row['policy_version'] != view['policy_version']):
                    raise _conflict('Observation display references are invalid')
            event = self._event(db, run)
            budget = db.execute('SELECT budget_record_id FROM run_budgets WHERE run_id=?', (run_id,)).fetchone()
            if budget is None:
                raise _conflict('Run budget is unavailable')
            verified, summaries = self._verification(db, run)
            pending = sorted(c.criterion_id for c in run['contract'].acceptance_criteria if c.criterion_id not in verified)
            epoch = execution_token.epoch if execution_token is not None else max(1, db.execute(
                'SELECT COALESCE(MAX(epoch),0) FROM steps WHERE run_id=?', (run_id,)).fetchone()[0])
            checkpoint_id = 'graph-observation-' + _digest([run_id, snapshot_id, event['event_id'], epoch, summaries])
            existing = self._checkpoint(db, run_id, checkpoint_id)
            if existing is not None:
                return existing
            sequence = db.execute('SELECT COALESCE(MAX(sequence),0) FROM steps WHERE run_id=?', (run_id,)).fetchone()[0]
            # Current object is a declared target reference, not a page title or
            # model-supplied identification claim. The verifier still proves it.
            current_object = run['contract'].targets[0].object_id if run['contract'].targets else snapshot_id
            body = dict(checkpoint_id=checkpoint_id, task_id=run['task_id'], run_id=run_id,
                contract_version=run['contract_version'], current_subgoal=pending[0] if pending else 'aggregate',
                verified_item_ids=verified, pending_item_ids=pending, current_object_id=current_object,
                current_object_version=None, current_snapshot_id=snapshot_id, flow_version=None,
                action_sequence=sequence, business_event_id=event['event_id'], budget_record_ref=budget[0],
                identity_ref=run['contract'].identity_ref, pending_operation_ids=[], epoch=epoch,
                evidence_ids=sorted(observation['evidence_ids']), saved_at=utc_text())
            operations = [r[0] for r in db.execute('''SELECT operation_id FROM write_intents
                WHERE task_id=? AND status IN ('INTENT','UNKNOWN') ORDER BY operation_id''', (run['task_id'],))]
            body['pending_operation_ids'] = operations
            checkpoint = RunCheckpoint.model_validate_json(canonical_json(body))
            db.execute('''INSERT INTO run_checkpoints(checkpoint_id,task_id,run_id,contract_version,current_subgoal,
                verified_item_ids_json,pending_item_ids_json,current_object_id,current_object_version,
                current_snapshot_id,flow_version,action_sequence,business_event_id,budget_record_ref,
                identity_ref,epoch,saved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (checkpoint_id,run['task_id'],run_id,run['contract_version'],body['current_subgoal'],
                 canonical_json(verified),canonical_json(pending),current_object,None,snapshot_id,None,
                 sequence,event['event_id'],budget[0],run['contract'].identity_ref,epoch,body['saved_at']))
            db.executemany('INSERT INTO run_checkpoints_evidence VALUES(?,?,?)',
                           [(run_id, checkpoint_id, ref) for ref in body['evidence_ids']])
            db.executemany('INSERT INTO checkpoint_operations VALUES(?,?,?,?)',
                           [(run['task_id'],run_id,checkpoint_id,op) for op in operations])
            return checkpoint

    def _state(self, db, run):
        progress = db.execute('SELECT * FROM graph_progress WHERE run_id=? ORDER BY progress_id DESC LIMIT 1',
                              (run['run_id'],)).fetchone()
        checkpoint = self._checkpoint(db, run['run_id'], progress['checkpoint_id'] if progress else None)
        _, summaries = self._verification(db, run)
        counts = {row['phase']: row['n'] for row in db.execute('''SELECT phase,count(*) AS n FROM graph_progress
            WHERE run_id=? GROUP BY phase''', (run['run_id'],))}
        latest_event = db.execute('SELECT COALESCE(MAX(event_id),0) FROM task_events WHERE run_id=?',
                                  (run['run_id'],)).fetchone()[0]
        waiting = run['state'] in _WAITING
        terminal = run['state'] in TERMINAL_STATES
        current_progress = (progress is not None and progress['state_version'] == run['state_version']
                            and progress['business_event_id'] == latest_event)
        route = 'stopped' if terminal else ('wait' if waiting else 'reconcile')
        return GraphSnapshot(run_id=run['run_id'],contract_version=run['contract_version'],
            state_version=run['state_version'],business_event_id=latest_event,
            business_checkpoint_id=checkpoint.checkpoint_id if checkpoint else None,
            # A business transition can commit before its graph progress row.
            # Keep historical counters/refs, but do not label that old row as
            # progress for the new event/version when rebuilding graph state.
            progress_id=progress['progress_id'] if current_progress else None,
            snapshot_id=(progress['snapshot_id'] if progress and progress['snapshot_id'] is not None
                         else checkpoint.current_snapshot_id if checkpoint else None),
            route=route,iteration=progress['iteration'] if progress else 0,
            observations=db.execute('''SELECT count(DISTINCT snapshot_id) FROM graph_progress
                WHERE run_id=? AND phase IN ('observe','confirm')''', (run['run_id'],)).fetchone()[0],
            decisions=counts.get('decide',0),
            actions=counts.get('dispatch',0),verifications=counts.get('verify',0),
            diagnostic=progress['diagnostic'] if progress else None,
            evidence_ids=checkpoint.evidence_ids if checkpoint else [],verified_summary_refs=summaries,
            wait_id=progress['wait_id'] if progress and waiting else None,completed=terminal).state()

    def load_state(self, run_id) -> GraphState:
        with connect(self.path) as db:
            return self._state(db, self._run(db, run_id))

    def load_control_state(self, operation) -> GraphState:
        """Read one applied stop receipt and its exact graph anchor together.

        A pending control request is an accepted intent, not new graph
        progress. It may follow a pause's wait event before the framework save
        finishes. Ordinary load_state retains its strict latest-event rule.
        """
        operation_id = _identifier(operation['operation_id'], 'operation_id')
        run_id = _identifier(operation['run_id'], 'run_id')
        with connect(self.path) as db:
            db.execute('BEGIN')
            control = db.execute('SELECT * FROM run_controls WHERE operation_id=? AND run_id=?',
                                 (operation_id, run_id)).fetchone()
            if (control is None or control['status'] != 'APPLIED'
                    or control['action'] not in ('pause', 'cancel')
                    or operation.get('action') != control['action']
                    or operation.get('status') != control['status']):
                raise _conflict('Control completion is unavailable')
            run = self._run(db, run_id)
            result = json.loads(control['result_json'])
            expected = 'PAUSED' if control['action'] == 'pause' else 'CANCELLED'
            completed = db.execute('SELECT * FROM task_events WHERE run_id=? AND event_id=?',
                                   (run_id, control['completed_event_id'])).fetchone()
            payload = json.loads(completed['payload_json']) if completed is not None else {}
            if (control['task_id'] != run['task_id'] or control['contract_version'] != run['contract_version']
                    or result.get('run_id') != run_id or result.get('state') != expected
                    or result.get('state_version') != run['state_version'] or run['state'] != expected
                    or completed is None or completed['task_id'] != run['task_id']
                    or completed['state_version'] != run['state_version']
                    or completed['event_type'] != 'operation_completed'
                    or payload.get('operation_id') != operation_id or payload.get('result_ref') != operation_id
                    or payload.get('action') != control['action'] or payload.get('status') != 'APPLIED'):
                raise _conflict('Control completion no longer matches the Run')
            state = self._state(db, run)
            if control['action'] == 'cancel':
                # Cancellation may be committed despite damaged old optional
                # progress; retain the existing terminal repair behavior.
                return state
            queue = db.execute('SELECT * FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone()
            progress = db.execute('SELECT * FROM graph_progress WHERE run_id=? ORDER BY progress_id DESC LIMIT 1',
                                  (run_id,)).fetchone()
            # The control transaction registers this wait immediately after
            # its completion. A later registration, even reusing the same
            # wait_id, must not replace that immutable anchor.
            wait = db.execute('''SELECT * FROM task_events WHERE run_id=? AND event_id>?
                ORDER BY event_id LIMIT 1''', (run_id, control['completed_event_id'])).fetchone()
            wait_payload = json.loads(wait['payload_json']) if wait is not None else {}
            wait_id = result.get('wait_id')
            if (queue is None or queue['status'] != 'WAITING' or queue['run_state_version'] != run['state_version']
                    or queue['epoch'] != result.get('epoch') or queue['revision'] != result.get('queue_revision')
                    or progress is None or progress['phase'] != 'wait' or not wait_id
                    or progress['wait_id'] != wait_id or progress['state_version'] != run['state_version']
                    or progress['contract_sha256'] != run['contract_sha256']
                    or progress['graph_version'] != GRAPH_VERSION or progress['state_schema_version'] != STATE_SCHEMA_VERSION
                    or wait is None or wait['event_type'] != 'wait_registered' or wait['task_id'] != run['task_id']
                    or wait['state_version'] != run['state_version'] or wait_payload.get('wait_id') != wait_id
                    or wait_payload.get('reason') != 'pause' or progress['business_event_id'] != wait['event_id']
                    or wait['event_id'] <= control['completed_event_id']):
                raise _conflict('Control pause has no current authoritative wait')
            for event in db.execute('SELECT * FROM task_events WHERE run_id=? AND event_id>? ORDER BY event_id',
                                    (run_id, wait['event_id'])):
                pending = db.execute('SELECT * FROM run_controls WHERE run_id=? AND requested_event_id=?',
                                     (run_id, event['event_id'])).fetchone()
                request = json.loads(event['payload_json'])
                if (event['event_type'] != 'operation_requested' or event['state_version'] != run['state_version']
                        or pending is None or pending['status'] != 'PENDING'
                        or pending['task_id'] != run['task_id'] or pending['contract_version'] != run['contract_version']
                        or pending['accepted_run_state_version'] != run['state_version']
                        or pending['requested_state_version'] != run['state_version']
                        or pending['parent_run_id'] != run['parent_run_id']
                        or pending['action'] not in ('pause', 'resume', 'cancel')
                        or request.get('operation_id') != pending['operation_id']
                        or request.get('action') != pending['action']):
                    raise _conflict('Business facts changed after the control wait')
            state.update(business_event_id=wait['event_id'], progress_id=progress['progress_id'])
            checkpoint = self._checkpoint(db, run_id, progress['checkpoint_id']) if progress['checkpoint_id'] else None
            latest_checkpoint = self._checkpoint(db, run_id)
            if (progress['checkpoint_id'] and checkpoint is None
                    or (latest_checkpoint.checkpoint_id if latest_checkpoint else None) != progress['checkpoint_id']
                    or checkpoint is not None and (checkpoint.task_id != run['task_id']
                        or checkpoint.contract_version != run['contract_version']
                        or checkpoint.business_event_id > wait['event_id']
                        or checkpoint.identity_ref != run['contract'].identity_ref)):
                raise _conflict('Control checkpoint references changed')
            if state['snapshot_id']:
                view = EvidenceStore.filtered_observation_row(db, state['snapshot_id'], run_id)
                if (view['policy_version'] != POLICY_VERSION
                        or not any(source.permits(view['content']['source_url']) for source in run['contract'].sources)):
                    raise _conflict('Control observation publication policy is incompatible')
            for ref in state['evidence_ids']:
                metadata = EvidenceStore._metadata(db, ref, run_id)
                if metadata['availability'] != 'AVAILABLE' or metadata['capture_status'] != 'COMPLETE':
                    raise _conflict('Control evidence references are unavailable')
            return validate_graph_state(state)

    def _record(self, db, run, phase, *, snapshot_id=None, verification_id=None,
                diagnostic=None, iteration=None, wait_id=None, event=None):
        if phase not in get_args(Phase) or diagnostic is not None and diagnostic not in get_args(Diagnostic):
            raise BusinessError('INVALID_PARAMETER', 'Invalid graph phase or diagnostic')
        checkpoint = self._checkpoint(db, run['run_id'])
        if snapshot_id is not None:
            _identifier(snapshot_id, 'snapshot_id')
            view = EvidenceStore.filtered_observation_row(db, snapshot_id, run['run_id'])
            if view['policy_version'] != POLICY_VERSION:
                raise _conflict('Observation publication policy is incompatible')
        if verification_id is not None:
            _identifier(verification_id, 'verification_id')
            if not db.execute('''SELECT 1 FROM run_verifications WHERE run_id=? AND verification_id=?
                AND contract_sha256=? AND state_version<=?''',
                (run['run_id'],verification_id,run['contract_sha256'],run['state_version'])).fetchone():
                raise _conflict('Verification reference is unavailable')
        event = event or self._event(db, run)
        if iteration is None:
            previous = db.execute('''SELECT iteration FROM graph_progress WHERE run_id=?
                ORDER BY progress_id DESC LIMIT 1''', (run['run_id'],)).fetchone()
            iteration = previous[0] if previous else 0
        if type(iteration) is not int or not 0 <= iteration < 2**63 - 1:
            raise BusinessError('INVALID_PARAMETER', 'Invalid graph iteration')
        checkpoint_id = checkpoint.checkpoint_id if checkpoint else None
        key = _digest([run['state_version'],phase,event['event_id'],checkpoint_id,snapshot_id,
                       verification_id,wait_id,iteration,diagnostic])
        db.execute('''INSERT INTO graph_progress(run_id,state_version,contract_sha256,graph_version,
            state_schema_version,phase,business_event_id,checkpoint_id,snapshot_id,verification_id,wait_id,
            iteration,diagnostic,idempotency_key,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,idempotency_key) DO NOTHING''',
            (run['run_id'],run['state_version'],run['contract_sha256'],GRAPH_VERSION,STATE_SCHEMA_VERSION,
             phase,event['event_id'],checkpoint_id,snapshot_id,verification_id,wait_id,iteration,diagnostic,key,utc_text()))
        return self._state(db, run)

    def record_progress(self, run_id, phase, *, expected_state_version, execution_token=None,
                        snapshot_id=None, verification_id=None, diagnostic=None, iteration=None):
        with connect(self.path) as db, transaction(db):
            run = self._run(db, run_id)
            if run['state'] in TERMINAL_STATES:
                if expected_state_version != run['state_version'] or phase not in ('aggregate','stopped'):
                    raise _conflict()
                # A terminal event is already a committed business fact. This
                # path cannot create success or revive revoked execution.
                event = self._event(db, run)
                return self._record(db,run,phase,snapshot_id=snapshot_id,verification_id=verification_id,
                                    diagnostic=diagnostic,iteration=iteration,event=event)
            self._qualify(db,run,expected_state_version,execution_token)
            return self._record(db,run,phase,snapshot_id=snapshot_id,verification_id=verification_id,
                                diagnostic=diagnostic,iteration=iteration)

    def record_wait_progress(self, run_id, wait_id, *, expected_state_version, diagnostic=None):
        _identifier(wait_id, 'wait_id')
        with connect(self.path) as db, transaction(db):
            run = self._run(db, run_id)
            if type(expected_state_version) is not int or run['state_version'] != expected_state_version or run['state'] not in _WAITING:
                raise _conflict('Run has no current durable wait')
            event = db.execute('''SELECT * FROM task_events WHERE run_id=? AND state_version=?
                AND event_type='wait_registered' AND json_extract(payload_json,'$.wait_id')=?
                ORDER BY event_id DESC LIMIT 1''', (run_id,expected_state_version,wait_id)).fetchone()
            queue = db.execute('SELECT status FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone()
            if event is None or queue is not None and queue['status'] != 'WAITING':
                raise _conflict('Durable wait registration is unavailable')
            return self._record(db,run,'wait',diagnostic=diagnostic,wait_id=wait_id,event=event)

    def list_progress(self, run_id, *, after=0, limit=100):
        if type(after) is not int or not 0 <= after < 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 1000:
            raise BusinessError('INVALID_PARAMETER', 'Invalid graph progress cursor or page size')
        with connect(self.path) as db:
            self._run(db, run_id)
            return [dict(row) for row in db.execute('''SELECT * FROM graph_progress
                WHERE run_id=? AND progress_id>? ORDER BY progress_id LIMIT ?''', (run_id,after,limit))]

    @staticmethod
    def validate_state(value: dict) -> GraphState:
        try:
            return validate_graph_state(value)
        except (ValidationError, TypeError):
            raise _conflict('Stored graph state is incompatible') from None
