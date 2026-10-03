"""Own one disposable Chromium process and at most four isolated contexts.

This is an internal lifecycle service, not a browser action gateway. Starting
the service acquires a local process lock; only creating a session launches a
browser. Authentication is saved explicitly and only as an encrypted snapshot.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import os
import stat
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import async_playwright

from ..config import Settings
from ..db import connect
from ..errors import BusinessError
from ..network.config import NetworkConfig
from ..network.policy import NetworkPolicy
from ..network.proxy import EgressProxy
from ..storage import initialize_business_storage
from .models import SessionInfo, SessionOwner, identifier
from .store import SessionRegistry

OPERATION_TIMEOUT_SECONDS = 30
CLOSE_TIMEOUT_SECONDS = 5
MAX_CONTEXTS = 4

# No caller-supplied switch may replace these network restrictions. The browser
# process itself uses a deny-all proxy; each context gets its own authenticated
# policy proxy. There is no DIRECT fallback, including for localhost/link-local.
NETWORK_ARGUMENTS = (
    "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--disable-quic",
    # Headed Chrome consumes the unprefixed preference switch; Playwright's
    # default headless-shell consumes force-*. Both embedders must be covered.
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-domain-reliability",
    "--disable-breakpad",
    "--no-first-run",
    "--dns-prefetch-disable",
    "--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication,DnsOverHttps,WebTransport",
)

# Native proxy/DNS/UDP restrictions are the boundary. These unreplaceable page
# globals additionally turn off unsupported browser APIs in new page/frame
# realms, rather than silently presenting an intermittently working feature.
DISABLED_NETWORK_APIS = """(() => {
  const blocked = function() { throw new DOMException('This browser capability is disabled', 'NotAllowedError'); };
  for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
    Object.defineProperty(globalThis, name, {value: blocked, writable: false, configurable: false});
  }
})();"""


def _disable_protocol_debugging():
    # The pinned Python transport prints entire IPC payloads when DEBUGP is
    # present. Its Node driver inherits the environment, DEBUG=pw:* and
    # DEBUG_FILE enable protocol logs, and PWDEBUG enables its inspector.
    # Explicit zeroes also override npm_config_* fallbacks in the Node driver.
    for name in ("DEBUG", "DEBUG_FILE", "DEBUGP"):
        os.environ.pop(name, None)
    os.environ["PWDEBUG"] = "0"
    os.environ["PWDEBUGIMPL"] = "0"


def _unavailable(message="Managed browser is unavailable", *, field="session"):
    return BusinessError("SERVICE_UNAVAILABLE", message, status=503, field=field)


@dataclass
class _Context:
    context: Any
    browser: Any
    owner: SessionOwner
    intentional_close: bool = False
    physically_closed: bool = False
    loss_reason: str | None = None
    proxy: Any = None
    gateway_downloads: bool = False
    download_permit: Any = None
    gateway_dispatch: Any = None
    used_gateway_steps: set[str] = field(default_factory=set)


class ManagedBrowser:
    def __init__(self, settings: Settings, *, auth_store=None,
                 playwright_factory=async_playwright, headless=False, launch_options=None,
                 network_config=None, proxy_factory=EgressProxy):
        self.settings = settings
        self.registry = SessionRegistry(settings.business_db)
        self.manager_id = str(uuid4())
        self._auth_store = auth_store
        self._factory = playwright_factory
        self._headless = headless
        self._network_config = network_config if network_config is not None else NetworkConfig.from_env()
        self._proxy_factory = proxy_factory
        self._proxies: dict[int, Any] = {}
        self._background_proxy = None
        self._launch_options = dict(launch_options or {})
        # Only harmless fixture diagnostics can be added. In particular no
        # proxy, DNS, feature, profile, executable or extension override exists.
        allowed = {"args", "timeout", "slow_mo"}
        if set(self._launch_options) - allowed or type(headless) is not bool:
            raise BusinessError("INVALID_PARAMETER", "Unsupported managed browser options", field="browser")
        args = self._launch_options.get("args", [])
        if type(args) is not list or any(arg not in ("--enable-automation",) for arg in args):
            raise BusinessError("INVALID_PARAMETER", "Unsupported managed browser arguments", field="browser")
        self._gate = asyncio.Lock()
        self._lock_fd = None
        self._started = False
        self._shutdown = False
        self._shutdown_task = None
        self._playwright = None
        self._playwright_context = None
        self._browser = None
        self._contexts: dict[str, _Context] = {}
        self._event_tasks: set[asyncio.Task] = set()
        self._event_failed = False
        self._uncertain_resource = False
        self._resource_tasks: set[asyncio.Task] = set()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.aclose()

    async def start(self):
        async with self._gate:
            if self._shutdown:
                raise _unavailable("Managed browser has been shut down")
            if self._started:
                return self
            directory = self.settings.data_dir / "sessions"
            fd = None
            directory_fd = None
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                fd = os.open("manager.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise OSError("invalid lock file")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                initialize_business_storage(self.settings)
                self.registry.recover_orphans(self.manager_id)
                self._lock_fd, fd = fd, None
                self._started = True
            except BlockingIOError:
                raise BusinessError("RESOURCE_CONFLICT", "Another browser manager owns this data directory",
                                    status=409, field="session") from None
            except Exception:
                raise _unavailable("Cannot initialize managed browser storage") from None
            finally:
                if fd is not None:
                    os.close(fd)
                if directory_fd is not None:
                    os.close(directory_fd)
            return self

    def _ready(self):
        if not self._started or self._shutdown:
            raise _unavailable("Managed browser is not running")
        if self._event_failed:
            raise _unavailable("Managed browser lifecycle could not be persisted")
        if self._uncertain_resource:
            raise _unavailable("Managed browser resource creation was interrupted")

    def _authentication(self):
        if self._auth_store is None:
            from .auth import AuthStateStore
            self._auth_store = AuthStateStore(self.settings.data_dir / "sessions" / "auth")
        return self._auth_store

    async def _new_proxy(self, policy):
        proxy = self._proxy_factory(policy)
        self._proxies[id(proxy)] = proxy
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                await proxy.start()
            return proxy
        except (Exception, asyncio.CancelledError):
            cleanup = asyncio.create_task(self._close_proxy(proxy))
            await asyncio.shield(cleanup)
            raise

    async def _close_proxy(self, proxy):
        if proxy is None or id(proxy) not in self._proxies:
            return True
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                await proxy.aclose()
            self._proxies.pop(id(proxy), None)
            return True
        except Exception:
            return False

    @staticmethod
    def _proxy_options(proxy):
        return {**proxy.playwright_proxy, "bypass": "<-loopback>"}

    async def _install_network_boundary(self, context, proxy):
        async def route(request_route):
            try:
                permitted = urlsplit(request_route.request.url).scheme in ("http", "https") and proxy.active
            except Exception:
                permitted = False
            if permitted:
                await request_route.continue_()
            else:
                await request_route.abort("blockedbyclient")

        async def websocket(socket_route):
            await socket_route.close(code=1008, reason="Managed browser WebSockets are disabled")

        # Context handlers exist before the first page, so initial popup and
        # iframe requests cannot race installation. The proxy independently
        # checks every network connection, including redirected destinations.
        await context.route("**/*", route)
        await context.route_web_socket("**/*", websocket)
        await context.add_init_script(DISABLED_NETWORK_APIS)

    async def _allocate(self, awaitable, accept):
        """A cancelled caller must not orphan a remotely created resource.

        Playwright commands already sent to its driver can finish after Python
        cancellation. Keep the command alive briefly to collect the resulting
        resource; if it remains uncertain, reject new work until shutdown.
        """
        task = asyncio.create_task(awaitable)
        self._resource_tasks.add(task)
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                result = await asyncio.shield(task)
            accept(result)
            return result
        except (Exception, asyncio.CancelledError):
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    result = await asyncio.shield(task)
                accept(result)
            except BaseException:
                if not task.done():
                    self._uncertain_resource = True
                    # A retained completion callback still records the resource
                    # if the driver eventually answers before shutdown.
                    def late(completed):
                        if not completed.cancelled() and completed.exception() is None:
                            accept(completed.result())
                    task.add_done_callback(late)
            raise
        finally:
            task.add_done_callback(self._resource_tasks.discard)

    async def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._playwright is None:
            _disable_protocol_debugging()
            self._playwright_context = self._factory()
            await self._allocate(self._playwright_context.start(), lambda value: setattr(self, "_playwright", value))
        if self._background_proxy is None:
            self._background_proxy = await self._new_proxy(NetworkPolicy(realm="webarena", webarena_endpoints=()))
        options = {"timeout": OPERATION_TIMEOUT_SECONDS * 1000, **self._launch_options,
                   "proxy": self._proxy_options(self._background_proxy),
                   "args": [*self._launch_options.get("args", []), *NETWORK_ARGUMENTS]}

        def accept(browser):
            self._browser = browser
            browser.on("disconnected", lambda *_: self._disconnected(browser))
        return await self._allocate(self._playwright.chromium.launch(headless=self._headless, **options), accept)

    def _spawn_event(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._event_tasks.add(task)

        def done(completed):
            self._event_tasks.discard(completed)
            if completed.cancelled():
                self._event_failed = True
            elif completed.exception() is not None:
                # Do not log raw browser exceptions: they can include URLs or
                # page content. Future operations fail closed on durable errors.
                self._event_failed = True
        task.add_done_callback(done)

    def _queue_loss(self, session_id, entry, reason):
        if entry.intentional_close or self._shutdown:
            return
        entry.loss_reason = entry.loss_reason or reason
        self._spawn_event(self._lose(session_id, entry, entry.loss_reason))

    def _context_closed(self, session_id, entry):
        entry.physically_closed = True
        if entry.loss_reason == "window_closed" and not entry.intentional_close:
            entry.loss_reason = "context_closed"
        self._queue_loss(session_id, entry, "context_closed")

    def _page_closed(self, session_id, entry):
        if entry.intentional_close or self._shutdown:
            return
        if not any(not page.is_closed() for page in entry.context.pages):
            # Record the observed fact synchronously: an already queued normal
            # close must not relabel user window loss while waiting for _gate.
            self._queue_loss(session_id, entry, "window_closed")
        else:
            self._spawn_event(self._last_page_closed(session_id, entry))

    def _watch_page(self, session_id, entry, page):
        page.on("close", lambda *_: self._page_closed(session_id, entry))
        page.on("crash", lambda *_: self._queue_loss(session_id, entry, "page_crashed"))
        page.on("download", lambda download: self._watch_download(entry, page, download))

    def _watch_download(self, entry, page, download):
        permit = entry.download_permit
        allowed = bool(entry.gateway_downloads and permit is not None
                       and permit[0] is page and permit[1] == download.url)
        # Consume synchronously: a second download from the same click has no
        # permission, even before the asynchronous qualification check runs.
        if allowed:
            entry.download_permit = None
        self._spawn_event(self._check_download(entry, download, permit if allowed else None))

    async def _check_download(self, entry, download, permit):
        try:
            if permit is not None:
                self.registry.validate_execution(entry.owner, permit[2])
                return
        except BusinessError:
            pass
        except Exception:
            # Failure to read persistent authority must also cancel bytes.
            self._event_failed = True
        async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
            await download.cancel()

    @asynccontextmanager
    async def download_permission(self, session_id, owner, *, execution_token, page, attachment_url):
        """One exact attachment from a gateway-owned click; no public RPC."""
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            self.registry.validate_execution(entry.owner, execution_token)
            if (entry.owner.kind != "run" or not entry.gateway_downloads
                    or page not in entry.context.pages or page.is_closed()
                    or type(attachment_url) is not str or entry.download_permit is not None):
                raise BusinessError("FORBIDDEN", "Controlled gateway download is unavailable", status=403)
            permit = (page, attachment_url, execution_token, object())
            entry.download_permit = permit
        try:
            yield
        finally:
            if entry.download_permit is permit:
                entry.download_permit = None

    def _disconnected(self, browser):
        if self._browser is browser:
            self._browser = None
        for session_id, entry in tuple(self._contexts.items()):
            if entry.browser is browser:
                entry.physically_closed = True
                self._queue_loss(session_id, entry, "browser_disconnected")

    async def _last_page_closed(self, session_id, entry):
        async with self._gate:
            if entry.intentional_close or self._shutdown or self._contexts.get(session_id) is not entry:
                return
            if not any(not page.is_closed() for page in entry.context.pages):
                entry.loss_reason = entry.loss_reason or "window_closed"
                await self._lose_locked(session_id, entry, entry.loss_reason)

    async def _lose(self, session_id, entry, reason):
        async with self._gate:
            if entry.intentional_close or self._shutdown or self._contexts.get(session_id) is not entry:
                return
            await self._lose_locked(session_id, entry, entry.loss_reason or reason)

    async def _lose_locked(self, session_id, entry, reason):
        try:
            self.registry.lost(session_id, self.manager_id, reason)
        finally:
            # A crashed page can leave its context alive. Capacity is released
            # only after physical close, including when persistence fails.
            await self._dispose(session_id, entry)

    async def _dispose(self, session_id, entry):
        entry.intentional_close = True
        proxy_closed = await self._close_proxy(entry.proxy)
        if not entry.physically_closed and entry.browser.is_connected():
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await entry.context.close()
                entry.physically_closed = True
            except Exception:
                pass
        if proxy_closed and (entry.physically_closed or not entry.browser.is_connected()):
            self._contexts.pop(session_id, None)
            return True
        return False

    async def create(self, owner: SessionOwner, auth_ref: str | None = None,
                     replaces: str | None = None, *, execution_token=None,
                     gateway_downloads=False) -> SessionInfo:
        if not isinstance(owner, SessionOwner):
            raise BusinessError("INVALID_PARAMETER", "SessionOwner is required", field="owner")
        owner = SessionOwner(**asdict(owner))
        if type(gateway_downloads) is not bool:
            raise BusinessError("INVALID_PARAMETER", "Invalid gateway download option", field="gateway_downloads")
        if gateway_downloads and (owner.kind != "run" or execution_token is None):
            raise BusinessError("FORBIDDEN", "Controlled downloads require a scheduled Run", status=403)
        await self.start()
        async with self._gate:
            self._ready()
            if len(self._contexts) >= MAX_CONTEXTS:
                raise BusinessError("RESOURCE_CONFLICT", "Four managed browser contexts are already reserved",
                                    status=409, field="session")
            record = self.registry.reserve(self.manager_id, owner, auth_ref=auth_ref, replaces=replaces,
                                           execution_token=execution_token)
            owner = record.owner
            entry = None
            proxy = None
            reason = "auth_unavailable" if auth_ref else "launch_failed"
            try:
                state = None
                if auth_ref is not None:
                    async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                        state = await asyncio.to_thread(self._authentication().load, auth_ref,
                            site_id=owner.site_id, identity_ref=owner.identity_ref, realm=owner.realm,
                            expected_sha256=record.auth_sha256)
                reason = "launch_failed"
                browser = await self._ensure_browser()
                reason = "context_create_failed"
                proxy = await self._new_proxy(self._network_config.policy_for(owner.realm))
                def accept(context):
                    nonlocal entry
                    entry = _Context(context, browser, owner, proxy=proxy, gateway_downloads=gateway_downloads)
                    self._contexts[record.session_id] = entry
                    context.on("close", lambda *_: self._context_closed(record.session_id, entry))
                    context.on("page", lambda page: self._watch_page(record.session_id, entry, page))
                context = await self._allocate(browser.new_context(
                    proxy=self._proxy_options(proxy), service_workers="block", accept_downloads=gateway_downloads,
                    **({"storage_state": state} if state is not None else {})), accept)
                async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                    await self._install_network_boundary(context, proxy)
                    page = await context.new_page()
                if (entry.loss_reason or entry.physically_closed or not browser.is_connected()
                        or not proxy.active or page.is_closed()):
                    raise _unavailable()
                self.registry.validate_execution(owner, execution_token, allow_reconciling=True)
                result = self.registry.opened(record.session_id, self.manager_id, execution_token=execution_token)
            except (Exception, asyncio.CancelledError) as error:
                reason = "operation_cancelled" if isinstance(error, asyncio.CancelledError) else (
                    entry.loss_reason if entry and entry.loss_reason else reason)
                try:
                    self.registry.lost(record.session_id, self.manager_id, reason)
                except Exception:
                    self._event_failed = True
                finally:
                    if entry is not None:
                        # Shield cleanup from the cancellation that interrupted
                        # creation; never silently keep an untracked context.
                        cleanup = asyncio.create_task(self._dispose(record.session_id, entry))
                        await asyncio.shield(cleanup)
                    elif proxy is not None:
                        cleanup = asyncio.create_task(self._close_proxy(proxy))
                        await asyncio.shield(cleanup)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise _unavailable("Managed browser session could not be created",
                                   field="auth_ref" if reason == "auth_unavailable" else "session") from None
            return result

    def _owned_open(self, session_id, owner):
        record = self.registry.get(session_id, owner)
        if record.manager_id != self.manager_id:
            raise BusinessError("STATE_CONFLICT", "Session belongs to another manager generation", status=409)
        entry = self._contexts.get(session_id)
        if (record.state != "OPEN" or entry is None or entry.loss_reason or entry.physically_closed
                or not entry.browser.is_connected()
                or entry.proxy is not None and not entry.proxy.active
                or not any(not page.is_closed() for page in entry.context.pages)):
            raise BusinessError("STATE_CONFLICT", "Managed session is no longer open", status=409)
        return entry

    async def context(self, session_id: str, owner: SessionOwner, *, execution_token=None):
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            if entry.owner.kind == "login":
                raise BusinessError("FORBIDDEN", "Login sessions are excluded from automated collection", status=403)
            self.registry.validate_execution(entry.owner, execution_token)
            return entry.context

    async def recovery_context(self, session_id: str, owner: SessionOwner, *, execution_token):
        """Private gateway-only access to a currently owned recovery surface.

        Ordinary context() remains closed to RECONCILING and stale epochs. The
        read-only recovery backend has its own fixed vocabulary and GET fence.
        """
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            self._recovery_execution(session_id, owner, execution_token)
            return entry.context

    def _recovery_execution(self, session_id, owner, execution_token):
        if owner.kind != 'run':
            raise BusinessError('FORBIDDEN', 'Recovery requires a Run context', status=403)
        with connect(self.settings.business_db) as db:
            db.execute('BEGIN')
            self.registry._execution(db, owner, execution_token, allow_reconciling=True)
            row = db.execute('''SELECT r.state,q.session_id,q.worker_id,q.worker_generation,q.epoch
                FROM runs r JOIN scheduler_context_reservations q USING(run_id)
                WHERE r.run_id=?''', (owner.owner_id,)).fetchone()
            if (row is None or row['state'] != 'RECONCILING' or row['session_id'] != session_id
                    or row['worker_id'] != execution_token.worker_id
                    or row['worker_generation'] != execution_token.worker_generation
                    or row['epoch'] != execution_token.epoch):
                raise BusinessError('RESOURCE_CONFLICT', 'Recovery surface qualification changed', status=409)

    async def recovery_proxy_credentials(self, session_id, owner, *, execution_token):
        """Private proxy challenge response under the same recovery surface guard."""
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            self._recovery_execution(session_id, owner, execution_token)
            options = self._proxy_options(entry.proxy)
            return {key: options[key] for key in ('server', 'username', 'password')}

    def _write_check_execution(self, session_id, owner, token, operation_id):
        """Private read authority; no terminal, stale or human-owned context."""
        if owner.kind != 'run':
            raise BusinessError('FORBIDDEN', 'Write checking requires a Run context', status=403)
        with connect(self.settings.business_db) as db:
            db.execute('BEGIN')
            self.registry._execution(db, owner, token, allow_reconciling=True)
            row = db.execute('''SELECT r.state,r.task_id,q.session_id,q.worker_id,q.worker_generation,q.epoch,
                s.identity_ref FROM runs r JOIN scheduler_context_reservations q USING(run_id)
                JOIN browser_sessions s USING(session_id) WHERE r.run_id=?''', (owner.owner_id,)).fetchone()
            operation = db.execute('SELECT task_id,identity_ref,target FROM write_intents WHERE operation_id=?',
                                   (operation_id,)).fetchone()
            if (row is None or operation is None or row['state'] not in ('RUNNING','VERIFYING','RECONCILING')
                    or row['session_id'] != session_id or row['worker_id'] != token.worker_id
                    or row['worker_generation'] != token.worker_generation or row['epoch'] != token.epoch
                    or operation['task_id'] != row['task_id'] or operation['identity_ref'] != row['identity_ref']
                    or operation['target'] not in token.resources):
                raise BusinessError('RESOURCE_CONFLICT', 'Write check scope changed', status=409)
            from ..controls.models import ControlPending
            from ..controls.store import ControlStore
            pending = ControlStore.pending_in_transaction(db, token.run_id)
            if pending is not None:
                raise ControlPending(pending)
            stopped = db.execute('SELECT stop_reason FROM budget_timers WHERE run_id=?', (token.run_id,)).fetchone()
            if stopped is None or stopped['stop_reason']:
                raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409)

    async def write_check_context(self, session_id, owner, *, execution_token, operation_id):
        """Used only by the gateway's GET-only write-check backend."""
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            self._write_check_execution(session_id, owner, execution_token, operation_id)
            return entry.context

    async def write_check_proxy_credentials(self, session_id, owner, *, execution_token, operation_id):
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            self._write_check_execution(session_id, owner, execution_token, operation_id)
            options = self._proxy_options(entry.proxy)
            return {key: options[key] for key in ('server', 'username', 'password')}

    def _gateway_execution(self, session_id, entry, token, step_id, operation_id):
        if (entry.owner.kind != "run" or entry.gateway_dispatch is None
                or entry.gateway_dispatch != (token, step_id, operation_id)):
            raise BusinessError("FORBIDDEN", "Current single-use gateway dispatch permission is required", status=403)
        return self.registry.validate_gateway_dispatch(session_id, entry.owner,
            execution_token=token, step_id=step_id, operation_id=operation_id)

    @asynccontextmanager
    async def gateway_dispatch_permission(self, session_id, owner, *, execution_token, step_id, operation_id):
        """One current INTENT, without authorizing unresolved write replay."""
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            if entry.gateway_dispatch is not None or step_id in entry.used_gateway_steps:
                raise BusinessError("STATE_CONFLICT", "Gateway step cannot be dispatched twice", status=409)
            self.registry.validate_gateway_dispatch(session_id, entry.owner,
                execution_token=execution_token, step_id=step_id, operation_id=operation_id)
            permit = (execution_token, step_id, operation_id)
            entry.used_gateway_steps.add(step_id)
            entry.gateway_dispatch = permit
        try:
            yield
        finally:
            if entry.gateway_dispatch is permit:
                entry.gateway_dispatch = None

    async def gateway_context(self, session_id, owner, *, execution_token, step_id, operation_id):
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            self._gateway_execution(session_id, entry, execution_token, step_id, operation_id)
            return entry.context

    async def gateway_proxy_credentials(self, session_id, owner, *, execution_token,
                                        step_id=None, operation_id=None):
        """Ephemeral managed proxy auth for the internal CDP request guard.

        This does not expose site credentials and has no public RPC operation.
        Callers may answer only a matching native Proxy challenge, never a
        website's Server authentication challenge.
        """
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            if entry.owner.kind != "run":
                raise BusinessError("FORBIDDEN", "Gateway proxy auth requires a Run", status=403)
            if step_id is not None or operation_id is not None:
                self._gateway_execution(session_id, entry, execution_token, step_id, operation_id)
            else:
                self.registry.validate_execution(entry.owner, execution_token)
            options = self._proxy_options(entry.proxy)
            return {key: options[key] for key in ("server", "username", "password")}

    async def login_context(self, session_id: str, owner: SessionOwner):
        """Internal identity-service access for navigation and fixed auth signals.

        This is never an RPC operation or a model/automation collection entry
        point. Login contexts remain excluded even after successful confirmation;
        subsequent task Runs must create their own context and recheck identity.
        """
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            if entry.owner.kind != "login":
                raise BusinessError("FORBIDDEN", "A dedicated login session is required", status=403)
            return entry.context

    def _check_login_export(self, session_id, owner, identity_ref, expected_version):
        if type(expected_version) is not int or expected_version < 0:
            raise BusinessError("INVALID_PARAMETER", "Invalid login state version", field="expected_version")
        with connect(self.settings.business_db) as db:
            row = db.execute("""SELECT state,state_version,session_id,manager_id,candidate_identity_ref
                FROM login_requests WHERE login_id=?""", (owner.owner_id,)).fetchone()
        if (row is None or row["state"] != "VERIFYING" or row["state_version"] != expected_version
                or row["session_id"] != session_id or row["manager_id"] != self.manager_id
                or row["candidate_identity_ref"] != identity_ref):
            raise BusinessError("STATE_CONFLICT", "Login confirmation no longer authorizes authentication export",
                                status=409)

    async def export_auth_for_identity(self, session_id: str, owner: SessionOwner,
                                       verified_identity_ref: str, *, expected_version: int):
        """Return only an encrypted receipt for a durably claimed confirmation.

        The anonymous browser owner is immutable. The identity service publishes
        the receipt and final account reference in a separate atomic transaction
        after its second fixed-signal check; no browser auth binding occurs here.
        A cancelled OS/file write can leave an unpublished encrypted receipt.
        """
        identity_ref = identifier(verified_identity_ref, "identity_ref")
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            if entry.owner.kind != "login":
                raise BusinessError("FORBIDDEN", "A dedicated login confirmation is required", status=403)
            owner = entry.owner
            self._check_login_export(session_id, owner, identity_ref, expected_version)
            try:
                async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                    state = await entry.context.storage_state(indexed_db=True)
                    snapshot = await asyncio.to_thread(self._authentication().save, state,
                        site_id=owner.site_id, identity_ref=identity_ref, realm=owner.realm)
                self._owned_open(session_id, owner)
                self._check_login_export(session_id, owner, identity_ref, expected_version)
                return snapshot
            except BusinessError:
                raise
            except Exception:
                raise _unavailable("Authentication state could not be saved", field="auth_ref") from None

    async def save_auth(self, session_id: str, owner: SessionOwner, *, execution_token=None):
        async with self._gate:
            self._ready()
            entry = self._owned_open(session_id, owner)
            if entry.owner.kind == "login":
                raise BusinessError("FORBIDDEN", "Login authentication requires explicit identity confirmation", status=403)
            self.registry.validate_execution(entry.owner, execution_token)
            owner = entry.owner
            if owner.identity_ref is None:
                raise BusinessError("INVALID_PARAMETER", "Authentication snapshots require an identity reference",
                                    field="identity_ref")
            try:
                async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                    state = await entry.context.storage_state(indexed_db=True)
                    snapshot = await asyncio.to_thread(self._authentication().save, state,
                        site_id=owner.site_id, identity_ref=owner.identity_ref, realm=owner.realm)
                self._owned_open(session_id, owner)
                self.registry.validate_execution(owner, execution_token)
                self.registry.bind_auth(session_id, self.manager_id, snapshot.ref, snapshot.sha256,
                                        execution_token=execution_token)
                return snapshot
            except BusinessError:
                raise
            except Exception:
                raise _unavailable("Authentication state could not be saved", field="auth_ref") from None

    async def _close_locked(self, session_id, entry):
        if not entry.intentional_close and not any(not page.is_closed() for page in entry.context.pages):
            entry.loss_reason = entry.loss_reason or "window_closed"
        if entry.loss_reason:
            self.registry.lost(session_id, self.manager_id, entry.loss_reason)
        self.registry.closing(session_id, self.manager_id)
        entry.intentional_close = True
        if await self._dispose(session_id, entry):
            return self.registry.closed(session_id, self.manager_id)
        return self.registry.lost(session_id, self.manager_id, "close_failed")

    async def close_preparation(self, session_id, owner, *, execution_token, paused_settlement=False):
        """Close a failed preparation only while its original owner permits it."""
        return await self.close(session_id, owner, _preparation_token=execution_token,
                                _paused_settlement=paused_settlement)

    async def close_terminal(self, session_id, owner):
        """Automatic terminal cleanup retains a human-controlled window."""
        return await self.close(session_id, owner, _terminal_cleanup=True)

    async def close(self, session_id: str, owner: SessionOwner, *, _preparation_token=None,
                    _paused_settlement=False, _terminal_cleanup=False) -> SessionInfo:
        async with self._gate:
            self._ready()
            record = self.registry.get(session_id, owner)
            if record.manager_id != self.manager_id:
                raise BusinessError("STATE_CONFLICT", "Session belongs to another manager generation", status=409)
            if _preparation_token is not None:
                record = self.registry.closing_preparation(session_id, self.manager_id, owner,
                    execution_token=_preparation_token, paused_settlement=_paused_settlement)
            elif _terminal_cleanup:
                record = self.registry.closing_terminal(session_id, self.manager_id, owner)
            entry = self._contexts.get(session_id)
            if entry is None:
                if record.state in ("CLOSED", "LOST"):
                    return record
                return self.registry.lost(session_id, self.manager_id, "context_closed")
            try:
                return await self._close_locked(session_id, entry)
            except asyncio.CancelledError:
                try:
                    self.registry.lost(session_id, self.manager_id, "operation_cancelled")
                except Exception:
                    self._event_failed = True
                finally:
                    cleanup = asyncio.create_task(self._dispose(session_id, entry))
                    await asyncio.shield(cleanup)
                raise
            except Exception:
                self._event_failed = True
                try:
                    self.registry.lost(session_id, self.manager_id, "close_failed")
                except Exception:
                    pass
                finally:
                    cleanup = asyncio.create_task(self._dispose(session_id, entry))
                    await asyncio.shield(cleanup)
                raise _unavailable("Managed browser close could not be persisted") from None

    async def drain_events(self):
        # Give Playwright's event callbacks and done callbacks a chance to run.
        await asyncio.sleep(0)
        while self._event_tasks:
            await asyncio.gather(*tuple(self._event_tasks), return_exceptions=True)
            await asyncio.sleep(0)
        if self._event_failed:
            raise _unavailable("Managed browser lifecycle could not be persisted")

    async def aclose(self):
        if self._shutdown_task is None or self._shutdown_task.done() and self._lock_fd is not None:
            self._shutdown_task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._shutdown_task)
            raise

    async def _stop(self):
        self._shutdown = True
        failure = False
        async with self._gate:
            for session_id, entry in tuple(self._contexts.items()):
                try:
                    if entry.loss_reason:
                        self.registry.lost(session_id, self.manager_id, entry.loss_reason)
                    await self._close_locked(session_id, entry)
                except Exception:
                    failure = True
            browser_closed = (not self._uncertain_resource and self._browser is None
                              or self._browser is not None and not self._browser.is_connected())
            if not browser_closed:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                        if self._browser is None:
                            raise RuntimeError("Browser creation is not settled")
                        await self._browser.close()
                    browser_closed = True
                except Exception:
                    browser_closed = self._browser is not None and not self._browser.is_connected()
            driver_stopped = self._playwright_context is None
            if not driver_stopped:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                        if self._playwright is not None:
                            await self._playwright.stop()
                        else:
                            await self._playwright_context.__aexit__(None, None, None)
                    driver_stopped = True
                    self._playwright = None
                    self._playwright_context = None
                except Exception:
                    pass
            if not driver_stopped and (self._uncertain_resource or not browser_closed):
                # Retain the lock while an owned browser might still survive.
                # A later aclose can retry, and process exit releases the lock.
                for session_id in tuple(self._contexts):
                    self.registry.lost(session_id, self.manager_id, "shutdown_failed")
                raise _unavailable("Owned browser process could not be stopped")
            for task in tuple(self._resource_tasks):
                task.cancel()
            if self._resource_tasks:
                _, pending = await asyncio.wait(tuple(self._resource_tasks), timeout=CLOSE_TIMEOUT_SECONDS)
                if pending:
                    # asyncio.wait_for/gather would wait indefinitely for a
                    # command that suppresses cancellation. Preserve the lock
                    # and resource handles for a bounded, explicit retry.
                    raise _unavailable("Browser resource cleanup did not finish")
            for proxy in tuple(self._proxies.values()):
                if not await self._close_proxy(proxy):
                    raise _unavailable("Managed network proxy could not be stopped")
            self._background_proxy = None
            self._contexts.clear()
            self._browser = None
            if self._started:
                for record in self.registry.list_owned(self.manager_id):
                    if record.state in ("OPENING", "OPEN", "CLOSING"):
                        self.registry.lost(record.session_id, self.manager_id, "shutdown_failed")
                self._started = False
            if self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None
        await self.drain_events()
        if failure:
            raise _unavailable("Managed browser shutdown could not be persisted")
