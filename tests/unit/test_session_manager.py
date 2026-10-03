"""Lifecycle races with in-process browser fakes and real isolated SQLite.

No existing browser, OS credential or external network is accessed here. The
separate verification script exercises this same manager with headed Chromium.
"""
import asyncio
import copy
import hashlib
import json
import os
import sqlite3
from uuid import uuid4

import pytest

from webagent.config import Settings
from webagent.errors import BusinessError
from webagent.network.config import NetworkConfig
from webagent.network.policy import Endpoint
from webagent.sessions import manager as manager_module
from webagent.sessions.manager import ManagedBrowser
from webagent.sessions.models import SessionOwner
from webagent.sessions.auth import AuthSnapshot

SECRET = "synthetic-browser-secret-never-log"
OWNER = SessionOwner("verification", "fixture-one", "fixture", "fixture-account")


class Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event, *args):
        for callback in self.handlers.get(event, []):
            callback(*args)


class Page(Emitter):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        if not self.closed:
            self.closed = True
            self.context.pages.remove(self)
            self.emit("close", self)

    def crash(self):
        self.emit("crash", self)


class Context(Emitter):
    def __init__(self, browser, options):
        super().__init__()
        self.browser = browser
        self.options = copy.deepcopy(options)
        self.pages = []
        self.closed = False
        self.close_error = False
        self.close_gate = None
        self.close_started = asyncio.Event()
        self.storage_calls = []
        self.boundary_calls = []
        self.routes = []
        self.websockets = []
        self.scripts = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))
        self.boundary_calls.append("route")

    async def route_web_socket(self, pattern, handler):
        self.websockets.append((pattern, handler))
        self.boundary_calls.append("websocket")

    async def add_init_script(self, script):
        self.scripts.append(script)
        self.boundary_calls.append("script")

    async def new_page(self):
        self.boundary_calls.append("page")
        factory = self.browser.factory
        factory.page_started.set()
        if factory.page_gate is not None:
            await factory.page_gate.wait()
        if factory.page_error:
            raise RuntimeError(SECRET)
        page = Page(self)
        self.pages.append(page)
        self.emit("page", page)
        if factory.page_crash_on_create:
            page.crash()
        return page

    async def close(self):
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error:
            raise RuntimeError(SECRET)
        if not self.closed:
            for page in tuple(self.pages):
                await page.close()
            self.closed = True
            if self in self.browser.contexts:
                self.browser.contexts.remove(self)
            self.emit("close", self)

    async def storage_state(self, **options):
        self.storage_calls.append(options)
        return {"cookies": [], "origins": [{"origin": "http://127.0.0.1",
                "localStorage": [{"name": "account", "value": SECRET}], "indexedDB": []}]}


class Browser(Emitter):
    def __init__(self, factory):
        super().__init__()
        self.factory = factory
        self.contexts = []
        self.connected = True

    def is_connected(self):
        return self.connected

    async def new_context(self, **options):
        factory = self.factory
        factory.context_started.set()
        if factory.context_gate is not None:
            await factory.context_gate.wait()
        if factory.context_error:
            raise RuntimeError(SECRET)
        context = Context(self, options)
        self.contexts.append(context)
        factory.created_contexts.append(context)
        return context

    def disconnect(self):
        self.connected = False
        for context in tuple(self.contexts):
            context.closed = True
            for page in context.pages:
                page.closed = True
            context.pages.clear()
        self.contexts.clear()
        self.emit("disconnected", self)

    async def close(self):
        if self.factory.browser_close_error:
            raise RuntimeError(SECRET)
        self.disconnect()


class Factory:
    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.launches = []
        self.browsers = []
        self.created_contexts = []
        self.launch_error = False
        self.context_error = False
        self.page_error = False
        self.page_crash_on_create = False
        self.context_gate = None
        self.page_gate = None
        self.context_started = asyncio.Event()
        self.page_started = asyncio.Event()
        self.browser_close_error = False
        self.stop_error = False
        self.chromium = self

    def __call__(self):
        return self

    async def start(self):
        self.starts += 1
        return self

    async def launch(self, **options):
        self.launches.append(options)
        if self.launch_error:
            raise RuntimeError(SECRET)
        browser = Browser(self)
        self.browsers.append(browser)
        return browser

    async def stop(self):
        self.stops += 1
        if self.stop_error:
            raise RuntimeError(SECRET)
        for browser in self.browsers:
            browser.disconnect()

    async def __aexit__(self, *_):
        await self.stop()


