"""Independent dispatch-pump tests with no browser, credentials or default data.

The fake store models durable fencing separately from coroutine cancellation:
an old operation may ignore cancellation but cannot retain execution authority.
SQLite and child-process verification are covered by the scheduler probe.
"""
import asyncio
from dataclasses import replace
import threading

import pytest

from webagent.db.connection import StorageBusyError
from webagent.errors import BusinessError
from webagent.scheduler.models import ExecutionToken, Resource
from webagent.scheduler.store import SchedulerStore
from webagent.scheduler.worker import QueueWorker


class Store:
    """Thread-safe authority ledger; never infer success from an executor return."""

    def __init__(self, count=0):
        self.lock = threading.RLock()
        self.pending = [f'run-{index}' for index in range(count)]
        self.claims = {}
        self.outcomes = {}
        self.generation = 0
        self.live = False
        self.starts = self.stops = self.sweeps = self.claim_calls = 0
        self.worker_heartbeats = 0
        self.heartbeats = []
        self.abandons = []
        self.claim_entered = threading.Event()
        self.claim_release = None
        self.start_release = None
        self.start_entered = threading.Event()
        self.heartbeat_error = None
        self.sweep_error = None
        self.claim_error = None
        self.worker_heartbeat_error = None
        self.stop_error = None

    def start_worker(self, worker_id):
        with self.lock:
            self.starts += 1
            self.generation += 1
            self.live = True
            generation = self.generation
        self.start_entered.set()
        if self.start_release is not None:
            assert self.start_release.wait(2), 'Test registration gate was not released'
        return generation

    def stop_worker(self, worker_id, generation):
        with self.lock:
            self.stops += 1
            if self.stop_error is not None:
                raise self.stop_error
            self.live = False
            for run_id in self.claims:
                self.outcomes[run_id] = 'RECOVERY'
            self.claims.clear()

    def heartbeat_worker(self, worker_id, generation):
        with self.lock:
            if self.worker_heartbeat_error is not None:
                raise self.worker_heartbeat_error
            if not self.live or generation != self.generation:
                raise BusinessError('STATE_CONFLICT', 'Worker generation is obsolete', status=409)
            self.worker_heartbeats += 1
            return '2026-09-30T00:01:00.000000Z'

    def sweep_expired(self):
        with self.lock:
            self.sweeps += 1
            if self.sweep_error is not None:
                raise self.sweep_error

    def claim(self, worker_id, generation):
        with self.lock:
            self.claim_calls += 1
            if self.claim_error is not None:
                raise self.claim_error
            if not self.live or generation != self.generation:
                raise BusinessError('STATE_CONFLICT', 'Worker generation is obsolete', status=409)
            if not self.pending:
                return None
            run_id = self.pending.pop(0)
            token = ExecutionToken(run_id, worker_id, generation, 1, 1,
                                   '2026-09-30T00:00:30.000000Z',
                                   (Resource.active_slot(self.claim_calls % 2).resource_key,))
            self.claims[run_id] = token
        self.claim_entered.set()
        # Delay the return after a committed claim, as a real to_thread call can.
        if self.claim_release is not None:
            assert self.claim_release.wait(2), 'Test claim gate was not released'
        return token

    def authorize(self, token):
        with self.lock:
            current = self.claims.get(token.run_id)
            if (not self.live or token.worker_generation != self.generation or current is None
                    or current.epoch != token.epoch):
                raise BusinessError('STATE_CONFLICT', 'Execution qualification was revoked', status=409)
            return current

    def heartbeat(self, token):
        with self.lock:
            self.authorize(token)
            if self.heartbeat_error is not None:
                raise self.heartbeat_error
            renewed = replace(token, expires_at='2026-09-30T00:01:00.000000Z')
            self.claims[token.run_id] = renewed
            self.heartbeats.append(renewed)
            return renewed

    def abandon(self, token, *, reason):
        with self.lock:
            self.authorize(token)
            self.abandons.append((token, reason))
            del self.claims[token.run_id]
            self.outcomes[token.run_id] = 'RECOVERY'

    def explicitly_release(self, token, outcome):
        with self.lock:
            self.authorize(token)
            del self.claims[token.run_id]
            self.outcomes[token.run_id] = outcome


async def until(predicate, *, seconds=.8):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.001)


