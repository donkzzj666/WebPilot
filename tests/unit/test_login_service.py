"""Login orchestration with the real manager/store and synthetic browser I/O."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from webagent.config import Settings
from webagent.db import connect
from webagent.errors import BusinessError
from webagent.identities import service as service_module
from webagent.identities.service import LoginService
from webagent.identities.sites import FixtureSiteAdapter, IdentityObservation, IdentityReason, IdentityStatus
from webagent.sessions.manager import ManagedBrowser
from webagent.sessions.models import SessionOwner

from test_session_manager import Auth, Factory, Page, Proxy, SECRET


class Adapter:
    base = FixtureSiteAdapter(site_id="fixture-github", origin="http://127.0.0.1:19182")
    site_id = base.site_id
    realm = base.realm
    origin = base.origin
    login_url = base.login_url
    verification_url = base.verification_url
    adapter_id = "synthetic-test-adapter-v1"

    def __init__(self):
        self.calls = []
        self.answers = []
        self.started = asyncio.Event()
        self.gate = None
        self.error = None

    def normalize_account(self, value):
        return self.base.normalize_account(value)

    def result(self, status=IdentityStatus.VERIFIED, account="alice", **changes):
        return IdentityObservation(status, account,
            IdentityReason.VERIFIED if status == IdentityStatus.VERIFIED else IdentityReason.NOT_AUTHENTICATED,
            self.site_id, self.realm, self.verification_url, "a" * 64, "document-one", **changes)

    async def confirm(self, page, account):
        self.calls.append((page, account))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        if self.answers:
            return self.answers.pop(0)
        return self.result()


class Catalog:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, site_id):
        if site_id != self.adapter.site_id:
            raise BusinessError("INVALID_PARAMETER", "Unknown supported site", field="site_id")
        return self.adapter

    def list(self):
        return [{"site_id": self.adapter.site_id, "realm": self.adapter.realm, "origin": self.adapter.origin}]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    factory, auth, adapter = Factory(), Auth(), Adapter()
    async def goto(page, url, **options):
        if getattr(factory, "navigation_error", False):
            raise RuntimeError(SECRET)
        page.navigation = (url, options)
        return SimpleNamespace(status=200)
    monkeypatch.setattr(Page, "goto", goto, raising=False)
    manager = ManagedBrowser(Settings(tmp_path), playwright_factory=factory, auth_store=auth, proxy_factory=Proxy)
    service = LoginService(Settings(tmp_path), manager, catalog=Catalog(adapter))
    return SimpleNamespace(service=service, manager=manager, factory=factory, auth=auth, adapter=adapter)


async def create(rig, account="Alice", identity=None):
    return await rig.service.create(site_id=rig.adapter.site_id, expected_account=account,
                                     expected_identity_ref=identity)


def count(rig, table):
    with connect(rig.manager.settings.business_db) as db:
        return db.execute("SELECT count(*) FROM " + table).fetchone()[0]


async def verify(rig):
    value = await create(rig)
    return await rig.service.confirm(value["login_id"], value["state_version"])


def test_create_only_opens_trusted_login_page_without_auth_collection_or_models(rig):
    async def exercise():
        try:
            value = await create(rig)
            assert value["state"] == "AWAITING_USER" and value["capture_blocked"] is True
            assert value["identity_ref"] is None and value["expected_account"] == "alice"
            assert not rig.adapter.calls and not rig.auth.saved
            context = rig.factory.created_contexts[0]
            assert context.pages[0].navigation[0] == rig.adapter.login_url
            assert context.storage_calls == []
            owner = rig.service._owner(rig.service.store.get(value["login_id"]))
            assert owner.kind == "login" and owner.identity_ref is None
            for operation in (rig.manager.context, rig.manager.save_auth):
                with pytest.raises(BusinessError) as caught:
                    await operation(value["session_id"], owner)
                assert caught.value.code == "FORBIDDEN"
            assert not rig.auth.saved and not context.storage_calls
            assert count(rig, "runs") == count(rig, "model_generations") == count(rig, "identities") == 0
            assert {"candidate_identity_ref", "auth_ref", "auth_sha256", "manager_id"}.isdisjoint(value)
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("status,reason", [(IdentityStatus.NOT_AUTHENTICATED, "not_authenticated"),
    (IdentityStatus.ACCOUNT_MISMATCH, "account_mismatch"), (IdentityStatus.UNVERIFIABLE, "unverifiable")])
def test_confirmation_without_matching_authenticated_account_does_not_export_or_publish(rig, status, reason):
    async def exercise():
        try:
            rig.adapter.answers = [rig.adapter.result(status, None)]
            created = await create(rig)
            value = await rig.service.confirm(created["login_id"], created["state_version"])
            assert value["state"] == "NEEDS_LOGIN" and value["reason"] == reason and value["identity_ref"] is None
            assert rig.auth.saved == [] and len(rig.adapter.calls) == 1
            assert count(rig, "identities") == count(rig, "identity_verifications") == 0
            assert rig.factory.created_contexts[0].storage_calls == []
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_success_double_checks_signals_then_atomically_publishes_encrypted_receipt(rig):
    async def exercise():
        try:
            value = await verify(rig)
            assert value["state"] == "VERIFIED" and value["identity_ref"].startswith("identity-")
            assert value["capture_blocked"] and len(rig.adapter.calls) == 2
            identity = rig.service.store.get_identity(value["identity_ref"])
            assert identity.normalized_account == "alice" and identity.requires_identity_check
            assert rig.auth.saved[0][1] == {"site_id": rig.adapter.site_id,
                "identity_ref": value["identity_ref"], "realm": "webarena"}
            original = rig.service.store.get(value["login_id"])
            session = rig.manager.registry.get(value["session_id"], rig.service._owner(original))
            assert session.owner.identity_ref is None and session.auth_ref is None
            assert count(rig, "identity_verifications") == count(rig, "identities") == 1
            assert count(rig, "runs") == count(rig, "model_generations") == 0
            with connect(rig.manager.settings.business_db) as db:
                assert SECRET not in "\n".join(db.iterdump())
            identities = await rig.service.list_identities()
            assert len(identities) == 1 and identities[0]["requires_recheck"] is True
            assert "auth_ref" not in identities[0] and SECRET not in json.dumps(value)
            with pytest.raises(BusinessError):
                await rig.manager.context(value["session_id"], rig.service._owner(original))
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_manager_cannot_export_before_confirmation_or_under_wrong_candidate(rig):
    async def exercise():
        try:
            value = await create(rig)
            login = rig.service.store.get(value["login_id"])
            owner = rig.service._owner(login)
            with pytest.raises(BusinessError) as caught:
                await rig.manager.export_auth_for_identity(value["session_id"], owner, "identity-unpublished",
                                                            expected_version=login.state_version)
            assert caught.value.code == "STATE_CONFLICT"
            checking = rig.service.store.begin_confirm(login.login_id, login.state_version)
            candidate = rig.service.store.identity_candidate(checking.login_id, checking.state_version, "alice")
            with pytest.raises(BusinessError):
                await rig.manager.export_auth_for_identity(value["session_id"], owner, "identity-wrong",
                                                            expected_version=candidate.state_version)
            assert not rig.auth.saved and rig.factory.created_contexts[0].storage_calls == []
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["account", "evidence", "url", "not_authenticated"])
def test_changed_verification_after_save_leaves_receipt_unpublished(rig, change):
    async def exercise():
        try:
            second = {"account": replace(rig.adapter.result(), account="mallory"),
                      "evidence": replace(rig.adapter.result(), evidence_sha256="b" * 64),
                      "url": replace(rig.adapter.result(), verification_url="http://wrong.invalid/identity"),
                      "not_authenticated": rig.adapter.result(IdentityStatus.NOT_AUTHENTICATED, None)}[change]
            rig.adapter.answers = [rig.adapter.result(), second]
            value = await verify(rig)
            assert value["state"] == "NEEDS_LOGIN" and value["identity_ref"] is None
            assert len(rig.auth.saved) == 1
            assert count(rig, "identities") == count(rig, "identity_verifications") == 0
            assert count(rig, "browser_auth_snapshots") == 0
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_different_document_tokens_across_fresh_verification_navigations_are_expected(rig):
    async def exercise():
        try:
            rig.adapter.answers = [rig.adapter.result(), replace(rig.adapter.result(), document_token="document-two")]
            assert (await verify(rig))["state"] == "VERIFIED"
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["adapter", "encryption", "publication"])
def test_failures_never_publish_verified_or_leak_raw_exception(rig, monkeypatch, phase):
    async def exercise():
        try:
            def fail(*args, **kwargs):
                raise RuntimeError(SECRET)
            if phase == "adapter":
                rig.adapter.error = RuntimeError(SECRET)
            elif phase == "encryption":
                monkeypatch.setattr(rig.auth, "save", fail)
            else:
                monkeypatch.setattr(rig.service.store, "finalize_verified", fail)
            value = await verify(rig)
            assert value["state"] == "NEEDS_LOGIN" and value["identity_ref"] is None
            assert SECRET not in json.dumps(value)
            assert count(rig, "identities") == count(rig, "identity_verifications") == 0
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_stale_and_concurrent_confirmation_conflict_before_extra_browser_reads(rig):
    async def exercise():
        try:
            created = await create(rig)
            with pytest.raises(BusinessError) as stale:
                await rig.service.confirm(created["login_id"], created["state_version"] - 1)
            assert stale.value.code == "STATE_CONFLICT" and not rig.adapter.calls
            rig.adapter.gate = asyncio.Event()
            first = asyncio.create_task(rig.service.confirm(created["login_id"], created["state_version"]))
            await rig.adapter.started.wait()
            with pytest.raises(BusinessError) as concurrent:
                await rig.service.confirm(created["login_id"], created["state_version"])
            assert concurrent.value.code == "STATE_CONFLICT" and len(rig.adapter.calls) == 1
            rig.adapter.gate.set()
            assert (await first)["state"] == "VERIFIED"
            assert count(rig, "identity_verifications") == 1
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_cancelled_confirmation_releases_claim_without_publishing(rig):
    async def exercise():
        try:
            created = await create(rig)
            rig.adapter.gate = asyncio.Event()
            checking = asyncio.create_task(rig.service.confirm(created["login_id"], created["state_version"]))
            await rig.adapter.started.wait()
            checking.cancel()
            with pytest.raises(asyncio.CancelledError):
                await checking
            value = await rig.service.get(created["login_id"])
            assert value["state"] == "NEEDS_LOGIN" and value["reason"] == "operation_cancelled"
            assert not rig.auth.saved and count(rig, "identities") == 0
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_confirmation_deadline_is_static_and_does_not_publish(rig, monkeypatch):
    monkeypatch.setattr(service_module, "CONFIRM_TIMEOUT_SECONDS", 0.01)
    async def exercise():
        try:
            rig.adapter.gate = asyncio.Event()
            value = await verify(rig)
            assert value["state"] == "NEEDS_LOGIN" and value["reason"] == "verification_timeout"
            assert not rig.auth.saved
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_explicit_close_wins_over_inflight_confirmation(rig):
    async def exercise():
        try:
            created = await create(rig)
            rig.adapter.gate = asyncio.Event()
            checking = asyncio.create_task(rig.service.confirm(created["login_id"], created["state_version"]))
            await rig.adapter.started.wait()
            current = await rig.service.get(created["login_id"])
            closed = await rig.service.close(current["login_id"], current["state_version"])
            assert closed["state"] == "CLOSED" and closed["browser_state"] == "CLOSED"
            rig.adapter.gate.set()
            assert (await checking)["state"] == "CLOSED"
            assert not rig.auth.saved and count(rig, "identities") == 0
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_window_loss_is_visible_without_reading_any_page_content(rig):
    async def exercise():
        try:
            created = await create(rig)
            await rig.factory.created_contexts[0].pages[0].close()
            value = await rig.service.get(created["login_id"])
            assert value["state"] == "LOST" and value["browser_state"] == "LOST"
            assert not rig.adapter.calls and not rig.auth.saved
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_restoration_uses_existing_scope_and_requires_fresh_confirmation(rig):
    async def exercise():
        try:
            verified = await verify(rig)
            original = rig.service.store.get_identity(verified["identity_ref"])
            await rig.service.close(verified["login_id"], verified["state_version"])
            rig.adapter.calls.clear()
            restored = await create(rig, identity=original.identity_ref)
            assert restored["state"] == "AWAITING_USER" and restored["identity_ref"] is None
            assert rig.adapter.calls == []
            login = rig.service.store.get(restored["login_id"])
            session = rig.manager.registry.get(restored["session_id"], rig.service._owner(login))
            assert session.owner.identity_ref == original.identity_ref and session.auth_ref == original.auth_ref
            assert rig.auth.loaded[-1][0] == original.auth_ref
            confirmed = await rig.service.confirm(restored["login_id"], restored["state_version"])
            assert confirmed["state"] == "VERIFIED" and confirmed["identity_ref"] == original.identity_ref
            assert count(rig, "identities") == 1 and count(rig, "identity_verifications") == 2
            assert rig.service.store.get_identity(original.identity_ref).auth_ref != original.auth_ref
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["expired", "wrong_account", "missing_key"])
def test_expired_restore_or_unavailable_key_never_claims_identity_ready(rig, failure):
    async def exercise():
        try:
            verified = await verify(rig)
            await rig.service.close(verified["login_id"], verified["state_version"])
            if failure == "missing_key":
                rig.auth.load_error = True
            value = await create(rig, identity=verified["identity_ref"])
            if failure != "missing_key":
                status = IdentityStatus.NOT_AUTHENTICATED if failure == "expired" else IdentityStatus.ACCOUNT_MISMATCH
                rig.adapter.answers = [rig.adapter.result(status, None)]
                value = await rig.service.confirm(value["login_id"], value["state_version"])
            assert value["state"] in ("NEEDS_LOGIN", "FAILED") and value["identity_ref"] is None
            assert rig.service.store.get_identity(verified["identity_ref"]).state == "NEEDS_LOGIN"
            assert count(rig, "identity_verifications") == 1
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


def test_restore_rejects_different_expected_account_before_creating_browser(rig):
    async def exercise():
        try:
            verified = await verify(rig)
            with pytest.raises(BusinessError) as caught:
                await create(rig, account="mallory", identity=verified["identity_ref"])
            assert caught.value.code == "FORBIDDEN" and len(rig.factory.created_contexts) == 1
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["launch", "navigation"])
def test_create_failure_is_queryable_and_never_collects_credentials(rig, failure):
    async def exercise():
        try:
            if failure == "launch":
                rig.factory.launch_error = True
            else:
                rig.factory.navigation_error = True
            value = await create(rig)
            assert value["state"] in ("FAILED", "NEEDS_LOGIN") and value["identity_ref"] is None
            assert SECRET not in json.dumps(value)
            assert (await rig.service.get(value["login_id"]))["identity_ref"] is None
            assert not rig.adapter.calls and not rig.auth.saved
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("version", [True, -1, 0.5, "1", None])
def test_version_input_is_strict_before_browser_verification(rig, version):
    async def exercise():
        try:
            created = await create(rig)
            with pytest.raises(BusinessError) as caught:
                await rig.service.confirm(created["login_id"], version)
            assert caught.value.code == "INVALID_PARAMETER" and not rig.adapter.calls
        finally:
            await rig.manager.aclose()
    asyncio.run(exercise())