class Auth:
    def __init__(self):
        self.states = {}
        self.saved = []
        self.loaded = []
        self.load_error = False

    def save(self, state, **scope):
        snapshot = AuthSnapshot(str(uuid4()), hashlib.sha256(json.dumps(state).encode()).hexdigest())
        self.states[snapshot.ref] = copy.deepcopy(state)
        self.saved.append((state, scope, snapshot))
        return snapshot

    def load(self, ref, **scope):
        self.loaded.append((ref, scope))
        if self.load_error:
            raise RuntimeError(SECRET)
        return copy.deepcopy(self.states[ref])


class Proxy:
    def __init__(self, policy):
        self.policy = policy
        self.active = False
        self.start_error = False
        self.close_error = False
        self.closed = False
        self.options = {"server": "http://127.0.0.1:12345", "username": str(uuid4()), "password": str(uuid4())}

    @property
    def playwright_proxy(self):
        assert self.active
        return dict(self.options)

    async def start(self):
        if self.start_error:
            raise RuntimeError(SECRET)
        self.active = True
        return self

    async def aclose(self):
        if self.close_error:
            raise RuntimeError(SECRET)
        self.active = False
        self.closed = True


def setup(tmp_path, **kwargs):
    factory = Factory()
    auth = Auth()
    manager = ManagedBrowser(Settings(tmp_path), playwright_factory=factory, auth_store=auth,
                             proxy_factory=kwargs.pop("proxy_factory", Proxy), **kwargs)
    return manager, factory, auth


def owned(number):
    return SessionOwner("verification", f"fixture-{number}", "fixture", "fixture-account")


def rows(manager):
    return manager.registry.list_owned(manager.manager_id)


def test_start_migrates_and_locks_without_browser_or_auth_access(tmp_path):
    async def exercise():
        manager, factory, auth = setup(tmp_path)
        assert await manager.start() is manager
        assert await manager.start() is manager
        assert not factory.starts and not factory.launches and not auth.saved and not auth.loaded
        other, other_factory, _ = setup(tmp_path)
        with pytest.raises(BusinessError) as caught:
            await other.start()
        assert caught.value.code == "RESOURCE_CONFLICT" and caught.value.status == 409
        await other.aclose()  # failed start can always be finalized
        await manager.aclose()
        replacement, _, _ = setup(tmp_path)
        await replacement.start()
        await replacement.aclose()
        assert not other_factory.starts and not manager._lock_fd
    asyncio.run(exercise())


def test_start_recovers_orphans_only_after_lock_is_owned(tmp_path):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        await manager.start()
        orphan = manager.registry.reserve("prior-process", OWNER)
        manager.registry.opened(orphan.session_id, "prior-process")
        other, _, _ = setup(tmp_path)
        with pytest.raises(BusinessError):
            await other.start()
        assert manager.registry.get(orphan.session_id, OWNER).state == "OPEN"
        await manager.aclose()
        await other.start()
        recovered = other.registry.get(orphan.session_id, OWNER)
        assert recovered.state == "LOST" and recovered.loss_reason == "manager_restarted"
        assert recovered.requires_identity_check and recovered.requires_business_check
        await other.aclose()
    asyncio.run(exercise())


