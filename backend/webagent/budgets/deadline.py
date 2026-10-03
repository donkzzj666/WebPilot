"""A deadline watcher outside the executor, graph, model or parser coroutine.

Expiration must commit its durable fencing before an in-flight awaitable is
cancelled. Cancellation is bounded; a coroutine that ignores cancellation does
not regain authority, and its owner must stop dispatching further work.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import math

from ..errors import BusinessError


class BudgetDeadlineExceeded(BusinessError):
    def __init__(self, reason: str, *, cancellation_completed: bool):
        super().__init__('BUDGET_EXCEEDED', 'Run budget deadline reached', status=409)
        self.reason = reason
        self.cancellation_completed = cancellation_completed


class DeadlineController:
    """Guard an awaitable using fresh persisted authority and budget readings.

``check`` and ``expire`` are async callbacks, so callers can move synchronous
SQLite operations to a thread without blocking this watchdog's event loop.
``fence_failure`` revokes execution when the budget journal is unavailable. It
is required by the Worker; pure deadline tests may omit it where no execution
authority exists. Callback exceptions are propagated without raw error data.
"""

    def __init__(self, check, expire, *, fence_failure=None,
                 poll_seconds=.1, cancel_seconds=5):
        for value in (poll_seconds, cancel_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Deadline intervals must be finite positive numbers')
        if not callable(check) or not callable(expire):
            raise ValueError('Budget check and durable expiration callbacks are required')
        if fence_failure is not None and not callable(fence_failure):
            raise ValueError('A durable failure fence callback is required')
        self.check, self.expire, self.fence_failure = check, expire, fence_failure
        self.poll_seconds, self.cancel_seconds = poll_seconds, cancel_seconds
        self.cancellation_completed = True

    async def _watch(self, operation):
        while not operation.done():
            reason = await self._check()
            if reason is not None:
                return reason
            await asyncio.sleep(self.poll_seconds)
        return None

    async def _check(self):
        status = await self.check()
        if type(status) is not dict or type(status.get('exhausted')) is not bool:
            raise ValueError('Budget journal returned an invalid deadline status')
        if status['exhausted']:
            reason = status.get('reason')
            if type(reason) is not str or not reason:
                raise ValueError('Exhausted budget requires a reason')
            await self.expire(reason)
            return reason
        return None

    async def _cancel(self, operation):
        if not operation.done():
            operation.cancel()
            _, pending = await asyncio.wait({operation}, timeout=self.cancel_seconds)
            self.cancellation_completed = not pending
        if operation.done():
            # Consume cancellation or a late result without changing persisted
            # termination; an executor result after fencing cannot be success.
            with suppress(BaseException):
                operation.result()
        else:
            # A hostile coroutine may outlive this guard. Consume its eventual
            # exception without keeping authority or blocking shutdown.
            operation.add_done_callback(self._consume)

    @staticmethod
    def _consume(task):
        with suppress(BaseException):
            task.result()

    async def _fail_closed(self, operation):
        try:
            if self.fence_failure is not None:
                await self.fence_failure()
        finally:
            if operation is not None:
                await self._cancel(operation)

    async def run(self, awaitable):
        operation = watcher = None
        supplied = None if callable(awaitable) else awaitable
        try:
            # Do not start a model, parser or trusted executor when its initial
            # persisted permission has already expired.
            reason = await self._check()
            if reason is not None:
                raise BudgetDeadlineExceeded(reason, cancellation_completed=True)
            supplied = awaitable() if callable(awaitable) else awaitable
            operation = asyncio.ensure_future(supplied)
            watcher = asyncio.create_task(self._watch(operation))
            done, _ = await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                try:
                    reason = watcher.result()
                except BusinessError as error:
                    if error.status == 409 and operation.done():
                        # Explicit finish/defer can invalidate this runtime
                        # token as the executor returns. Persisted state owns
                        # the outcome; this candidate cannot promote it.
                        return operation.result()
                    raise
                if reason is not None:
                    await self._cancel(operation)
                    raise BudgetDeadlineExceeded(reason, cancellation_completed=self.cancellation_completed)
            result = await operation
            try:
                reason = await self._check()
            except BusinessError as error:
                if error.status == 409:
                    return result
                raise
            if reason is not None:
                raise BudgetDeadlineExceeded(reason, cancellation_completed=True)
            return result
        except BudgetDeadlineExceeded:
            raise
        except BusinessError as error:
            if error.code == 'BUDGET_EXCEEDED':
                # A rejected 151st action, 26th content page or fourth recovery
                # can finish the coroutine before the periodic timer fires.
                # Only the committed journal stop can authorize termination.
                try:
                    reason = await self._check()
                except BusinessError as check_error:
                    if check_error.status != 409:
                        await self._fail_closed(operation)
                    elif operation is not None:
                        await self._cancel(operation)
                    raise
                except Exception:
                    await self._fail_closed(operation)
                    raise
                if reason is not None:
                    if operation is not None:
                        await self._cancel(operation)
                    raise BudgetDeadlineExceeded(reason, cancellation_completed=self.cancellation_completed) from None
            if error.status == 409:
                # A fresh epoch/state check established that this operation has
                # already lost authority (including normal defer and finish).
                if operation is not None:
                    await self._cancel(operation)
            else:
                await self._fail_closed(operation)
            raise
        except asyncio.CancelledError:
            # The caller revokes persisted authority before cancelling a guard
            # (Worker shutdown does this in aclose()).
            if operation is not None:
                await self._cancel(operation)
            raise
        except Exception:
            await self._fail_closed(operation)
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(BaseException):
                    await watcher
            if operation is None:
                if inspect.iscoroutine(supplied):
                    supplied.close()
                elif isinstance(supplied, asyncio.Future):
                    await self._cancel(supplied)


BudgetGuard = DeadlineController