def pump(store, executor=None, **updates):
    return QueueWorker(store, 'fixture-worker', executor=executor,
                       **{'poll_seconds': .005, 'heartbeat_seconds': .01,
                          'shutdown_seconds': .025, **updates})


def test_no_executor_registers_and_sweeps_without_claiming_business_work():
    async def scenario():
        store = Store(3)
        worker = pump(store)
        try:
            await worker.tick()
            await worker.tick()
            assert store.starts == 1
            assert store.sweeps == 2
            assert store.worker_heartbeats >= 1
            assert store.claim_calls == 0
            assert len(store.pending) == 3
        finally:
            await worker.aclose()
        assert not store.live and store.stops == 1
    asyncio.run(scenario())


def test_two_local_executors_and_third_only_after_a_claim_finishes():
    async def scenario():
        store = Store(3)
        entered = []
        release = asyncio.Event()

        async def execute(token):
            entered.append(token.run_id)
            await release.wait()
            store.explicitly_release(token, 'FINISHED')

        worker = pump(store, execute)
        try:
            await worker.tick()
            await until(lambda: len(entered) == 2)
            await worker.tick()
            assert len(entered) == 2 and len(store.pending) == 1
            release.set()
            await until(lambda: all(task.done() for task in worker._claims.values()))
            await worker.tick()
            await until(lambda: len(entered) == 3)
            await until(lambda: not store.claims)
            assert set(store.outcomes.values()) == {'FINISHED'}
        finally:
            await worker.aclose()
    asyncio.run(scenario())


def test_long_operation_gets_periodic_heartbeat_and_renewed_token_is_abandoned():
    async def scenario():
        store = Store(1)
        release = asyncio.Event()

        async def execute(token):
            await release.wait()

        worker = pump(store, execute)
        try:
            await worker.tick()
            await until(lambda: len(store.heartbeats) >= 2)
            release.set()
            await until(lambda: len(store.abandons) == 1)
            token, reason = store.abandons[0]
            assert reason == 'executor_returned'
            assert token.expires_at == store.heartbeats[-1].expires_at
            assert store.outcomes == {'run-0': 'RECOVERY'}
        finally:
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('fails', [False, True])
def test_return_or_exception_never_creates_business_success(fails):
    async def scenario():
        store = Store(1)

        async def execute(token):
            if fails:
                raise RuntimeError('Synthetic private browser detail')

        worker = pump(store, execute)
        try:
            await worker.tick()
            await until(lambda: bool(store.abandons))
            assert store.outcomes == {'run-0': 'RECOVERY'}
            assert store.abandons[0][1] == ('executor_failed' if fails else 'executor_returned')
        finally:
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['FINISHED', 'WAITING_CI', 'PAUSED'])
def test_executor_explicit_release_survives_finally_abandon(outcome):
    async def scenario():
        store = Store(1)

        async def execute(token):
            store.explicitly_release(token, outcome)

        worker = pump(store, execute)
        try:
            await worker.tick()
            await until(lambda: bool(store.outcomes))
            await until(lambda: all(task.done() for task in worker._claims.values()))
            assert store.outcomes == {'run-0': outcome}
            assert store.abandons == []
            assert not worker._failed
        finally:
            await worker.aclose()
    asyncio.run(scenario())


def test_epoch_revocation_cancels_inflight_operation_without_overwriting_new_owner():
    async def scenario():
        store = Store(1)
        cancelled = asyncio.Event()

        async def execute(token):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        worker = pump(store, execute)
        try:
            await worker.tick()
            await until(lambda: bool(store.claims))
            with store.lock:
                previous = store.claims['run-0']
                store.claims['run-0'] = replace(previous, epoch=2)
            await asyncio.wait_for(cancelled.wait(), .8)
            await until(lambda: all(task.done() for task in worker._claims.values()))
            assert store.claims['run-0'].epoch == 2
            assert store.outcomes == {} and store.abandons == []
        finally:
            await worker.aclose()
    asyncio.run(scenario())


def test_stop_revokes_generation_before_cancel_handler_can_dispatch():
    async def scenario():
        store = Store(1)
        entered = asyncio.Event()
        cancellation_authority = []

        async def execute(token):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                try:
                    store.authorize(token)
                except BusinessError:
                    cancellation_authority.append(False)
                else:
                    cancellation_authority.append(True)
                raise

        worker = pump(store, execute)
        await worker.tick()
        await asyncio.wait_for(entered.wait(), .8)
        await worker.aclose()
        assert cancellation_authority == [False]
        assert not store.live and not store.claims
        await worker.aclose()
        assert store.stops == 1
    asyncio.run(scenario())