def test_default_headed_contexts_are_independent_and_capacity_is_four(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        try:
            records = await asyncio.gather(*(manager.create(owned(n)) for n in range(4)))
            assert len(factory.launches) == 1 and factory.launches[0]["headless"] is False
            contexts = [await manager.context(item.session_id, item.owner) for item in records]
            assert len(set(map(id, contexts))) == 4
            assert all(ctx.options["service_workers"] == "block" for ctx in contexts)
            assert all(item.state == "OPEN" and item.requires_identity_check and item.requires_business_check
                       for item in records)
            with pytest.raises(BusinessError) as caught:
                await manager.create(owned(5))
            assert caught.value.code == "RESOURCE_CONFLICT" and len(rows(manager)) == 4
            await manager.close(records[0].session_id, records[0].owner)
            assert (await manager.create(owned(5))).state == "OPEN"
        finally:
            await manager.aclose()
        assert all(ctx.closed for ctx in factory.created_contexts)
    asyncio.run(exercise())


@pytest.mark.parametrize("change", [{"kind": "login"}, {"owner_id": "foreign"}, {"site_id": "foreign"},
                                     {"identity_ref": "foreign"}, {"realm": "webarena"}])
def test_all_owner_dimensions_are_checked_before_context_close_or_auth(tmp_path, change):
    async def exercise():
        manager, _, auth = setup(tmp_path)
        try:
            record = await manager.create(OWNER)
            wrong = SessionOwner(**{**OWNER.__dict__, **change})
            for operation in (manager.context, manager.close, manager.save_auth):
                with pytest.raises(BusinessError) as caught:
                    await operation(record.session_id, wrong)
                assert caught.value.code == "FORBIDDEN"
            assert manager.registry.get(record.session_id, OWNER).state == "OPEN"
            assert not auth.saved
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_last_window_closure_is_lost_but_closing_one_popup_is_not(tmp_path):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        try:
            record = await manager.create(OWNER)
            context = await manager.context(record.session_id, OWNER)
            popup = await context.new_page()
            await popup.close()
            await manager.drain_events()
            assert manager.registry.get(record.session_id, OWNER).state == "OPEN"
            await context.pages[0].close()
            with pytest.raises(BusinessError) as caught:
                await manager.context(record.session_id, OWNER)
            assert caught.value.code == "STATE_CONFLICT"
            await manager.drain_events()
            lost = manager.registry.get(record.session_id, OWNER)
            assert lost.state == "LOST" and lost.loss_reason == "window_closed"
            assert context.closed and not manager._contexts
            assert (await manager.close(record.session_id, OWNER)).state == "LOST"
        finally:
            await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("event,reason", [("context", "context_closed"), ("crash", "page_crashed")])
def test_external_close_or_crash_is_lost_and_closes_physical_context(tmp_path, event, reason):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        try:
            record = await manager.create(OWNER)
            context = await manager.context(record.session_id, OWNER)
            if event == "context":
                await context.close()
            else:
                context.pages[0].crash()
            await manager.drain_events()
            lost = manager.registry.get(record.session_id, OWNER)
            assert lost.state == "LOST" and lost.loss_reason == reason
            assert context.closed and len(manager._contexts) == 0
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_browser_disconnect_loses_all_owned_contexts_and_next_create_gets_new_browser(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        try:
            first = await manager.create(OWNER)
            second = await manager.create(owned(2))
            old_browser = manager._browser
            old_browser.disconnect()
            await manager.drain_events()
            assert all(item.state == "LOST" and item.loss_reason == "browser_disconnected" for item in rows(manager))
            replacement = await manager.create(OWNER, replaces=first.session_id)
            assert replacement.generation == 2 and replacement.restored_from_session_id == first.session_id
            old_browser.emit("disconnected", old_browser)  # stale process callback cannot lose new context
            await manager.drain_events()
            assert manager.registry.get(replacement.session_id, OWNER).state == "OPEN"
            assert manager._browser is factory.browsers[-1] and len(factory.browsers) == 2
            assert manager.registry.get(second.session_id, second.owner).state == "LOST"
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_programmatic_close_is_closed_idempotent_and_never_auto_saves(tmp_path):
    async def exercise():
        manager, _, auth = setup(tmp_path)
        record = await manager.create(OWNER)
        closed = await manager.close(record.session_id, OWNER)
        await manager.drain_events()
        assert closed.state == "CLOSED" and closed.loss_reason is None
        assert (await manager.close(record.session_id, OWNER)) == closed
        await manager.aclose()
        await manager.aclose()
        assert not auth.saved
        with sqlite3.connect(manager.settings.business_db) as db:
            assert [row[0] for row in db.execute("SELECT event_type FROM browser_session_events ORDER BY event_id")] == [
                "reserved", "recheck_required", "opened", "closing", "closed"]
    asyncio.run(exercise())


def test_explicit_auth_save_uses_indexeddb_memory_state_and_restores_with_digest(tmp_path):
    async def exercise():
        manager, _, auth = setup(tmp_path)
        try:
            original = await manager.create(OWNER)
            context = await manager.context(original.session_id, OWNER)
            snapshot = await manager.save_auth(original.session_id, OWNER)
            assert context.storage_calls == [{"indexed_db": True}]
            assert auth.saved[0][1] == {"site_id": OWNER.site_id, "identity_ref": OWNER.identity_ref, "realm": "public"}
            await manager.close(original.session_id, OWNER)
            restored = await manager.create(OWNER, auth_ref=snapshot.ref, replaces=original.session_id)
            replacement = await manager.context(restored.session_id, OWNER)
            assert replacement.options["storage_state"] == auth.states[snapshot.ref]
            assert "sessionStorage" not in json.dumps(replacement.options)
            assert auth.loaded == [(snapshot.ref, {"site_id": OWNER.site_id, "identity_ref": OWNER.identity_ref,
                                                    "realm": "public", "expected_sha256": snapshot.sha256})]
            assert restored.auth_ref == snapshot.ref and restored.auth_sha256 == snapshot.sha256
            assert restored.requires_identity_check and restored.requires_business_check
            with sqlite3.connect(manager.settings.business_db) as db:
                assert SECRET not in "\n".join(db.iterdump())
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_anonymous_session_cannot_save_auth_or_read_browser_state_for_it(tmp_path):
    async def exercise():
        manager, _, auth = setup(tmp_path)
        try:
            owner = SessionOwner("verification", "anonymous", "fixture")
            record = await manager.create(owner)
            context = await manager.context(record.session_id, owner)
            with pytest.raises(BusinessError) as caught:
                await manager.save_auth(record.session_id, owner)
            assert caught.value.code == "INVALID_PARAMETER" and caught.value.field == "identity_ref"
            assert context.storage_calls == [] and auth.saved == []
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_unregistered_auth_ref_is_rejected_before_read_or_launch(tmp_path):
    async def exercise():
        manager, factory, auth = setup(tmp_path)
        try:
            with pytest.raises(BusinessError) as caught:
                await manager.create(OWNER, auth_ref=str(uuid4()))
            assert caught.value.code == "FORBIDDEN"
            assert not factory.starts and not auth.loaded and not rows(manager)
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_auth_load_failure_is_safe_and_persists_lost_attempt(tmp_path):
    async def exercise():
        manager, factory, auth = setup(tmp_path)
        try:
            original = await manager.create(OWNER)
            snapshot = await manager.save_auth(original.session_id, OWNER)
            await manager.close(original.session_id, OWNER)
            auth.load_error = True
            with pytest.raises(BusinessError) as caught:
                await manager.create(OWNER, auth_ref=snapshot.ref)
            assert caught.value.code == "SERVICE_UNAVAILABLE" and caught.value.field == "auth_ref"
            assert SECRET not in str(caught.value)
            assert [item.loss_reason for item in rows(manager) if item.state == "LOST"] == ["auth_unavailable"]
            assert len(factory.created_contexts) == 1
        finally:
            await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure,reason", [("launch_error", "launch_failed"),
                                            ("context_error", "context_create_failed"),
                                            ("page_error", "context_create_failed"),
                                            ("page_crash_on_create", "page_crashed")])
