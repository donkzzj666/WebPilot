"""Budget authority composed with real queue claims, waits and SQLite races."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import multiprocessing
import sqlite3

import pytest

from webagent.db import connect, transaction
from webagent.db.repository import add_contract, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.tasks.compiler import compile_draft


class Clock:
    domain = 'budget-scheduler-fixture'

    def __init__(self, wall=None):
        self.wall = wall or datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
        self.ns = 0

    def utcnow(self):
        return self.wall

    def monotonic_ns(self):
        return self.ns

    def advance(self, seconds, *, wall_seconds=None):
        self.ns += round(seconds * 1_000_000_000)
        self.wall += timedelta(seconds=seconds if wall_seconds is None else wall_seconds)


def queue_run(path, store, name, *, queue_class='ordinary', source_kind=None,
              parent=None, limits=None, available_at=None, task_id=None):
    task_id = task_id or ('task-' + name)
    now = utc_text(store.budgets.clock.utcnow())
    parameters = ({'source_id': 'local-fixture', 'source_kind': source_kind,
                   'baseline': False, 'scheduled_at': now, 'confirmed_boundary': None}
                  if source_kind else {'queries': ['fixture'], 'topic_criteria': ['fixture'],
                                      'cutoff_at': now, 'max_items': 3})
    contract = compile_draft(
        {'instruction': 'Synthetic scheduler budget fixture',
         'scenario': 'monitoring' if source_kind else 'research',
         'source_ids': ['local-fixture'], 'parameters': parameters},
        task_id=task_id, version=1, created_at=now,
        provenance=[{'origin': 'api', 'reference': 'budget-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    contract['budget_profile'].update(limits or {})
    with connect(path) as db, transaction(db):
        if not db.execute('SELECT 1 FROM tasks WHERE task_id=?', (task_id,)).fetchone():
            create_task(db, task_id=task_id, instruction=contract['original_instruction'],
                        requested_fields=['contract'])
            add_contract(db, contract)
        create_run(db, run_id=name, task_id=task_id, contract_version=1,
                   graph_version='graph-v1', graph_state_schema_version='state-v1',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64,
                   parent_run_id=parent)
    store.enqueue(name, [Resource.site_identity('local-fixture', name),
                         Resource.browser_context(name)], expected_state_version=0,
                  queue_class=queue_class, available_at=available_at)
    return contract


def make_store(path, clock=None):
    from webagent.budgets.store import BudgetStore
    clock = clock or Clock()
    return SchedulerStore(path, budgets=BudgetStore(path, clock=clock), clock=clock.utcnow)


def started(store, name='worker'):
    return store.start_worker(name)


def consume_ordinary(path, store, generation, count, *, prefix='ordinary'):
    for index in range(count):
        name = f'{prefix}-{index}'
        queue_run(path, store, name)
        token = store.claim('worker', generation)
        assert token.run_id == name
        store.finish(token)


def ledger(path):
    with connect(path) as db:
        return [dict(row) for row in db.execute('SELECT * FROM quota_debits ORDER BY debited_at,debit_id')]


def accounting_snapshot(path):
    with connect(path) as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('run_budgets', 'budget_timers', 'budget_attempts', 'quota_buckets',
                              'quota_debits', 'task_events')}


def test_first_debit_state_leases_reservation_and_event_commit_as_one_transaction(database):
    store = make_store(database)
    generation = started(store)
    queue_run(database, store, 'first')
    assert ledger(database) == []
    token = store.claim('worker', generation)
    with connect(database) as db:
        run = db.execute("SELECT state,state_version,started_at FROM runs WHERE run_id='first'").fetchone()
        budget = db.execute("SELECT quota_debit_id FROM run_budgets WHERE run_id='first'").fetchone()
        assert tuple(run[:2]) == ('RUNNING', token.state_version)
        assert run['started_at'] is not None
        assert budget['quota_debit_id'] == ledger(database)[0]['debit_id']
        assert db.execute("SELECT count(*) FROM resource_leases WHERE holder_run_id='first'").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM scheduler_context_reservations WHERE run_id='first'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM task_events WHERE run_id='first' AND event_type='state_changed'").fetchone()[0] == 1


@pytest.mark.parametrize('failure_point', ['lease', 'queue'])
def test_failed_claim_rolls_back_debit_run_event_and_all_resources(database, failure_point):
    store = make_store(database)
    generation = started(store)
    queue_run(database, store, 'rollback')
    with connect(database) as db:
        statement = ("CREATE TRIGGER fixture_fail_claim BEFORE INSERT ON resource_leases "
                     "WHEN NEW.resource_type='browser_context' BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
                     if failure_point == 'lease' else
                     "CREATE TRIGGER fixture_fail_claim BEFORE UPDATE ON scheduler_queue "
                     "WHEN NEW.status='ACTIVE' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        db.execute(statement)
    with pytest.raises(sqlite3.IntegrityError):
        store.claim('worker', generation)
    assert ledger(database) == []
    assert not store.snapshot()['leases']
    assert not store.snapshot()['context_reservations']
    with connect(database) as db:
        assert tuple(db.execute("SELECT state,state_version,started_at FROM runs WHERE run_id='rollback'").fetchone()) == ('QUEUED', 0, None)
        assert db.execute("SELECT count(*) FROM task_events WHERE run_id='rollback' AND event_type='state_changed'").fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM quota_buckets').fetchone()[0] == 0


def test_ordinary_43_is_skipped_while_reserved_monitor_and_webarena_can_claim(database):
    store = make_store(database)
    generation = started(store)
    consume_ordinary(database, store, generation, 42)
    queue_run(database, store, 'ordinary-43')
    assert store.claim('worker', generation) is None
    queue_run(database, store, 'monitor', queue_class='monitoring', source_kind='cisa_kev')
    token = store.claim('worker', generation)
    assert token.run_id == 'monitor'
    store.finish(token)
    queue_run(database, store, 'webarena', queue_class='webarena')
    arena = store.claim('worker', generation)
    assert arena.run_id == 'webarena'
    assert len(ledger(database)) == 43
    assert not any(item['run_id'] == 'ordinary-43' for item in ledger(database))


def test_total_50_rejects_51_and_monitor_source_has_four_reserved_slots(database):
    store = make_store(database)
    generation = started(store)
    consume_ordinary(database, store, generation, 42)
    for kind in ('cisa_kev', 'security_community'):
        for index in range(4):
            name = kind + '-' + str(index)
            queue_run(database, store, name, queue_class='monitoring', source_kind=kind)
            token = store.claim('worker', generation)
            assert token.run_id == name
            store.finish(token)
    queue_run(database, store, 'fifty-one', queue_class='monitoring', source_kind='cisa_kev')
    assert store.claim('worker', generation) is None
    assert len(ledger(database)) == 50
    with connect(database) as db:
        assert db.execute('SELECT used FROM quota_buckets').fetchone()[0] == 50
        assert db.execute("SELECT state,started_at FROM runs WHERE run_id='fifty-one'").fetchone()['state'] == 'QUEUED'


def test_one_monitor_source_cannot_consume_other_sources_reserved_slots(database):
    store = make_store(database)
    generation = started(store)
    for index in range(4):
        name = 'cisa-reserved-' + str(index)
        queue_run(database, store, name, queue_class='monitoring', source_kind='cisa_kev')
        store.finish(store.claim('worker', generation))
    queue_run(database, store, 'cisa-fifth', queue_class='monitoring', source_kind='cisa_kev')
    queue_run(database, store, 'community-first', queue_class='monitoring', source_kind='security_community')
    assert store.claim('worker', generation).run_id == 'community-first'
    assert not any(row['run_id'] == 'cisa-fifth' for row in ledger(database))


def test_monitor_reexecution_uses_public_remainder_instead_of_reserved_source_allowance(database):
    store = make_store(database)
    generation = started(store)
    queue_run(database, store, 'original', queue_class='monitoring', source_kind='cisa_kev')
    store.finish(store.claim('worker', generation))
    queue_run(database, store, 'rerun', queue_class='monitoring', source_kind='cisa_kev',
              parent='original', task_id='task-original')
    store.finish(store.claim('worker', generation))
    assert {row['run_id']: row['debit_kind'] for row in ledger(database)} == {
        'original': 'monitoring', 'rerun': 'ordinary'}
    consume_ordinary(database, store, generation, 41)
    queue_run(database, store, 'second-rerun', queue_class='monitoring', source_kind='cisa_kev',
              parent='original', task_id='task-original')
    assert store.claim('worker', generation) is None
    queue_run(database, store, 'other-monitor', queue_class='monitoring', source_kind='cisa_kev')
    assert store.claim('worker', generation).run_id == 'other-monitor'


def test_original_run_resume_across_shanghai_midnight_keeps_single_debit(database):
    clock = Clock(datetime(2026, 9, 30, 15, 59, 59, tzinfo=timezone.utc))
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'same-run')
    token = store.claim('worker', generation)
    clock.advance(.2)
    paused = store.defer(token, 'PAUSED')
    clock.advance(3)
    store.heartbeat_worker('worker', generation)
    store.resume('same-run', paused['run_state_version'])
    token = store.claim('worker', generation)
    token = store.reconcile(token.run_id, token.state_version)
    assert ledger(database)[0]['quota_date'] == '2026-09-30'
    assert len(ledger(database)) == 1
    assert store.budgets.status('same-run')['active_ms'] == 200
    store.finish(token)
    queue_run(database, store, 'new-day')
    store.finish(store.claim('worker', generation))
    assert {row['quota_date'] for row in ledger(database)} == {'2026-09-30', '2026-10-01'}


@pytest.mark.parametrize('target', ['PAUSED', 'WAITING_HANDOFF', 'WAITING_CI', 'WAITING_SITE'])
def test_wait_modes_charge_correct_budget_without_reset_on_repeated_resume(database, target):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'wait')
    token = store.claim('worker', generation)
    for iteration in range(3):
        clock.advance(2)
        token = store.heartbeat(token)
        args = {'next_eligible_at': clock.utcnow() + timedelta(seconds=5)} if target == 'WAITING_SITE' else {}
        waiting = store.defer(token, target, **args)
        clock.advance(5)
        store.heartbeat_worker('worker', generation)
        store.resume('wait', waiting['run_state_version'])
        token = store.claim('worker', generation)
        token = store.reconcile('wait', token.state_version)
        assert store.budgets.status('wait')['active_ms'] == (iteration + 1) * (7000 if target == 'WAITING_SITE' else 2000)
        assert store.budgets.status('wait')['ci_wait_ms'] == (iteration + 1) * (5000 if target == 'WAITING_CI' else 0)
    assert len(ledger(database)) == 1


def test_ci_wait_twenty_minutes_is_cumulative_across_multiple_resume_rounds(database):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'ci-rounds')
    token = store.claim('worker', generation)
    for iteration in range(2):
        waiting = store.defer(token, 'WAITING_CI')
        # The real Worker continues its generation heartbeat while waiting;
        # advance the virtual clock in intervals shorter than that lease.
        for _ in range(30):
            clock.advance(20)
            store.heartbeat_worker('worker', generation)
        if iteration == 0:
            store.resume('ci-rounds', waiting['run_state_version'])
            token = store.claim('worker', generation)
            token = store.reconcile('ci-rounds', token.state_version)
    store.sweep_budget_due()
    status = store.budgets.status('ci-rounds')
    assert status['ci_wait_ms'] == 1_200_000
    assert status['active_ms'] == 0
    assert status['exhausted'] and status['reason'] == 'ci_wait'
    with connect(database) as db:
        assert db.execute("SELECT state FROM runs WHERE run_id='ci-rounds'").fetchone()[0] == 'FAILED'
    assert len(ledger(database)) == 1


@pytest.mark.parametrize('target', ['WAITING_CI', 'WAITING_SITE'])
def test_resume_queue_time_stops_old_wait_meter_before_a_slot_is_acquired(database, target):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'queued-resume')
    token = store.claim('worker', generation)
    clock.advance(2)
    waiting = store.defer(token, target)
    clock.advance(4)
    store.resume(token.run_id, waiting['run_state_version'])
    settled = store.budgets.status(token.run_id)
    clock.advance(10)
    store.heartbeat_worker('worker', generation)
    still_queued = store.budgets.status(token.run_id)
    assert settled['active_ms'] == still_queued['active_ms'] == (6000 if target == 'WAITING_SITE' else 2000)
    assert settled['ci_wait_ms'] == still_queued['ci_wait_ms'] == (4000 if target == 'WAITING_CI' else 0)
    assert still_queued['mode'] == 'inactive'
    assert len(ledger(database)) == 1


@pytest.mark.parametrize('target', ['SUCCEEDED', 'PARTIAL'])
def test_current_finish_cannot_bypass_a_due_deadline_to_claim_a_delivery_outcome(database, target):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'late-finish', limits={'max_active_seconds': 1})
    token = store.claim('worker', generation)
    if target == 'SUCCEEDED':
        # The verification adapter is not implemented yet. Prepare the legal
        # VERIFYING state and its trusted queue binding in one local fixture
        # transaction, so refusal is specifically the budget check.
        from webagent.state import transition_in_transaction
        with connect(database) as db, transaction(db):
            transition_in_transaction(db, run_id=token.run_id, expected_state_version=token.state_version,
                                      target='VERIFYING')
            row = store._row(db, token.run_id)
            store._change(db, row, utc_text(clock.utcnow()), 'reconciled', run_state_version=row['run_state_version'])
        token = store.refresh_qualification(token)
    clock.advance(1)
    before = store.snapshot()
    with pytest.raises(BusinessError) as due:
        store.finish(token, target)
    assert due.value.code == 'BUDGET_EXCEEDED' and due.value.field == 'active_time'
    assert store.snapshot() == before
    store.sweep_budget_due()
    with connect(database) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (token.run_id,)).fetchone()[0] == 'FAILED'


@pytest.mark.parametrize('target', ['SUCCEEDED', 'PARTIAL'])
def test_unresolved_write_prevents_delivery_outcome_even_with_remaining_budget(database, target):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'uncertain-finish')
    token = store.claim('worker', generation)
    if target == 'SUCCEEDED':
        from webagent.state import transition_in_transaction
        with connect(database) as db, transaction(db):
            transition_in_transaction(db, run_id=token.run_id, expected_state_version=token.state_version,
                                      target='VERIFYING')
            row = store._row(db, token.run_id)
            store._change(db, row, utc_text(clock.utcnow()), 'reconciled', run_state_version=row['run_state_version'])
        token = store.refresh_qualification(token)
    with connect(database) as db, transaction(db):
        now = utc_text(clock.utcnow())
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('pending','pending','task-uncertain-finish','uncertain-finish','fixture','fixture change',
                   'fixture','v1','UNKNOWN',?,?)''', (now, now))
    before = store.snapshot()
    with pytest.raises(BusinessError) as unresolved:
        store.finish(token, target)
    assert unresolved.value.code == 'RESOURCE_CONFLICT'
    assert store.snapshot() == before
    assert not store.budgets.status(token.run_id)['exhausted']
    with connect(database) as db:
        assert db.execute("SELECT status FROM write_intents WHERE operation_id='pending'").fetchone()[0] == 'UNKNOWN'


@pytest.mark.parametrize('stage', ['claim', 'consume'])
def test_expired_scoped_cooldown_cannot_hide_a_legacy_site_block(database, stage):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'blocked-site')
    token = store.claim('worker', generation) if stage == 'consume' else None
    with connect(database) as db, transaction(db):
        db.execute("INSERT INTO site_gates(site_id,state,blocked_reason,updated_at) VALUES('local-fixture','BLOCKED','fixture challenge',?)",
                   (utc_text(clock.utcnow()),))
        db.execute("INSERT INTO site_gates(site_id,state,next_eligible_at,updated_at) VALUES('public:local-fixture','COOLDOWN',?,?)",
                   (utc_text(clock.utcnow() - timedelta(seconds=1)), utc_text(clock.utcnow())))
    if stage == 'claim':
        assert store.claim('worker', generation) is None
        assert ledger(database) == [] and not store.snapshot()['leases']
    else:
        with pytest.raises(BusinessError) as blocked:
            store.budgets.consume(token, kind='action', attempt_id='blocked-nav',
                                  site_id='local-fixture', navigation=True)
        assert blocked.value.code == 'SITE_THROTTLED'
        assert store.budgets.status(token.run_id)['actions_used'] == 0
        with connect(database) as db:
            assert db.execute('SELECT count(*) FROM budget_attempts').fetchone()[0] == 0


def test_active_clock_backward_wall_jump_and_recovery_never_regress_usage(database):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'monotonic')
    token = store.claim('worker', generation)
    clock.advance(10, wall_seconds=-300)
    store.heartbeat(token)
    first = store.budgets.status('monotonic')['active_ms']
    assert first == 10_000
    clock.advance(7, wall_seconds=300)
    generation = started(store, 'replacement')
    after = store.budgets.status('monotonic')['active_ms']
    assert after >= 17_000
    assert after >= first
    assert len(ledger(database)) == 1


@pytest.mark.parametrize('boundary', ['epoch', 'worker', 'generation', 'scope', 'expired', 'human', 'unbound_state'])
@pytest.mark.parametrize('method', ['refresh_qualification', 'runtime_budget', 'runtime_heartbeat'])
def test_runtime_qualification_refresh_cannot_bypass_authority_boundaries(database, boundary, method):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'refresh')
    token = store.claim('worker', generation)
    candidate = token
    if boundary == 'epoch':
        candidate = replace(token, epoch=token.epoch + 1)
    elif boundary == 'worker':
        candidate = replace(token, worker_id='another-worker')
    elif boundary == 'generation':
        candidate = replace(token, worker_generation=token.worker_generation + 1)
    elif boundary == 'scope':
        candidate = replace(token, resources=token.resources[:-1])
    elif boundary == 'expired':
        clock.advance(31)
    elif boundary == 'human':
        with connect(database) as db, transaction(db):
            db.execute("UPDATE resource_leases SET control_owner='human' WHERE holder_run_id='refresh' AND resource_type='site_identity'")
    else:
        from webagent.state import transition
        transition(database, run_id='refresh', expected_state_version=token.state_version, target='VERIFYING')
    before = store.snapshot()
    accounting_before = accounting_snapshot(database)
    with pytest.raises(BusinessError):
        getattr(store, method)(candidate)
    assert store.snapshot() == before
    assert accounting_snapshot(database) == accounting_before
    assert store.budgets.status('refresh')['actions_used'] == 0


def test_handoff_deadline_is_first_window_and_cannot_be_extended_by_resume(database):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'handoff', limits={'max_handoff_seconds': 60})
    token = store.claim('worker', generation)
    waiting = store.defer(token, 'WAITING_HANDOFF', control_owner='human')
    original_deadline = store.budgets.status('handoff')['handoff_deadline']
    clock.advance(5)
    store.resume('handoff', waiting['run_state_version'])
    token = store.claim('worker', generation)
    token = store.reconcile('handoff', token.state_version)
    try:
        store.defer(token, 'WAITING_HANDOFF', control_owner='human',
                    handoff_deadline=clock.utcnow() + timedelta(hours=23))
    except BusinessError:
        # Explicitly refusing an extension is also valid; the persisted first
        # deadline must remain fixed across either result.
        pass
    assert store.budgets.status('handoff')['handoff_deadline'] == original_deadline
    with connect(database) as db:
        assert db.execute("SELECT handoff_deadline FROM runs WHERE run_id='handoff'").fetchone()[0] == original_deadline


def test_waiting_site_deadline_preserves_cooldown_and_retains_unknown_write_quarantine(database):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'unknown', limits={'max_active_seconds': 10})
    token = store.claim('worker', generation)
    store.wait_site(token, 'local-fixture', 9)
    key = Resource.site_identity('local-fixture', 'unknown').resource_key
    with connect(database) as db, transaction(db):
        now = utc_text(clock.utcnow())
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('uncertain','uncertain','task-unknown','unknown',?,'fixture change','fixture','v1','UNKNOWN',?,?)''',
                   (key, now, now))
        db.execute('INSERT INTO resource_quarantines VALUES(?,?,?)', (key, 'uncertain', now))
    clock.advance(11)
    store.heartbeat_worker('worker', generation)
    store.sweep_budget_due()
    with connect(database) as db:
        run = db.execute("SELECT * FROM runs WHERE run_id='unknown'").fetchone()
        assert run['state'] == 'FAILED'
        gate = db.execute("SELECT * FROM site_gates WHERE site_id='public:local-fixture'").fetchone()
        assert gate['next_eligible_at'] == utc_text(clock.utcnow() - timedelta(seconds=2))
        assert db.execute("SELECT status FROM write_intents WHERE operation_id='uncertain'").fetchone()[0] == 'UNKNOWN'
        assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM resource_leases WHERE resource_key=?', (key,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM resource_leases WHERE holder_run_id='unknown' AND resource_type='active_slot'").fetchone()[0] == 0
    assert store.budgets.status('unknown')['exhausted']


def test_retry_after_longer_than_remaining_budget_stops_without_erasing_cooldown(database):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'slow-site', limits={'max_active_seconds': 10})
    token = store.claim('worker', generation)
    clock.advance(8)
    store.heartbeat(token)
    store.wait_site(token, 'local-fixture', 5)
    with connect(database) as db:
        run = db.execute("SELECT * FROM runs WHERE run_id='slow-site'").fetchone()
        assert run['state'] == 'FAILED'
        gate = db.execute("SELECT * FROM site_gates WHERE site_id LIKE '%local-fixture'").fetchone()
        assert gate is not None and gate['state'] == 'COOLDOWN'
        assert gate['next_eligible_at'] == utc_text(clock.utcnow() + timedelta(seconds=5))
    assert len(ledger(database)) == 1
    assert not any(item['resource_type'] == 'active_slot' for item in store.snapshot()['leases'])


def test_site_wait_is_shared_across_accounts_before_new_run_debit(database):
    clock = Clock()
    store = make_store(database, clock)
    generation = started(store)
    queue_run(database, store, 'first-site')
    first = store.claim('worker', generation)
    store.wait_site(first, 'local-fixture', 5)
    queue_run(database, store, 'second-account')
    assert store.claim('worker', generation) is None
    assert len(ledger(database)) == 1
    clock.advance(5)
    store.heartbeat_worker('worker', generation)
    assert store.claim('worker', generation).run_id == 'second-account'


def _quota_claim_child(path, generation, output):
    try:
        store = make_store(path)
        token = store.claim('worker', generation)
        output.put(token.run_id if token else None)
    except BaseException as error:
        output.put(type(error).__name__)


def test_four_processes_cannot_take_same_last_ordinary_quota(database):
    store = make_store(database)
    generation = started(store)
    consume_ordinary(database, store, generation, 41)
    for number in range(4):
        queue_run(database, store, 'contender-' + str(number))
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    children = [context.Process(target=_quota_claim_child, args=(database, generation, output)) for _ in range(4)]
    for child in children:
        child.start()
    for child in children:
        child.join(20)
        assert child.exitcode == 0
    results = [output.get(timeout=5) for _ in children]
    assert results.count(None) == 3
    assert [value for value in results if value is not None] == ['contender-0']
    assert len(ledger(database)) == 42
    assert sum(row['status'] == 'ACTIVE' for row in store.snapshot()['queue']) == 1