def test_noncooperative_cancel_is_bounded_and_cannot_retain_durable_authority():
    async def scenario():
        store = Store(1)
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        cancellation_authority = []

        async def execute(token):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                try:
                    store.authorize(token)
                except BusinessError:
                    cancellation_authority.append(False)
                else:
                    cancellation_authority.append(True)
                await release.wait()

        worker = pump(store, execute)
        try:
            await worker.tick()
            await asyncio.wait_for(entered.wait(), .8)
            await asyncio.wait_for(worker.aclose(), .8)
            assert cancelled.is_set()
            assert cancellation_authority == [False]
            assert not store.live and not store.claims
            assert worker._failed
        finally:
            release.set()
            await asyncio.sleep(.005)
            await worker.aclose()
    asyncio.run(scenario())


def test_failed_stop_still_cancels_operations_and_a_later_close_retries_fencing():
    async def scenario():
        store = Store(1)
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def execute(token):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        worker = pump(store, execute)
        await worker.tick()
        await asyncio.wait_for(entered.wait(), .8)
        store.stop_error = StorageBusyError('Synthetic shutdown persistence failure')
        with pytest.raises(StorageBusyError):
            await worker.aclose()
        assert cancelled.is_set()
        assert worker._failed and worker._closing
        assert worker.generation is not None
        store.stop_error = None
        await worker.aclose()
        assert worker.generation is None and not store.live
        assert store.stops == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('location', ['claim', 'sweep', 'heartbeat', 'worker_heartbeat'])
def test_storage_busy_fails_closed_and_stops_generation(location):
    async def scenario():
        store = Store(1)
        setattr(store, f'{location}_error', StorageBusyError('Synthetic busy storage'))
        entered = []

        async def execute(token):
            entered.append(token.run_id)
            await asyncio.Future()

        worker = pump(store, execute)
        stopped = asyncio.Event()
        with pytest.raises((StorageBusyError, RuntimeError)):
            await asyncio.wait_for(worker.run(stopped), .8)
        assert not store.live and not store.claims
        if location != 'heartbeat':
            assert entered == []
        assert store.stops == 1
    asyncio.run(scenario())


def test_shutdown_while_claim_return_is_delayed_never_starts_an_executor():
    async def scenario():
        store = Store(1)
        store.claim_release = threading.Event()
        entered = []

        async def execute(token):
            entered.append(token.run_id)

        worker = pump(store, execute)
        tick = asyncio.create_task(worker.tick())
        try:
            await until(store.claim_entered.is_set)
            close = asyncio.create_task(worker.aclose())
            await until(lambda: worker._closing)
            store.claim_release.set()
            await asyncio.gather(tick, close)
            await asyncio.sleep(.005)
            assert entered == []
            assert not store.live and not store.claims and not worker._claims
        finally:
            store.claim_release.set()
            await worker.aclose()
            with pytest.raises(RuntimeError):
                await worker.start()
    asyncio.run(scenario())


def test_concurrent_start_registers_exactly_one_generation():
    async def scenario():
        store = Store()
        store.start_release = threading.Event()
        worker = pump(store)
        first = asyncio.create_task(worker.start())
        second = None
        try:
            await until(store.start_entered.is_set)
            second = asyncio.create_task(worker.start())
            await asyncio.sleep(.01)
            store.start_release.set()
            await asyncio.gather(first, second)
            assert store.starts == 1
            assert worker.generation == store.generation == 1
        finally:
            store.start_release.set()
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('through_run', [False, True])
def test_cancelled_registration_tracks_the_committed_generation_for_cleanup(through_run):
    async def scenario():
        store = Store()
        store.start_release = threading.Event()
        worker = pump(store)
        operation = asyncio.create_task(worker.run(asyncio.Event()) if through_run else worker.start())
        try:
            await until(store.start_entered.is_set)
            operation.cancel()
            await asyncio.sleep(.005)
            store.start_release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
            if not through_run:
                assert worker.generation == store.generation == 1
                await worker.aclose()
            assert worker.generation is None
            assert store.starts == store.stops == 1
            assert not store.live
        finally:
            store.start_release.set()
            await worker.aclose()
    asyncio.run(scenario())


