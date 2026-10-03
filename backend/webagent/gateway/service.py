"""Trusted Worker pipeline: observe, check, journal, dispatch, record.

No public route accepts execution tokens, page adapters, or callable builders.
Raw captures and downloads remain bounded, transient Worker data until the
separate evidence/redaction module can produce deliverable evidence.
"""
import asyncio
from collections import OrderedDict
from contextlib import suppress
from dataclasses import asdict, is_dataclass
import hashlib
import math
from pathlib import Path
import time
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from ..budgets.deadline import BudgetDeadlineExceeded, DeadlineController
from ..db import connect
from ..db.repository import canonical_json
from ..errors import BusinessError
from ..evidence.service import EvidenceService
from ..evidence.redaction import TextRedactor, safe_url, filter_model_text
from ..models.schema import Action, CoordinateLocator
from ..scheduler.models import ExecutionToken, Resource
from ..scheduler.store import SchedulerStore, validate_in_transaction
from ..sessions.models import SessionOwner
from ..sessions.store import SessionRegistry
from ..tasks.models import TaskContract
from .permissions import validate_write_authorization
from .store import GatewayStore

_ACTION = TypeAdapter(Action)


def _safe_result(value):
    """Hash byte payloads before giving the journal its metadata-only result."""
    if type(value) is dict:
        return {key: _safe_result(item) for key, item in value.items()
                if not (key == 'source_url' and type(item) is str and TextRedactor().contains_sensitive(item))}
    if isinstance(value, bytes):
        return {'sha256': hashlib.sha256(value).hexdigest(), 'size': len(value)}
    if type(value) in (list, tuple):
        return [_safe_result(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return TextRedactor().filter(value) if type(value) is str else value
    return {'available': True}


class BrowserGateway:
    def __init__(self, path, browser_backend, *, store=None, scheduler=None,
                 write_authorizer=None, write_verifier=None, cancel_seconds=1):
        if (type(cancel_seconds) not in (int, float) or not math.isfinite(cancel_seconds)
                or not 0 < cancel_seconds <= 5):
            raise ValueError('Cancellation interval must be between zero and five seconds')
        if write_authorizer is not None and not callable(write_authorizer):
            raise ValueError('Write authorization must be a trusted adapter')
        if write_verifier is not None and not callable(write_verifier):
            raise ValueError('Write verification must be a trusted adapter')
        self.path, self.browser = Path(path), browser_backend
        self.scheduler = scheduler or SchedulerStore(self.path)
        self.store = store or GatewayStore(self.path, self.scheduler.budgets)
        self.registry = SessionRegistry(self.path)
        self.evidence = EvidenceService(self.path.parent)
        self.write_authorizer, self.cancel_seconds = write_authorizer, cancel_seconds
        self.write_verifier = write_verifier
        self._gate, self._closed = asyncio.Lock(), False
        self._local_results = OrderedDict()
        if not isinstance(self.browser.owner, SessionOwner) or self.browser.owner.kind != 'run':
            raise BusinessError('FORBIDDEN', 'Action gateway requires a managed Run context', status=403)

    @property
    def supports_write_protocol(self):
        return self.write_authorizer is not None and self.write_verifier is not None

    @classmethod
    def from_managed(cls, managed, session, **kwargs):
        from .browser import BrowserBackend
        with connect(managed.settings.business_db) as db:
            row = db.execute('''SELECT c.content_json FROM runs r JOIN contracts c
                ON c.task_id=r.task_id AND c.contract_version=r.contract_version WHERE r.run_id=?''',
                (session.owner.owner_id,)).fetchone()
            if row is None:
                raise BusinessError('NOT_FOUND', 'Run not found', status=404)
            contract = TaskContract.model_validate_json(row[0])
        backend = BrowserBackend(managed, session.session_id, session.owner,
                                 allowed_sources=tuple(contract.sources))
        return cls(managed.settings.business_db, backend, **kwargs)

    def _qualified(self, token, *, _recovery=False, _new_work=False):
        self.evidence.store.assert_dispatch_allowed()
        if (self._closed or not isinstance(token, ExecutionToken)
                or token.run_id != self.browser.owner.owner_id):
            raise BusinessError('RESOURCE_CONFLICT', 'Current Run execution qualification is required', status=409)
        with connect(self.path) as db:
            db.execute('BEGIN')
            qualification = validate_in_transaction(db, token, allow_reconciling=_recovery)
            if _new_work:
                from ..controls.models import ControlPending
                from ..controls.store import ControlStore
                pending = ControlStore.pending_in_transaction(db, token.run_id)
                if pending is not None:
                    raise ControlPending(pending)
            if _recovery and qualification['state'] != 'RECONCILING':
                raise BusinessError('RESOURCE_CONFLICT', 'A recovery qualification is required', status=409)
            session = self.registry._owned(db, self.browser.session_id, self.browser.owner)
            if session['state'] != 'OPEN' or session['manager_id'] != self.browser.managed.manager_id:
                raise BusinessError('STATE_CONFLICT', 'Managed browser generation is no longer current', status=409)
            contract = self.store._contract(db, token)
            if contract.identity_ref != session['identity_ref']:
                raise BusinessError('FORBIDDEN', 'Session identity differs from the frozen Run', status=403)
            if _recovery and (contract.action_policy.mode != 'read_only' or db.execute(
                    "SELECT 1 FROM write_intents WHERE task_id=? AND status IN ('INTENT','UNKNOWN') LIMIT 1",
                    (contract.task_id,)).fetchone()):
                raise BusinessError('RESOURCE_CONFLICT', 'Uncertain writes forbid automatic recovery', status=409)
            return contract, dict(session)

    def _recovery_qualified(self, token, recovery_id):
        from ..graph.recovery import RecoveryStore
        RecoveryStore(self.path.parent).require_active(token, recovery_id)
        return self._qualified(token, _recovery=True)

    def _write_check_qualified(self, token, operation_id):
        """A query qualification grants no ordinary browser action permission."""
        self.evidence.store.assert_dispatch_allowed()
        if (self._closed or not isinstance(token, ExecutionToken)
                or token.run_id != self.browser.owner.owner_id or not self.supports_write_protocol):
            raise BusinessError('RESOURCE_CONFLICT', 'Trusted write query qualification is required', status=409)
        with connect(self.path) as db:
            db.execute('BEGIN')
            validate_in_transaction(db, token, allow_reconciling=True)
            from ..controls.models import ControlPending
            from ..controls.store import ControlStore
            pending = ControlStore.pending_in_transaction(db, token.run_id)
            if pending is not None:
                raise ControlPending(pending)
            session = self.registry._owned(db, self.browser.session_id, self.browser.owner)
            contract = self.store._contract(db, token)
            operation = db.execute('SELECT * FROM write_intents WHERE operation_id=?', (operation_id,)).fetchone()
            if (session['state'] != 'OPEN' or session['manager_id'] != self.browser.managed.manager_id
                    or contract.identity_ref != session['identity_ref'] or operation is None
                    or operation['task_id'] != contract.task_id or operation['identity_ref'] != contract.identity_ref):
                raise BusinessError('FORBIDDEN', 'Write query identity, task or session binding differs', status=403)
            if db.execute('SELECT stop_reason FROM budget_timers WHERE run_id=?', (token.run_id,)).fetchone()[0]:
                raise BusinessError('BUDGET_EXCEEDED', 'Write query budget is exhausted', status=409)
            self.store._session(db, token, {'session_id': session['session_id'],
                'manager_id': session['manager_id'], 'session_generation': session['generation']},
                allow_reconciling=True)
            return contract, dict(session)

    def _scope(self, contract, token, url):
        owner = self.browser.owner
        permitted = [source for source in contract.sources if source.permits(url)]
        if not any(Resource.site_identity(source.site_id, owner.identity_ref, realm=owner.realm).resource_key
                   in token.resources for source in permitted):
            raise BusinessError('FORBIDDEN', 'Browser URL is outside the frozen or leased source scope', status=403)

    def _binding(self, session, capture):
        return {**{key: capture[key] for key in ('tab_id', 'frame_id', 'page_version', 'width', 'height')},
                'session_id': session['session_id'], 'manager_id': session['manager_id'],
                'session_generation': session['generation']}

    async def _fence(self, token, step_id, error_code):
        self._closed = True
        try:
            if step_id is not None:
                await asyncio.to_thread(self.store.finish, token, step_id, outcome='UNKNOWN', error_code=error_code)
        finally:
            try:
                await asyncio.to_thread(self.scheduler.abandon, token)
            except BusinessError as error:
                if error.status != 409:
                    raise

    async def _budget_failure(self, token, error):
        if isinstance(error, BusinessError) and error.code == 'BUDGET_EXCEEDED':
            self._closed = True
            status = await asyncio.to_thread(self.scheduler.budgets.status, token.run_id)
            if status['exhausted']:
                try:
                    await asyncio.to_thread(self.scheduler.expire_budget, token.run_id, status['reason'])
                except BusinessError as conflict:
                    if conflict.status != 409:
                        raise

    async def _journal(self, token, function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, token, *args, **kwargs)
        except BusinessError as error:
            await self._budget_failure(token, error)
            raise
        except Exception as error:
            self.evidence.check_storage_error(error)
            raise

    async def _bounded(self, token, factory, timeout, *, step_id=None, read_check=False):
        local_deadline = time.monotonic() + timeout

        async def check():
            status = await asyncio.to_thread(self.scheduler.budgets.flush, token)
            if status['exhausted']:
                return status
            if time.monotonic() >= local_deadline:
                return {'exhausted': True, 'reason': 'action_timeout'}
            return status

        async def expire(reason):
            if reason == 'action_timeout':
                await self._fence(token, step_id, 'TIMEOUT')
            else:
                self._closed = True
                await asyncio.to_thread(self.scheduler.expire_budget, token.run_id, reason)

        async def fail_closed():
            await self._fence(token, step_id, 'RESOURCE_CONFLICT')

        controller = DeadlineController(check, expire, fence_failure=fail_closed,
            poll_seconds=.02, cancel_seconds=self.cancel_seconds)
        async def guarded_factory():
            try:
                return True, await factory()
            except BusinessError as error:
                rejected_read = (read_check and error.code in ('STATE_CONFLICT', 'INPUT_BLOCKED',
                                  'SERVICE_UNAVAILABLE', 'EVIDENCE_MISSING', 'EVIDENCE_CORRUPT'))
                if step_id is None and (rejected_read or error.code in ('FORBIDDEN', 'INVALID_PARAMETER', 'NOT_FOUND')):
                    # A policy rejection during read-only preparation has no
                    # dispatched intent to fence. Still run the final deadline
                    # check before returning that rejection to the caller.
                    return False, error
                raise
        operation = asyncio.create_task(controller.run(guarded_factory))
        try:
            accepted, result = await asyncio.shield(operation)
            if not accepted:
                raise result
            return result
        except asyncio.CancelledError:
            # Shield the child until its intent and qualification have been
            # fenced; cancellation cannot race an unfenced browser operation.
            await self._fence(token, step_id, 'CANCELLED')
            operation.cancel()
            await asyncio.wait({operation}, timeout=self.cancel_seconds + .1)
            with suppress(BaseException):
                if operation.done():
                    operation.result()
            raise
        except BudgetDeadlineExceeded as error:
            self._closed = True
            if error.reason == 'action_timeout':
                raise BusinessError('TIMEOUT', 'Browser action timed out', status=504) from None
            raise
        except BusinessError:
            raise
        except Exception:
            raise BusinessError('SERVICE_UNAVAILABLE', 'Browser gateway operation failed', status=503) from None

    async def _observe(self, token, *, include_screenshot=False, tab_id=None, frame_id=None,
                       recovery_id=None, write_operation_id=None):
        if write_operation_id is not None:
            contract, session = await asyncio.to_thread(self._write_check_qualified, token, write_operation_id)
            capture_factory = lambda: self.browser.write_check_capture(token, write_operation_id)
        elif recovery_id is None:
            contract, session = await asyncio.to_thread(self._qualified, token, _new_work=True)
            capture_factory = lambda: self.browser.capture(token, include_screenshot=include_screenshot,
                tab_id=tab_id, frame_id=frame_id)
        else:
            contract, session = await asyncio.to_thread(self._recovery_qualified, token, recovery_id)
            capture_factory = lambda: self.browser.recovery_capture(token)
        snapshot_id = 'snapshot-' + str(uuid4())
        attempt_id = 'capture-' + snapshot_id
        await self._journal(token, self.store.admit_observation, attempt_id=attempt_id,
                            include_screenshot=include_screenshot)
        capture = await self._bounded(token, capture_factory, contract.budget_profile.action_timeout_seconds,
                                      read_check=write_operation_id is not None)
        if recovery_id is not None:
            await asyncio.to_thread(self._recovery_qualified, token, recovery_id)
        if write_operation_id is not None:
            await asyncio.to_thread(self._write_check_qualified, token, write_operation_id)
        capture = asdict(capture) if is_dataclass(capture) else dict(capture)
        url = capture.get('page_url', capture.get('source_url'))
        if TextRedactor().contains_sensitive(url):
            raise BusinessError('INPUT_BLOCKED', 'Sensitive browser binding cannot be persisted', status=409)
        if url != 'about:blank':
            self._scope(contract, token, url)
        metadata = {'snapshot_id': snapshot_id, 'source_url': url,
                    'screenshot_sha256': capture.get('screenshot_sha256')}
        if capture.get('screenshot') is not None:
            metadata['screenshot_sha256'] = hashlib.sha256(capture['screenshot']).hexdigest()
            metadata['screenshot_evidence_id'] = 'shot-' + str(uuid4())
        snapshot = await self._journal(token, self.store.record_observation, self._binding(session, capture),
            metadata, attempt_id=attempt_id, admitted=True)
        capture['links'] = [{**link, 'link_evidence_id': 'link-' + str(uuid4())}
                            for link in capture.get('links', ())]
        self._remember(snapshot_id, capture)
        if url != 'about:blank':
            await asyncio.to_thread(self.evidence.publish_observation, snapshot, capture, execution_token=token)
        return snapshot

    async def _write_check_navigate(self, token, url, operation_id):
        contract, _ = await asyncio.to_thread(self._write_check_qualified, token, operation_id)
        self._scope(contract, token, url)
        source = next(source for source in contract.sources if source.permits(url))
        attempt_id = 'write-query-navigation-' + uuid4().hex
        async def paced_query():
            while True:
                await asyncio.to_thread(self._write_check_qualified, token, operation_id)
                try:
                    charge = await self._journal(token, self.scheduler.budgets.consume, kind='action', attempt_id=attempt_id,
                        site_id=self.browser.owner.realm + ':' + source.site_id, content_page=True, navigation=True,
                        allow_write_check=True)
                    break
                except BusinessError as error:
                    if error.code != 'SITE_THROTTLED':
                        raise
                    # The same local deadline and remaining Run budget cover
                    # pacing; no token renewal or action refund occurs here.
                    await asyncio.sleep(.05)
            if not charge['dispatch_allowed']:
                raise BusinessError('STATE_CONFLICT', 'Write query GET was already consumed', status=409)
            result = await self.browser.write_check_navigate(token, url, operation_id)
            await asyncio.to_thread(self._write_check_qualified, token, operation_id)
            return result
        return await self._bounded(token, paced_query, contract.budget_profile.action_timeout_seconds, read_check=True)

    async def _assert_write_check_snapshot(self, token, operation_id, snapshot_id):
        contract, session = await asyncio.to_thread(self._write_check_qualified, token, operation_id)
        snapshot = await asyncio.to_thread(self.store.get_observation, token.run_id, snapshot_id)
        await self._journal(token, self.store.admit_observation,
                            attempt_id='write-query-recheck-' + uuid4().hex)
        current = await self._bounded(token, lambda: self.browser.write_check_capture(token, operation_id),
                                     contract.budget_profile.action_timeout_seconds, read_check=True)
        current = asdict(current) if is_dataclass(current) else dict(current)
        binding = self._binding(session, current)
        if (any(binding[key] != snapshot[key] for key in binding)
                or current.get('page_url', current.get('source_url')) != snapshot['source_url']):
            raise BusinessError('STATE_CONFLICT', 'Write query page changed before proof commit', status=409)
        await asyncio.to_thread(self._write_check_qualified, token, operation_id)

    async def reconcile_write(self, token, operation_id, *, check_id=None):
        """Query an existing intent; a query never dispatches a stored action."""
        if not self.supports_write_protocol:
            raise BusinessError('FORBIDDEN', 'A trusted write protocol adapter is required', status=403)
        from ..writes.service import WriteCheckSurface, WriteVerificationService
        async with self._gate:
            await asyncio.to_thread(self._write_check_qualified, token, operation_id)
            surface = WriteCheckSurface(self, token, operation_id)
            verifier = WriteVerificationService(self.path, self.evidence)
            try:
                return await self._bounded(token,
                    lambda: verifier.check(token, operation_id, self.write_verifier, surface, check_id=check_id),
                    (await asyncio.to_thread(self._write_check_qualified, token, operation_id))[0].budget_profile.action_timeout_seconds,
                    read_check=True)
            except BaseException:
                # Invalid facts, cancellation, or changed surfaces preserve the
                # uncertainty; cleanup never claims an external rollback.
                with suppress(BusinessError):
                    await asyncio.to_thread(verifier.store.mark_unknown, token, operation_id,
                                            reason='WRITE_CHECK_FAILED')
                raise

    def model_observation(self, snapshot_id):
        """Verified derivative DTO, distinct from raw action-binding metadata."""
        return self.evidence.store.filtered_observation(snapshot_id,
            self.browser.owner.owner_id)['content']

    async def observe(self, token, *, include_screenshot=False, tab_id=None, frame_id=None):
        if type(include_screenshot) is not bool:
            raise BusinessError('INVALID_PARAMETER', 'Invalid screenshot option', field='include_screenshot')
        async with self._gate:
            return await self._observe(token, include_screenshot=include_screenshot, tab_id=tab_id, frame_id=frame_id)

    async def recovery_observe(self, token, recovery_id):
        """Bounded original evidence for reconciliation, never model context."""
        async with self._gate:
            return await self._observe(token, recovery_id=recovery_id)

    async def recovery_navigate(self, token, url, recovery_id):
        """A new journaled, paced GET; old clicks/actions are never replayed."""
        from ..graph.recovery import RecoveryStore
        recovery = RecoveryStore(self.path.parent)
        async with self._gate:
            contract, session = await asyncio.to_thread(self._recovery_qualified, token, recovery_id)
            self._scope(contract, token, url)
            site_id = next(source.site_id for source in contract.sources if source.permits(url))
            attempt_id = 'recovery-navigation-' + uuid4().hex
            # Pacing waits retain the same budget and qualification. A new
            # process cannot use restoration to receive another free action.
            while True:
                await asyncio.to_thread(self._recovery_qualified, token, recovery_id)
                try:
                    charged = await asyncio.to_thread(recovery.navigation_intent, token,
                        recovery_id, url, attempt_id, site_id=site_id)
                    break
                except BusinessError as error:
                    if error.code != 'SITE_THROTTLED':
                        await self._budget_failure(token, error)
                        raise
                    await asyncio.sleep(.1)
            if not charged['dispatch_allowed']:
                raise BusinessError('STATE_CONFLICT', 'Recovery GET was already consumed', status=409)
            try:
                result = await self._bounded(token, lambda: self.browser.recovery_navigate(token, url),
                                             contract.budget_profile.action_timeout_seconds)
                await asyncio.to_thread(self._recovery_qualified, token, recovery_id)
                await asyncio.to_thread(recovery.record_navigation, token, recovery_id, url, attempt_id, 'COMPLETED')
                return result
            except BaseException:
                # This is a read ledger. Its UNKNOWN fact never becomes an
                # authorization to repeat a click, form action, or write.
                with suppress(BusinessError):
                    await asyncio.to_thread(recovery.record_navigation, token, recovery_id, url, attempt_id, 'UNKNOWN')
                raise

    def _remember(self, key, value):
        self._local_results[key] = value
        self._local_results.move_to_end(key)
        while len(self._local_results) > 8:
            self._local_results.popitem(last=False)

    def local_result(self, key):
        """Private runtime data, neither HTTP-readable nor deliverable evidence."""
        return self._local_results.get(key)

    async def _dispatch(self, token, value):
        try:
            if hasattr(value, 'model_dump'):
                value = value.model_dump(mode='json')
            action = _ACTION.validate_json(canonical_json(value))
        except (ValidationError, TypeError, ValueError):
            raise BusinessError('INVALID_PARAMETER', 'Invalid structured browser action', field='action') from None
        filter_model_text({'action': action.model_dump(mode='json')})
        contract, session = await asyncio.to_thread(self._qualified, token, _new_work=True)
        action_deadline = time.monotonic() + contract.budget_profile.action_timeout_seconds
        if action.run_id != token.run_id or action.epoch != token.epoch:
            raise BusinessError('RESOURCE_CONFLICT', 'Action belongs to an old executor', status=409)
        snapshot = await asyncio.to_thread(self.store.get_observation, token.run_id, action.snapshot_id)
        self._scope(contract, token, action.target.page_url)
        if action.action_type == 'navigate':
            self._scope(contract, token, action.args.url)
        if action.action_type == 'download_attachment':
            self._scope(contract, token, action.args.attachment_url)
        external_write = action.expected_effect == 'write'
        if external_write and self.write_authorizer is None:
            raise BusinessError('FORBIDDEN', 'A trusted page adapter must authorize writes', status=403)
        if external_write:
            # The timestamp is a trusted recheck request, not a model claim.
            # Keep the same DTO through prepare and execute so its fingerprint
            # also fences any subsequent changes to arguments or write scope.
            scope = action.target.write_scope.model_copy(update={'target_rechecked_at': self.store.clock()})
            action = action.model_copy(update={'target': action.target.model_copy(update={'write_scope': scope})})
        if isinstance(action.target.locator, CoordinateLocator):
            await self._journal(token, self.scheduler.budgets.consume,
                attempt_id='coordinate-check-' + str(uuid4()), kind='screenshot')
        if action.action_type == 'download_attachment':
            capture = self.local_result(action.snapshot_id)
            links = capture.get('links', ()) if capture else ()
            if not any(link.get('link_evidence_id') == action.args.link_evidence_id
                       and link.get('href') == action.args.attachment_url for link in links):
                raise BusinessError('STATE_CONFLICT', 'Attachment reference belongs to another observation', status=409)
        prepared = await self._bounded(token, lambda: self.browser.prepare(token, action, snapshot,
            trusted_write=external_write), action_deadline - time.monotonic())
        allowed_mutations = ()
        write_claim = None
        if external_write:
            grant = await self._bounded(token, lambda: self.write_authorizer(action, snapshot, prepared),
                                        action_deadline - time.monotonic())
            grant = validate_write_authorization(grant, action, contract)
            if self.supports_write_protocol and (grant.expected_change_sha256 is None
                    or grant.precondition_version is None or grant.adapter_id is None):
                raise BusinessError('FORBIDDEN', 'Write protocol requires explicit trusted effect, object version and adapter facts', status=403)
            allowed_mutations = grant.allowed_mutations
            from ..writes.models import WriteClaim
            write_claim = WriteClaim(identity_ref=grant.identity_ref,
                target={**{key: getattr(grant, key) for key in ('repository', 'branch', 'base_sha', 'operation')},
                        'files': list(grant.files)},
                expected_change_sha256=grant.expected_change_sha256 or hashlib.sha256(
                    canonical_json([action.action_type, action.args.model_dump(mode='json')]).encode()).hexdigest(),
                precondition_version=grant.precondition_version or snapshot['page_version'],
                adapter_id=grant.adapter_id or 'gateway-structured-v1')
        binding = {key: snapshot[key] for key in ('session_id', 'manager_id', 'session_generation',
                   'tab_id', 'frame_id', 'page_version', 'width', 'height')}
        intent = await self._journal(token, self.store.prepare, action, binding,
                                     external_write=external_write, write_claim=write_claim)
        if not intent['dispatch_allowed']:
            return intent
        if external_write and intent['operation_id'] != action.target.write_scope.operation_id:
            scope = action.target.write_scope.model_copy(update={'operation_id': intent['operation_id']})
            action = action.model_copy(update={'target': action.target.model_copy(update={'write_scope': scope})})
            # PreparedTarget hashes the action. Refresh only its immutable
            # fingerprint after substituting the existing semantic operation.
            from dataclasses import replace
            prepared = replace(prepared, action_sha256=hashlib.sha256(
                canonical_json(action.model_dump(mode='json')).encode()).hexdigest())
        try:
            self.evidence.store.assert_dispatch_allowed()
            result = await self._bounded(token, lambda: self.browser.execute(token, action, prepared,
                allowed_mutations=allowed_mutations), action_deadline - time.monotonic(),
                step_id=action.step_id)
            try:
                with connect(self.path) as db:
                    validate_in_transaction(db, token, allow_reconciling=True)
            except BusinessError as changed:
                if changed.status != 409:
                    raise
                # Late results after pause/revocation remain UNKNOWN; stale
                # executors cannot publish their bytes as current evidence.
                return await asyncio.to_thread(self.store.finish, token, action.step_id,
                    result=_safe_result(result))
            await asyncio.to_thread(self.evidence.publish_result, token.run_id,
                action.snapshot_id, action.step_id, result, execution_token=token,
                source_url=action.args.url if action.action_type == 'navigate' else action.target.page_url)
            record = await asyncio.to_thread(self.store.finish, token, action.step_id, result=_safe_result(result))
            if record.get('result_accepted'):
                self._remember(action.step_id, result)
            return record
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = error.code if isinstance(error, BusinessError) else 'BROWSER_ACTION_FAILED'
            await self._fence(token, action.step_id, code)
            if not isinstance(error, BusinessError):
                raise BusinessError('SERVICE_UNAVAILABLE', 'Browser action did not produce a confirmed result', status=503) from None
            raise

    async def dispatch(self, token, action):
        async with self._gate:
            return await self._dispatch(token, action)

    async def navigate(self, token, url, step_id):
        """Count initial navigation through the same intent and action gateway."""
        async with self._gate:
            snapshot = await self._observe(token)
            action = {'run_id': token.run_id, 'step_id': step_id, 'epoch': token.epoch,
                'snapshot_id': snapshot['snapshot_id'], 'action_type': 'navigate', 'expected_effect': 'read',
                'target': {'page_url': url if snapshot['source_url'] == 'about:blank' else snapshot['source_url'],
                           'tab_id': snapshot['tab_id'], 'frame_id': snapshot['frame_id'], 'locator': None, 'write_scope': None},
                'args': {'url': url}}
            return await self._dispatch(token, action)

    async def sequence(self, token, builders):
        """Trusted composite helpers still dispatch and charge each atomic step."""
        if not isinstance(builders, (tuple, list)) or not 1 <= len(builders) <= 150 or not all(callable(b) for b in builders):
            raise BusinessError('INVALID_PARAMETER', 'A bounded sequence of trusted builders is required')
        async with self._gate:
            results = []
            for builder in builders:
                snapshot = await self._observe(token)
                results.append(await self._dispatch(token, builder(snapshot)))
                if results[-1]['status'] != 'COMPLETED':
                    break
            return results
