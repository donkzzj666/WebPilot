"""Local authority for observation bindings and irreversible dispatch records.

There is deliberately no browser, model, network or async operation in this
module. A bounded filesystem fault-gate read precedes ``prepare``. A caller
commits ``prepare`` before handing its result to the
private browser driver. Completion records never establish a business write's
receipt: those operations remain UNKNOWN until later reconciliation.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from pydantic import TypeAdapter, ValidationError

from ..budgets.store import BudgetStore
from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..events import ActionEvent, append_event
from ..models.schema import Action
from ..scheduler.models import ExecutionToken, Resource, canonical_site
from ..scheduler.store import validate_in_transaction
from ..tasks.models import RepositoryWritePolicy, TaskContract

_ACTION = TypeAdapter(Action)
_HASH = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_BINDING = ('session_id', 'manager_id', 'session_generation', 'tab_id', 'frame_id',
            'page_version', 'width', 'height')
_READS = ('read_visible', 'screenshot')


def _invalid(field):
    return BusinessError('INVALID_PARAMETER', 'Invalid gateway metadata', field=field)


def _conflict(message='Observation or execution qualification is no longer current'):
    return BusinessError('STATE_CONFLICT', message, status=409)


def _id(value, field):
    if (type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise _invalid(field)
    return value


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _dict(value, field):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if type(value) is not dict:
        raise _invalid(field)
    return value


def _binding(value):
    value = _dict(value, 'binding')
    if not set(_BINDING) <= set(value):
        raise _invalid('binding')
    result = {key: value[key] for key in _BINDING}
    for key in ('session_id', 'manager_id', 'tab_id', 'frame_id', 'page_version'):
        _id(result[key], key)
    if type(result['session_generation']) is not int or result['session_generation'] < 1:
        raise _invalid('session_generation')
    for key in ('width', 'height'):
        if type(result[key]) is not int or not 1 <= result[key] <= 16384:
            raise _invalid(key)
    return result


class GatewayStore:
    def __init__(self, path: Path, budgets=None, *, clock=None):
        self.path = Path(path)
        self.budgets = budgets or BudgetStore(self.path)
        self.clock = clock or self.budgets.clock.utcnow

    def _now(self):
        return utc_text(self.clock())

    def _session(self, db, token, binding, *, allow_reconciling=False):
        now = self.clock()
        validate_in_transaction(db, token, now=now, allow_reconciling=allow_reconciling)
        session = db.execute('SELECT * FROM browser_sessions WHERE session_id=?',
                             (binding['session_id'],)).fetchone()
        if (session is None or session['state'] != 'OPEN' or session['owner_kind'] != 'run'
                or session['run_id'] != token.run_id or session['owner_id'] != token.run_id
                or session['manager_id'] != binding['manager_id']
                or session['generation'] != binding['session_generation']):
            raise _conflict('Managed session belongs to another owner or generation')
        validate_in_transaction(db, token, resource=Resource.browser_context(token.run_id), now=now,
                                allow_reconciling=allow_reconciling)
        scope = Resource.site_identity(session['site_id'], session['identity_ref'], realm=session['realm'])
        validate_in_transaction(db, token, resource=scope, now=now, allow_reconciling=allow_reconciling)
        reservation = db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?',
                                 (token.run_id,)).fetchone()
        if (reservation is None or reservation['session_id'] != session['session_id']
                or reservation['worker_id'] != token.worker_id
                or reservation['worker_generation'] != token.worker_generation
                or reservation['epoch'] != token.epoch):
            raise _conflict('Managed context reservation no longer belongs to this executor')
        return session

    @staticmethod
    def _contract(db, token):
        row = db.execute('''SELECT c.content_json,r.task_id,r.contract_sha256 FROM runs r JOIN contracts c
            ON c.task_id=r.task_id AND c.contract_version=r.contract_version
              AND c.contract_sha256=r.contract_sha256 WHERE r.run_id=?''', (token.run_id,)).fetchone()
        if row is None:
            raise _conflict('Frozen Run contract is unavailable')
        try:
            contract = TaskContract.model_validate_json(row['content_json'])
        except ValidationError:
            raise BusinessError('FORBIDDEN', 'A complete frozen contract is required for browser dispatch',
                                status=403) from None
        return contract

    @staticmethod
    def _site_for_url(contract, token, session, url):
        sites = {canonical_site(source.site_id) for source in contract.sources if source.permits(url)
                 and Resource.site_identity(source.site_id, session['identity_ref'], realm=session['realm']).resource_key
                    in token.resources}
        if len(sites) != 1:
            raise BusinessError('FORBIDDEN', 'Browser URL requires an unambiguous leased source scope', status=403)
        return session['realm'] + ':' + next(iter(sites))

    @staticmethod
    def _permitted(db, token, contract, action, session, external_write, target_resource):
        target = action.target
        urls = [target.page_url]
        if action.action_type == 'navigate':
            urls.append(action.args.url)
        elif action.action_type == 'download_attachment':
            urls.append(action.args.attachment_url)
        for url in urls:
            GatewayStore._site_for_url(contract, token, session, url)
        if contract.identity_ref != session['identity_ref']:
            raise BusinessError('FORBIDDEN', 'Managed session identity differs from the frozen contract', status=403)
        if external_write != (action.expected_effect == 'write'):
            raise BusinessError('FORBIDDEN', 'Write classification cannot bypass the frozen policy', status=403)
        if not external_write:
            if target_resource is not None:
                raise _invalid('target_resource')
            return None
        policy, scope = contract.action_policy, target.write_scope
        if (not isinstance(policy, RepositoryWritePolicy) or scope is None
                or scope.repository != policy.repository or scope.branch != policy.branch
                or scope.base_sha != policy.base_sha or scope.operation not in policy.allowed_operations
                or scope.identity_ref != contract.identity_ref or session['identity_ref'] is None
                or any(not policy.permits_file(path) for path in scope.files)
                or (scope.operation == 'edit_file' and not scope.files)):
            raise BusinessError('FORBIDDEN', 'Write target is outside the frozen repository policy', status=403)
        identity = db.execute('SELECT * FROM identities WHERE identity_ref=?', (scope.identity_ref,)).fetchone()
        if (identity is None or identity['state'] != 'VERIFIED' or identity['realm'] != session['realm']
                or canonical_site(identity['site_id']) != canonical_site(session['site_id'])):
            raise BusinessError('FORBIDDEN', 'Write identity requires fresh scoped verification', status=403)
        resource = Resource.repository_write(policy.repository).resource_key
        if target_resource is not None and target_resource != resource:
            raise BusinessError('FORBIDDEN', 'Write resource differs from the frozen repository', status=403)
        return resource

    @staticmethod
    def _snapshot(db, snapshot_id):
        row = db.execute('''SELECT g.*,o.captured_at,o.source_url,o.title,o.visible_excerpt,o.redaction_status
            FROM gateway_observations g JOIN observations o USING(snapshot_id,run_id)
            WHERE g.snapshot_id=?''', (snapshot_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Gateway observation not found', status=404)
        return dict(row)

    def admit_observation(self, token, *, attempt_id, include_screenshot=False):
        """Reserve capture counters before its first browser call."""
        if type(include_screenshot) is not bool:
            raise _invalid('include_screenshot')
        error = None
        with connect(self.path) as db, transaction(db):
            charge, error = self.budgets.consume_in_transaction(db, token,
                kind='observation', attempt_id=attempt_id)
            if error is None and not charge['dispatch_allowed']:
                raise _conflict('Capture admission was already consumed')
            if error is None and include_screenshot:
                charge, error = self.budgets.consume_in_transaction(db, token,
                    kind='screenshot', attempt_id='shot-' + _hash(attempt_id))
                if error is None and not charge['dispatch_allowed']:
                    raise _conflict('Screenshot admission was already consumed')
        if error is not None:
            raise error

    def record_observation(self, token, binding, capture, *, attempt_id, admitted=False):
        """Record metadata from one already-completed, private Worker capture.

        Sensitive visible content and pixels stay in the private runtime. M1-14
        owns durable redaction/evidence; these records therefore remain BLOCKED.
        """
        if not isinstance(token, ExecutionToken):
            raise _conflict()
        binding, capture = _binding(binding), _dict(capture, 'capture')
        snapshot_id = _id(capture.get('snapshot_id'), 'snapshot_id')
        _id(attempt_id, 'attempt_id')
        if type(admitted) is not bool or admitted and attempt_id != 'capture-' + snapshot_id:
            raise _invalid('attempt_id')
        source_url = capture.get('source_url')
        if type(source_url) is not str or not 1 <= len(source_url) <= 8192:
            raise _invalid('source_url')
        screenshot = capture.get('screenshot_sha256')
        screenshot_ref = capture.get('screenshot_evidence_id')
        if screenshot is not None:
            if type(screenshot) is not str or _HASH.fullmatch(screenshot) is None:
                raise _invalid('screenshot_sha256')
            screenshot_ref = _id(screenshot_ref or ('shot-' + _hash([token.run_id, snapshot_id])), 'screenshot_evidence_id')
        elif screenshot_ref is not None:
            raise _invalid('screenshot_evidence_id')
        safe_capture = dict(snapshot_id=snapshot_id, source_url=source_url,
                            screenshot_sha256=screenshot, screenshot_evidence_id=screenshot_ref)
        fingerprint = _hash([token.run_id, token.epoch, token.state_version, binding, safe_capture])
        error = None
        with connect(self.path) as db, transaction(db):
            session = self._session(db, token, binding, allow_reconciling=True)
            contract = self._contract(db, token)
            if (contract.identity_ref != session['identity_ref'] or not any(
                    source.permits(source_url) for source in contract.sources) and source_url != 'about:blank'):
                raise BusinessError('FORBIDDEN', 'Observation is outside the frozen source or identity scope', status=403)
            if source_url != 'about:blank':
                self._site_for_url(contract, token, session, source_url)
            existing = db.execute('SELECT capture_sha256 FROM gateway_observations WHERE snapshot_id=?',
                                   (snapshot_id,)).fetchone()
            if existing:
                if existing['capture_sha256'] != fingerprint:
                    raise _conflict('Snapshot identifier is bound to another capture')
                return self._snapshot(db, snapshot_id)
            charge, error = self.budgets.consume_in_transaction(db, token, kind='observation', attempt_id=attempt_id)
            if error is None and not charge['dispatch_allowed'] and not (admitted and charge['duplicate']):
                raise _conflict('Capture attempt identifier is already consumed')
            if error is None and screenshot is not None:
                charge, error = self.budgets.consume_in_transaction(db, token, kind='screenshot',
                                                                   attempt_id='shot-' + _hash(attempt_id))
                if error is None and not charge['dispatch_allowed'] and not (admitted and charge['duplicate']):
                    raise _conflict('Screenshot capture attempt identifier is already consumed')
            if error is None:
                now = self._now()
                db.execute('''INSERT INTO observations(snapshot_id,run_id,captured_at,source_url,title,tab_id,
                    frame_id,page_version,width,height,visible_excerpt,redaction_status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'','BLOCKED')''',
                           (snapshot_id, token.run_id, now, source_url, '[worker observation]',
                            binding['tab_id'], binding['frame_id'], binding['page_version'],
                            binding['width'], binding['height']))
                db.execute('''INSERT INTO gateway_observations(snapshot_id,run_id,epoch,state_version,session_id,
                    manager_id,session_generation,tab_id,frame_id,page_version,width,height,screenshot_sha256,
                    screenshot_evidence_id,capture_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (snapshot_id, token.run_id, token.epoch, token.state_version,
                            *[binding[key] for key in _BINDING], screenshot, screenshot_ref, fingerprint))
                db.execute('''INSERT INTO gateway_page_heads VALUES(?,?,1) ON CONFLICT(run_id) DO UPDATE SET
                    snapshot_id=excluded.snapshot_id,valid=1''', (token.run_id, snapshot_id))
                result = self._snapshot(db, snapshot_id)
        if error is not None:
            raise error
        return result

    @staticmethod
    def _outcome(db, step_id):
        row = db.execute('''SELECT g.*,s.status,s.sequence,s.error_code,s.started_at,s.ended_at,
            s.actual_result_json FROM gateway_attempts g JOIN steps s USING(step_id,run_id)
            WHERE g.step_id=?''', (step_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Gateway attempt not found', status=404)
        result = dict(row)
        result['actual_result'] = json.loads(result.pop('actual_result_json'))
        return result

    def prepare(self, token, action, binding, *, external_write=False, business_key=None,
                target_resource=None, expected_change=None, write_claim=None):
        """Commit one dispatch reservation, counted budget, and action intent.

        Retrying the same step never authorizes dispatch again, even if the
        caller lost the response. A new step requires a new Worker observation.
        """
        if not isinstance(token, ExecutionToken):
            raise _conflict()
        from ..evidence.store import EvidenceStore
        EvidenceStore(self.path.parent).assert_dispatch_allowed()
        try:
            action = _ACTION.validate_json(canonical_json(_dict(action, 'action')))
        except (ValidationError, TypeError, ValueError):
            raise _invalid('action') from None
        from ..evidence.redaction import filter_model_text
        filter_model_text({'action': action.model_dump(mode='json')})
        binding = _binding(binding)
        if type(external_write) is not bool:
            raise _invalid('external_write')
        if external_write != (action.expected_effect == 'write'):
            raise BusinessError('FORBIDDEN', 'Write classification cannot bypass the frozen policy', status=403)
        if (action.run_id != token.run_id or action.epoch != token.epoch
                or action.target.tab_id != binding['tab_id'] or action.target.frame_id != binding['frame_id']):
            raise _conflict()
        if business_key is not None:
            _id(business_key, 'business_key')
        # Hash every supplied argument; no input text or free-form effect is
        # retained in the action journal or event stream.
        request = action.model_dump(mode='json')
        claim_content = write_claim.model_dump(mode='json') if hasattr(write_claim, 'model_dump') else write_claim
        fingerprint = _hash([request, binding, external_write, business_key, target_resource, claim_content])
        if external_write:
            from ..writes.models import WriteClaim
            from ..writes.store import WriteProtocolStore
            operation = action.target.write_scope
            try:
                proof_claim = WriteClaim.model_validate_json(canonical_json(claim_content or {
                    'identity_ref': operation.identity_ref,
                    'target': {key: getattr(operation, key) for key in
                               ('repository', 'branch', 'base_sha', 'operation', 'files')},
                    'expected_change_sha256': _hash([action.action_type, request['args']]),
                    'precondition_version': binding['page_version']}))
            except (ValidationError, TypeError, ValueError):
                raise _invalid('write_claim') from None
            # Reopen and hash originals before the bounded writer. Metadata
            # availability alone cannot authorize a cached receipt or retry.
            WriteProtocolStore(self.path, clock=self.clock).assert_claim_proof_integrity(token.run_id, proof_claim)
        error = None
        with connect(self.path) as db, transaction(db):
            duplicate = db.execute('SELECT request_sha256 FROM gateway_attempts WHERE step_id=?',
                                    (action.step_id,)).fetchone()
            if duplicate:
                self._session(db, token, binding, allow_reconciling=True)
                if duplicate['request_sha256'] != fingerprint:
                    raise _conflict('Step identifier is bound to another atomic action')
                outcome = self._outcome(db, action.step_id)
                if outcome['run_id'] != token.run_id or outcome['epoch'] != token.epoch:
                    raise _conflict()
                return {**outcome, 'dispatch_allowed': False, 'duplicate': True}
            from ..controls.models import ControlPending
            from ..controls.store import ControlStore
            pending_control = ControlStore.pending_in_transaction(db, token.run_id)
            if pending_control is not None:
                raise ControlPending(pending_control)
            kind = 'observation' if action.action_type == 'read_visible' else 'screenshot' if action.action_type == 'screenshot' else 'action'
            session = self._session(db, token, binding, allow_reconciling=action.action_type in _READS)
            contract = self._contract(db, token)
            resource = self._permitted(db, token, contract, action, session, external_write, target_resource)
            if resource is not None:
                validate_in_transaction(db, token, resource=resource, now=self.clock())
            snapshot = self._snapshot(db, action.snapshot_id)
            head = db.execute('SELECT * FROM gateway_page_heads WHERE run_id=?', (token.run_id,)).fetchone()
            if (snapshot['run_id'] != token.run_id or snapshot['epoch'] != token.epoch
                    or snapshot['state_version'] != token.state_version or any(
                        snapshot[key] != binding[key] for key in _BINDING)
                    or head is None or head['snapshot_id'] != action.snapshot_id or not head['valid']
                    or (action.target.page_url != snapshot['source_url'] and not (
                        snapshot['source_url'] == 'about:blank' and action.action_type == 'navigate'
                        and action.target.page_url == action.args.url))):
                raise _conflict('A fresh observation of the exact page, tab and frame is required')
            locator = action.target.locator
            if external_write and not snapshot['captured_at'] <= utc_text(action.target.write_scope.target_rechecked_at) <= self._now():
                raise _conflict('Write target was not rechecked against this current observation')
            if locator is not None and locator.strategy == 'coordinate' and (
                    snapshot['screenshot_sha256'] is None
                    or locator.screenshot_evidence_id != snapshot['screenshot_evidence_id']
                    or locator.width != binding['width'] or locator.height != binding['height']):
                raise _conflict('Coordinates are not bound to the current viewport and screenshot')
            pending = db.execute("SELECT s.step_id FROM gateway_attempts g JOIN steps s USING(step_id,run_id) WHERE g.run_id=? AND s.status='INTENT'",
                                 (token.run_id,)).fetchall()
            if pending:
                from ..graph.recovery import RecoveryStore
                from ..writes.store import WriteProtocolStore
                resolved = RecoveryStore(self.path.parent).resolved_read_steps(db, token.run_id)
                if any(row['step_id'] not in resolved and not WriteProtocolStore.is_resolved_step(db, row['step_id'])
                       for row in pending):
                    raise _conflict('An earlier dispatched action still has no outcome')
            operation_id = None
            operation = action.target.write_scope
            write_admission = None
            if external_write:
                from ..writes.models import WriteClaim, WriteTarget, business_key as protocol_key
                from ..writes.store import WriteProtocolStore
                # The key describes the semantic target and expected change,
                # never the model's operation_id, Run, epoch, or snapshot.
                claim = WriteClaim.model_validate(write_claim or {
                    'identity_ref': operation.identity_ref,
                    'target': {key: getattr(operation, key) for key in
                               ('repository', 'branch', 'base_sha', 'operation', 'files')},
                    'expected_change_sha256': _hash([action.action_type, request['args']]),
                    'precondition_version': binding['page_version']})
                if (claim.identity_ref != operation.identity_ref
                        or claim.target != WriteTarget.model_validate({key: getattr(operation, key) for key in
                            ('repository', 'branch', 'base_sha', 'operation', 'files')})):
                    raise BusinessError('FORBIDDEN', 'Trusted write claim differs from the authorized target', status=403)
                db.execute('SAVEPOINT gateway_write_admission')
                if db.execute('SELECT 1 FROM write_protocol_claims WHERE business_key=?',
                              (protocol_key(contract.task_id, claim),)).fetchone():
                    write_admission = WriteProtocolStore(self.path, clock=self.clock).prepare_in_transaction(
                        db, token, claim, operation_id=operation.operation_id)
                    operation_id = write_admission['operation_id']
                    if not write_admission['dispatch_allowed']:
                        db.execute('RELEASE gateway_write_admission')
                        if (write_admission['status'] == 'CONFIRMED'
                                and not write_admission.get('verified_current', False)):
                            # A historical receipt cannot authorize the current
                            # page. Return its stable operation for read-only
                            # reconciliation without changing the durable ledger
                            # or admitting another action budget/physical write.
                            return {**write_admission, 'duplicate': True,
                                    'status': 'UNKNOWN', 'ledger_status': 'CONFIRMED',
                                    'requires_reconciliation': True}
                        return {**write_admission, 'duplicate': True, 'status': write_admission['status']}
                else:
                    # Charge before introducing INTENT: ordinary budget
                    # admission must continue rejecting all unresolved writes.
                    operation_id = operation.operation_id
            opens_page = action.action_type == 'navigate' or (action.action_type == 'click' and not external_write)
            budget_site = self._site_for_url(contract, token, session,
                action.args.url if action.action_type == 'navigate' else action.target.page_url)
            coordinate_check = locator is not None and locator.strategy == 'coordinate'
            if coordinate_check:
                db.execute('SAVEPOINT gateway_charge')
            charge, error = self.budgets.consume_in_transaction(
                db, token, kind=kind, attempt_id='gateway-' + _hash([token.run_id, action.step_id]),
                site_id=budget_site,
                content_page=opens_page, navigation=opens_page)
            if error is None and not charge['dispatch_allowed']:
                raise _conflict('Budget attempt identifier is already consumed')
            if error is None and coordinate_check:
                # The driver's final pixel comparison is a separate screenshot
                # attempt, reserved with the action intent before dispatch.
                charge, error = self.budgets.consume_in_transaction(db, token, kind='screenshot',
                    attempt_id='gateway-final-shot-' + _hash([token.run_id, action.step_id]))
                if error is None and not charge['dispatch_allowed']:
                    raise _conflict('Screenshot attempt identifier is already consumed')
                if error is not None:
                    # A deadline between the two reservations cannot leave an
                    # action debit with no intent. Keep elapsed time and stop
                    # durable after rolling back this dispatch reservation.
                    db.execute('ROLLBACK TO gateway_charge')
                    self.budgets.flush_in_transaction(db, token.run_id)
            if coordinate_check:
                db.execute('RELEASE gateway_charge')
            if error is not None and external_write:
                db.execute('ROLLBACK TO gateway_write_admission')
                self.budgets.flush_in_transaction(db, token.run_id)
            if error is None:
                if external_write and write_admission is None:
                    write_admission = WriteProtocolStore(self.path, clock=self.clock).prepare_in_transaction(
                        db, token, claim, operation_id=operation.operation_id)
                now = self._now()
                sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM steps WHERE run_id=?',
                                      (token.run_id,)).fetchone()[0]
                safe_action = {'action_type': action.action_type, 'request_sha256': fingerprint,
                               'target_sha256': _hash(request['target']), 'args_sha256': _hash(request['args']),
                               'expected_effect': action.expected_effect}
                db.execute('''INSERT INTO steps(step_id,run_id,sequence,step_kind,epoch,input_snapshot_id,
                    action_json,started_at) VALUES(?,?,?,'atomic_action',?,?,?,?)''',
                           (action.step_id, token.run_id, sequence, token.epoch, action.snapshot_id,
                            canonical_json(safe_action), now))
                db.execute('''INSERT INTO gateway_attempts(step_id,run_id,snapshot_id,action_kind,request_sha256,
                    session_id,manager_id,session_generation,worker_id,worker_generation,epoch,state_version,
                    external_write,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (action.step_id, token.run_id, action.snapshot_id, action.action_type, fingerprint,
                            binding['session_id'], binding['manager_id'], binding['session_generation'],
                            token.worker_id, token.worker_generation, token.epoch, token.state_version,
                            int(external_write), operation_id))
                if operation_id is not None:
                    WriteProtocolStore(self.path, clock=self.clock).link_attempt_in_transaction(
                        db, token, operation_id, action.step_id, new=write_admission['new'])
                db.execute('UPDATE gateway_page_heads SET valid=0 WHERE run_id=?', (token.run_id,))
                append_event(db, run_id=token.run_id, expected_state_version=token.state_version,
                             payload=ActionEvent(step_id=action.step_id, action_type=action.action_type,
                                                 attempt_status='INTENT', evidence_ids=[]))
                result = {**self._outcome(db, action.step_id), 'dispatch_allowed': True, 'duplicate': False}
            if external_write:
                db.execute('RELEASE gateway_write_admission')
        if error is not None:
            raise error
        return result

    def finish(self, token, step_id, *, outcome='COMPLETED', error_code=None, result=None):
        """Conservatively persist the dispatched action's single terminal outcome.

        Loss of qualification discards a late result. Any external write keeps
        UNKNOWN and logical quarantine, regardless of Playwright's return value.
        This is cleanup only and never creates or renews execution permission.
        """
        if not isinstance(token, ExecutionToken):
            raise _conflict()
        _id(step_id, 'step_id')
        if outcome not in ('COMPLETED', 'FAILED', 'UNKNOWN'):
            raise _invalid('outcome')
        if error_code is not None:
            _id(error_code, 'error_code')
            if re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', error_code) is None:
                raise _invalid('error_code')
        with connect(self.path) as db, transaction(db):
            attempt = self._outcome(db, step_id)
            if (attempt['run_id'] != token.run_id or attempt['worker_id'] != token.worker_id
                    or attempt['worker_generation'] != token.worker_generation or attempt['epoch'] != token.epoch
                    or attempt['state_version'] != token.state_version):
                raise _conflict('Cleanup token does not belong to this dispatched attempt')
            if attempt['status'] != 'INTENT':
                return {**attempt, 'dispatch_allowed': False, 'duplicate': True}
            fresh = True
            try:
                snapshot = self._snapshot(db, attempt['snapshot_id'])
                session = self._session(db, token, snapshot, allow_reconciling=True)
                status = self.budgets.flush_in_transaction(db, token.run_id)
                if status is None or status['exhausted']:
                    fresh = False
            except BusinessError:
                fresh = False
            final = 'UNKNOWN' if attempt['external_write'] or not fresh else outcome
            safe_result = ({'result_sha256': _hash(result)} if result is not None and fresh and final == 'COMPLETED' else None)
            if safe_result is not None and not attempt['external_write'] and type(result) is dict:
                # The private browser's current URL is a restoration hint for
                # a new read, never authorization to replay this action.
                # Page content and arbitrary result fields remain hash-only.
                from ..evidence.redaction import TextRedactor, REDACTED
                from urllib.parse import unquote
                source_url = result.get('source_url')
                if (type(source_url) is str and 0 < len(source_url) <= 8192
                        and REDACTED not in unquote(source_url)
                        and not TextRedactor().contains_sensitive(source_url)):
                    contract = self._contract(db, token)
                    try:
                        self._site_for_url(contract, token, session, source_url)
                    except BusinessError:
                        pass
                    else:
                        safe_result['source_url'] = source_url
            final_error = error_code if fresh else 'RESOURCE_CONFLICT'
            if attempt['external_write']:
                final_error = error_code or 'WRITE_REQUIRES_RECONCILIATION'
                operation_id = attempt['operation_id']
                now = max(self._now(), attempt['started_at'])
                from ..writes.store import WriteProtocolStore
                WriteProtocolStore(self.path, clock=self.clock).mark_unknown_in_transaction(
                    db, operation_id, now=datetime.fromisoformat(now.replace('Z', '+00:00')), reason=final_error)
                for lease in db.execute("SELECT resource_key FROM resource_leases WHERE holder_run_id=? AND resource_type IN ('site_identity','repository_write','webarena_environment')",
                                        (token.run_id,)).fetchall():
                    db.execute('INSERT INTO resource_quarantines VALUES(?,?,?) ON CONFLICT DO NOTHING',
                               (lease['resource_key'], operation_id, now))
                    db.execute('''UPDATE resource_leases SET logical_hold=1,state_version=state_version+1
                        WHERE resource_key=?''', (lease['resource_key'],))
            db.execute('''UPDATE steps SET status=?,error_code=?,actual_result_json=?,ended_at=? WHERE step_id=?''',
                       (final, final_error, canonical_json(safe_result), max(self._now(), attempt['started_at']), step_id))
            run = db.execute('SELECT state_version FROM runs WHERE run_id=?', (token.run_id,)).fetchone()
            append_event(db, run_id=token.run_id, expected_state_version=run['state_version'],
                         payload=ActionEvent(step_id=step_id, action_type=attempt['action_kind'],
                                             attempt_status=final, evidence_ids=[]))
            return {**self._outcome(db, step_id), 'dispatch_allowed': False, 'duplicate': False,
                    'result_accepted': fresh and final == 'COMPLETED'}

    def get_observation(self, run_id, snapshot_id):
        _id(run_id, 'run_id'); _id(snapshot_id, 'snapshot_id')
        with connect(self.path) as db:
            snapshot = self._snapshot(db, snapshot_id)
            if snapshot['run_id'] != run_id:
                raise BusinessError('NOT_FOUND', 'Gateway observation not found', status=404)
            return snapshot

    def get_attempt(self, run_id, step_id):
        _id(run_id, 'run_id'); _id(step_id, 'step_id')
        with connect(self.path) as db:
            result = self._outcome(db, step_id)
            if result['run_id'] != run_id:
                raise BusinessError('NOT_FOUND', 'Gateway attempt not found', status=404)
            return result

    def list_attempts(self, run_id, *, limit=100):
        _id(run_id, 'run_id')
        if type(limit) is not int or not 1 <= limit <= 500:
            raise _invalid('limit')
        with connect(self.path) as db:
            db.execute('BEGIN')
            rows = db.execute('''SELECT g.step_id FROM gateway_attempts g JOIN steps s USING(step_id,run_id)
                WHERE g.run_id=? ORDER BY s.sequence LIMIT ?''', (run_id, limit)).fetchall()
            result = [self._outcome(db, row['step_id']) for row in rows]
            db.execute('COMMIT')
            return result