def test_concurrent_ticks_keep_two_coroutine_bound():
    async def scenario():
        store = Store(4)
        store.claim_release = threading.Event()
        entered = []
        release = asyncio.Event()

        async def execute(token):
            entered.append(token.run_id)
            await release.wait()

        worker = pump(store, execute)
        await worker.start()
        first = asyncio.create_task(worker.tick())
        second = None
        try:
            await until(store.claim_entered.is_set)
            second = asyncio.create_task(worker.tick())
            await asyncio.sleep(.01)
            store.claim_release.set()
            await asyncio.gather(first, second)
            await until(lambda: len(entered) >= 2)
            assert len(entered) == len(worker._claims) == 2
            assert len(store.pending) == 2
        finally:
            store.claim_release.set()
            release.set()
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['poll_seconds', 'heartbeat_seconds', 'shutdown_seconds'])
@pytest.mark.parametrize('value', [0, -1, True, float('inf'), float('nan'), '1'])
def test_invalid_intervals_are_rejected(name, value):
    with pytest.raises(ValueError):
        pump(Store(), **{name: value})


def test_non_callable_executor_is_rejected():
    with pytest.raises(ValueError):
        pump(Store(), executor='user-supplied-command')


@pytest.mark.parametrize('raises', [False, True])
def test_misconfigured_callable_cannot_leave_an_unowned_claim(raises):
    async def scenario():
        store = Store(1)

        def execute(token):
            if raises:
                raise RuntimeError('Synthetic adapter configuration error')

        worker = pump(store, execute)
        try:
            await worker.tick()
            await until(lambda: bool(store.abandons))
            assert not store.claims
            assert store.outcomes == {'run-0': 'RECOVERY'}
            assert store.abandons[0][1] == 'executor_failed'
        finally:
            await worker.aclose()
    asyncio.run(scenario())


