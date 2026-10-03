"""Bounded Worker pump for durable claims, heartbeats and fencing.

Executors are trusted dependencies injected by the product graph adapter, never
an API-supplied callable. Until the budget/gateway/graph is ready, the normal
Worker registers and maintains its generation without claiming business work.
An executor must explicitly defer or finish its claim; returning or failing
without doing so leaves recovery work, never fabricated business success.
"""
from __future__ import annotations

import asyncio
import math
from contextlib import suppress

from ..budgets.deadline import BudgetDeadlineExceeded, DeadlineController
from ..errors import BusinessError
from .store import SchedulerStore


class QueueWorker:
    def __init__(self, store: SchedulerStore, worker_id: str, *, executor=None,
                 poll_seconds=.25, heartbeat_seconds=5, shutdown_seconds=5,
                 deadline_seconds=.1, controls=None, control_settler=None):
        for value in (poll_seconds, heartbeat_seconds, shutdown_seconds, deadline_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Worker intervals must be finite positive numbers')
        if executor is not None and not callable(executor):
            raise ValueError('A trusted executor callable is required')
        if control_settler is not None and not callable(control_settler):
            raise ValueError('A trusted control settlement callable is required')
        self.store, self.worker_id, self.executor = store, worker_id, executor
        self.controls, self.control_settler = controls, control_settler
        self.poll_seconds, self.heartbeat_seconds = poll_seconds, heartbeat_seconds
        self.shutdown_seconds = shutdown_seconds
        self.deadline_seconds = deadline_seconds
        self.generation = None
        self._claims = {}
        self._closing = False
        self._failed = False
        self._start_gate = asyncio.Lock()
        self._tick_gate = asyncio.Lock()
        self._controls_saved = set()
        self._fenced_control_settlements = set()

    async def start(self):
        async with self._start_gate:
            if self._closing:
                raise RuntimeError('Worker pump has closed')
            if self.generation is None:
                registration = asyncio.create_task(asyncio.to_thread(self.store.start_worker, self.worker_id))
                try:
                    self.generation = await asyncio.shield(registration)
                except asyncio.CancelledError:
                    # The SQLite thread may already have committed. Retain its
                    # identity so run()'s finally can revoke that generation.
                    self.generation = await registration
                    raise
        return self

    async def _abandon(self, token, reason):
        try:
            await asyncio.to_thread(self.store.abandon, token, reason=reason)
            return True
        except BusinessError as error:
            if error.status != 409:
                self._failed = True
        except Exception:
            self._failed = True
        return False

    async def _settle_fenced_control(self, run_id):
        """Complete one accepted stop intent after its executor has exited.

        This is idle bookkeeping under the store's own transaction. It never
        borrows the revoked token, resumes the pump, or admits browser/model I/O.
        """
        if self.controls is None:
            return
        repair = None
        def consume(task):
            with suppress(BaseException):
                task.result()
        try:
            pending = await asyncio.to_thread(self.controls.pending, run_id)
            if pending is None or pending['action'] not in ('pause', 'cancel'):
                return
            operation = await asyncio.to_thread(self.controls.apply_idle, run_id,
                                                expected_operation_id=pending['operation_id'])
            if (operation is None or operation['status'] != 'APPLIED'
                    or operation['action'] not in ('pause', 'cancel')
                    or self.control_settler is None):
                return
            # Business completion is already durable. A damaged graph or saver
            # can block this repair without erasing the API completion receipt.
            repair = asyncio.create_task(self.control_settler(operation))
            done, _ = await asyncio.wait({repair}, timeout=self.shutdown_seconds)
            if not done:
                repair.cancel()
                await asyncio.wait({repair}, timeout=self.shutdown_seconds)
                self._failed = True
                return
            repair.result()
            self._controls_saved.add(operation['operation_id'])
        except BusinessError:
            self._failed = True
        except Exception:
            self._failed = True
        finally:
            if repair is not None:
                if not repair.done():
                    repair.cancel()
                    repair.add_done_callback(consume)
                else:
                    consume(repair)

    async def _execute(self, token):
        operation = None
        guard = None
        reason = 'executor_returned'
        settlement_deadline = None
        loop = asyncio.get_running_loop()

        async def settling(error):
            nonlocal settlement_deadline
            if (error.status != 409 or not hasattr(self.store, 'settlement')
                    or settlement_deadline is not None and loop.time() >= settlement_deadline):
                return False
            receipt = await asyncio.to_thread(self.store.settlement, token)
            if not receipt and hasattr(self.store, 'recovery_blocked_receipt'):
                receipt = await asyncio.to_thread(self.store.recovery_blocked_receipt, token)
            if not receipt and self.controls is not None:
                receipt = await asyncio.to_thread(self.controls.completion_receipt, token)
            if not receipt:
                return False
            # Fixed once: frequent watchdog/heartbeat checks cannot extend the
            # tail. All browser/model qualification remains durably revoked.
            if settlement_deadline is None:
                settlement_deadline = loop.time() + self.shutdown_seconds
            return loop.time() < settlement_deadline

        try:
            if hasattr(self.store, 'budgets') and hasattr(self.store, 'expire_budget'):
                async def refresh_runtime_token():
                    nonlocal token
                    if hasattr(self.store, 'refresh_qualification'):
                        token = await asyncio.to_thread(self.store.refresh_qualification, token)

                async def check_budget():
                    nonlocal token
                    try:
                        if hasattr(self.store, 'runtime_budget'):
                            token, status = await asyncio.to_thread(self.store.runtime_budget, token)
                            return status
                        await refresh_runtime_token()
                        return await asyncio.to_thread(self.store.budgets.flush, token)
                    except BusinessError as error:
                        if await settling(error):
                            return {'exhausted': False}
                        raise

                async def expire_budget(reason):
                    await asyncio.to_thread(self.store.expire_budget, token.run_id, reason)

                async def fence_failure():
                    self._failed = True
                    await self._abandon(token, 'budget_control_failed')

                guard = DeadlineController(check_budget, expire_budget,
                                           fence_failure=fence_failure,
                                           poll_seconds=self.deadline_seconds,
                                           cancel_seconds=self.shutdown_seconds)
                async def guarded_execute():
                    return await self.executor(token)

                operation = asyncio.create_task(guard.run(guarded_execute))
            else:
                operation = asyncio.create_task(self.executor(token))
            while not operation.done():
                done, _ = await asyncio.wait({operation}, timeout=self.heartbeat_seconds)
                if done:
                    break
                try:
                    if hasattr(self.store, 'runtime_heartbeat'):
                        token = await asyncio.to_thread(self.store.runtime_heartbeat, token)
                    else:
                        if hasattr(self.store, 'refresh_qualification'):
                            token = await asyncio.to_thread(self.store.refresh_qualification, token)
                        token = await asyncio.to_thread(self.store.heartbeat, token)
                except BusinessError as error:
                    if await settling(error):
                        continue
                    raise
                except Exception:
                    self._failed = True
                    raise
            result = await operation
            if (isinstance(result, dict) and result.get('recovery_blocked') is True
                    and hasattr(self.store, 'recovery_blocked_receipt')
                    and await asyncio.to_thread(self.store.recovery_blocked_receipt, token)):
                reason = 'recovery_blocked'
        except BudgetDeadlineExceeded as error:
            reason = 'budget_exhausted'
            if not error.cancellation_completed:
                # Its authority is already fenced, but do not dispatch another
                # task beside an uncooperative coroutine in this process.
                self._failed = True
        except asyncio.CancelledError:
            reason = 'worker_stopping'
            raise
        except BusinessError:
            reason = 'execution_fenced'
        except Exception:
            # Raw exception text can contain browser or credential data.
            reason = 'executor_failed'
        finally:
            # Establish durable fencing before invoking cancellation handlers.
            abandoned = await self._abandon(token, reason)
            if operation is not None and not operation.done():
                operation.cancel()
                done, _ = await asyncio.wait({operation}, timeout=self.shutdown_seconds)
                if not done:
                    # A non-cooperative executor cannot retain durable authority.
                    self._failed = True
            if operation is not None and operation.done():
                # A heartbeat can fence the same operation as its independent
                # watchdog. Consume a concurrent late exception even when the
                # heartbeat path did not await that already finished task.
                with suppress(BaseException):
                    operation.result()
            if guard is not None and not guard.cancellation_completed:
                self._failed = True
            if abandoned and reason in ('executor_returned', 'executor_failed', 'execution_fenced'):
                # An incomplete executor must not immediately dispatch the same
                # recovery Run again. A new worker can obtain reconciliation
                # authority only after the adapter has been checked.
                self._failed = True
            if (abandoned and operation is not None and operation.done()
                    and (guard is None or guard.cancellation_completed)):
                # An action timeout can revoke its owner while a user stop
                # request is still pending. Its graph has now released the
                # thread gate, so finish that request without restarting or
                # letting this failed pump claim another Run.
                self._fenced_control_settlements.add(token.run_id)
                try:
                    await self._settle_fenced_control(token.run_id)
                finally:
                    self._fenced_control_settlements.discard(token.run_id)

    async def tick(self):
        async with self._tick_gate:
            await self._tick()

    async def _tick(self):
        await self.start()
        if self._closing or self._failed or self._fenced_control_settlements:
            return
        for run_id, task in list(self._claims.items()):
            if task.done():
                with suppress(BaseException):
                    task.result()
                del self._claims[run_id]
        if self.controls is not None:
            operations = await asyncio.to_thread(self.controls.apply_idle_pending,
                                                 exclude_run_ids=tuple(self._claims))
            # A prior process may have committed a pause but died before its
            # framework interrupt save. Applied receipts remain authoritative.
            completed, cursor = [], 0
            while True:
                page = await asyncio.to_thread(self.controls.list_completed, after=cursor, limit=100)
                completed.extend(page)
                if len(page) < 100:
                    break
                cursor = page[-1]['operation_seq']
            if self.control_settler is not None:
                unique = {op['operation_id']: op for op in [*operations, *completed]
                          if op['status'] == 'APPLIED' and op['action'] in ('pause', 'cancel')}
                for operation in unique.values():
                    if operation['run_id'] in self._claims:
                        continue
                    if operation['operation_id'] in self._controls_saved:
                        continue
                    try:
                        await self.control_settler(operation)
                        self._controls_saved.add(operation['operation_id'])
                    except BusinessError as error:
                        if error.status != 409:
                            raise
        await asyncio.to_thread(self.store.heartbeat_worker, self.worker_id, self.generation)
        await asyncio.to_thread(self.store.sweep_expired)
        if hasattr(self.store, 'sweep_budget_due'):
            # Waiting for a site, CI or handoff can consume a budget without a
            # live executor. Keep these deadlines independent of graph ticks.
            await asyncio.to_thread(self.store.sweep_budget_due)
        if self.executor is None:
            return
        # The local bound avoids excess coroutines; the SQLite active-slot
        # reservation remains authoritative across concurrent processes.
        while len(self._claims) < 2 and not self._closing:
            token = await asyncio.to_thread(self.store.claim, self.worker_id, self.generation)
            if token is None:
                break
            if self._closing or self._failed:
                await self._abandon(token, 'worker_stopping')
                break
            if token.run_id in self._claims:
                await self._abandon(token, 'duplicate_dispatch')
                self._failed = True
                break
            self._claims[token.run_id] = asyncio.create_task(self._execute(token))

    async def run(self, stopped: asyncio.Event):
        try:
            await self.start()
            while not stopped.is_set() and not self._closing:
                await self.tick()
                if self._failed and not self._fenced_control_settlements:
                    raise RuntimeError('Scheduler stopped after an execution or persistence failure')
                try:
                    await asyncio.wait_for(stopped.wait(), self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.aclose()

    async def aclose(self):
        self._closing = True
        # Revoke persisted authority before awaiting coroutine cancellation.
        # A non-cooperative executor must lose authorization immediately.
        try:
            async with self._start_gate:
                if self.generation is not None:
                    await asyncio.to_thread(self.store.stop_worker, self.worker_id, self.generation)
                    self.generation = None
        except Exception:
            self._failed = True
            raise
        finally:
            tasks = tuple(self._claims.values())
            for task in tasks:
                task.cancel()
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=self.shutdown_seconds + 1)
                if pending:
                    self._failed = True
            self._claims.clear()
