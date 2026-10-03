"""Human login preparation with fixed local identity checks and atomic publish.

No model, screenshot, tracing, password field, OTP field or broad DOM collection
is part of this service. The Worker is the sole browser owner. A browser receipt
is published only after two matching authenticated-account observations.
"""
from __future__ import annotations

import asyncio
import re

from ..config import Settings
from ..errors import BusinessError
from ..sessions.models import SessionOwner
from .sites import SiteCatalog
from .store import IdentityStore

CONFIRM_TIMEOUT_SECONDS = 60
NAVIGATION_TIMEOUT_MS = 15000


def _version(value):
    if type(value) is not int or value < 0:
        raise BusinessError("INVALID_PARAMETER", "A nonnegative login state version is required",
                            field="expected_version")
    return value


class LoginService:
    def __init__(self, settings: Settings, manager, *, store=None, catalog=None):
        self.settings = settings
        self.manager = manager
        self.store = store if store is not None else IdentityStore(settings.business_db)
        self.catalog = catalog if catalog is not None else SiteCatalog()
        self._started = False
        self._start_gate = asyncio.Lock()

    async def start(self):
        async with self._start_gate:
            if not self._started:
                await self.manager.start()
                self.store.recover_orphans(self.manager.manager_id)
                self._started = True
        return self

    @staticmethod
    def _owner(login):
        # A first login remains anonymous for its whole context lifetime. A
        # restored context is scoped to the previously established identity.
        return SessionOwner("login", login.login_id, login.site_id,
                            login.expected_identity_ref, login.realm)

    def _public(self, login):
        value = login.as_dict()
        # Treat the DTO serializer as a convenience, not as permission to add
        # future private receipt/candidate fields to the public response.
        allowed = {"login_id", "site_id", "realm", "origin", "expected_account",
                   "expected_identity_ref", "session_id", "state", "state_version",
                   "identity_ref", "reason", "created_at", "updated_at"}
        value = {name: item for name, item in value.items() if name in allowed}
        value["capture_blocked"] = True
        value["login_session_id"] = login.login_id
        value["context_id"] = login.session_id
        if login.state != "VERIFIED":
            value["identity_ref"] = None
        if login.session_id is not None:
            try:
                value["browser_state"] = self.manager.registry.get(login.session_id, self._owner(login)).state
            except BusinessError:
                value["browser_state"] = "LOST"
        else:
            value["browser_state"] = None
        return value

    def _fail(self, login, reason):
        try:
            return self.store.fail_confirm(login.login_id, login.state_version, reason)
        except BusinessError as error:
            if error.status != 409:
                raise
            # A concurrent explicit close/newer result wins. Never overwrite it
            # with the stale failure of a cancelled or delayed confirmation.
            return self.store.reconcile(login.login_id)

    async def create(self, *, site_id: str, expected_account: str,
                     expected_identity_ref: str | None = None):
        adapter = self.catalog.get(site_id)
        account = adapter.normalize_account(expected_account)
        await self.start()
        identity = None
        if expected_identity_ref is not None:
            identity = self.store.get_identity(expected_identity_ref)
            if (identity.site_id, identity.realm, identity.origin, identity.normalized_account) != (
                    adapter.site_id, adapter.realm, adapter.origin, account):
                raise BusinessError("FORBIDDEN", "Identity does not match the requested site and account", status=403)
        login = self.store.create(site_id=adapter.site_id, realm=adapter.realm, origin=adapter.origin,
            expected_account=account, expected_identity_ref=expected_identity_ref)
        phase = "browser"
        session = None
        try:
            session = await self.manager.create(self._owner(login), auth_ref=login.restore_auth_ref if identity else None)
            login = self.store.attach_session(login.login_id, login.state_version,
                                              session.session_id, self.manager.manager_id)
            phase = "navigation"
            context = await self.manager.login_context(session.session_id, self._owner(login))
            # Navigation is to trusted adapter configuration only, never an API
            # URL, selector, JavaScript string or webpage-provided destination.
            await context.pages[0].goto(adapter.login_url, wait_until="domcontentloaded",
                                        timeout=NAVIGATION_TIMEOUT_MS)
            return self._public(self.store.reconcile(login.login_id))
        except asyncio.CancelledError:
            self._fail(login, "operation_cancelled")
            if session is not None:
                await self._close_browser(session.session_id, self._owner(login))
            raise
        except Exception:
            reason = "navigation_failed" if phase == "navigation" else (
                "auth_unavailable" if identity is not None else "browser_unavailable")
            failed = self._fail(login, reason)
            if session is not None:
                await self._close_browser(session.session_id, self._owner(login))
            return self._public(failed)

    async def get(self, login_id: str):
        await self.start()
        await self.manager.drain_events()
        return self._public(self.store.reconcile(login_id))

    async def status(self, login_id: str):
        return await self.get(login_id)

    @staticmethod
    def _observation(observation, adapter, expected_account):
        status = getattr(observation.status, "value", observation.status)
        if status == "NOT_AUTHENTICATED":
            return None, "not_authenticated"
        if status == "ACCOUNT_MISMATCH":
            return None, "account_mismatch"
        if status != "VERIFIED":
            return None, "unverifiable"
        account = adapter.normalize_account(observation.account)
        if account != expected_account:
            return None, "account_mismatch"
        if (observation.verification_url != adapter.verification_url
                or type(observation.evidence_sha256) is not str
                or re.fullmatch("[0-9a-f]{64}", observation.evidence_sha256) is None):
            return None, "unverifiable"
        return (account, observation.verification_url, observation.evidence_sha256), None

    async def confirm(self, login_id: str, expected_version: int):
        expected_version = _version(expected_version)
        await self.start()
        await self.manager.drain_events()
        self.store.reconcile(login_id)
        login = self.store.begin_confirm(login_id, expected_version)
        phase = "verification"
        try:
            async with asyncio.timeout(CONFIRM_TIMEOUT_SECONDS):
                adapter = self.catalog.get(login.site_id)
                if adapter.realm != login.realm or adapter.origin != login.origin:
                    return self._public(self._fail(login, "unverifiable"))
                context = await self.manager.login_context(login.session_id, self._owner(login))
                page = context.pages[0]
                before = await adapter.confirm(page, login.expected_account)
                first, reason = self._observation(before, adapter, login.expected_account)
                if reason is not None:
                    return self._public(self._fail(login, reason))
                login = self.store.identity_candidate(login.login_id, login.state_version, first[0])
                phase = "authentication"
                receipt = await self.manager.export_auth_for_identity(login.session_id, self._owner(login),
                    login.candidate_identity_ref, expected_version=login.state_version)
                phase = "verification"
                after = await adapter.confirm(page, login.expected_account)
                second, reason = self._observation(after, adapter, login.expected_account)
                if reason is not None:
                    return self._public(self._fail(login, reason))
                if first != second:
                    return self._public(self._fail(login, "verification_changed"))
                # Check physical session state again after both awaits. The
                # store repeats durable OPEN/manager/CAS checks in its publish TX.
                await self.manager.login_context(login.session_id, self._owner(login))
                phase = "publication"
                verified = self.store.finalize_verified(login.login_id, login.state_version,
                    identity_ref=login.candidate_identity_ref, normalized_account=second[0],
                    auth_ref=receipt.ref, auth_sha256=receipt.sha256,
                    verification_origin=adapter.origin, adapter_id=adapter.adapter_id,
                    evidence_sha256=second[2])
                return self._public(verified)
        except asyncio.CancelledError:
            self._fail(login, "operation_cancelled")
            raise
        except TimeoutError:
            return self._public(self._fail(login, "verification_timeout"))
        except Exception:
            reason = {"authentication": "auth_unavailable", "publication": "storage_unavailable"}.get(
                phase, "unverifiable")
            return self._public(self._fail(login, reason))

    async def _close_browser(self, session_id, owner):
        try:
            await self.manager.close(session_id, owner)
        except Exception:
            # The session manager retains physical capacity and durable failure
            # state. Never reinterpret an uncertain close as successful login.
            pass

    async def close(self, login_id: str, expected_version: int):
        expected_version = _version(expected_version)
        await self.start()
        self.store.reconcile(login_id)
        login = self.store.close(login_id, expected_version)
        if login.session_id is not None:
            await self._close_browser(login.session_id, self._owner(login))
        return self._public(self.store.reconcile(login_id))

    async def list_identities(self):
        await self.start()
        allowed = {"identity_ref", "site_id", "realm", "origin", "normalized_account", "account",
                   "state", "status", "created_at", "updated_at", "last_verified_at", "state_version",
                   "requires_recheck", "requires_identity_check", "requires_business_check", "last_verification_id"}
        return [{name: item for name, item in identity.as_dict().items() if name in allowed}
                for identity in self.store.list_identities()]

    async def list_sites(self):
        return self.catalog.list()