def test_failed_create_persists_safe_reason_and_closes_created_context(tmp_path, failure, reason):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        setattr(factory, failure, True)
        try:
            with pytest.raises(BusinessError) as caught:
                await manager.create(OWNER)
            assert caught.value.code == "SERVICE_UNAVAILABLE" and SECRET not in str(caught.value)
            await manager.drain_events()
            assert rows(manager)[0].state == "LOST" and rows(manager)[0].loss_reason == reason
            assert all(context.closed for context in factory.created_contexts)
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_cancel_context_creation_collects_late_resource_then_closes_it(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        factory.context_gate = asyncio.Event()
        task = asyncio.create_task(manager.create(OWNER))
        await factory.context_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        factory.context_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(factory.created_contexts) == 1 and factory.created_contexts[0].closed
        assert rows(manager)[0].loss_reason == "operation_cancelled" and not manager._contexts
        await manager.aclose()
    asyncio.run(exercise())


def test_cancel_unsettled_resource_prevents_new_work_and_shutdown_stops_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "CLOSE_TIMEOUT_SECONDS", 0.02)
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        factory.context_gate = asyncio.Event()
        task = asyncio.create_task(manager.create(OWNER))
        await factory.context_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager._uncertain_resource
        with pytest.raises(BusinessError) as caught:
            await manager.create(owned(2))
        assert caught.value.code == "SERVICE_UNAVAILABLE" and len(rows(manager)) == 1
        await manager.aclose()
        assert factory.stops == 1 and not manager._resource_tasks and manager._lock_fd is None
    asyncio.run(exercise())


