"""Independent watchdog tests; no credentials, model provider or browser I/O."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from webagent.budgets import clock as clock_module
from webagent.budgets.clock import SystemClock
from webagent.budgets.deadline import BudgetDeadlineExceeded, DeadlineController
from webagent.errors import BusinessError


async def until(predicate):
    async with asyncio.timeout(.8):
        while not predicate():
            await asyncio.sleep(.001)


class VirtualClock:
    def __init__(self):
        self.wall = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.mono = 0
        self.domain = 'test:boot-domain'

    def utcnow(self):
        return self.wall

    def monotonic_ns(self):
        return self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += int(seconds * 1_000_000_000)


class BudgetFixture:
    """An external clock and authority ledger, independent of executor state."""
    def __init__(self, clock, *, limit_ms=1_200_000, reason='active_time_exhausted'):
        self.clock, self.limit_ms, self.reason = clock, limit_ms, reason
        self.authorized = True
        self.reads = 0
        self.expirations = []
        self.events = []
        self.error = None

    async def check(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        if not self.authorized:
            raise BusinessError('RESOURCE_CONFLICT', 'Synthetic old qualification', status=409)
        remaining = max(0, self.limit_ms - self.clock.monotonic_ns() // 1_000_000)
        return {'exhausted': remaining == 0, 'reason': self.reason if remaining == 0 else None,
                'remaining_active_ms': remaining}

    async def expire(self, reason):
        self.events.append('durable_fence')
        self.authorized = False
        self.expirations.append(reason)

    async def fail_fence(self):
        self.events.append('failure_fence')
        self.authorized = False

    def guard(self, **updates):
        return DeadlineController(self.check, self.expire, fence_failure=self.fail_fence,
                                  **{'poll_seconds': .002, 'cancel_seconds': .01, **updates})


def test_system_clock_utc_monotonic_and_domain_agree_between_instances():
    first, second = SystemClock(), SystemClock()
    assert first.utcnow().utcoffset() == timedelta(0)
    before = first.monotonic_ns()
    assert second.monotonic_ns() >= before
    assert first.domain == second.domain
    assert first.domain.startswith(('boot:', 'process:'))


@pytest.mark.parametrize('system', ['Linux', 'Darwin'])
def test_known_boot_domain_uses_readonly_stable_source(monkeypatch, system):
    clock_module._boot_domain.cache_clear()
    monkeypatch.setattr(clock_module.platform, 'system', lambda: system)
    calls = []
    monkeypatch.setattr(clock_module.Path, 'read_text', lambda self: 'fixture-boot-id\n')

    def sysctl(args, **options):
        calls.append((args, options))
        return SimpleNamespace(stdout='{ sec = 1790712000, usec = 123456 }\n')

    monkeypatch.setattr(clock_module.subprocess, 'run', sysctl)
    try:
        one, two = SystemClock(), SystemClock()
        assert one.domain == two.domain
        assert one.domain.startswith('boot:') and len(one.domain) == 69
        if system == 'Darwin':
            assert len(calls) == 1
            assert calls[0][0] == ['/usr/sbin/sysctl', '-n', 'kern.boottime']
            assert calls[0][1]['timeout'] == 2
            assert not calls[0][1].get('shell', False)
        else:
            assert not calls
    finally:
        clock_module._boot_domain.cache_clear()


def test_unknown_boot_domain_is_process_local_and_stable_in_process(monkeypatch):
    clock_module._boot_domain.cache_clear()
    monkeypatch.setattr(clock_module.platform, 'system', lambda: 'Darwin')

    def unavailable(*args, **kwargs):
        raise OSError('Synthetic unavailable kernel clock')

    monkeypatch.setattr(clock_module.subprocess, 'run', unavailable)
    try:
        one, two = SystemClock(), SystemClock()
        assert one.domain == two.domain and one.domain.startswith('process:')
        clock_module._boot_domain.cache_clear()
        assert SystemClock().domain == one.domain
        monkeypatch.setattr(clock_module, '_PROCESS_DOMAIN', 'process:next-worker-process')
        clock_module._boot_domain.cache_clear()
        assert SystemClock().domain != one.domain
    finally:
        clock_module._boot_domain.cache_clear()


@pytest.mark.parametrize('waiting_component', ['model', 'attachment_parser', 'page', 'graph'])
def test_twenty_minute_deadline_cancels_hanging_component_independent_of_graph(waiting_component):
    async def scenario():
        clock = VirtualClock()
        budget = BudgetFixture(clock)
        entered = asyncio.Event()
        observations = []

        async def hang():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                budget.events.append(f'cancel:{waiting_component}')
                observations.append(budget.authorized)

        guard = budget.guard()
        invocation = asyncio.create_task(guard.run(hang()))
        await asyncio.wait_for(entered.wait(), .8)
        clock.advance(1199.999)
        await until(lambda: budget.reads >= 3)
        assert not invocation.done() and budget.authorized
        clock.advance(.001)
        with pytest.raises(BudgetDeadlineExceeded) as result:
            await asyncio.wait_for(invocation, .8)
        assert result.value.reason == 'active_time_exhausted'
        assert result.value.cancellation_completed
        assert observations == [False]
        assert budget.events == ['durable_fence', f'cancel:{waiting_component}']
        assert budget.expirations == ['active_time_exhausted']
    asyncio.run(scenario())


@pytest.mark.parametrize('reason', ['active_time_exhausted', 'ci_wait_exhausted', 'handoff_deadline'])
def test_initial_expiration_fences_without_invoking_awaitable(reason):
    async def scenario():
        budget = BudgetFixture(VirtualClock(), limit_ms=0, reason=reason)
        called = []

        async def execute():
            called.append(True)

        with pytest.raises(BudgetDeadlineExceeded) as result:
            await budget.guard().run(execute())
        assert result.value.reason == reason
        assert called == []
        assert budget.expirations == [reason] and not budget.authorized
    asyncio.run(scenario())


def test_exhausted_budget_does_not_even_call_trusted_executor_factory():
    async def scenario():
        budget = BudgetFixture(VirtualClock(), limit_ms=0)
        called = []

        def factory():
            called.append('called')
            return asyncio.sleep(0)

        with pytest.raises(BudgetDeadlineExceeded):
            await budget.guard().run(factory)
        assert called == [] and not budget.authorized
    asyncio.run(scenario())


def test_finished_operation_stops_watchdog_without_writing_expiration():
    async def scenario():
        budget = BudgetFixture(VirtualClock())

        async def execute():
            return 'verified fixture result'

        assert await budget.guard().run(execute()) == 'verified fixture result'
        reads = budget.reads
        budget.clock.advance(1200)
        await asyncio.sleep(.006)
        assert budget.reads == reads
        assert budget.authorized and not budget.expirations
    asyncio.run(scenario())


def test_result_and_deadline_ready_together_cannot_return_candidate():
    async def scenario():
        budget = BudgetFixture(VirtualClock())

        async def execute():
            budget.clock.advance(1200)
            return 'candidate at deadline'

        with pytest.raises(BudgetDeadlineExceeded) as result:
            await budget.guard().run(execute)
        assert result.value.reason == 'active_time_exhausted'
        assert budget.expirations == ['active_time_exhausted']
        assert not budget.authorized
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['FINISHED', 'DEFERRED'])
def test_final_permission_conflict_after_explicit_release_can_return_candidate(outcome):
    async def scenario():
        budget = BudgetFixture(VirtualClock())

        async def execute():
            budget.authorized = False
            budget.events.append(outcome)
            return 'candidate with persisted outcome'

        assert await budget.guard().run(execute) == 'candidate with persisted outcome'
        assert budget.events == [outcome] and not budget.expirations
    asyncio.run(scenario())


def test_failed_final_budget_read_fails_closed_without_returning_candidate():
    async def scenario():
        budget = BudgetFixture(VirtualClock())

        async def execute():
            budget.error = RuntimeError('Synthetic final persistence failure')
            return 'candidate that must be withheld'

        with pytest.raises(RuntimeError):
            await budget.guard().run(execute)
        assert budget.events == ['failure_fence'] and not budget.authorized
    asyncio.run(scenario())


@pytest.mark.parametrize('reason', ['action_limit', 'content_page_limit', 'recovery_limit'])
def test_immediate_budget_dispatch_rejection_terminates_using_committed_reason(reason):
    async def scenario():
        budget = BudgetFixture(VirtualClock(), reason=reason)

        async def execute():
            budget.limit_ms = 0  # Atomic consume persisted the denied stop.
            raise BusinessError('BUDGET_EXCEEDED', 'Synthetic dispatch limit', status=409,
                                field=reason)

        with pytest.raises(BudgetDeadlineExceeded) as result:
            await budget.guard().run(execute)
        assert result.value.reason == reason
        assert budget.expirations == [reason] and not budget.authorized
    asyncio.run(scenario())


def test_budget_error_without_committed_stop_does_not_fake_budget_termination():
    async def scenario():
        budget = BudgetFixture(VirtualClock())

        async def execute():
            raise BusinessError('BUDGET_EXCEEDED', 'Synthetic untrusted error', status=409,
                                field='action_limit')

        with pytest.raises(BusinessError) as result:
            await budget.guard().run(execute)
        assert type(result.value) is BusinessError
        assert budget.authorized and budget.expirations == []
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['PAUSED', 'WAITING_CI', 'WAITING_HANDOFF', 'CANCELLED'])
def test_deferred_or_finished_authority_does_not_become_budget_failure(outcome):
    async def scenario():
        budget = BudgetFixture(VirtualClock())
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def execute():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
                assert not budget.authorized

        invocation = asyncio.create_task(budget.guard().run(execute()))
        await asyncio.wait_for(entered.wait(), .8)
        budget.authorized = False  # A durable state transition already completed.
        budget.events.append(outcome)
        with pytest.raises(BusinessError) as result:
            await asyncio.wait_for(invocation, .8)
        assert result.value.code == 'RESOURCE_CONFLICT'
        assert cancelled.is_set() and budget.expirations == []
        assert budget.events == [outcome]
    asyncio.run(scenario())


def test_noncooperative_cancellation_is_bounded_and_old_execution_stays_fenced():
    async def scenario():
        clock = VirtualClock()
        budget = BudgetFixture(clock)
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        authority = []

        async def ignores_cancel():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                authority.append(budget.authorized)
                await release.wait()
            finally:
                finished.set()

        guard = budget.guard()
        invocation = asyncio.create_task(guard.run(ignores_cancel()))
        await asyncio.wait_for(entered.wait(), .8)
        clock.advance(1200)
        try:
            with pytest.raises(BudgetDeadlineExceeded) as result:
                await asyncio.wait_for(invocation, .2)
            assert not result.value.cancellation_completed
            assert not guard.cancellation_completed
            assert authority == [False] and not finished.is_set()
            assert budget.expirations == ['active_time_exhausted']
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), .8)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['read', 'invalid_status', 'expire'])
def test_budget_control_failure_fences_before_cancel_and_propagates(failure):
    async def scenario():
        clock = VirtualClock()
        budget = BudgetFixture(clock)
        entered = asyncio.Event()
        cancellation_authority = []

        async def execute():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancellation_authority.append(budget.authorized)

        guard = budget.guard()
        invocation = asyncio.create_task(guard.run(execute()))
        await asyncio.wait_for(entered.wait(), .8)
        if failure == 'read':
            budget.error = RuntimeError('Synthetic unavailable budget journal')
        elif failure == 'invalid_status':
            async def malformed():
                return {'exhausted': 'false'}
            guard.check = malformed
        else:
            async def broken_expire(reason):
                raise RuntimeError('Synthetic failed durable expiration')
            guard.expire = broken_expire
            clock.advance(1200)
        with pytest.raises((RuntimeError, ValueError)):
            await asyncio.wait_for(invocation, .8)
        assert cancellation_authority == [False]
        assert budget.events == ['failure_fence'] and not budget.expirations
    asyncio.run(scenario())


@pytest.mark.parametrize('parameter', ['poll_seconds', 'cancel_seconds'])
@pytest.mark.parametrize('value', [False, 0, -1, float('inf'), float('nan'), '1'])
def test_invalid_watchdog_intervals_are_rejected(parameter, value):
    budget = BudgetFixture(VirtualClock())
    with pytest.raises(ValueError):
        budget.guard(**{parameter: value})


@pytest.mark.parametrize('check,expire,fence', [(None, lambda: None, None),
                                              (lambda: None, None, None),
                                              (lambda: None, lambda: None, 'command')])
def test_invalid_callbacks_are_rejected(check, expire, fence):
    with pytest.raises(ValueError):
        DeadlineController(check, expire, fence_failure=fence)