def sqlite_store(path):
    """Minimal registered Run, exclusively in pytest's temporary directory."""
    from webagent.db import connect, migrate, transaction
    from webagent.db.repository import add_contract, create_run, create_task

    migrate(path)
    with connect(path) as db, transaction(db):
        create_task(db, task_id='pump-task', instruction='Synthetic scheduler test',
                    requested_fields=['contract'])
        add_contract(db, {'schema_version': 'm0-contract-v1', 'task_id': 'pump-task',
                          'contract_version': 1, 'scenario': 'research',
                          'objective': 'Synthetic dispatch pump recovery',
                          'parameters': {'query': 'scheduler'}, 'sources': ['local-fixture'],
                          'action_policy': {'mode': 'read_only'}})
        create_run(db, run_id='pump-run', task_id='pump-task', contract_version=1,
                   graph_version='fixture-v1', graph_state_schema_version='fixture-state-v1',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
    store = SchedulerStore(path)
    store.enqueue('pump-run', (Resource.site_identity('fixture'), Resource.browser_context('pump-run')),
                  expected_state_version=0)
    return store


def test_real_sqlite_return_is_durable_recovery_and_old_epoch_cannot_resume(tmp_path):
    async def scenario():
        store = sqlite_store(tmp_path / 'business.sqlite3')
        tokens = []

        async def execute(token):
            tokens.append(token)

        worker = pump(store, execute)
        try:
            await worker.tick()
            await until(lambda: bool(tokens))
            await until(lambda: all(task.done() for task in worker._claims.values()))
            snapshot = store.snapshot()
            assert snapshot['queue'][0]['status'] == 'RECOVERY'
            assert snapshot['queue'][0]['state'] == 'RECONCILING'
            assert snapshot['queue'][0]['epoch'] > tokens[0].epoch
            assert not any(lease['resource_type'] == 'active_slot' for lease in snapshot['leases'])
            assert any(lease['logical_hold'] for lease in snapshot['leases'])
            with pytest.raises(BusinessError):
                store.validate(tokens[0], allow_reconciling=True)
        finally:
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('fails', [False, True])
def test_real_sqlite_unreleased_executor_stops_pump_instead_of_redispatching_recovery(tmp_path, fails):
    """Recovery cannot invoke the same unqualified executor in a retry loop."""
    async def scenario():
        store = sqlite_store(tmp_path / 'business.sqlite3')
        dispatches = []

        async def execute(token):
            dispatches.append(token)
            if fails:
                raise RuntimeError('Synthetic executor interruption')

        worker = pump(store, execute)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(worker.run(asyncio.Event()), .8)
        assert len(dispatches) == 1
        assert worker._failed and worker._closing
        snapshot = store.snapshot()
        assert snapshot['queue'][0]['status'] == 'RECOVERY'
        assert snapshot['queue'][0]['state'] == 'RECONCILING'
        assert snapshot['workers'][0]['state'] == 'STOPPED'
        assert not any(lease['resource_type'] == 'active_slot' for lease in snapshot['leases'])
        with pytest.raises(BusinessError):
            store.validate(dispatches[0], allow_reconciling=True)
    asyncio.run(scenario())


def test_real_sqlite_stop_then_new_generation_requires_reconciliation(tmp_path):
    async def scenario():
        store = sqlite_store(tmp_path / 'business.sqlite3')
        first_token = []

        async def first_execute(token):
            first_token.append(token)
            await asyncio.Future()

        first = pump(store, first_execute)
        await first.tick()
        await until(lambda: bool(first_token))
        await first.aclose()
        assert store.snapshot()['workers'][0]['state'] == 'STOPPED'
        assert store.snapshot()['queue'][0]['status'] == 'RECOVERY'
        completed = asyncio.Event()

        async def second_execute(token):
            assert token.worker_generation > first_token[0].worker_generation
            assert token.epoch > first_token[0].epoch
            with pytest.raises(BusinessError):
                store.validate(token)
            store.validate(token, allow_reconciling=True)
            reconciled = store.reconcile(token.run_id, token.state_version)
            store.finish(reconciled, 'CANCELLED')
            completed.set()

        second = pump(store, second_execute)
        try:
            await second.tick()
            await asyncio.wait_for(completed.wait(), .8)
            await until(lambda: all(task.done() for task in second._claims.values()))
            snapshot = store.snapshot()
            assert snapshot['queue'][0]['status'] == 'FINISHED'
            assert snapshot['queue'][0]['state'] == 'CANCELLED'
            assert snapshot['leases'] == []
            assert snapshot['context_reservations'] == []
            assert not second._failed
        finally:
            await second.aclose()
        with pytest.raises(BusinessError):
            store.validate(first_token[0], allow_reconciling=True)
    asyncio.run(scenario())


def test_materialized_context_recovery_token_does_not_open_normal_browser_access(tmp_path):
    """A live fake context is not authorization after a durable wait/resume."""
    from webagent.config import Settings
    from webagent.sessions.manager import ManagedBrowser, _Context
    from webagent.sessions.models import SessionOwner
    from webagent.sessions.store import SessionRegistry

    async def scenario():
        store = sqlite_store(tmp_path / 'business.sqlite3')
        generation = store.start_worker('fixture-worker')
        token = store.claim('fixture-worker', generation)
        registry = SessionRegistry(store.path)
        owner = SessionOwner('run', token.run_id, 'fixture')
        session = registry.reserve('fixture-manager', owner, execution_token=token)
        registry.opened(session.session_id, 'fixture-manager', execution_token=token)

        class Browser:
            def is_connected(self):
                return True

        class Page:
            def is_closed(self):
                return False

        class Context:
            pages = [Page()]

        manager = ManagedBrowser(Settings(tmp_path))
        manager.manager_id = 'fixture-manager'
        manager._started = True
        context = Context()
        manager._contexts[session.session_id] = _Context(context, Browser(), owner)
        assert await manager.context(session.session_id, owner, execution_token=token) is context
        waiting = store.defer(token, 'PAUSED')
        store.resume(token.run_id, waiting['run_state_version'])
        recovered = store.claim('fixture-worker', generation)
        store.validate(recovered, allow_reconciling=True)
        assert recovered.epoch > token.epoch
        for qualification in (token, recovered, None):
            with pytest.raises(BusinessError):
                await manager.context(session.session_id, owner, execution_token=qualification)
        assert registry.get(session.session_id, owner).state == 'OPEN'
        assert len(store.snapshot()['context_reservations']) == 1
        store.stop_worker('fixture-worker', generation)
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['INTENT', 'UNKNOWN'])
def test_unresolved_write_without_quarantine_blocks_normal_authority_and_release(tmp_path, status):
    from webagent.db import connect, transaction
    from webagent.db.repository import utc_text

    store = sqlite_store(tmp_path / 'business.sqlite3')
    generation = store.start_worker('fixture-worker')
    token = store.claim('fixture-worker', generation)
    now = utc_text()
    with connect(store.path) as db, transaction(db):
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('pending-operation','pending-business-key','pump-task','pump-run',
                   'synthetic-target','synthetic-change','synthetic-identity','v1',?,?,?)''',
                   (status, now, now))
        assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 0
    with pytest.raises(BusinessError):
        store.validate(token)
    recovering = store.abandon(token)
    before = store.snapshot()['leases']
    with pytest.raises(BusinessError):
        store.reconcile(token.run_id, recovering['run_state_version'], release_resources=True)
    assert store.snapshot()['leases'] == before
    assert before and all(lease['logical_hold'] for lease in before)
    store.stop_worker('fixture-worker', generation)


def test_recovery_resource_release_cannot_clear_persisted_human_control(tmp_path):
    from webagent.db import connect, transaction

    store = sqlite_store(tmp_path / 'business.sqlite3')
    generation = store.start_worker('fixture-worker')
    token = store.claim('fixture-worker', generation)
    recovering = store.abandon(token)
    with connect(store.path) as db, transaction(db):
        db.execute("UPDATE resource_leases SET control_owner='human',logical_hold=1 WHERE holder_run_id='pump-run'")
    before = store.snapshot()['leases']
    with pytest.raises(BusinessError):
        store.reconcile(token.run_id, recovering['run_state_version'], release_resources=True)
    assert store.snapshot()['leases'] == before
    assert before and all(lease['control_owner'] == 'human' for lease in before)
    store.stop_worker('fixture-worker', generation)


@pytest.mark.parametrize('outcome', ['PAUSED', 'CANCELLED'])
def test_real_sqlite_settlement_is_read_only_cleanup_proof_not_authority(tmp_path, outcome):
    from webagent.db import connect, transaction
    store = sqlite_store(tmp_path / 'business.sqlite3')
    generation = store.start_worker('fixture-worker')
    token = store.claim('fixture-worker', generation)
    assert store.settlement(token) is False
    (store.defer(token, outcome) if outcome == 'PAUSED' else store.finish(token, outcome))
    assert store.settlement(token) is True
    assert store.settlement(replace(token, worker_generation=generation + 1)) is False
    assert store.settlement(replace(token, epoch=token.epoch + 1)) is False
    before = store.snapshot()
    assert store.settlement(token) is True and store.snapshot() == before
    for reconcile in (False, True):
        with pytest.raises(BusinessError):
            store.validate(token, allow_reconciling=reconcile)
    # A changed queue revision without its immutable waiting/finished receipt
    # cannot retain the cleanup grace, even though the Run remains settled.
    with connect(store.path) as db, transaction(db):
        db.execute("UPDATE scheduler_queue SET revision=revision+1 WHERE run_id='pump-run'")
    assert store.settlement(token) is False
    store.stop_worker('fixture-worker', generation)


@pytest.mark.parametrize('condition', ['worker_stopped', 'worker_generation', 'worker_expired', 'token_expired', 'recovery'])
def test_real_sqlite_settlement_rejects_revocation_expiry_and_new_generation(tmp_path, condition):
    from datetime import datetime, timedelta, timezone
    from webagent.db import connect, transaction
    from webagent.db.repository import utc_text
    store = sqlite_store(tmp_path / 'business.sqlite3')
    generation = store.start_worker('fixture-worker')
    token = store.claim('fixture-worker', generation)
    store.defer(token, 'PAUSED')
    if condition == 'worker_stopped':
        store.stop_worker('fixture-worker', generation)
    elif condition == 'worker_generation':
        generation = store.start_worker('fixture-worker')
    elif condition == 'worker_expired':
        store.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=60)
    elif condition == 'token_expired':
        token = replace(token, expires_at=utc_text(datetime.now(timezone.utc) - timedelta(seconds=1)))
    else:
        waiting = store.snapshot()['queue'][0]
        store.resume(token.run_id, waiting['run_state_version'])
    assert store.settlement(token) is False
    store.stop_worker('fixture-worker', generation)


@pytest.mark.parametrize('outcome', ['PAUSED', 'CANCELLED'])
def test_real_sqlite_post_commit_grace_finishes_framework_save_and_client_cleanup(tmp_path, outcome):
    from typing import TypedDict
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import StateGraph, START, END
    class Saved(TypedDict):
        cleaned: bool

    async def scenario():
        store = sqlite_store(tmp_path / 'business.sqlite3')
        closed, returned, refused, settled = [], [], [], asyncio.Event()
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            await saver.setup()
            async def execute(token):
                async def commit(state):
                    (store.defer(token, outcome) if outcome == 'PAUSED' else store.finish(token, outcome))
                    settled.set()
                    await asyncio.sleep(.08)
                    with pytest.raises(BusinessError):
                        store.validate(token, allow_reconciling=True)
                    refused.append(True)
                    return {'cleaned': True}
                graph = StateGraph(Saved)
                graph.add_node('commit', commit)
                graph.add_edge(START, 'commit')
                graph.add_edge('commit', END)
                compiled = graph.compile(checkpointer=saver)
                await compiled.ainvoke({'cleaned': False}, {'configurable': {'thread_id': token.run_id}}, durability='sync')
                await asyncio.sleep(.02)
                closed.append(True)
                returned.append(True)
            worker = pump(store, execute, shutdown_seconds=.3, deadline_seconds=.005, heartbeat_seconds=.01)
            try:
                await worker.tick()
                await asyncio.wait_for(settled.wait(), 2)
                await until(lambda: all(task.done() for task in worker._claims.values()), seconds=2)
                assert returned == closed == refused == [True] and not worker._failed
                saved = await saver.aget_tuple({'configurable': {'thread_id': 'pump-run'}})
                assert saved.checkpoint['channel_values']['cleaned'] is True
                assert store.snapshot()['queue'][0]['state'] == outcome
            finally:
                await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['PAUSED', 'CANCELLED'])
def test_real_sqlite_post_commit_grace_has_fixed_deadline_then_cancels_hung_tail(tmp_path, outcome):
    async def scenario():
        store = sqlite_store(tmp_path / 'business.sqlite3')
        settled, cancelled = asyncio.Event(), asyncio.Event()
        entered, forbidden = [], []
        async def execute(token):
            (store.defer(token, outcome) if outcome == 'PAUSED' else store.finish(token, outcome))
            entered.append(asyncio.get_running_loop().time())
            settled.set()
            try:
                await asyncio.Future()
            finally:
                with pytest.raises(BusinessError):
                    store.validate(token, allow_reconciling=True)
                forbidden.append(True)
                cancelled.set()
        worker = pump(store, execute, shutdown_seconds=.08, deadline_seconds=.005, heartbeat_seconds=.01)
        try:
            await worker.tick()
            await asyncio.wait_for(settled.wait(), 2)
            await asyncio.wait_for(cancelled.wait(), 1)
            elapsed = asyncio.get_running_loop().time() - entered[0]
            assert .06 <= elapsed < .5 and forbidden == [True]
            await until(lambda: all(task.done() for task in worker._claims.values()))
            assert store.snapshot()['queue'][0]['state'] == outcome
        finally:
            await worker.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('revocation', ['new_generation', 'resumed_recovery'])
def test_post_commit_grace_does_not_survive_new_generation_or_recovery(tmp_path, revocation):
    async def scenario():
        store = sqlite_store(tmp_path / 'business.sqlite3')
        settled, cancelled = asyncio.Event(), asyncio.Event()
        async def execute(token):
            store.defer(token, 'PAUSED')
            settled.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        worker = pump(store, execute, shutdown_seconds=.5, deadline_seconds=.005, heartbeat_seconds=.01)
        new_generation = None
        try:
            await worker.tick()
            await asyncio.wait_for(settled.wait(), 2)
            if revocation == 'new_generation':
                new_generation = store.start_worker(worker.worker_id)
            else:
                queue = store.snapshot()['queue'][0]
                store.resume('pump-run', queue['run_state_version'])
            await asyncio.wait_for(cancelled.wait(), .25)
            await until(lambda: all(task.done() for task in worker._claims.values()))
        finally:
            if new_generation is None:
                await worker.aclose()
            else:
                with pytest.raises(BusinessError):
                    await worker.aclose()
                store.stop_worker(worker.worker_id, new_generation)
    asyncio.run(scenario())