def test_cancel_initial_page_creation_cleans_context_and_preserves_cancellation(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        factory.page_gate = asyncio.Event()
        task = asyncio.create_task(manager.create(OWNER))
        await factory.page_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert factory.created_contexts[0].closed
        assert rows(manager)[0].state == "LOST" and rows(manager)[0].loss_reason == "operation_cancelled"
        await manager.aclose()
    asyncio.run(exercise())


def test_failed_physical_close_is_lost_and_still_consumes_capacity(tmp_path):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        records = [await manager.create(owned(n)) for n in range(4)]
        context = await manager.context(records[0].session_id, records[0].owner)
        context.close_error = True
        lost = await manager.close(records[0].session_id, records[0].owner)
        assert lost.state == "LOST" and lost.loss_reason == "close_failed" and not context.closed
        with pytest.raises(BusinessError) as caught:
            await manager.create(owned(5))
        assert caught.value.code == "RESOURCE_CONFLICT" and len(manager._contexts) == 4
        context.close_error = False
        assert (await manager.close(lost.session_id, lost.owner)).state == "LOST"
        assert (await manager.create(owned(5))).state == "OPEN"
        await manager.aclose()
    asyncio.run(exercise())


def test_shutdown_uses_driver_fallback_and_does_not_unlock_surviving_browser(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        context.close_error = factory.browser_close_error = factory.stop_error = True
        with pytest.raises(BusinessError) as caught:
            await manager.aclose()
        assert caught.value.code == "SERVICE_UNAVAILABLE" and manager._lock_fd is not None
        assert manager.registry.get(record.session_id, OWNER).state == "LOST"
        other, _, _ = setup(tmp_path)
        with pytest.raises(BusinessError) as conflict:
            await other.start()
        assert conflict.value.code == "RESOURCE_CONFLICT"
        factory.stop_error = False
        await manager.aclose()
        assert not factory.browsers[0].connected and manager._lock_fd is None
        await other.start()
        await other.aclose()
    asyncio.run(exercise())


def test_cancel_shutdown_waits_for_owned_cleanup_before_unlocking(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        context.close_gate = asyncio.Event()
        task = asyncio.create_task(manager.aclose())
        await context.close_started.wait()
        task.cancel()
        assert manager._lock_fd is not None
        context.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert factory.stops == 1 and context.closed and manager._lock_fd is None
        assert manager.registry.get(record.session_id, OWNER).state == "CLOSED"
    asyncio.run(exercise())


def test_owner_is_copied_before_awaiting_browser_creation(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        factory.context_gate = asyncio.Event()
        caller_owner = owned(1)
        task = asyncio.create_task(manager.create(caller_owner))
        await factory.context_started.wait()
        object.__setattr__(caller_owner, "site_id", "mutated")
        factory.context_gate.set()
        record = await task
        assert record.owner.site_id == "fixture"
        assert (await manager.context(record.session_id, owned(1))).options["service_workers"] == "block"
        await manager.aclose()
    asyncio.run(exercise())


def test_event_persistence_failure_is_safe_and_blocks_new_operations(tmp_path, monkeypatch):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        original = manager.registry.lost
        def fail(*args, **kwargs):
            raise RuntimeError(SECRET)
        monkeypatch.setattr(manager.registry, "lost", fail)
        context.pages[0].crash()
        with pytest.raises(BusinessError) as caught:
            await manager.drain_events()
        assert SECRET not in str(caught.value)
        with pytest.raises(BusinessError):
            await manager.create(owned(2))
        monkeypatch.setattr(manager.registry, "lost", original)
        with pytest.raises(BusinessError):
            await manager.aclose()  # reports failed event even after resources are closed
        assert context.closed and manager._lock_fd is None
    asyncio.run(exercise())


@pytest.mark.parametrize("options", [{"executable_path": "/Applications/Chrome"}, {"channel": "chrome"},
    {"user_data_dir": "/tmp/profile"}, {"args": ["--user-data-dir=/tmp/profile"]},
    {"args": ["--remote-debugging-port=9222"]}, {"env": {"SECRET": SECRET}}])
def test_test_overrides_cannot_select_installed_browser_or_profile(tmp_path, options):
    with pytest.raises(BusinessError) as caught:
        setup(tmp_path, launch_options=options)
    assert caught.value.code == "INVALID_PARAMETER" and SECRET not in str(caught.value)


def test_symlink_manager_lock_is_not_followed(tmp_path):
    async def exercise():
        destination = tmp_path / "outside"
        destination.write_text("untouched")
        directory = tmp_path / "sessions"
        directory.mkdir()
        (directory / "manager.lock").symlink_to(destination)
        manager, factory, _ = setup(tmp_path)
        with pytest.raises(BusinessError) as caught:
            await manager.start()
        assert caught.value.code == "SERVICE_UNAVAILABLE" and destination.read_text() == "untouched"
        assert not factory.starts
        await manager.aclose()
    asyncio.run(exercise())


def test_driver_debug_channels_are_disabled_before_driver_start(tmp_path, monkeypatch):
    for name, value in {"DEBUG": "pw:protocol,pw:channel", "DEBUG_FILE": str(tmp_path / "unsafe.log"),
                        "DEBUGP": "1", "PWDEBUG": "console", "PWDEBUGIMPL": "1",
                        "npm_config_pwdebug": "1", "npm_config_pwdebugimpl": "1"}.items():
        monkeypatch.setenv(name, value)
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        original = factory.start
        async def checked_start():
            assert all(name not in os.environ for name in ("DEBUG", "DEBUG_FILE", "DEBUGP"))
            assert os.environ["PWDEBUG"] == os.environ["PWDEBUGIMPL"] == "0"
            return await original()
        factory.start = checked_start
        await manager.create(OWNER)
        await manager.aclose()
        assert not (tmp_path / "unsafe.log").exists()
    asyncio.run(exercise())


def test_shutdown_pending_command_is_bounded_even_when_it_suppresses_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "CLOSE_TIMEOUT_SECONDS", 0.02)
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        await manager.start()
        release = asyncio.Event()
        entered = asyncio.Event()
        async def stubborn_command():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        command = asyncio.create_task(stubborn_command())
        manager._resource_tasks.add(command)
        command.add_done_callback(manager._resource_tasks.discard)
        await entered.wait()
        with pytest.raises(BusinessError) as caught:
            await asyncio.wait_for(manager.aclose(), timeout=0.3)
        assert caught.value.code == "SERVICE_UNAVAILABLE" and manager._lock_fd is not None
        release.set()
        await command
        await manager.aclose()
        assert manager._lock_fd is None and not factory.starts
    asyncio.run(exercise())


def test_close_database_failure_is_safe_and_stops_context_use(tmp_path, monkeypatch):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        original = manager.registry.closing
        def fail(*_):
            raise RuntimeError(SECRET)
        monkeypatch.setattr(manager.registry, "closing", fail)
        with pytest.raises(BusinessError) as caught:
            await manager.close(record.session_id, OWNER)
        assert caught.value.code == "SERVICE_UNAVAILABLE" and SECRET not in str(caught.value)
        assert context.closed
        with pytest.raises(BusinessError):
            await manager.context(record.session_id, OWNER)
        assert manager.registry.get(record.session_id, OWNER).state == "LOST"
        monkeypatch.setattr(manager.registry, "closing", original)
        with pytest.raises(BusinessError):
            await manager.aclose()
        assert manager._lock_fd is None
    asyncio.run(exercise())


def test_cancelled_launch_collects_late_browser_and_shutdown_closes_it(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        launched, release = asyncio.Event(), asyncio.Event()
        original = factory.launch
        async def delayed_launch(**options):
            launched.set()
            await release.wait()
            return await original(**options)
        factory.launch = delayed_launch
        task = asyncio.create_task(manager.create(OWNER))
        await launched.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(factory.browsers) == 1 and manager._browser is factory.browsers[0]
        assert rows(manager)[0].loss_reason == "operation_cancelled" and not factory.created_contexts
        await manager.aclose()
        assert not factory.browsers[0].connected and manager._lock_fd is None
    asyncio.run(exercise())


def test_cancelled_close_remains_lost_and_finishes_physical_cleanup(tmp_path):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        context.close_gate = asyncio.Event()
        task = asyncio.create_task(manager.close(record.session_id, OWNER))
        await context.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        context.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await manager.drain_events()
        lost = manager.registry.get(record.session_id, OWNER)
        assert lost.state == "LOST" and lost.loss_reason == "operation_cancelled"
        assert context.closed and not manager._contexts
        await manager.aclose()
    asyncio.run(exercise())


def test_browser_and_each_context_have_distinct_authenticated_proxy_before_first_page(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        first = await manager.create(OWNER)
        second = await manager.create(owned(2))
        one = await manager.context(first.session_id, OWNER)
        two = await manager.context(second.session_id, second.owner)
        configurations = [factory.launches[0]["proxy"], one.options["proxy"], two.options["proxy"]]
        assert len({item["username"] for item in configurations}) == 3
        assert all(item["password"] and item["bypass"] == "<-loopback>" for item in configurations)
        assert manager._background_proxy.policy.realm == "webarena"
        assert manager._background_proxy.policy.webarena_endpoints == ()
        assert one.boundary_calls[:4] == two.boundary_calls[:4] == ["route", "websocket", "script", "page"]
        assert one.options["service_workers"] == "block" and one.options["accept_downloads"] is False
        assert one.routes[0][0] == one.websockets[0][0] == "**/*"
        assert "RTCPeerConnection" in one.scripts[0] and "WebTransport" in one.scripts[0]
        proxies = list(manager._proxies.values())
        await manager.aclose()
        assert all(proxy.closed for proxy in proxies) and not manager._proxies
    asyncio.run(exercise())


def test_browser_switches_disable_loopback_bypass_dns_udp_and_background_connections(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path, launch_options={"args": ["--enable-automation"]})
        await manager.create(OWNER)
        switches = factory.launches[0]["args"]
        assert "--proxy-bypass-list=<-loopback>" in switches
        assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in switches
        assert "--disable-quic" in switches
        assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in switches
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in switches
        assert "--disable-background-networking" in switches and "--disable-extensions" in switches
        assert switches.count("--enable-automation") == 1
        await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("url,allowed", [
    ("https://public.example/resource", True), ("http://127.0.0.1:8080", True),
    ("file:///tmp/synthetic-only", False), ("ftp://host/resource", False),
    ("data:text/html,synthetic", False), ("javascript:alert(1)", False),
    ("ws://example.test/socket", False), ("wss://example.test/socket", False),
    ("chrome://settings", False), ("blob:https://example.test/id", False),
])
def test_route_blocks_non_http_schemes_while_proxy_decides_network_addresses(tmp_path, url, allowed):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        class Route:
            request = type("Request", (), {"url": url})()
            continued = False
            aborted = None
            async def continue_(self):
                self.continued = True
            async def abort(self, code):
                self.aborted = code
        route = Route()
        await context.routes[0][1](route)
        assert route.continued is allowed
        assert route.aborted == (None if allowed else "blockedbyclient")
        await manager.aclose()
    asyncio.run(exercise())


def test_page_websocket_is_closed_without_creating_upstream_connection(tmp_path):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        class Socket:
            close_options = None
            async def close(self, **options):
                self.close_options = options
            async def connect_to_server(self):
                raise AssertionError("WebSocket upstream must never be opened")
        socket = Socket()
        await context.websockets[0][1](socket)
        assert socket.close_options["code"] == 1008
        await manager.aclose()
    asyncio.run(exercise())


def test_lost_proxy_cannot_fall_back_to_direct_or_return_usable_context(tmp_path):
    async def exercise():
        manager, _, _ = setup(tmp_path)
        record = await manager.create(OWNER)
        context = await manager.context(record.session_id, OWNER)
        proxy = manager._contexts[record.session_id].proxy
        await proxy.aclose()
        class Route:
            request = type("Request", (), {"url": "https://public.example/"})()
            aborted = None
            async def continue_(self):
                raise AssertionError("Dead proxy must block route")
            async def abort(self, code):
                self.aborted = code
        route = Route()
        await context.routes[0][1](route)
        assert route.aborted == "blockedbyclient"
        with pytest.raises(BusinessError) as caught:
            await manager.context(record.session_id, OWNER)
        assert caught.value.code == "STATE_CONFLICT"
        await manager.aclose()
    asyncio.run(exercise())


def test_realm_policy_comes_from_trusted_config_and_not_page_or_owner_parameters(tmp_path):
    async def exercise():
        configured = NetworkConfig(webarena_endpoints=(Endpoint("http", "127.0.0.1", 19081),))
        manager, _, _ = setup(tmp_path, network_config=configured)
        public = await manager.create(OWNER)
        arena_owner = SessionOwner("verification", "arena", "fixture", "fixture-account", "webarena")
        arena = await manager.create(arena_owner)
        assert manager._contexts[public.session_id].proxy.policy.realm == "public"
        assert manager._contexts[arena.session_id].proxy.policy.realm == "webarena"
        assert manager._contexts[arena.session_id].proxy.policy.webarena_endpoints == configured.webarena_endpoints
        await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("position", [1, 2])
def test_proxy_start_failure_never_creates_an_unprotected_context(tmp_path, position):
    async def exercise():
        proxies = []
        def failing_proxy(policy):
            proxy = Proxy(policy)
            proxies.append(proxy)
            proxy.start_error = len(proxies) == position
            return proxy
        manager, factory, _ = setup(tmp_path, proxy_factory=failing_proxy)
        with pytest.raises(BusinessError) as caught:
            await manager.create(OWNER)
        assert caught.value.code == "SERVICE_UNAVAILABLE" and SECRET not in str(caught.value)
        assert not factory.created_contexts and rows(manager)[0].state == "LOST"
        await manager.aclose()
        assert all(proxy.closed for proxy in proxies)
    asyncio.run(exercise())


@pytest.mark.parametrize("options", [
    {"proxy": {"server": "http://127.0.0.1:9000"}}, {"chromium_sandbox": False},
    {"args": ["--no-proxy-server"]}, {"args": ["--proxy-bypass-list=*"]},
    {"args": ["--host-resolver-rules=MAP * 127.0.0.1"]}, {"args": ["--enable-quic"]},
    {"args": ["--disable-features="]}, {"args": ["--load-extension=/tmp/synthetic"]},
    {"args": ["--force-webrtc-ip-handling-policy=default"]},
    {"args": ["--webrtc-ip-handling-policy=default"]},
])
def test_network_security_flags_cannot_be_overridden_by_launch_options(tmp_path, options):
    with pytest.raises(BusinessError) as caught:
        setup(tmp_path, launch_options=options)
    assert caught.value.code == "INVALID_PARAMETER"
