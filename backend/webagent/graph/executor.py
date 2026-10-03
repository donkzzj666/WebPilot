"""Trusted Worker composition for frozen, read-only browser task Runs.

Only this factory owns live clients. Nothing here is serialized into graph
state, and initial navigation belongs to the action gateway inside the graph.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
from uuid import uuid4

from pydantic import ValidationError

from ..config import Settings
from ..db import connect, transaction
from ..errors import BusinessError
from ..events import WaitingEvent, append_event
from ..gateway.service import BrowserGateway
from ..identities.store import IdentityStore
from ..models.adapter import ModelAdapter
from ..scheduler.models import ExecutionToken, Resource
from ..scheduler.store import SchedulerStore, validate_in_transaction
from ..sessions.models import SessionOwner
from ..settings.secrets import MacOSKeychainSecretStore
from ..settings.service import provider_for_run
from ..state import TERMINAL_STATES
from ..tasks.models import TaskContract
from ..verification.service import VerificationService


async def _resolved(value):
    return await value if inspect.isawaitable(value) else value


class GraphExecutor:
    """QueueWorker callable with trusted injection seams, never caller tools.

    Factories are local application dependencies. A model/API payload cannot
    supply them. The default provider resolves the Run's immutable snapshot,
    including its original credential reference, rather than current settings.
    """

    def __init__(self, settings: Settings, manager, *, checkpointer, scheduler=None,
                 secret_store=None, provider_factory=None, model_adapter_factory=None,
                 gateway_factory=None, verifier_factory=None, source_adapter_factory=None,
                 identity_preparer=None, recovery_preparer=None, recovery_store=None,
                 graph_factory=None, graph_store=None, write_protocol_enabled=False, diagnostics=None):
        self.settings, self.manager, self.checkpointer = settings, manager, checkpointer
        self.scheduler = scheduler or SchedulerStore(settings.business_db)
        self.secret_store = secret_store if secret_store is not None else MacOSKeychainSecretStore()
        if type(write_protocol_enabled) is not bool or (write_protocol_enabled and gateway_factory is None):
            raise ValueError('Write protocol requires an explicitly installed trusted gateway factory')
        self.write_protocol_enabled = write_protocol_enabled
        from ..observability.logging import TrustedGraphDiagnostics
        if diagnostics is not None and not isinstance(diagnostics, TrustedGraphDiagnostics):
            raise ValueError('Graph diagnostics must use the trusted local adapter')
        self.diagnostics = diagnostics
        for factory in (provider_factory, model_adapter_factory, gateway_factory, verifier_factory,
                        source_adapter_factory, identity_preparer, recovery_preparer, graph_factory):
            if factory is not None and not callable(factory):
                raise ValueError('Executor factories must be trusted callables')
        self.provider_factory = provider_factory
        self.model_adapter_factory = model_adapter_factory or ModelAdapter
        self.gateway_factory = gateway_factory or BrowserGateway.from_managed
        self.verifier_factory = verifier_factory or VerificationService
        self.source_adapter_factory = source_adapter_factory
        self.identity_preparer = identity_preparer
        self.recovery_preparer = recovery_preparer
        if recovery_store is None:
            from .recovery import RecoveryStore
            recovery_store = RecoveryStore(settings.business_db)
        self.recovery_store = recovery_store
        self.graph_factory = graph_factory
        self.graph_store = graph_store
        from ..controls.store import ControlStore
        self.controls = ControlStore(settings.business_db, secret_store=self.secret_store,
                                     scheduler=self.scheduler)

    def __getstate__(self):
        raise TypeError('Live execution clients cannot be serialized')

    def _context(self, token):
        if not isinstance(token, ExecutionToken):
            raise BusinessError('RESOURCE_CONFLICT', 'Current execution qualification is required', status=409)
        with connect(self.settings.business_db) as db:
            validate_in_transaction(db, token, allow_reconciling=True)
            row = db.execute('''SELECT r.*,c.content_json FROM runs r JOIN contracts c
                ON c.task_id=r.task_id AND c.contract_version=r.contract_version WHERE r.run_id=?''',
                (token.run_id,)).fetchone()
            if hashlib.sha256(row['content_json'].encode()).hexdigest() != row['contract_sha256']:
                raise BusinessError('STATE_CONFLICT', 'Frozen contract integrity mismatch', status=409,
                                    field='contract_mismatch')
            try:
                contract = TaskContract.model_validate_json(row['content_json'])
            except ValidationError:
                raise BusinessError('STATE_CONFLICT', 'Execution requires a complete frozen contract', status=409,
                                    field='contract_mismatch') from None
        return contract

    @staticmethod
    def _owner(contract, token):
        realms = [realm for realm in ('public', 'webarena') if all(
            Resource.site_identity(source.site_id, contract.identity_ref, realm=realm).resource_key in token.resources
            for source in contract.sources)]
        if len(realms) != 1 or Resource.browser_context(token.run_id).resource_key not in token.resources:
            raise BusinessError('RESOURCE_CONFLICT', 'Frozen sources require complete scoped browser leases', status=409)
        first = next((source for source in contract.sources if source.permits(contract.start_urls[0])), None)
        if first is None:
            raise BusinessError('FORBIDDEN', 'Start URL is outside the frozen source scope', status=403)
        return SessionOwner('run', token.run_id, first.site_id, contract.identity_ref, realms[0])

    def _identity(self, contract, owner, *, recovering=False):
        if contract.identity_ref is None:
            return None
        identity = IdentityStore(self.settings.business_db).get_identity(contract.identity_ref)
        if (identity.state != 'VERIFIED' or identity.site_id != owner.site_id or identity.realm != owner.realm
                or any(source.origin.rstrip('/') != identity.origin.rstrip('/') for source in contract.sources)):
            raise BusinessError('IDENTITY_RECHECK_REQUIRED', 'Run identity requires scoped verification', status=409)
        if not recovering and self.identity_preparer is None:
            raise BusinessError('IDENTITY_RECHECK_REQUIRED', 'A trusted gateway identity recheck is required', status=409)
        return identity

    async def _session(self, owner, identity, token, *, recovering=False):
        owned = self.manager.registry.list_owned(self.manager.manager_id)
        live = [session for session in owned if session.owner.kind == 'run'
                and session.owner.owner_id == owner.owner_id and session.state in ('OPENING', 'OPEN', 'CLOSING')]
        if live:
            if len(live) != 1 or live[0].state != 'OPEN' or live[0].owner != owner:
                raise BusinessError('STATE_CONFLICT', 'Run has no reusable scoped browser session', status=409)
            if not recovering and identity is not None and live[0].auth_ref != identity.auth_ref:
                raise BusinessError('IDENTITY_RECHECK_REQUIRED', 'Run session restored an older identity snapshot', status=409)
            return live[0]
        # A new manager process must be able to link the exact old scoped
        # session, while an old process's OPEN entry is never silently stolen.
        with connect(self.settings.business_db) as db:
            rows = db.execute('''SELECT s.*,a.sha256 AS auth_sha256 FROM browser_sessions s
                LEFT JOIN browser_auth_snapshots a ON a.auth_ref=s.auth_ref
                WHERE s.owner_kind=? AND s.owner_id=? AND s.site_id=? AND s.identity_ref IS ?
                AND s.realm=? AND s.state IN ('CLOSED','LOST') ORDER BY s.created_at,s.session_id''',
                (owner.kind, owner.owner_id, owner.site_id, owner.identity_ref, owner.realm)).fetchall()
            prior = [self.manager.registry._info(row) for row in rows]
        return await self.manager.create(owner, auth_ref=identity.auth_ref if identity else None,
            replaces=prior[-1].session_id if prior else None, execution_token=token, gateway_downloads=True)

    def _pause(self, token, diagnostic):
        """Persist a no-action prerequisite wait without crashing the Worker."""
        wait_id = 'graph-prerequisite-' + uuid4().hex
        now, _ = self.scheduler._time()
        with connect(self.settings.business_db) as db, transaction(db):
            from ..controls.models import ControlPending
            pending = self.controls.pending_in_transaction(db, token.run_id)
            if pending is not None:
                raise ControlPending(pending)
            row = validate_in_transaction(db, token, allow_reconciling=True)
            # Recovery is not a completed identity/business reconciliation.
            # Keep its qualification fenced for M1-17 instead of inventing a
            # RUNNING transition simply to reach the PAUSED matrix edge.
            if row['state'] != 'RUNNING':
                raise BusinessError('RESOURCE_CONFLICT', 'Recovery prerequisites remain unresolved',
                                    status=409, field=diagnostic,
                                    current_state_version=token.state_version)
            self.scheduler._defer_in_transaction(db, token, 'PAUSED', now, now)
            append_event(db, run_id=token.run_id, expected_state_version=token.state_version + 1,
                         payload=WaitingEvent(wait_id=wait_id, reason='pause', deadline=None))
        if self.graph_store is None:
            from .store import GraphStore
            store = GraphStore(self.settings.business_db)
        else:
            store = self.graph_store
        return store.record_wait_progress(token.run_id, wait_id,
            expected_state_version=token.state_version + 1, diagnostic=diagnostic)

    async def _control_boundary(self, token):
        fresh = await asyncio.to_thread(self.scheduler.refresh_qualification, token)
        operation = await asyncio.to_thread(self.controls.apply_at_boundary, fresh)
        if operation is None or operation['status'] != 'APPLIED':
            return None
        from .runtime import StateGraphAdapter
        return await StateGraphAdapter(self.settings.data_dir, None, None, None,
            checkpointer=self.checkpointer, diagnostics=self.diagnostics).settle_control(operation)

    async def settle_control(self, operation):
        """Idle Worker cleanup never acquires a graph execution token."""
        from .runtime import StateGraphAdapter
        state = await StateGraphAdapter(self.settings.data_dir, None, None, None,
            checkpointer=self.checkpointer, diagnostics=self.diagnostics).settle_control(operation)
        if operation['action'] == 'cancel':
            closer = getattr(self.manager, 'close_terminal', None)
            if closer is not None:
                sessions = await asyncio.to_thread(self.manager.registry.list_owned, self.manager.manager_id)
                for session in sessions:
                    if (session.owner.kind == 'run' and session.owner.owner_id == operation['run_id']
                            and session.state in ('OPENING', 'OPEN', 'CLOSING')):
                        try:
                            await closer(session.session_id, session.owner)
                        except BusinessError as error:
                            if error.status != 409:
                                raise
            # After a restart, start() marks the old generation LOST. There
            # is no current context to close, but a terminal materialized
            # reservation still needs safe release. Human holds and unknown
            # write quarantine remain authoritative; this creates no token.
            with connect(self.settings.business_db) as db, transaction(db):
                run = db.execute('SELECT state FROM runs WHERE run_id=?', (operation['run_id'],)).fetchone()
                human = db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'",
                                   (operation['run_id'],)).fetchone()
                if run is not None and run['state'] == 'CANCELLED' and human is None:
                    self.scheduler._release_safe(db, operation['run_id'], preserve_context=False,
                                                 preserve_logical=False)
        return state

    async def __call__(self, token):
        provider, session, gateway, graph_started, recovering = None, None, None, False, False
        prerequisite_paused = False
        def pause(diagnostic):
            nonlocal prerequisite_paused
            result = self._pause(token, diagnostic)
            prerequisite_paused = True
            return result
        try:
            if isinstance(token, ExecutionToken):
                with connect(self.settings.business_db) as db:
                    row = db.execute('SELECT state FROM runs WHERE run_id=?', (token.run_id,)).fetchone()
                    recovering = row is not None and row['state'] == 'RECONCILING'
            controlled = await self._control_boundary(token)
            if controlled is not None:
                return controlled
            contract = self._context(token)
            plan = None
            if recovering:
                if self.write_protocol_enabled:
                    # Query the old effects before ordinary graph recovery.
                    # This private surface remains GET-only even though its
                    # Run holds quarantined write resources.
                    owner = self._owner(contract, token)
                    identity = self._identity(contract, owner, recovering=True)
                    session = await self._session(owner, identity, token, recovering=True)
                    gateway = await _resolved(self.gateway_factory(self.manager, session, scheduler=self.scheduler))
                    if getattr(gateway, 'supports_write_protocol', False) is not True:
                        raise BusinessError('STATE_CONFLICT', 'Trusted write checking is unavailable',
                                            status=409, field='unknown_write')
                    with connect(self.settings.business_db) as db:
                        operations = [row[0] for row in db.execute(
                            'SELECT operation_id FROM write_intents WHERE task_id=? ORDER BY operation_id',
                            (contract.task_id,))]
                    for operation_id in operations:
                        checked = await gateway.reconcile_write(token, operation_id)
                        if checked['status'] in ('INTENT','UNKNOWN') or checked.get('verified_current') is False:
                            raise BusinessError('STATE_CONFLICT', 'Write result remains unknown',
                                                status=409, field='unknown_write')
                from .recovery_state import load_saved_graph
                saved = await load_saved_graph(self.checkpointer, token.run_id)
                plan = await asyncio.to_thread(self.recovery_store.begin, token, saved)
                if not plan['allowed']:
                    raise BusinessError('STATE_CONFLICT', 'Recovery prerequisites are blocked', status=409,
                                        field=plan['reason'])
            if contract.action_policy.mode != 'read_only' and not self.write_protocol_enabled:
                if recovering:
                    raise BusinessError('STATE_CONFLICT', 'Recovery requires a trusted read-only adapter',
                                        status=409, field='unknown_write')
                return pause('write_adapter_unavailable')
            owner = self._owner(contract, token)
            try:
                identity = self._identity(contract, owner, recovering=recovering)
            except BusinessError as error:
                if not recovering and error.code in ('IDENTITY_RECHECK_REQUIRED', 'NOT_FOUND'):
                    return pause('identity_recheck_required')
                raise
            if recovering:
                self._context(token)
                if session is None:
                    session = await self._session(owner, identity, token, recovering=True)
                    gateway = await _resolved(self.gateway_factory(self.manager, session, scheduler=self.scheduler))
                controlled = await self._control_boundary(token)
                if controlled is not None:
                    return controlled
                self._context(token)
                if self.recovery_preparer is None:
                    snapshot = await gateway.recovery_observe(token, plan['recovery_id'])
                    if snapshot['source_url'] == 'about:blank':
                        await gateway.recovery_navigate(token, plan['restore_url'], plan['recovery_id'])
                        snapshot = await gateway.recovery_observe(token, plan['recovery_id'])
                    snapshot_id = snapshot['snapshot_id']
                    facts = await asyncio.to_thread(self.recovery_store.observed_facts, token, snapshot_id)
                else:
                    proof = await _resolved(self.recovery_preparer(gateway, token, contract, identity, plan))
                    if type(proof) is not dict or type(proof.get('snapshot_id')) is not str:
                        raise BusinessError('STATE_CONFLICT', 'Recovery requires persisted observation proof',
                                            status=409, field='proof_missing')
                    snapshot_id = proof['snapshot_id']
                    facts = {key: proof[key] for key in ('object_id', 'object_version', 'identity_ref',
                        'normalized_account', 'proof_evidence_ids', 'proof_bindings') if key in proof}
                    if 'object_id' not in facts:
                        raise BusinessError('STATE_CONFLICT', 'Recovery object proof is missing', status=409,
                                            field='proof_missing')
                controlled = await self._control_boundary(token)
                if controlled is not None:
                    return controlled
                self._context(token)
                await asyncio.to_thread(self.recovery_store.complete, token, snapshot_id, **facts)
                self._context(token)
                token = await asyncio.to_thread(self.scheduler.reconcile, token.run_id,
                                                 token.state_version)
                self._context(token)
            try:
                if self.provider_factory is None:
                    provider = await asyncio.to_thread(provider_for_run, self.settings.business_db,
                                                       self.secret_store, token.run_id)
                else:
                    provider = await _resolved(self.provider_factory(token.run_id))
            except BusinessError as error:
                if error.code in ('CONFIG_NOT_READY', 'CONFIG_SNAPSHOT_MISSING', 'CONFIG_SNAPSHOT_INVALID'):
                    return pause('configuration_required')
                raise
            # A credential lookup or injected factory may await while control
            # changes. Do not allocate a browser from the original stale token.
            controlled = await self._control_boundary(token)
            if controlled is not None:
                return controlled
            self._context(token)
            if session is None:
                session = await self._session(owner, identity, token)
                gateway = await _resolved(self.gateway_factory(self.manager, session, scheduler=self.scheduler))
            if contract.action_policy.mode != 'read_only' and getattr(gateway, 'supports_write_protocol', False) is not True:
                return pause('write_adapter_unavailable')
            controlled = await self._control_boundary(token)
            if controlled is not None:
                return controlled
            if identity is not None and not recovering:
                verified = await _resolved(self.identity_preparer(gateway, token, contract, identity))
                controlled = await self._control_boundary(token)
                if controlled is not None:
                    return controlled
                if verified is not True:
                    return pause('identity_recheck_required')
                self._context(token)
            model = await _resolved(self.model_adapter_factory(self.settings.business_db, provider))
            verifier = await _resolved(self.verifier_factory(self.settings.data_dir, scheduler=self.scheduler))
            source = None if self.source_adapter_factory is None else await _resolved(
                self.source_adapter_factory(contract, gateway))
            factory = self.graph_factory
            if factory is None:
                from .runtime import StateGraphAdapter
                factory = StateGraphAdapter
            graph_options = dict(source_adapter=source, checkpointer=self.checkpointer)
            if self.diagnostics is not None:
                graph_options['diagnostics'] = self.diagnostics
            graph = await _resolved(factory(self.settings.data_dir, gateway, model, verifier, **graph_options))
            graph_started = True
            return await graph.run(token)
        except BusinessError as error:
            pending = (await asyncio.to_thread(self.controls.pending, token.run_id)
                       if isinstance(token, ExecutionToken) else None)
            if error.code == 'CONTROL_PENDING' or pending is not None:
                controlled = await self._control_boundary(token)
                if controlled is not None:
                    return controlled
            await self._diagnose_error(token, error)
            if graph_started:
                raise
            if recovering:
                from .recovery import REASONS
                reason = error.field if error.field in REASONS else {
                    'IDENTITY_RECHECK_REQUIRED': 'identity_mismatch',
                    'NOT_FOUND': 'identity_mismatch', 'SERVICE_UNAVAILABLE': 'session_unavailable',
                    'FORBIDDEN': 'source_scope_mismatch', 'BUDGET_EXCEEDED': 'budget_exhausted',
                }.get(error.code, 'recovery_not_completed')
                if error.code == 'RESOURCE_CONFLICT':
                    with connect(self.settings.business_db) as db:
                        row = db.execute('''SELECT q.*,r.state,r.state_version FROM scheduler_queue q
                            JOIN runs r USING(run_id) WHERE q.run_id=?''', (token.run_id,)).fetchone()
                        human = db.execute("SELECT 1 FROM resource_leases WHERE holder_run_id=? AND control_owner='human'",
                                           (token.run_id,)).fetchone()
                        if (row is not None and row['state'] == 'RECONCILING' and row['status'] == 'ACTIVE'
                                and row['worker_id'] == token.worker_id
                                and row['worker_generation'] == token.worker_generation
                                and row['epoch'] == token.epoch and row['state_version'] == token.state_version
                                and human is not None):
                            reason = 'human_control'
                # If an await lost current authority, blocked() itself rejects
                # the stale token. It cannot overwrite a new owner's facts.
                await asyncio.to_thread(self.recovery_store.blocked, token, reason)
                await asyncio.to_thread(self.scheduler.abandon, token)
                return {'recovery_blocked': True, 'reason': reason, 'run_id': token.run_id}
            if error.code == 'IDENTITY_RECHECK_REQUIRED':
                return pause('identity_recheck_required')
            if error.code == 'SERVICE_UNAVAILABLE':
                return pause('graph_preparation_failed')
            raise
        except Exception as error:
            await self._diagnose_error(token, error)
            raise
        finally:
            try:
                try:
                    # Detach per-invocation observers/CDP sessions even when a
                    # durable wait deliberately retains the managed context.
                    browser = getattr(gateway, 'browser', None)
                    closer = getattr(browser, 'aclose', None)
                    if closer is not None:
                        await _resolved(closer())
                finally:
                    if provider is not None:
                        closer = getattr(provider, 'aclose', None)
                        if closer is not None:
                            await _resolved(closer())
            finally:
                if session is not None:
                    with connect(self.settings.business_db) as db:
                        state = db.execute('SELECT state FROM runs WHERE run_id=?', (token.run_id,)).fetchone()[0]
                    if state in TERMINAL_STATES:
                        closer = getattr(self.manager, 'close_terminal', None)
                        if closer is not None:
                            try:
                                await closer(session.session_id, session.owner)
                            except BusinessError as error:
                                if error.status != 409:
                                    raise
                    elif not graph_started and not recovering:
                        closer = getattr(self.manager, 'close_preparation', None)
                        if closer is not None:
                            try:
                                await closer(session.session_id, session.owner, execution_token=token,
                                             paused_settlement=prerequisite_paused)
                            except BusinessError as error:
                                if error.status != 409:
                                    raise

    async def _diagnose_error(self, token, error):
        if self.diagnostics is None or not isinstance(token, ExecutionToken):
            return
        try:
            async with asyncio.timeout(.25):
                await asyncio.to_thread(self.diagnostics.executor_error, token.run_id, error)
        except Exception:
            pass
