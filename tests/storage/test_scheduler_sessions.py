"""One physical/reserved browser pool and fresh qualifications in real SQLite."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Barrier

import pytest

from conftest import seed
from webagent.config import Settings
from webagent.db import connect, transaction
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.sessions.manager import ManagedBrowser, _Context
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry


def queued(database, store, run_id, *, site='github', identity=None):
    with connect(database) as db, transaction(db):
        seed(db, task_id='task-' + run_id, run_id=run_id)
    store.enqueue(run_id, [Resource.site_identity(site, identity), Resource.browser_context(run_id)],
                  expected_state_version=0)
    return SessionOwner('run', run_id, site, identity)


def login(index, site='login-fixture'):
    return SessionOwner('login', 'login-' + str(index), site)


def capacity(database):
    with connect(database) as db:
        return db.execute("""SELECT
            (SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING'))+
            (SELECT count(*) FROM scheduler_context_reservations WHERE session_id IS NULL)""").fetchone()[0]


def setup_store(database):
    store = SchedulerStore(database, lease_seconds=300)
    generation = store.start_worker('worker-one')
    return store, generation, SessionRegistry(database)


def test_two_login_windows_and_two_run_reservations_share_exactly_four_places(database):
    store, generation, registry = setup_store(database)
    first_login = registry.reserve('manager-one', login(1))
    registry.reserve('manager-one', login(2))
    owners = [queued(database, store, 'run-' + str(index), identity='account-' + str(index)) for index in (1, 2)]
    qualifications = [store.claim('worker-one', generation) for _ in owners]
    assert all(qualifications) and capacity(database) == 4
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', login(3))
    assert rejected.value.status == 409
    for owner, token in zip(owners, qualifications, strict=True):
        session = registry.reserve('manager-one', owner, execution_token=token)
        assert capacity(database) == 4
        registry.opened(session.session_id, 'manager-one', execution_token=token)
        assert capacity(database) == 4
        with pytest.raises(BusinessError) as duplicate:
            registry.reserve('manager-one', owner, execution_token=token)
        assert duplicate.value.status == 409 and capacity(database) == 4
    registry.closing(first_login.session_id, 'manager-one')
    assert capacity(database) == 4
    with pytest.raises(BusinessError):
        registry.reserve('manager-one', login(3))
    registry.closed(first_login.session_id, 'manager-one')
    assert registry.reserve('manager-one', login(3)).state == 'OPENING'
    assert capacity(database) == 4


def test_waiting_run_keeps_reserved_browser_place_after_releasing_active_slot(database):
    store, generation, registry = setup_store(database)
    owner = queued(database, store, 'waiting-run', site='task-fixture')
    token = store.claim('worker-one', generation)
    store.defer(token, 'PAUSED')
    for index in range(3):
        registry.reserve('manager-one', login(index))
    assert capacity(database) == 4
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', login(4))
    assert rejected.value.status == 409
    with pytest.raises(BusinessError):
        registry.reserve('manager-one', owner, execution_token=token)
    assert capacity(database) == 4
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM resource_leases WHERE resource_type='active_slot'").fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM scheduler_context_reservations').fetchone()[0] == 1


@pytest.mark.parametrize('alter', ['missing', 'different-run', 'old-epoch', 'different-worker',
                                 'different-generation', 'different-identity', 'different-site', 'different-realm'])
def test_scheduled_context_requires_current_qualification_and_its_exact_owner_scope(database, alter):
    store, generation, registry = setup_store(database)
    owner = queued(database, store, 'scheduled-run', identity='account-one')
    token = store.claim('worker-one', generation)
    if alter == 'missing':
        token = None
    elif alter == 'different-run':
        token = replace(token, run_id='other-run')
    elif alter == 'old-epoch':
        token = replace(token, epoch=token.epoch + 1)
    elif alter == 'different-worker':
        token = replace(token, worker_id='other-worker')
    elif alter == 'different-generation':
        token = replace(token, worker_generation=token.worker_generation + 1)
    elif alter == 'different-identity':
        owner = replace(owner, identity_ref='account-two')
    elif alter == 'different-site':
        owner = replace(owner, site_id='other-site')
    else:
        owner = replace(owner, realm='webarena')
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', owner, execution_token=token)
    assert rejected.value.status == 409
    assert capacity(database) == 1
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM browser_sessions').fetchone()[0] == 0
        assert db.execute('SELECT session_id FROM scheduler_context_reservations').fetchone()[0] is None


def test_failed_session_insert_rolls_back_reservation_materialization(database):
    store, generation, registry = setup_store(database)
    owner = queued(database, store, 'scheduled-run')
    token = store.claim('worker-one', generation)
    with connect(database) as db:
        db.execute("CREATE TRIGGER fixture_abort BEFORE INSERT ON browser_sessions BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        registry.reserve('manager-one', owner, execution_token=token)
    assert capacity(database) == 1
    with connect(database) as db:
        assert db.execute('SELECT session_id FROM scheduler_context_reservations').fetchone()[0] is None
        db.execute('DROP TRIGGER fixture_abort')
    assert registry.reserve('manager-one', owner, execution_token=token).state == 'OPENING'
    assert capacity(database) == 1


@pytest.mark.parametrize('foreign_owner', ['login', 'other-run'])
def test_deferred_database_binding_cannot_assign_another_owners_context(database, foreign_owner):
    store, generation, registry = setup_store(database)
    queued(database, store, 'scheduled-run')
    assert store.claim('worker-one', generation)
    if foreign_owner == 'login':
        owner = login(1)
    else:
        with connect(database) as db, transaction(db):
            seed(db, task_id='task-other', run_id='other-run')
        owner = SessionOwner('run', 'other-run', 'other-site')
    foreign_session = registry.reserve('manager-one', owner)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db):
            db.execute('UPDATE scheduler_context_reservations SET session_id=? WHERE run_id=?',
                       (foreign_session.session_id, 'scheduled-run'))
    assert capacity(database) == 2
    with connect(database) as db:
        assert db.execute('SELECT session_id FROM scheduler_context_reservations').fetchone()[0] is None


def test_two_concurrent_materializations_cannot_reuse_one_reservation(database):
    store, generation, registry = setup_store(database)
    owner = queued(database, store, 'scheduled-run')
    token = store.claim('worker-one', generation)

    def materialize(_):
        try:
            return registry.reserve('manager-one', owner, execution_token=token)
        except BusinessError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(materialize, range(2)))
    assert sum(isinstance(reply, BusinessError) for reply in replies) == 1
    assert capacity(database) == 1
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM browser_sessions').fetchone()[0] == 1


def test_fresh_browser_accessor_refuses_old_epoch_without_using_cached_context(database):
    store, generation, registry = setup_store(database)
    owner = queued(database, store, 'scheduled-run')
    token = store.claim('worker-one', generation)
    session = registry.reserve('manager-one', owner, execution_token=token)
    registry.opened(session.session_id, 'manager-one', execution_token=token)

    class Browser:
        def is_connected(self):
            return True

    class Page:
        def is_closed(self):
            return False

    class Context:
        pages = [Page()]

    async def exercise():
        manager = ManagedBrowser(Settings(database.parent))
        manager.manager_id = 'manager-one'
        manager._started = True
        context = Context()
        manager._contexts[session.session_id] = _Context(context, Browser(), owner)
        assert await manager.context(session.session_id, owner, execution_token=token) is context
        store.defer(token, 'PAUSED')
        with pytest.raises(BusinessError) as stale:
            await manager.context(session.session_id, owner, execution_token=token)
        assert stale.value.status == 409
        with pytest.raises(BusinessError):
            await manager.context(session.session_id, owner)

    asyncio.run(exercise())


def test_login_and_scheduled_run_share_the_same_site_gate_in_both_directions(database):
    store, generation, registry = setup_store(database)
    login_session = registry.reserve('manager-one', login(1, 'github.dev'))
    queued(database, store, 'scheduled-run', identity='account-one')
    assert store.claim('worker-one', generation) is None
    registry.closing(login_session.session_id, 'manager-one')
    assert store.claim('worker-one', generation) is None
    registry.closed(login_session.session_id, 'manager-one')
    token = store.claim('worker-one', generation)
    assert token is not None
    for alias in ('github', 'github.dev', 'github_editor'):
        with pytest.raises(BusinessError) as rejected:
            registry.reserve('manager-one', login(2, alias))
        assert rejected.value.status == 409
    assert registry.reserve('manager-one', login(3, 'other-site')).state == 'OPENING'


def test_expired_execution_does_not_free_account_for_a_login_window(database):
    store, generation, registry = setup_store(database)
    queued(database, store, 'scheduled-run', identity='account-one')
    assert store.claim('worker-one', generation)
    with connect(database) as db, transaction(db):
        db.execute("UPDATE resource_leases SET heartbeat_at='2000-01-01T00:00:00.000000Z',expires_at='2000-01-01T00:00:01.000000Z'")
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', login(1, 'github'))
    assert rejected.value.status == 409 and capacity(database) == 1


def test_login_allocation_and_scheduler_claim_cannot_race_into_a_fifth_place(database):
    store, generation, registry = setup_store(database)
    for index in range(3):
        registry.reserve('manager-one', login(index))
    queued(database, store, 'scheduled-run', site='task-fixture')
    ready = Barrier(2)

    def claim():
        ready.wait(timeout=5)
        return store.claim('worker-one', generation)

    def reserve_login():
        ready.wait(timeout=5)
        try:
            return registry.reserve('manager-one', login(4))
        except BusinessError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed, created = pool.submit(claim), pool.submit(reserve_login)
        qualification, session = claimed.result(), created.result()
    assert (qualification is None) != isinstance(session, BusinessError)
    assert capacity(database) == 4
