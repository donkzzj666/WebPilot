"""Application-owned async StateGraph; business ledgers remain authoritative."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import json
from pathlib import Path
from uuid import uuid4

from ..config import disable_external_tracing
disable_external_tracing()

from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from langgraph.errors import GraphBubbleUp

from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..events import WaitingEvent, append_event
from ..models.adapter import ModelError
from ..models.schema import ModelAction, RequestEvidence, RequestInput, ProposeResult
from ..scheduler.models import ExecutionToken
from ..scheduler.store import SchedulerStore, validate_in_transaction
from ..state import TERMINAL_STATES, transition_in_transaction
from .context import ModelContextBuilder
from .models import GraphState, EphemeralOutput, validate_graph_state, GRAPH_VERSION, STATE_SCHEMA_VERSION
from .source import StructuredJSONSourceAdapter
from .store import GraphStore

# QueueWorker gives one owner to a Run. This additional process-local gate
# rejects accidental simultaneous invokes by the same trusted owner.
_INVOCATIONS: dict[tuple[str, str], asyncio.Lock] = {}


class _ControlApplied(Exception):
    def __init__(self, operation):
        self.operation = operation


@dataclass
class ExecutionContext:
    token: ExecutionToken | None
    resuming: bool = False
    terminal_repair: bool = False
    output: EphemeralOutput = field(default_factory=EphemeralOutput)
    verification: dict | None = None
    requested_fields: tuple[str, ...] | None = None
    control_operation: dict | None = None
    control_announced: bool = False

    def __getstate__(self):
        raise TypeError('Execution authority is not graph state')


class StateGraphAdapter:
    def __init__(self, data_dir, gateway, model_adapter, verifier, *, source_adapter=None,
                 checkpointer=None, fault_hook=None, diagnostics=None):
        disable_external_tracing()
        if checkpointer is None:
            raise ValueError('A durable async checkpointer is required')
        self.database = Path(data_dir) / 'business.sqlite3'
        self.checkpointer = checkpointer
        self.gateway, self.model, self.verifier = gateway, model_adapter, verifier
        if fault_hook is not None and not callable(fault_hook):
            raise ValueError('Fault hooks must be trusted local callables')
        self.fault_hook = fault_hook
        from ..observability.logging import TrustedGraphDiagnostics
        if diagnostics is not None and not isinstance(diagnostics, TrustedGraphDiagnostics):
            raise ValueError('Graph diagnostics must use the trusted local adapter')
        self.diagnostics = diagnostics
        self.scheduler = getattr(verifier, 'scheduler', None) or SchedulerStore(self.database)
        self.store = GraphStore(self.database)
        from ..controls.store import ControlStore
        self.controls = ControlStore(self.database, scheduler=self.scheduler)
        self.context_builder = ModelContextBuilder(data_dir, store=self.store)
        self.source = source_adapter or StructuredJSONSourceAdapter(data_dir, verifier=verifier)
        graph = StateGraph(GraphState, context_schema=ExecutionContext)
        for name in ('reconcile', 'observe', 'decide', 'dispatch', 'confirm', 'verify',
                     'aggregate', 'recover', 'prepare_wait', 'wait', 'stopped'):
            graph.add_node(name, self._node(getattr(self, '_' + name), name))  # No automatic RetryPolicy.
        graph.add_edge(START, 'reconcile')
        routes = {name: name for name in ('reconcile', 'observe', 'decide', 'dispatch',
                  'confirm', 'verify', 'aggregate', 'recover', 'stopped')}
        routes.update(wait='prepare_wait', end=END)
        for name in routes:
            if name not in ('wait', 'end'):
                graph.add_conditional_edges(name, lambda state: state['route'], routes)
        graph.add_edge('prepare_wait', 'wait')
        graph.add_edge('wait', 'reconcile')
        self.graph = graph.compile(checkpointer=checkpointer, name=GRAPH_VERSION)

    async def _hook(self, stage, run_id):
        # Developer-owned crash probes only. No API, environment, graph state
        # or model output can install a hook or choose a fault boundary.
        if self.fault_hook is not None:
            value = self.fault_hook(stage, run_id)
            if inspect.isawaitable(value):
                await value

    async def _read_state(self, config):
        from .recovery_state import load_saved_graph
        refs = await load_saved_graph(self.checkpointer, config['configurable']['thread_id'])
        if refs:
            from .recovery import RecoveryStore
            plan = await asyncio.to_thread(RecoveryStore(self.database).inspect,
                config['configurable']['thread_id'], refs)
            if not plan['allowed']:
                raise BusinessError('STATE_CONFLICT', 'Saved business references are unavailable',
                                    status=409, field=plan['reason'])
        try:
            saved = await self.graph.aget_state(config)
            # The first sync save may contain only __start__. Its input still
            # has business refs to validate and must not look like no graph.
            return saved._replace(values=refs) if refs and not saved.values else saved
        except Exception:
            raise BusinessError('STATE_CONFLICT', 'Saved graph cannot be reconstructed',
                                status=409, field='graph_state_invalid') from None

    def _node(self, method, name):
        async def invoke(state, runtime: Runtime[ExecutionContext]):
            try:
                result = await method(state, runtime)
            except _ControlApplied as applied:
                runtime.context.control_operation = applied.operation
                result = await self._control_state(runtime.context)
            except BusinessError as error:
                # Accepting a request appends an event at the same business
                # version. A concurrent reference read can fail closed before
                # the explicit admission barrier sees that request.
                pending = await asyncio.to_thread(self.controls.pending, state['run_id'])
                if error.code != 'CONTROL_PENDING' and pending is None:
                    raise
                try:
                    await self._control_boundary(runtime.context)
                except _ControlApplied as applied:
                    runtime.context.control_operation = applied.operation
                    result = await self._control_state(runtime.context)
                else:
                    raise error
            await self._hook(name + '_return_before', state['run_id'])
            # The corresponding after-return window is the saver aput entry.
            # A caller-owned saver wrapper observes that actual framework edge.
            return result
        async def diagnosed(state, runtime):
            owner = runtime.context.token
            run_id = owner.run_id if owner is not None else state['run_id']
            # Sync durability has committed the previous superstep before
            # this node starts. Preserve that real reference before a later
            # budget watchdog can cancel the invocation's final diagnostics.
            await self._diagnose('checkpoint', run_id)
            await self._diagnose('node', run_id, name, 'started')
            try:
                result = await invoke(state, runtime)
            except (GraphBubbleUp, asyncio.CancelledError):
                await self._diagnose('node', run_id, name, 'interrupted')
                raise
            except Exception as error:
                await self._diagnose('node', run_id, name, 'failed', error=error)
                raise
            await self._diagnose('node', run_id, name, 'completed')
            return result
        return diagnosed

    async def _diagnose(self, method, *args, **kwargs):
        if self.diagnostics is None:
            return
        try:
            async with asyncio.timeout(.25):
                if method == 'checkpoint':
                    await self.diagnostics.checkpoint(*args, checkpointer=self.checkpointer)
                else:
                    await asyncio.to_thread(self.diagnostics.node, *args, **kwargs)
        except Exception:
            # Local diagnostics cannot replace a business result or exception.
            pass

    async def _control_boundary(self, context):
        # Requests do not revoke an accepted action. This runs only before a
        # node starts or after its awaited operation has produced its receipt.
        context.token = await asyncio.to_thread(self.scheduler.refresh_qualification, context.token)
        operation = await asyncio.to_thread(self.controls.apply_at_boundary, context.token)
        if operation is not None and operation['status'] == 'APPLIED':
            raise _ControlApplied(operation)

    async def _control_state(self, context):
        context.output.clear()
        context.verification = None
        context.requested_fields = None
        if not context.control_announced:
            context.control_announced = True
            await self._hook('control_applied', context.control_operation['run_id'])
        state = await asyncio.to_thread(self.store.load_control_state, context.control_operation)
        if state['completed']:
            return validate_graph_state({**state, 'route': 'stopped'})
        if state['wait_id'] is None:
            raise BusinessError('STATE_CONFLICT', 'Control pause has no durable wait', status=409)
        return validate_graph_state({**state, 'route': 'wait'})

    async def _guard(self, context):
        await self._control_boundary(context)
        token, budget = await asyncio.to_thread(self.scheduler.runtime_budget, context.token)
        context.token = token
        if budget['exhausted']:
            raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409, field=budget['reason'])
        run = await asyncio.to_thread(self.store.load_run, token.run_id)
        if run['state'] != 'RUNNING' and run['state'] != 'VERIFYING':
            raise BusinessError('STATE_CONFLICT', 'Reconciliation must complete before graph execution', status=409)
        if (run['graph_version'], run['graph_state_schema_version']) != (GRAPH_VERSION, STATE_SCHEMA_VERSION):
            raise BusinessError('STATE_CONFLICT', 'Unsupported graph version', status=409)
        return run

    async def _progress(self, context, phase, *, route, diagnostic=None, **refs):
        from .recovery import RecoveryStore
        await asyncio.to_thread(RecoveryStore(self.database).checkpoint_facts,
            context.token.run_id, context.token, snapshot_id=refs.get('snapshot_id'))
        state = await asyncio.to_thread(self.store.record_progress, context.token.run_id, phase,
            expected_state_version=context.token.state_version, execution_token=context.token,
            diagnostic=diagnostic, **refs)
        return validate_graph_state({**state, 'route': route, 'diagnostic': diagnostic})

    async def _reconcile(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        context.output.clear()
        context.verification = None
        if context.control_operation is not None:
            return await self._control_state(context)
        if context.terminal_repair:
            from .recovery import RecoveryStore
            plan = await asyncio.to_thread(RecoveryStore(self.database).inspect, state['run_id'], None)
            if not plan['allowed'] or not plan['terminal']:
                raise BusinessError('STATE_CONFLICT', 'Terminal business facts are unavailable', status=409)
            return {**self.store.load_state(state['run_id']), 'route': 'end', 'completed': True}
        await self._guard(context)
        # Scope, managed reservation, epoch and identity binding are checked
        # through the gateway before any initial navigation or observation.
        await asyncio.to_thread(self.gateway._qualified, context.token)
        return await self._progress(context, 'reconcile', route='observe')

    async def _observe(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        run = await self._guard(context)
        snapshot = await self.gateway.observe(context.token)
        if snapshot['source_url'] == 'about:blank':
            await self._navigation_ready(context, run['contract'].start_urls[0], run['contract'])
            await self.gateway.navigate(context.token, run['contract'].start_urls[0],
                'graph-start-' + str(context.token.epoch) + '-' + uuid4().hex)
            snapshot = await self.gateway.observe(context.token)
        await self._guard(context)
        await asyncio.to_thread(self.store.checkpoint_observation, context.token.run_id, snapshot['snapshot_id'],
            expected_state_version=context.token.state_version, execution_token=context.token)
        return await self._progress(context, 'observe', route='decide', snapshot_id=snapshot['snapshot_id'])

    async def _navigation_ready(self, context, url, contract):
        source = next(s for s in contract.sources if s.permits(url))
        def paced():
            site = self.scheduler.budgets._site(context.token, source.site_id)
            with connect(self.database) as db:
                row = db.execute('SELECT * FROM site_pacing WHERE site_id=?', (site,)).fetchone()
            return row is not None and self.scheduler.budgets._paced(
                row['last_utc'], row['last_mono_ns'], row['clock_domain'],
                self.scheduler.budgets._stamp(),
                max(row['interval_seconds'], contract.budget_profile.min_site_interval_seconds))
        while await asyncio.to_thread(paced):
            await self._guard(context)
            await asyncio.sleep(.1)
        await self._guard(context)

    async def _decide(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        await self._guard(context)
        model_input = await asyncio.to_thread(self.context_builder.build, context.token.run_id,
            state['snapshot_id'], execution_token=context.token, expected_state_version=context.token.state_version)
        try:
            await self._hook('model_before', context.token.run_id)
            generated = await self.model.generate(model_input, execution_token=context.token)
            await self._hook('model_after', context.token.run_id)
        except ModelError as error:
            await self._guard(context)
            diagnostic = 'invalid_model_output' if error.error_class == 'invalid_output' else 'model_failed'
            return await self._progress(context, 'decide', route='recover', diagnostic=diagnostic,
                                        iteration=state['iteration'] + 1)
        await self._guard(context)
        output = generated.output
        if isinstance(output, RequestEvidence):
            route, diagnostic = 'recover', 'evidence_required'
        elif isinstance(output, RequestInput):
            if len(output.requested_fields) > 64 or any(len(name) > 256 for name in output.requested_fields):
                route, diagnostic = 'recover', 'invalid_model_output'
            else:
                context.requested_fields = tuple(dict.fromkeys(output.requested_fields))
                route, diagnostic = 'wait', 'input_required'
        elif isinstance(output, ModelAction):
            context.output.put(output)
            route, diagnostic = 'dispatch', None
        elif isinstance(output, ProposeResult):
            context.output.put(output)
            route, diagnostic = 'verify', None
        else:
            raise BusinessError('INVALID_PARAMETER', 'Unsupported validated model output')
        return await self._progress(context, 'decide', route=route, diagnostic=diagnostic,
                                    iteration=state['iteration'] + 1)

    async def _dispatch(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        run = await self._guard(context)
        output = context.output.take()
        if not isinstance(output, ModelAction):
            raise BusinessError('STATE_CONFLICT', 'Missing current process action', status=409)
        if output.action.action_type == 'navigate':
            # Waiting spends active time and the independent Worker deadline
            # still fences this coroutine. The gateway also checks pacing.
            await self._navigation_ready(context, output.action.args.url, run['contract'])
        try:
            await self._hook('action_before', context.token.run_id)
            result = await self.gateway.dispatch(context.token, output.action)
            await self._hook('action_after', context.token.run_id)
        except BusinessError as error:
            if error.code == 'STATE_CONFLICT':
                return await self._progress(context, 'dispatch', route='recover', diagnostic='page_changed')
            raise
        if output.action.expected_effect == 'write':
            await self._control_boundary(context)
            if getattr(self.gateway, 'supports_write_protocol', False) is not True:
                raise BusinessError('STATE_CONFLICT', 'Write result requires a trusted query',
                                    status=409, field='unknown_write')
            try:
                checked = await self.gateway.reconcile_write(context.token, result['operation_id'])
            except BusinessError as error:
                # A completed read query that cannot prove the result becomes
                # a durable wait. Expiry, cancellation and lost authority keep
                # their independent fencing path and must not be swallowed.
                if error.code not in ('STATE_CONFLICT','FORBIDDEN','INVALID_PARAMETER','NOT_FOUND',
                                      'INPUT_BLOCKED','SERVICE_UNAVAILABLE','EVIDENCE_MISSING','EVIDENCE_CORRUPT'):
                    raise
                await self._guard(context)
                return await self._progress(context, 'dispatch', route='wait', diagnostic='recovery_required')
            if checked['status'] in ('INTENT','UNKNOWN') or checked.get('verified_current') is False:
                return await self._progress(context, 'dispatch', route='wait', diagnostic='recovery_required')
            if checked['status'] == 'NOT_APPLIED':
                # No click is repeated here. A new decision must pass the
                # gateway's single-use absence proof and new attempt admission.
                return await self._progress(context, 'dispatch', route='confirm')
            result = {**result, 'status': 'COMPLETED'}
        await self._guard(context)
        if result['status'] != 'COMPLETED':
            return await self._progress(context, 'dispatch', route='wait', diagnostic='recovery_required')
        return await self._progress(context, 'dispatch', route='confirm')

    async def _confirm(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        await self._guard(runtime.context)
        # COMPLETED means the atomic gateway action has a receipt, not that
        # the business contract has passed. Always obtain a fresh observation.
        return await self._progress(runtime.context, 'confirm', route='observe')

    async def _verify(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        await self._guard(context)
        proposal = context.output.take()
        if not isinstance(proposal, ProposeResult):
            raise BusinessError('STATE_CONFLICT', 'Missing current process proposal', status=409)
        context.token = await asyncio.to_thread(self.verifier.begin, context.token.run_id,
            context.token.state_version, context.token)
        bindings = await asyncio.to_thread(self.source.bindings, context.token.run_id, proposal,
                                           execution_token=context.token)
        context.verification = await self.verifier.verify(context.token.run_id, proposal, bindings,
            expected_state_version=context.token.state_version, execution_token=context.token,
            provider=self.model.provider)
        await self._guard(context)
        record = context.verification
        passed = (all(c['verdict'] == 'PASS' for c in record['checks'])
                  and bool(record['evaluation']['fields'])
                  and all(f['verdict'] == 'PASS' for f in record['evaluation']['fields'])
                  and not record['evaluation']['violations'] and not proposal.unresolved)
        return await self._progress(context, 'verify', route='aggregate' if passed else 'recover',
            verification_id=record['verification_id'],
            diagnostic=None if passed else 'verification_incomplete')

    async def _aggregate(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        await self._guard(context)
        if context.verification is None:
            raise BusinessError('STATE_CONFLICT', 'Verification must be reconstructed after recovery', status=409)
        await self._hook('business_before', context.token.run_id)
        await asyncio.to_thread(self.verifier.finalize, context.verification['verification_id'],
            expected_state_version=context.token.state_version, execution_token=context.token)
        await self._hook('business_after', context.token.run_id)
        state = await asyncio.to_thread(self.store.load_state, context.token.run_id)
        from .recovery import RecoveryStore
        await asyncio.to_thread(RecoveryStore(self.database).checkpoint_facts, context.token.run_id, None)
        await asyncio.to_thread(self.store.record_progress, context.token.run_id, 'aggregate',
            expected_state_version=state['state_version'], execution_token=None,
            verification_id=context.verification['verification_id'], diagnostic='run_finished')
        return validate_graph_state({**self.store.load_state(context.token.run_id), 'route': 'end', 'completed': True})

    def _supplement(self, token):
        with connect(self.database) as db, transaction(db):
            row = validate_in_transaction(db, token)
            if row['state'] != 'VERIFYING':
                return token
            budget = self.scheduler.budgets.on_transition(db, token.run_id, 'RUNNING')
            if budget['exhausted']:
                raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409, field=budget['reason'])
            transition_in_transaction(db, run_id=token.run_id, expected_state_version=token.state_version, target='RUNNING')
            now, _ = self.scheduler._time()
            self.scheduler._change(db, self.scheduler._row(db, token.run_id), now, 'heartbeat',
                                   run_state_version=token.state_version + 1)
        return self.scheduler.refresh_qualification(token)

    async def _recover(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        await self._guard(context)
        run = await self._guard(context)
        contract = run['contract']
        # Repeat observations for one unresolved subgoal spend the same durable
        # recovery allowance. They cannot run indefinitely at zero action cost.
        checkpoint = self.store.load_state(context.token.run_id)
        with connect(self.database) as db:
            row = db.execute('SELECT current_subgoal FROM run_checkpoints WHERE checkpoint_id=?',
                             (checkpoint['business_checkpoint_id'],)).fetchone()
            observed = db.execute('SELECT source_url FROM observations WHERE run_id=? AND snapshot_id=?',
                                  (context.token.run_id, state['snapshot_id'])).fetchone()
        source = next(s for s in contract.sources if s.permits(
            observed[0] if observed else contract.start_urls[0]))
        subgoal = row[0] if row else contract.acceptance_criteria[0].criterion_id
        await asyncio.to_thread(self.scheduler.budgets.consume, context.token, kind='recovery',
            attempt_id='graph-recovery-' + uuid4().hex, site_id=source.site_id,
            subgoal=subgoal,
            obstacle_type='business_validation')
        context.token = await asyncio.to_thread(self._supplement, context.token)
        context.output.clear()
        context.verification = None
        return await self._progress(context, 'recover', route='observe', diagnostic=state['diagnostic'])

    async def _prepare_wait(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        context = runtime.context
        if context.control_operation is not None:
            # The control service has already registered this exact wait in
            # the business transaction. Interrupt reentry cannot register it.
            return await self._control_state(context)
        await self._guard(context)
        with connect(self.database) as db:
            pending_write = self.scheduler._unresolved(db, context.token.run_id)
        if not pending_write:
            context.token = await asyncio.to_thread(self._supplement, context.token)
        wait_id = 'graph-wait-' + hashlib.sha256(
            f'{context.token.run_id}:{context.token.state_version}:{state["progress_id"]}'.encode()).hexdigest()[:32]
        now, _ = self.scheduler._time()
        with connect(self.database) as db, transaction(db):
            self.scheduler._defer_in_transaction(db, context.token, 'PAUSED', now, now)
            append_event(db, run_id=context.token.run_id, expected_state_version=context.token.state_version + 1,
                         payload=WaitingEvent(wait_id=wait_id, reason='pause', deadline=None))
            if context.requested_fields:
                db.execute('INSERT INTO graph_input_requests VALUES(?,?,?,?,?)',
                    (wait_id,context.token.run_id,context.token.state_version + 1,
                     canonical_json(context.requested_fields),utc_text()))
        context.requested_fields = None
        return self.store.record_wait_progress(context.token.run_id, wait_id,
            expected_state_version=context.token.state_version + 1, diagnostic=state['diagnostic'] or 'input_required')

    async def _wait(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        if runtime.context.control_operation is not None and state['completed']:
            # prepare_wait has a fixed edge to this node. A cancellation at
            # that boundary must reach reconcile/stopped without interrupting
            # an already terminal Run.
            return await self._control_state(runtime.context)
        # No browser side effects or wait registration inside the reentered
        # interrupt node. Only the trusted run() method can supply resume=True.
        interrupt({'run_id': state['run_id'], 'wait_id': state['wait_id'], 'diagnostic': state['diagnostic']})
        if not runtime.context.resuming:
            raise BusinessError('STATE_CONFLICT', 'Business resume qualification is required', status=409)
        await self._guard(runtime.context)
        return {**self.store.load_state(state['run_id']), 'route': 'reconcile'}

    async def _stopped(self, state: GraphState, runtime: Runtime[ExecutionContext]):
        return {**self.store.load_state(state['run_id']), 'route': 'end', 'completed': True}

    def _saved(self, saved, token):
        state = validate_graph_state(saved.values)
        run = self.store.load_run(token.run_id)
        if state['run_id'] != token.run_id or state['contract_version'] != run['contract_version']:
            raise BusinessError('STATE_CONFLICT', 'Saved graph belongs to another contract', status=409)
        with connect(self.database) as db:
            event = db.execute('SELECT state_version FROM task_events WHERE run_id=? AND event_id=?',
                               (token.run_id, state['business_event_id'])).fetchone()
            wait = db.execute('''SELECT p.* FROM graph_progress p JOIN task_events e
                ON e.run_id=p.run_id AND e.event_id=p.business_event_id
                WHERE p.run_id=? AND p.progress_id=? AND p.phase='wait'
                AND p.wait_id=? AND e.event_type='wait_registered'
                AND json_extract(e.payload_json,'$.wait_id')=p.wait_id''',
                (token.run_id, state['progress_id'], state['wait_id'])).fetchone()
        if event is None or event[0] != state['state_version'] or state['state_version'] > run['state_version']:
            raise BusinessError('STATE_CONFLICT', 'Graph event reference is inconsistent', status=409)
        if saved.next != ('wait',):
            raise BusinessError('STATE_CONFLICT', 'Crash recovery requires business reconciliation (M1-17)', status=409)
        if (wait is None or wait['business_event_id'] != state['business_event_id']
                or wait['state_version'] != state['state_version']
                or wait['checkpoint_id'] != state['business_checkpoint_id']
                or state['route'] != 'wait' or state['completed']):
            raise BusinessError('STATE_CONFLICT', 'Saved interrupt has no matching business wait', status=409)
        return state

    async def run(self, token):
        if not isinstance(token, ExecutionToken):
            raise BusinessError('RESOURCE_CONFLICT', 'Current execution qualification is required', status=409)
        key = (str(self.database.resolve()), token.run_id)
        gate = _INVOCATIONS.setdefault(key, asyncio.Lock())
        if gate.locked():
            raise BusinessError('RESOURCE_CONFLICT', 'A graph invocation already owns this Run', status=409)
        async with gate:
            context = ExecutionContext(token)
            config = {'configurable': {'thread_id': token.run_id}, 'recursion_limit': 2048}
            try:
                await self._guard(context)
                saved = await self._read_state(config)
                from .recovery import RecoveryStore
                recovery = RecoveryStore(self.database)
                with connect(self.database) as db:
                    entry = db.execute('''SELECT payload_json FROM task_events WHERE run_id=?
                        AND state_version=? AND event_type='state_changed' ORDER BY event_id DESC LIMIT 1''',
                        (token.run_id,context.token.state_version)).fetchone()
                # Even an absent/corrupt first graph save cannot bypass the
                # business restart barrier. A wait interrupt likewise proves
                # registration, never current identity/object/control.
                previous = json.loads(entry[0]).get('previous_state') if entry else None
                if saved.values or previous == 'RECONCILING':
                    await asyncio.to_thread(recovery.require_completed, context.token)
                if saved.values:
                    plan = await asyncio.to_thread(recovery.inspect, token.run_id, saved.values)
                    if not plan['allowed']:
                        raise BusinessError('STATE_CONFLICT', 'Saved graph requires recovery review',
                                            status=409, field=plan['reason'])
                    if saved.next == ('wait',):
                        self._saved(saved, context.token)
                        context.resuming = True
                        value = Command(resume=True)
                    else:
                        # Only a fresh, current-epoch business reconciliation
                        # permits discarding an unfinished framework task.
                        # New input starts at START; the pinned runtime clears
                        # prior pending tasks instead of replaying their writes.
                        value = self.store.load_state(token.run_id)
                else:
                    value = self.store.load_state(token.run_id)
                await self.graph.ainvoke(value, config, context=context, durability='sync')
                return self.store.load_state(token.run_id)
            except _ControlApplied as applied:
                return await self._save_control(applied.operation, config, context.token)
            except BusinessError as error:
                pending = await asyncio.to_thread(self.controls.pending, token.run_id)
                if error.code == 'CONTROL_PENDING' or pending is not None:
                    try:
                        await self._control_boundary(context)
                    except _ControlApplied as applied:
                        return await self._save_control(applied.operation, config, context.token)
                    raise
                if error.code != 'BUDGET_EXCEEDED':
                    raise
                status = self.scheduler.budgets.status(token.run_id)
                run = self.store.load_run(token.run_id)
                # A denied supplement may still have a current verification
                # capsule. Aggregate its verified deliverables before ending,
                # without another model/browser call or changing the criteria.
                # An independent deadline may already have fenced this owner;
                # in that case retain the existing durable budget failure.
                record = context.verification
                if (run['state'] == 'VERIFYING' and record is not None
                        and record['state_version'] == context.token.state_version):
                    try:
                        await asyncio.to_thread(self.verifier.finalize, record['verification_id'],
                            expected_state_version=context.token.state_version, execution_token=context.token)
                    except BusinessError:
                        pass  # Expiry/stale/corrupt evidence cannot be promoted.
                    run = self.store.load_run(token.run_id)
                if run['state'] not in TERMINAL_STATES and status['exhausted']:
                    self.scheduler.expire_budget(token.run_id, status['reason'])
                state = self.store.load_state(token.run_id)
                if self.store.load_run(token.run_id)['state'] not in TERMINAL_STATES:
                    raise
                self.store.record_progress(token.run_id, 'stopped', expected_state_version=state['state_version'],
                    execution_token=None, diagnostic='budget_exceeded')
                return self.store.load_state(token.run_id)
            finally:
                try:
                    # Read the actual saver even when invocation failed. A
                    # historical checkpoint remains a reference to its own
                    # committed event; diagnostics never manufacture a final
                    # checkpoint from a newer terminal business state.
                    await self._diagnose('checkpoint', token.run_id)
                finally:
                    context.output.clear()
                    context.verification = None

    async def _save_control(self, operation, config, token=None):
        saved = await self._read_state(config)
        persisted = await asyncio.to_thread(self.controls.read, operation['operation_id'])
        # read() exposes a response envelope; the local service also returns
        # a completed operation directly to the Worker boundary coordinator.
        persisted = persisted.get('operation', persisted)
        state = await asyncio.to_thread(self.store.load_control_state, operation)
        result = persisted.get('result') or {}
        if (persisted['run_id'] != operation['run_id'] or persisted['status'] != 'APPLIED'
                or persisted['action'] not in ('pause', 'cancel')
                or result.get('state_version') != state['state_version']
                or result.get('state') != ('PAUSED' if persisted['action'] == 'pause' else 'CANCELLED')):
            raise BusinessError('STATE_CONFLICT', 'Control completion no longer matches the Run', status=409)
        if (saved.values.get('state_version') == state['state_version']
                and saved.values.get('progress_id') == state['progress_id']
                and (persisted['action'] == 'pause' and saved.next == ('wait',)
                     or persisted['action'] == 'cancel' and not saved.next and saved.values.get('completed'))):
            await self._diagnose('checkpoint', state['run_id'])
            return state
        context = ExecutionContext(token, control_operation={**persisted, 'run_id': state['run_id']})
        await self.graph.ainvoke(state, config, context=context, durability='sync')
        await self._diagnose('checkpoint', state['run_id'])
        await self._hook('control_graph_saved', state['run_id'])
        return await asyncio.to_thread(self.store.load_control_state, persisted)

    async def settle_control(self, operation):
        """Save only an applied idle control; never borrow execution rights."""
        run_id = operation['run_id']
        key = (str(self.database.resolve()), run_id)
        gate = _INVOCATIONS.setdefault(key, asyncio.Lock())
        if gate.locked():
            raise BusinessError('RESOURCE_CONFLICT', 'A graph invocation already owns this Run', status=409)
        async with gate:
            config = {'configurable': {'thread_id': run_id}, 'recursion_limit': 2048}
            return await self._save_control(operation, config)

    async def repair_terminal(self, run_id):
        """Repair only framework refs after an already committed business end.

        No lease, model, browser or business mutation is created. Invalid or
        unavailable terminal evidence leaves the framework checkpoint intact.
        """
        from .recovery import RecoveryStore
        key = (str(self.database.resolve()), run_id)
        gate = _INVOCATIONS.setdefault(key, asyncio.Lock())
        if gate.locked():
            raise BusinessError('RESOURCE_CONFLICT', 'A graph invocation already owns this Run', status=409)
        async with gate:
            config = {'configurable': {'thread_id': run_id}, 'recursion_limit': 2048}
            saved = await self._read_state(config)
            plan = await asyncio.to_thread(RecoveryStore(self.database).inspect,
                                          run_id, saved.values or None)
            if not plan['allowed'] or not plan['terminal']:
                raise BusinessError('STATE_CONFLICT', 'Terminal checkpoint cannot be repaired',
                                    status=409, field=plan['reason'])
            state = self.store.load_state(run_id)
            if saved.values == {**state, 'route': 'end'} and not saved.next:
                return state
            await self.graph.ainvoke(state, config, context=ExecutionContext(None, terminal_repair=True),
                                     durability='sync')
            return self.store.load_state(run_id)

    async def repair_terminals(self):
        """Worker restart entry for the business-committed/graph-lagging window."""
        with connect(self.database) as db:
            ids = [row[0] for row in db.execute('''SELECT run_id FROM runs
                WHERE state IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED') ORDER BY run_id''')]
        repaired, blocked = [], []
        for run_id in ids:
            config = {'configurable': {'thread_id': run_id}}
            try:
                saved = await self._read_state(config)
                if not saved.values:
                    continue  # Historical Runs without graph execution.
                state = self.store.load_state(run_id)
                if (saved.next or saved.values.get('state_version') != state['state_version']
                        or not saved.values.get('completed')):
                    await self.repair_terminal(run_id)
                    repaired.append(run_id)
            except BusinessError as error:
                blocked.append({'run_id': run_id, 'reason': error.field or error.code})
        return {'repaired': repaired, 'blocked': blocked}
