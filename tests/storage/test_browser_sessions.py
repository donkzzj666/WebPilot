"""Real SQLite ownership/lifecycle tests; no browser, network or secret access."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest

from webagent.db import LATEST_VERSION, connect, migrate, transaction
from webagent.errors import BusinessError
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry
from webagent.state import transition
from conftest import seed


def owner(identifier='synthetic-owner', **values):
    return SessionOwner(kind='verification', owner_id=identifier, site_id='synthetic-site',
                        identity_ref='synthetic-account', realm='public', **values)


def events(path, session_id):
    with connect(path) as db:
        return [dict(row) for row in db.execute(
            'SELECT * FROM browser_session_events WHERE session_id=? ORDER BY event_id', (session_id,))]


def test_lifecycle_is_durable_idempotent_and_always_requires_rechecking(database):
    registry, owned_by = SessionRegistry(database), owner()
    initial = registry.reserve('manager-one', owned_by)
    assert initial.state == 'OPENING' and initial.generation == 1 and initial.state_version == 0
    assert initial.requires_identity_check and initial.requires_business_check
    assert registry.get(initial.session_id, owned_by) == initial
    assert [item['event_type'] for item in events(database, initial.session_id)] == ['reserved', 'recheck_required']
    opened = registry.opened(initial.session_id, 'manager-one')
    assert opened.state == 'OPEN' and opened.state_version == 1
    assert registry.opened(initial.session_id, 'manager-one') == opened
    closing = registry.closing(initial.session_id, 'manager-one')
    assert closing.state == 'CLOSING' and closing.state_version == 2
    assert registry.closing(initial.session_id, 'manager-one') == closing
    closed = registry.closed(initial.session_id, 'manager-one')
    assert closed.state == 'CLOSED' and closed.state_version == 3 and closed.closed_at
    assert registry.closed(initial.session_id, 'manager-one') == closed
    assert registry.lost(initial.session_id, 'manager-one', 'window_closed') == closed
    assert closed.requires_identity_check and closed.requires_business_check
    assert SessionRegistry(database).get(initial.session_id, owned_by) == closed
    assert [item['event_type'] for item in events(database, initial.session_id)] == [
        'reserved', 'recheck_required', 'opened', 'closing', 'closed']


@pytest.mark.parametrize('field,value', [
    ('kind', 'login'), ('owner_id', 'other-owner'), ('site_id', 'other-site'),
    ('identity_ref', 'other-account'), ('realm', 'webarena'),
])
def test_context_ownership_cannot_be_reused_across_any_scope(database, field, value):
    registry, owned_by = SessionRegistry(database), owner()
    initial = registry.reserve('manager-one', owned_by)
    with pytest.raises(BusinessError) as rejected:
        registry.get(initial.session_id, replace(owned_by, **{field: value}))
    assert rejected.value.status == 403
    assert registry.get(initial.session_id, owned_by) == initial


@pytest.mark.parametrize('operation', ['opened', 'closing', 'closed', 'lost'])
def test_old_or_different_manager_cannot_mutate_an_owned_session(database, operation):
    registry = SessionRegistry(database)
    initial = registry.reserve('manager-one', owner())
    with pytest.raises(BusinessError) as rejected:
        getattr(registry, operation)(initial.session_id, 'manager-other')
    assert rejected.value.status == 409
    assert registry.list_owned('manager-one') == [initial]
    assert registry.list_owned('manager-other') == []


def test_four_context_capacity_is_atomic_between_connections_and_retains_existing(database):
    def reserve(index):
        try:
            return SessionRegistry(database).reserve('manager-' + str(index), owner(str(index)))
        except BusinessError as error:
            return error

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(reserve, range(8)))
    accepted = [item for item in replies if not isinstance(item, BusinessError)]
    rejected = [item for item in replies if isinstance(item, BusinessError)]
    assert len(accepted) == len(rejected) == 4
    assert all(item.status == 409 and item.code == 'RESOURCE_CONFLICT' for item in rejected)
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM browser_sessions').fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM browser_sessions WHERE state='OPENING'").fetchone()[0] == 4
    registry = SessionRegistry(database)
    first = accepted[0]
    registry.lost(first.session_id, first.manager_id, 'launch_failed')
    replacement = registry.reserve('manager-next', owner('replacement-capacity'))
    assert replacement.state == 'OPENING'
    assert all(registry.get(item.session_id, item.owner) == item for item in accepted[1:])


def test_closing_session_still_occupies_a_slot(database):
    registry = SessionRegistry(database)
    sessions = [registry.reserve('manager-one', owner(str(index))) for index in range(4)]
    registry.closing(sessions[0].session_id, 'manager-one')
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', owner('fifth'))
    assert rejected.value.code == 'RESOURCE_CONFLICT'
    registry.closed(sessions[0].session_id, 'manager-one')
    assert registry.reserve('manager-one', owner('fifth')).state == 'OPENING'


def test_orphan_recovery_is_idempotent_and_only_changes_prior_managers_live_sessions(database):
    registry = SessionRegistry(database)
    old = [registry.reserve('old-manager', owner(str(index))) for index in range(3)]
    registry.opened(old[1].session_id, 'old-manager')
    registry.closing(old[2].session_id, 'old-manager')
    current = registry.reserve('new-manager', owner('current'))
    recovered = registry.recover_orphans('new-manager')
    assert {item.session_id for item in recovered} == {item.session_id for item in old}
    assert all(item.state == 'LOST' and item.loss_reason == 'manager_restarted' for item in recovered)
    assert all(item.requires_identity_check and item.requires_business_check for item in recovered)
    assert registry.recover_orphans('new-manager') == []
    assert registry.get(current.session_id, current.owner) == current
    for item in old:
        assert [event['event_type'] for event in events(database, item.session_id)][-2:] == ['lost', 'recheck_required']


def test_replacement_preserves_exact_owner_and_increments_generation_without_reopening_old(database):
    registry, owned_by = SessionRegistry(database), owner()
    initial = registry.reserve('manager-one', owned_by)
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', owned_by, replaces=initial.session_id)
    assert rejected.value.status == 409
    lost = registry.lost(initial.session_id, 'manager-one', 'window_closed')
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', replace(owned_by, identity_ref='different'), replaces=initial.session_id)
    assert rejected.value.status == 403
    rebuilt = registry.reserve('manager-two', owned_by, replaces=initial.session_id)
    assert rebuilt.generation == 2 and rebuilt.session_id != initial.session_id
    assert rebuilt.restored_from_session_id == initial.session_id
    assert rebuilt.auth_ref is None and rebuilt.requires_identity_check and rebuilt.requires_business_check
    assert registry.get(initial.session_id, owned_by) == lost


def test_auth_metadata_binds_identity_site_realm_and_preserves_previous_snapshot(database):
    registry, owned_by = SessionRegistry(database), owner()
    initial = registry.reserve('manager-one', owned_by)
    registry.opened(initial.session_id, 'manager-one')
    first_ref, second_ref = str(uuid4()), str(uuid4())
    first = registry.bind_auth(initial.session_id, 'manager-one', first_ref, 'a' * 64)
    second = registry.bind_auth(initial.session_id, 'manager-one', second_ref, 'b' * 64)
    assert first.auth_ref == first_ref and first.auth_sha256 == 'a' * 64
    assert second.auth_ref == second_ref and second.auth_sha256 == 'b' * 64
    assert second.state_version == first.state_version + 1
    registry.lost(initial.session_id, 'manager-one', 'context_closed')
    for changed in (replace(owned_by, identity_ref='other'), replace(owned_by, site_id='other'),
                    replace(owned_by, realm='webarena')):
        with pytest.raises(BusinessError) as rejected:
            registry.reserve('manager-two', changed, auth_ref=first_ref)
        assert rejected.value.status == 403
    same_scope_new_owner = replace(owned_by, owner_id='other-work')
    restored = registry.reserve('manager-two', same_scope_new_owner, auth_ref=first_ref)
    assert restored.auth_ref == first_ref and restored.auth_sha256 == 'a' * 64
    with connect(database) as db:
        metadata = [dict(row) for row in db.execute('SELECT * FROM browser_auth_snapshots')]
    assert len(metadata) == 2 and all(set(row) == {'auth_ref', 'site_id', 'identity_ref', 'realm', 'sha256', 'created_at'} for row in metadata)


@pytest.mark.parametrize('state', ['OPENING', 'CLOSING', 'CLOSED', 'LOST'])
def test_auth_can_only_be_published_for_an_open_session(database, state):
    registry = SessionRegistry(database)
    initial = registry.reserve('manager-one', owner())
    if state in ('CLOSING', 'CLOSED'):
        registry.closing(initial.session_id, 'manager-one')
    if state == 'CLOSED':
        registry.closed(initial.session_id, 'manager-one')
    if state == 'LOST':
        registry.lost(initial.session_id, 'manager-one')
    with pytest.raises(BusinessError) as rejected:
        registry.bind_auth(initial.session_id, 'manager-one', str(uuid4()), 'a' * 64)
    assert rejected.value.status == 409
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM browser_auth_snapshots').fetchone()[0] == 0


def test_run_owned_session_requires_a_real_nonterminal_run(database):
    registry = SessionRegistry(database)
    run_owner = replace(owner('session-run'), kind='run')
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-one', run_owner)
    assert rejected.value.status == 404
    with connect(database) as db, transaction(db):
        seed(db, run_id='session-run')
    created = registry.reserve('manager-one', run_owner)
    assert created.owner.kind == 'run'
    registry.lost(created.session_id, 'manager-one')
    transition(database, run_id='session-run', expected_state_version=0, target='CANCELLED')
    with pytest.raises(BusinessError) as rejected:
        registry.reserve('manager-two', run_owner, replaces=created.session_id)
    assert rejected.value.status == 409


def test_untrusted_loss_reason_is_replaced_with_static_reason(database):
    registry = SessionRegistry(database)
    initial = registry.reserve('manager-one', owner())
    marker = 'SYNTHETIC_COOKIE_PRIVATE_MESSAGE_815'
    result = registry.lost(initial.session_id, 'manager-one', marker)
    assert result.loss_reason == 'unknown'
    assert marker not in json.dumps(result.as_dict()) + json.dumps(events(database, initial.session_id))


@pytest.mark.parametrize('sql', [
    'DELETE FROM browser_sessions',
    "UPDATE browser_sessions SET owner_id='other',state_version=state_version+1",
    'UPDATE browser_sessions SET requires_identity_check=0,state_version=state_version+1',
    'UPDATE browser_sessions SET requires_business_check=0,state_version=state_version+1',
    "UPDATE browser_sessions SET state='OPEN',state_version=state_version+1,closed_at=NULL,loss_reason=NULL",
    'DELETE FROM browser_session_events',
    "UPDATE browser_session_events SET reason='rewritten'",
    'DELETE FROM browser_auth_snapshots',
    "UPDATE browser_auth_snapshots SET sha256='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
])
def test_database_preserves_terminal_history_recheck_flags_and_auth_metadata(database, sql):
    registry = SessionRegistry(database)
    initial = registry.reserve('manager-one', owner())
    registry.opened(initial.session_id, 'manager-one')
    registry.bind_auth(initial.session_id, 'manager-one', str(uuid4()), 'a' * 64)
    registry.lost(initial.session_id, 'manager-one', 'browser_disconnected')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(database) as db, transaction(db):
            db.execute(sql)


def test_v7_migration_preserves_existing_business_records(tmp_path):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=7)
    with connect(path) as db, transaction(db):
        seed(db, run_id='old-run')
    tables = ('tasks', 'contracts', 'runs')
    with connect(path) as db:
        before = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)] for name in tables}
    assert migrate(path)['applied'] == LATEST_VERSION - 7
    assert migrate(path)['applied'] == 0
    with connect(path) as db:
        after = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)] for name in tables}
        assert db.execute('SELECT count(*) FROM browser_sessions').fetchone()[0] == 0
    assert before == after


def test_user_last_window_loss_cannot_be_overwritten_by_an_already_waiting_close(database):
    """The close coroutine may queue before Playwright reports a user close."""
    from webagent.config import Settings
    from webagent.sessions.manager import ManagedBrowser, _Context

    class Browser:
        def is_connected(self):
            return True

        async def close(self):
            pass

    class Context:
        pages = []  # The user has already closed the last real window.

        async def close(self):
            pass

    async def exercise():
        manager = ManagedBrowser(Settings(database.parent))
        await manager.start()
        owned_by = owner()
        session = manager.registry.reserve(manager.manager_id, owned_by)
        manager.registry.opened(session.session_id, manager.manager_id)
        entry = _Context(Context(), Browser(), owned_by)
        manager._contexts[session.session_id] = entry
        try:
            # A long operation holds the gate. A normal-close request queued
            # first; the user then closes the last page before it can run.
            await manager._gate.acquire()
            closing = asyncio.create_task(manager.close(session.session_id, owned_by))
            await asyncio.sleep(0)
            manager._page_closed(session.session_id, entry)
            manager._gate.release()
            result = await closing
            await manager.drain_events()
            assert result.state == 'LOST' and result.loss_reason == 'window_closed'
            assert manager.registry.get(session.session_id, owned_by).state == 'LOST'
        finally:
            if manager._gate.locked():
                manager._gate.release()
            await manager.aclose()

    asyncio.run(exercise())


def test_failed_close_persistence_does_not_disable_later_window_loss_events(database, monkeypatch):
    from webagent.config import Settings
    from webagent.sessions.manager import ManagedBrowser, _Context

    class Page:
        def is_closed(self):
            return False

    class Browser:
        def is_connected(self):
            return True

        async def close(self):
            pass

    class Context:
        def __init__(self):
            self.pages = [Page()]

        async def close(self):
            self.pages.clear()

    async def exercise():
        manager = ManagedBrowser(Settings(database.parent))
        await manager.start()
        owned_by = owner()
        session = manager.registry.reserve(manager.manager_id, owned_by)
        manager.registry.opened(session.session_id, manager.manager_id)
        entry = _Context(Context(), Browser(), owned_by)
        manager._contexts[session.session_id] = entry
        real_closing, failures = manager.registry.closing, []

        def fail_first(session_id, manager_id):
            if not failures:
                failures.append(True)
                raise sqlite3.OperationalError('synthetic transient database error')
            return real_closing(session_id, manager_id)

        monkeypatch.setattr(manager.registry, 'closing', fail_first)
        try:
            with pytest.raises(BusinessError) as rejected:
                await manager.close(session.session_id, owned_by)
            assert rejected.value.code == 'SERVICE_UNAVAILABLE' and rejected.value.status == 503
            assert entry.context.pages == []
            with pytest.raises(BusinessError) as blocked:
                await manager.context(session.session_id, owned_by)
            assert blocked.value.code == 'SERVICE_UNAVAILABLE'
            entry.context.pages.clear()  # User closes window after failed close.
            manager._page_closed(session.session_id, entry)
            with pytest.raises(BusinessError) as unhealthy:
                await manager.drain_events()
            assert unhealthy.value.code == 'SERVICE_UNAVAILABLE'
            assert manager.registry.get(session.session_id, owned_by).state == 'LOST'
        finally:
            with pytest.raises(BusinessError) as shutdown:
                await manager.aclose()
            assert shutdown.value.code == 'SERVICE_UNAVAILABLE'
            assert manager._lock_fd is None

    asyncio.run(exercise())
