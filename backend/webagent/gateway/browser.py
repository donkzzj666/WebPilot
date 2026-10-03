"""A fixed Playwright action vocabulary over one managed Run context.

These scripts are developer-owned DOM probes. Neither the public action DTO nor
the model can supply script, selectors, requests, or filesystem paths. Captures
are transient input to the gateway journal; they are not deliverable evidence.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import stat
from typing import Any
from urllib.parse import urldefrag
from uuid import uuid4

from pydantic import TypeAdapter

from ..errors import BusinessError
from ..models.schema import Action, CoordinateLocator, DOMLocator, SemanticLocator
from ..scheduler.models import ExecutionToken, Resource, canonical_site
from ..sessions.models import SessionOwner
from ..tasks.models import SourceScope

_ACTION = TypeAdapter(Action)
_MAX_NODES = 256
_MUTATING = frozenset({"input", "keypress", "select"})
_ELEMENT_ACTIONS = frozenset({"click", "input", "keypress", "select", "download_attachment"})
_ALLOWED = frozenset({"navigate", "click", "input", "keypress", "select", "scroll",
                      "switch_tab", "read_visible", "screenshot", "download_attachment"})
_PASSIVE_TYPES = frozenset({'stylesheet', 'image', 'font', 'media', 'script'})
_CDP_PASSIVE_TYPES = {'Stylesheet': 'stylesheet', 'Image': 'image', 'Font': 'font',
                      'Media': 'media', 'Script': 'script'}

# A closure keeps revision and node identities away from DOM attributes. It
# observes all mutations, including changes outside the bounded visible sample.
# Passwords, input values, textarea text, and editable document content are not
# collected. The remaining raw sample is Worker-local, and persistent captures
# stay BLOCKED until the separate evidence/redaction module is implemented.
_INSTALL = """() => {
  const key = '__webpilotGatewayProbeV1';
  if (Object.prototype.hasOwnProperty.call(window, key)) {
    if (!window[key] || window[key].version !== 1) throw new Error('probe unavailable');
    return;
  }
  let revision = 0, serial = 0;
  const ids = new WeakMap();
  const id = e => { if (!ids.has(e)) ids.set(e, ++serial); return ids.get(e); };
  const visible = e => {
    if (!(e instanceof Element)) return false;
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.visibility !== 'collapse';
  };
  const excluded = e => e.closest('input,textarea,[contenteditable],script,style,noscript');
  const clean = s => String(s || '').replace(/\\s+/g, ' ').trim().slice(0, 400);
  const metadata = e => {
    const r = e.getBoundingClientRect();
    const attrs = {};
    for (const a of ['id','data-testid','name','href','role','aria-label','type','formaction','formmethod','download']) {
      if (e.hasAttribute(a)) attrs[a] = e.getAttribute(a).slice(0, 2048);
    }
    const labels = e.labels ? Array.from(e.labels).map(l => clean(l.textContent)).join(' ') : '';
    const labelled = (e.getAttribute('aria-labelledby') || '').split(/\\s+/).map(x => document.getElementById(x)).filter(Boolean).map(x => clean(x.textContent)).join(' ');
    const name = e.getAttribute('aria-label') || labelled || labels || e.getAttribute('alt') || (excluded(e) ? '' : clean(e.innerText));
    const form = e.form;
    return {node_id: id(e), tag: e.tagName.toLowerCase(), attrs,
      label: clean(labels), role: e.getAttribute('role') || ({A:'link',BUTTON:'button',SELECT:'combobox',TEXTAREA:'textbox',H1:'heading',H2:'heading',H3:'heading',H4:'heading',IMG:'img',INPUT: e.type === 'checkbox' ? 'checkbox' : e.type === 'radio' ? 'radio' : ['button','submit','reset'].includes(e.type) ? 'button' : 'textbox'}[e.tagName] || null),
      name: clean(name), visible: visible(e), disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true',
      readonly: !!e.readOnly, inline_handler: e.hasAttribute('onclick'), href: e.tagName === 'A' ? e.href : null,
      form_method: form ? (e.getAttribute('formmethod') || form.method || 'get').toUpperCase() : null,
      form_action: form ? (e.getAttribute('formaction') ? new URL(e.getAttribute('formaction'), document.baseURI).href : form.action) : null,
      bounds: {x:r.x,y:r.y,width:r.width,height:r.height}};
  };
  new MutationObserver(() => revision++).observe(document, {subtree:true,childList:true,attributes:true,characterData:true});
  for (const ev of ['resize','scroll','hashchange','popstate','input','change','focus','blur']) window.addEventListener(ev, () => revision++, true);
  const capture = limit => {
    const texts = [], walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    let n, size = 0, text_truncated = false;
    while ((n = walker.nextNode())) {
      const p = n.parentElement;
      if (!p || excluded(p) || !visible(p)) continue;
      // Metadata names are short, but business text has the overall capture
      // bound. Preserve JSON/string whitespace and long disclosure text.
      const original = String(n.textContent || '').trim();
      if (!original) continue;
      if (size >= limit) { text_truncated = true; break; }
      const value = original.slice(0, limit - size);
      if (value) { texts.push(value); size += value.length + 1; }
      if (value.length < original.length) { text_truncated = true; break; }
    }
    const elements = Array.from(document.querySelectorAll('a,button,input,select,textarea,[role],[data-testid],[id]')).filter(visible).slice(0, 256).map(metadata);
    return {revision, title:document.title.slice(0,2048), text:texts.join('\\n').slice(0,limit),
      text_truncated, viewport:{width:innerWidth,height:innerHeight,scroll_x:scrollX,scroll_y:scrollY,dpr:devicePixelRatio}, elements};
  };
  Object.defineProperty(window, key, {value:Object.freeze({version:1,capture,metadata}),writable:false,configurable:false});
}"""
_CAPTURE = "limit => window.__webpilotGatewayProbeV1.capture(limit)"
_HIT = "p => {const hit = document.elementFromPoint(p.x,p.y); const e = hit && (hit.closest('a,button,input,select,textarea,[role]') || hit); return e ? window.__webpilotGatewayProbeV1.metadata(e) : null;}"
_MATCH = """loc => {
  const result = [];
  for (const e of document.querySelectorAll('*')) {
    if (loc.strategy === 'dom' && e.getAttribute(loc.attribute) !== loc.value) continue;
    const m = window.__webpilotGatewayProbeV1.metadata(e);
    if (!m.visible) continue;
    if (loc.strategy === 'semantic' && ((loc.role && m.role !== loc.role) || (loc.accessible_name && m.name !== loc.accessible_name) || (loc.label && m.label !== loc.label))) continue;
    result.push(m);
    if (result.length > 256) break;
  }
  return result;
}"""
_SCROLL = "async p => {window.scrollBy(p.x,p.y); await new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve,0)));}"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _denied(message="Browser target is not permitted", *, field="target", code="FORBIDDEN", status=403):
    return BusinessError(code, message, status=status, field=field)


def _stale():
    return _denied("Browser observation is no longer current", field="snapshot_id", code="STATE_CONFLICT", status=409)


def _field(value, name, *aliases):
    for key in (name, *aliases):
        if isinstance(value, dict) and key in value:
            return value[key]
        if not isinstance(value, dict) and hasattr(value, key):
            return getattr(value, key)
    return None


def _css_string(value: str) -> str:
    """Encode every code point as a CSS string escape; never interpolate CSS."""
    if "\x00" in value:
        raise _denied("Invalid exact DOM attribute", field="locator")
    return "".join("\\" + format(ord(c), "x") + " " for c in value)


@dataclass(frozen=True)
class BrowserCapture:
    tab_id: str
    frame_id: str
    page_version: str
    page_url: str
    title: str
    width: int
    height: int
    visible_text: str
    visible_sha256: str
    dom_sha256: str
    screenshot: bytes | None
    screenshot_sha256: str | None
    links: tuple[dict, ...]
    elements: tuple[dict, ...]
    text_truncated: bool = False


@dataclass(frozen=True)
class PreparedTarget:
    action_type: str
    tab_id: str
    frame_id: str
    page_version: str
    source_url: str
    signature: str | None
    locator: Any
    action_sha256: str
    link_url: str | None = None
    coordinate: tuple[int, int] | None = None
    trusted_write: bool = False
    screenshot_sha256: str | None = None


class BrowserBackend:
    def __init__(self, managed, session_id, owner, *, allowed_sources: tuple[SourceScope, ...],
                 max_visible_chars=12000, max_download_bytes=8 * 1024 * 1024):
        if (not isinstance(owner, SessionOwner) or owner.kind != "run"
                or not allowed_sources or any(not isinstance(s, SourceScope) for s in allowed_sources)
                or type(max_visible_chars) is not int or not 1 <= max_visible_chars <= 50000
                or type(max_download_bytes) is not int or not 1 <= max_download_bytes <= 32 * 1024 * 1024):
            raise _denied("Invalid trusted browser configuration", field="browser", code="INVALID_PARAMETER", status=422)
        self.managed, self.session_id, self.owner = managed, session_id, owner
        self.allowed_sources = tuple(allowed_sources)
        self.max_visible_chars, self.max_download_bytes = max_visible_chars, max_download_bytes
        self._contexts: set[int] = set()
        self._pages: dict[str, Any] = {}
        self._page_ids: dict[int, str] = {}
        self._frames: dict[str, Any] = {}
        self._frame_ids: dict[int, str] = {}
        self._generations: dict[str, int] = {}
        self._selected_tab: str | None = None
        self._allowed_mutations: frozenset[tuple[str, str]] = frozenset()
        self._blocked_requests = 0
        self._critical_blocked_requests = 0
        self._optional_blocked_requests = 0
        self._dispatch_gate = asyncio.Lock()
        self._cdps: dict[int, Any] = {}
        self._world_name = "webpilot-gateway-" + uuid4().hex
        self._guarded: set[int] = set()
        self._guard_gates: dict[int, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._latest_token = None
        self._guard_failed = False
        self._frame_cdps: dict[int, Any] = {}
        self._dead_cdps: set[int] = set()
        self._dispatch_step_id = None
        self._dispatch_operation_id = None
        self._recovery_token = None
        self._write_check_token = None
        self._write_check_operation_id = None
        self._retired_contexts: set[int] = set()
        self._listeners: list[tuple[Any, str, Any]] = []

    def _permits(self, url):
        return type(url) is str and any(scope.permits(urldefrag(url)[0]) for scope in self.allowed_sources)

    def _same_site(self, current, destination):
        sites = {canonical_site(scope.site_id) for scope in self.allowed_sources if scope.permits(urldefrag(current)[0])}
        return any(canonical_site(scope.site_id) in sites and scope.permits(urldefrag(destination)[0])
                   for scope in self.allowed_sources)

    async def _context(self, token):
        if not isinstance(token, ExecutionToken) or token.run_id != self.owner.owner_id:
            raise _denied("A current typed Run execution qualification is required", field="execution_token",
                          code="RESOURCE_CONFLICT", status=409)
        if self._guard_failed:
            raise _denied("Browser request guard is unavailable", field="network", code="SERVICE_UNAVAILABLE", status=503)
        context = await self._managed_context(token)
        self._latest_token = token
        if id(context) not in self._contexts:
            async def method_fence(route):
                if id(context) in self._retired_contexts:
                    # A newer current gateway installed its own fence before
                    # retiring this one. Never remove the idle current fence.
                    await route.fallback()
                    return
                request = route.request
                method, url = request.method.upper(), request.url
                safe = await self._request_permitted(method, url)
                # A popup's first response can precede the public page event.
                # It cannot navigate before its native redirect guard exists.
                try:
                    request_frame = request.frame
                except Exception:
                    request_frame = None
                navigation, is_redirect, main_frame, guard_ready = None, None, None, False
                frame_page, frame_url = None, None
                try:
                    is_redirect = request.redirected_from is not None
                    frame_page = request_frame.page if request_frame is not None else None
                    if request_frame is not None and request_frame != frame_page.main_frame:
                        await self._install_frame_guard(request_frame)
                    navigation = request.is_navigation_request()
                    main_frame = request_frame is frame_page.main_frame if request_frame is not None else None
                    guard_ready = (request_frame is not None and request_frame in frame_page.frames
                        and not request_frame.is_detached() and id(frame_page) in self._guarded)
                    frame_url = request_frame.url if guard_ready else None
                    if navigation and not guard_ready:
                        safe = False
                except Exception:
                    safe = False
                    guard_ready = False
                if not safe:
                    try:
                        resource_type = request.resource_type
                    except Exception:
                        resource_type = None
                    await self._record_request_denial(method, url, resource_type=resource_type,
                        is_navigation=navigation, is_redirect=is_redirect, is_main_frame=main_frame,
                        page=frame_page, frame_url=frame_url, guard_ready=guard_ready)
                    await route.abort("blockedbyclient")
                    return
                # fallback preserves the ManagedBrowser's network policy route.
                await route.fallback()
            await context.route("**/*", method_fence)
            previous = getattr(context, '_webpilot_gateway_fence', None)
            context._webpilot_gateway_fence = (self, method_fence)
            if previous is not None and previous[0] is not self:
                previous[0]._retired_contexts.add(id(context))
                remover = getattr(context, 'unroute', None)
                if remover is not None:
                    await remover('**/*', previous[1])
            context.on("page", self._page_arrived)
            self._listeners.append((context, 'page', self._page_arrived))
            self._contexts.add(id(context))
        for page in context.pages:
            if page.is_closed():
                continue
            self._register_page(page)
            await self._install_guard(page)
            for frame in page.frames:
                if frame != page.main_frame:
                    await self._install_frame_guard(frame)
        return context

    async def _managed_context(self, token):
        if self._write_check_token is not None:
            if token != self._write_check_token:
                raise _stale()
            return await self.managed.write_check_context(self.session_id, self.owner,
                execution_token=token, operation_id=self._write_check_operation_id)
        if self._recovery_token is not None:
            if token != self._recovery_token:
                raise _stale()
            return await self.managed.recovery_context(self.session_id, self.owner, execution_token=token)
        if self._dispatch_operation_id is not None:
            return await self.managed.gateway_context(self.session_id, self.owner, execution_token=token,
                step_id=self._dispatch_step_id, operation_id=self._dispatch_operation_id)
        return await self.managed.context(self.session_id, self.owner, execution_token=token)

    async def _request_permitted(self, method, url):
        token = self._latest_token
        if (self._guard_failed or not isinstance(token, ExecutionToken) or token.run_id != self.owner.owner_id
                or not self._leased_source(url, token) or method not in ("GET", "HEAD")
                and (method, url) not in self._allowed_mutations):
            return False
        if (self._recovery_token is not None or self._write_check_token is not None) and method not in ('GET', 'HEAD'):
            return False
        try:
            # This is a fresh persistent execution/control check, not a cached
            # token flag. Late requests after pause/revocation are denied too.
            await self._managed_context(self._latest_token)
        except Exception:
            return False
        return True

    async def _record_request_denial(self, method, url, *, resource_type=None,
                                     is_navigation=None, is_redirect=None, is_main_frame=None,
                                     page=None, frame_url=None, guard_ready=False):
        """Every denied request stays denied; classify only its action outcome.

        A scoped document can remain readable when its undeclared optional
        styling/image/script is aborted. Missing qualification, main-frame identity,
        redirects, declared-site leases, document navigation and active requests remain
        critical. This classification never forwards a request or grants proxy
        credentials, and it uses no URL-extension or website-supplied hints.
        """
        optional, token = False, self._latest_token
        try:
            if (guard_ready is True and is_navigation is False and is_redirect is False
                    and is_main_frame is True and method in ('GET', 'HEAD')
                    and type(resource_type) is str and resource_type in _PASSIVE_TYPES and not self._guard_failed
                    and isinstance(token, ExecutionToken) and token.run_id == self.owner.owner_id
                    and page is not None and id(page) in self._guarded
                    and any(owned is page for owned in self._pages.values())
                    and not self._permits(url)  # Declared but unleased is still critical.
                    and self._leased_source(frame_url, token)
                    and self._leased_source(page.main_frame.url, token)):
                await self._managed_context(token)
                optional = (token == self._latest_token and not self._guard_failed
                    and not page.is_closed() and self._leased_source(page.main_frame.url, token))
        except Exception:
            optional = False
        self._blocked_requests += 1
        if optional:
            self._optional_blocked_requests += 1
        else:
            self._critical_blocked_requests += 1

    @staticmethod
    async def _native_main_frame_url(cdp, frame_id):
        """Resolve only the native root frame; child/missing identities stay unknown."""
        if type(frame_id) is not str or not frame_id:
            return None
        try:
            tree = await cdp.send('Page.getFrameTree')
            root = tree['frameTree']['frame']
            if type(root) is not dict or root.get('id') != frame_id:
                return None
            pending, matches, count = [tree['frameTree']], [], 0
            while pending:
                branch = pending.pop()
                count += 1
                if count > 256 or type(branch) is not dict or type(branch.get('frame')) is not dict:
                    return None
                frame = branch['frame']
                if frame.get('id') == frame_id:
                    matches.append(frame.get('url'))
                children = branch.get('childFrames', [])
                if type(children) is not list:
                    return None
                pending.extend(children)
            return matches[0] if len(matches) == 1 and type(matches[0]) is str else None
        except Exception:
            return None

    def _leased_source(self, url, token):
        if type(url) is not str:
            return False
        try:
            sites = {canonical_site(scope.site_id) for scope in self.allowed_sources
                     if scope.permits(urldefrag(url)[0])}
            if len(sites) != 1:
                return False
            site = next(iter(sites))
            required = Resource.site_identity(site, self.owner.identity_ref, realm=self.owner.realm).resource_key
            return required in token.resources
        except BusinessError:
            return False

    def _page_arrived(self, page):
        if self._guard_failed:
            return
        self._register_page(page)
        task = asyncio.create_task(self._install_guard(page))
        self._background_tasks.add(task)
        def complete(done):
            self._background_tasks.discard(done)
            if not page.is_closed() and (done.cancelled() or done.exception() is not None):
                self._guard_failed = True
        task.add_done_callback(complete)

    async def _install_guard(self, page):
        gate = self._guard_gates.setdefault(id(page), asyncio.Lock())
        async with gate:
            if id(page) in self._guarded:
                return
            cdp = self._cdps.get(id(page))
            if cdp is None:
                cdp = await page.context.new_cdp_session(page)
                self._cdps[id(page)] = cdp
            await self._attach_fetch(cdp, page)
            self._guarded.add(id(page))

    async def _install_frame_guard(self, frame):
        if id(frame) in self._frame_cdps or frame.is_detached():
            return
        gate = self._guard_gates.setdefault(id(frame), asyncio.Lock())
        async with gate:
            if id(frame) in self._frame_cdps:
                return
            try:
                cdp = await frame.page.context.new_cdp_session(frame)
            except Exception as error:
                # Same-process child frames share the parent's native Fetch
                # guard. Out-of-process frames expose a separate CDP target.
                if "does not have a separate CDP session" in str(error):
                    return
                raise _denied("Frame request guard is unavailable", field="network", code="SERVICE_UNAVAILABLE", status=503) from None
            await self._attach_fetch(cdp, frame.page)
            self._frame_cdps[id(frame)] = cdp

    async def _attach_fetch(self, cdp, page):
        authenticated = set()
        async def paused(event):
            request = event["request"]
            permitted = await self._request_permitted(request["method"].upper(), request["url"])
            method = "Fetch.continueRequest" if permitted else "Fetch.failRequest"
            args = {"requestId": event["requestId"]}
            if not permitted:
                native_type = event.get('resourceType')
                resource_type = _CDP_PASSIVE_TYPES.get(native_type) if type(native_type) is str else None
                frame_url = (await self._native_main_frame_url(cdp, event.get('frameId'))
                    if resource_type is not None and cdp is self._cdps.get(id(page)) else None)
                redirect_id = event.get('redirectedRequestId')
                is_redirect = (False if 'redirectedRequestId' not in event else
                    True if type(redirect_id) is str and redirect_id else None)
                await self._record_request_denial(request['method'].upper(), request['url'],
                    resource_type=resource_type, is_navigation=False if resource_type is not None else None,
                    is_redirect=is_redirect, is_main_frame=frame_url is not None,
                    page=page, frame_url=frame_url, guard_ready=id(page) in self._guarded)
                args["errorReason"] = "BlockedByClient"
            try:
                await cdp.send(method, args)
            except Exception:
                # A destroyed target has no outstanding browser authority;
                # another protocol failure closes the gateway on next use.
                if not page.is_closed() and id(cdp) not in self._dead_cdps:
                    self._guard_failed = True
        cdp.on("Fetch.requestPaused", paused)
        async def authentication(event):
            request, challenge = event["request"], event["authChallenge"]
            response = {"response": "CancelAuth"}
            request_id = event["requestId"]
            if (challenge.get("source") == "Proxy" and request_id not in authenticated
                    and len(authenticated) < 4096
                    and await self._request_permitted(request["method"].upper(), request["url"])):
                try:
                    kwargs = {"execution_token": self._latest_token}
                    if self._dispatch_operation_id is not None:
                        kwargs.update(step_id=self._dispatch_step_id, operation_id=self._dispatch_operation_id)
                    if self._write_check_token is not None:
                        credentials = await self.managed.write_check_proxy_credentials(self.session_id, self.owner,
                            execution_token=self._write_check_token, operation_id=self._write_check_operation_id)
                    elif self._recovery_token is not None:
                        credentials = await self.managed.recovery_proxy_credentials(self.session_id, self.owner,
                                                                                  execution_token=self._recovery_token)
                    else:
                        credentials = await self.managed.gateway_proxy_credentials(self.session_id, self.owner, **kwargs)
                    if challenge.get("origin", "").rstrip("/") == credentials["server"].rstrip("/"):
                        authenticated.add(request_id)
                        response = {"response": "ProvideCredentials", "username": credentials["username"],
                                    "password": credentials["password"]}
                except Exception:
                    pass
            try:
                await cdp.send("Fetch.continueWithAuth", {"requestId": request_id,
                               "authChallengeResponse": response})
            except Exception:
                if not page.is_closed() and id(cdp) not in self._dead_cdps:
                    self._guard_failed = True
        cdp.on("Fetch.authRequired", authentication)
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}],
                                       "handleAuthRequests": True})

    async def _detach(self, cdp):
        self._dead_cdps.add(id(cdp))
        try:
            async with asyncio.timeout(.5):
                await cdp.detach()
        except Exception:
            pass

    async def aclose(self):
        """Release protocol observers; the managed context retains its fence."""
        self._guard_failed, self._latest_token = True, None
        self._allowed_mutations = frozenset()
        for emitter, event, callback in self._listeners:
            remover = getattr(emitter, 'remove_listener', None)
            if remover is not None:
                remover(event, callback)
        self._listeners.clear()
        pending = set(self._background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=.5)
        sessions = {id(cdp): cdp for cdp in (*self._cdps.values(), *self._frame_cdps.values())}
        await asyncio.gather(*(self._detach(cdp) for cdp in sessions.values()))
        self._cdps.clear()
        self._frame_cdps.clear()

    def _register_frame(self, frame):
        existing = self._frame_ids.get(id(frame))
        if existing:
            return existing
        identifier = "frame_" + uuid4().hex
        self._frame_ids[id(frame)], self._frames[identifier] = identifier, frame
        self._generations[identifier] = 0
        return identifier

    def _register_page(self, page):
        existing = self._page_ids.get(id(page))
        if existing:
            for frame in page.frames:
                self._register_frame(frame)
            return existing
        identifier = "tab_" + uuid4().hex
        self._page_ids[id(page)], self._pages[identifier] = identifier, page
        for frame in page.frames:
            self._register_frame(frame)
        def navigated(frame):
            frame_id = self._register_frame(frame)
            self._generations[frame_id] += 1
            old = self._frame_cdps.pop(id(frame), None)
            if old is not None:
                self._dead_cdps.add(id(old))
                task = asyncio.create_task(self._detach(old))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
        page.on("framenavigated", navigated)
        page.on("frameattached", self._register_frame)
        self._listeners.extend(((page, 'framenavigated', navigated),
                                (page, 'frameattached', self._register_frame)))
        if self._selected_tab is None:
            self._selected_tab = identifier
        return identifier

    async def _surface(self, token, tab_id=None, frame_id=None):
        await self._context(token)
        tab_id = tab_id or self._selected_tab
        page = self._pages.get(tab_id)
        if page is None or page.is_closed():
            raise _stale()
        frame = self._frames.get(frame_id) if frame_id else page.main_frame
        if frame is None or frame not in page.frames or frame.is_detached():
            raise _stale()
        frame_id = self._register_frame(frame)
        if frame.url != "about:blank" and not self._permits(frame.url):
            raise _denied("Current browser page is outside the source scope")
        if page.url != "about:blank" and not self._permits(page.url):
            raise _denied("Current browser tab is outside the source scope")
        return page, frame, tab_id, frame_id

    async def _probe(self, page, frame, tab_id, frame_id):
        probe = await self._isolated(page, frame, _CAPTURE, self.max_visible_chars)
        if type(probe) is not dict or type(probe.get('text_truncated')) is not bool:
            raise _stale()
        viewport = page.viewport_size
        if viewport is None:
            root = probe if frame == page.main_frame else await self._isolated(page, page.main_frame, _CAPTURE, 1)
            viewport = {"width": root["viewport"]["width"], "height": root["viewport"]["height"]}
        width, height = viewport["width"], viewport["height"]
        if (type(width) is not int or type(height) is not int or width < 1 or height < 1
                or width > 10000 or height > 10000 or width * height > 16000000):
            raise _stale()
        fingerprint = _digest({"tab": tab_id, "frame": frame_id, "generation": self._generations[frame_id],
                               "url": frame.url, "viewport": viewport, "probe": probe})
        return probe, width, height, fingerprint

    async def _isolated(self, page, frame, script, args=None):
        """Run only developer constants in a CDP isolated world.

        Main-world page globals/prototypes cannot replace the observer or DOM
        primitives. An ambiguous child-frame mapping is rejected rather than
        collecting another frame under the caller's frame identifier.
        """
        if script not in (_CAPTURE, _HIT, _MATCH, _SCROLL):
            raise _denied("Unsupported internal browser probe", field="browser")
        cdp = self._frame_cdps.get(id(frame)) or self._cdps.get(id(page))
        if cdp is None:
            cdp = await page.context.new_cdp_session(page)
            self._cdps[id(page)] = cdp
        tree = (await cdp.send("Page.getFrameTree"))["frameTree"]
        if frame == page.main_frame or id(frame) in self._frame_cdps:
            target = tree["frame"]
        else:
            def walk(branch):
                for child in branch.get("childFrames", ()):
                    yield child["frame"]
                    yield from walk(child)
            matches = [candidate for candidate in walk(tree) if candidate.get("url") == frame.url
                       and candidate.get("name", "") == frame.name]
            if len(matches) != 1:
                raise _stale()
            target = matches[0]
        if target.get("url") != frame.url:
            raise _stale()
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": target["id"], "worldName": self._world_name})
        params = {"contextId": world["executionContextId"], "returnByValue": True, "awaitPromise": True}
        installed = await cdp.send("Runtime.evaluate", {**params, "expression": "(" + _INSTALL + ")()"})
        if installed.get("exceptionDetails"):
            raise _stale()
        value = await cdp.send("Runtime.evaluate", {**params, "expression": "(" + script + ")(" + json.dumps(args, ensure_ascii=True) + ")"})
        if value.get("exceptionDetails"):
            raise _stale()
        # Detect navigations that raced the protocol round trip. DOM changes
        # remain in the same observer world and alter its revision fingerprint.
        after = (await cdp.send("Page.getFrameTree"))["frameTree"]
        def all_frames(branch):
            yield branch["frame"]
            for child in branch.get("childFrames", ()):
                yield from all_frames(child)
        current = [candidate for candidate in all_frames(after) if candidate["id"] == target["id"]]
        if (len(current) != 1 or current[0].get("loaderId") != target.get("loaderId")
                or current[0].get("url") != frame.url):
            raise _stale()
        return value.get("result", {}).get("value")

    async def capture(self, token, *, tab_id=None, frame_id=None, include_screenshot=False):
        page, frame, tab_id, frame_id = await self._surface(token, tab_id, frame_id)
        probe, width, height, fingerprint = await self._probe(page, frame, tab_id, frame_id)
        screenshot = await page.screenshot(type="png", full_page=False, scale="css", caret="initial") if include_screenshot else None
        # Screenshot acquisition can yield while a page changes. It may not be
        # paired with the earlier DOM version unless the second probe agrees.
        if screenshot is not None:
            if len(screenshot) > 32 * 1024 * 1024:
                raise _denied("Screenshot exceeds the bounded capture limit", field="screenshot")
            _, _, _, after = await self._probe(page, frame, tab_id, frame_id)
            if after != fingerprint:
                raise _stale()
        elements = tuple(probe["elements"])
        return BrowserCapture(tab_id, frame_id, fingerprint, frame.url, probe["title"], width, height,
                              probe["text"], hashlib.sha256(probe["text"].encode()).hexdigest(),
                              _digest(elements), screenshot,
                              hashlib.sha256(screenshot).hexdigest() if screenshot is not None else None,
                              tuple(e for e in elements if e.get("tag") == "a" and e.get("href")), elements,
                              probe['text_truncated'])

    async def recovery_capture(self, token):
        """Fixed bounded DOM observation; no action/model/selector is accepted."""
        if self._recovery_token is not None or self._write_check_token is not None:
            raise _stale()
        self._recovery_token = token
        try:
            return await self.capture(token)
        finally:
            self._recovery_token = None

    async def recovery_navigate(self, token, url):
        """A freshly journaled recovery GET, never a replayed browser action."""
        if self._recovery_token is not None or self._write_check_token is not None or not self._leased_source(url, token):
            raise _denied('Recovery URL is outside the leased source scope', field='url')
        self._recovery_token = token
        try:
            page, frame, _, _ = await self._surface(token)
            blocked = self._critical_blocked_requests
            response = await frame.goto(url, wait_until='domcontentloaded')
            await self._managed_context(token)
            if (self._guard_failed or not self._leased_source(frame.url, token) or self._critical_blocked_requests != blocked
                    or response is not None and response.status >= 400):
                raise _denied('Recovery GET could not confirm its scoped page', field='url')
            return {'source_url': frame.url, 'http_status': response.status if response else None}
        finally:
            self._recovery_token = None

    async def write_check_capture(self, token, operation_id):
        """Read the current viewport under the fixed external-write check fence."""
        if self._recovery_token is not None or self._write_check_token is not None:
            raise _stale()
        self._write_check_token, self._write_check_operation_id = token, operation_id
        try:
            return await self.capture(token)
        finally:
            self._write_check_token = self._write_check_operation_id = None

    async def write_check_navigate(self, token, url, operation_id):
        """Scoped GET only; no selector, click, form or mutation may be replayed."""
        if (self._recovery_token is not None or self._write_check_token is not None
                or not self._leased_source(url, token)):
            raise _denied('Write query URL is outside the leased source scope', field='url')
        self._write_check_token, self._write_check_operation_id = token, operation_id
        try:
            page, frame, _, _ = await self._surface(token)
            blocked = self._critical_blocked_requests
            response = await frame.goto(url, wait_until='domcontentloaded')
            await self._managed_context(token)
            if (self._guard_failed or not self._leased_source(frame.url, token) or self._critical_blocked_requests != blocked
                    or response is not None and response.status >= 400):
                raise _denied('Write query GET could not confirm its scoped page', field='url')
            return {'source_url': frame.url, 'http_status': response.status if response else None}
        finally:
            self._write_check_token = self._write_check_operation_id = None

    @staticmethod
    def _action(action):
        try:
            if type(action) is dict:
                def encode(value):
                    if isinstance(value, datetime):
                        return value.isoformat()
                    raise TypeError()
                parsed = _ACTION.validate_json(json.dumps(action, default=encode, allow_nan=False))
            else:
                parsed = _ACTION.validate_python(action)
        except Exception:
            raise _denied("Unsupported structured browser action", field="action", code="INVALID_PARAMETER", status=422) from None
        if parsed.action_type not in _ALLOWED:
            raise _denied("Unsupported structured browser action", field="action", code="INVALID_PARAMETER", status=422)
        return parsed

    async def _locator(self, frame, locator):
        if isinstance(locator, DOMLocator):
            return frame.locator(f'[{locator.attribute}="{_css_string(locator.value)}"]')
        if isinstance(locator, SemanticLocator):
            target = None
            if locator.role:
                target = frame.get_by_role(locator.role, name=locator.accessible_name, exact=True) if locator.accessible_name else frame.get_by_role(locator.role)
            if locator.label:
                labelled = frame.get_by_label(locator.label, exact=True)
                target = labelled if target is None else target.and_(labelled)
            if target is None and locator.accessible_name:
                # Name-only proposals cannot silently fall back to a CSS
                # selector. Exact visible text/label is the conservative path.
                target = frame.get_by_text(locator.accessible_name, exact=True).or_(frame.get_by_label(locator.accessible_name, exact=True))
            return target
        raise _denied("A declared element locator is required", field="locator")

    async def _unique(self, page, frame, locator):
        target = await self._locator(frame, locator)
        count = await target.count()
        if count > _MAX_NODES:
            raise _denied("Element locator is not uniquely visible", field="locator", code="STATE_CONFLICT", status=409)
        visible = [target.nth(index) for index in range(count) if await target.nth(index).is_visible()]
        if len(visible) != 1:
            raise _denied("Element locator is not uniquely visible", field="locator", code="STATE_CONFLICT", status=409)
        candidate = visible[0]
        if not await candidate.is_enabled():
            raise _denied("Target element is disabled", field="locator", code="STATE_CONFLICT", status=409)
        matches = await self._isolated(page, frame, _MATCH, locator.model_dump(mode="json"))
        if len(matches) != 1:
            raise _denied("Element semantics are not uniquely current", field="locator", code="STATE_CONFLICT", status=409)
        metadata = matches[0]
        if not metadata.get("visible") or metadata.get("disabled"):
            raise _stale()
        return candidate, metadata

    async def _coordinate_point(self, page, frame, locator):
        x, y = locator.x, locator.y
        offset_x = offset_y = 0
        cdp = self._cdps.get(id(page))
        tree = (await cdp.send("Page.getFrameTree"))["frameTree"]
        target_id = tree["frame"]["id"]
        if frame != page.main_frame:
            def walk(branch):
                for child in branch.get("childFrames", ()):
                    yield child["frame"]
                    yield from walk(child)
            matches = [candidate for candidate in walk(tree) if candidate.get("url") == frame.url
                       and candidate.get("name", "") == frame.name]
            if len(matches) != 1:
                raise _stale()
            target_id = matches[0]["id"]
            owner = await cdp.send("DOM.getFrameOwner", {"frameId": matches[0]["id"]})
            box = (await cdp.send("DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]}))["model"]
            quad = box["content"]
            # Rotated/skewed/scaled iframe coordinates require a new supported
            # transform model; they may not use an approximate screenshot hit.
            if (len(quad) != 8 or quad[1] != quad[3] or quad[5] != quad[7]
                    or quad[0] != quad[6] or quad[2] != quad[4]):
                raise _stale()
            offset_x, offset_y = quad[0], quad[1]
            viewport = (await self._isolated(page, frame, _CAPTURE, 1))["viewport"]
            if abs(quad[2] - quad[0] - viewport["width"]) > 1 or abs(quad[7] - quad[1] - viewport["height"]) > 1:
                raise _stale()
        actual = await cdp.send("DOM.getNodeForLocation", {"x": x, "y": y,
                               "includeUserAgentShadowDOM": False, "ignorePointerEventsNone": False})
        if actual.get("frameId") != target_id:
            raise _denied("Screenshot coordinate is covered by another frame or overlay", field="locator", code="STATE_CONFLICT", status=409)
        return {"x": x - offset_x, "y": y - offset_y}

    @staticmethod
    def coordinate_check_requires_screenshot(action):
        return isinstance(BrowserBackend._action(action).target.locator, CoordinateLocator)

    async def _check_pixels(self, page, expected):
        if not expected:
            raise _stale()
        image = await page.screenshot(type="png", full_page=False, scale="css", caret="initial")
        if len(image) > 32 * 1024 * 1024 or hashlib.sha256(image).hexdigest() != expected:
            raise _stale()

    async def _coordinate(self, page, frame, locator):
        metadata = await self._isolated(page, frame, _HIT, await self._coordinate_point(page, frame, locator))
        if metadata is None or not metadata.get("visible") or metadata.get("disabled"):
            raise _denied("Screenshot coordinate does not hit an enabled target", field="locator", code="STATE_CONFLICT", status=409)
        return metadata

    async def prepare(self, token, action, snapshot, *, trusted_write=False):
        if self._recovery_token is not None or self._write_check_token is not None:
            raise _denied('Recovery permits observations and journaled GETs only', field='action')
        action = self._action(action)
        if type(trusted_write) is not bool or (action.expected_effect == "write" and not trusted_write):
            raise _denied("Write action requires trusted authorization", field="expected_effect")
        if action.action_type in _MUTATING and action.expected_effect != "write":
            raise _denied("Form interaction requires trusted authorization", field="expected_effect")
        bootstrap = (action.action_type == "navigate" and _field(snapshot, "source_url", "page_url") == "about:blank"
                     and action.target.page_url == action.args.url and self._permits(action.args.url))
        if (action.snapshot_id != _field(snapshot, "snapshot_id", "id")
                or action.target.tab_id != _field(snapshot, "tab_id")
                or action.target.frame_id != _field(snapshot, "frame_id")
                or not bootstrap and action.target.page_url != _field(snapshot, "source_url", "page_url")):
            raise _stale()
        page, frame, tab_id, frame_id = await self._surface(token, action.target.tab_id, action.target.frame_id)
        _, width, height, fingerprint = await self._probe(page, frame, tab_id, frame_id)
        if (fingerprint != _field(snapshot, "page_version")
                or bootstrap and frame.url != "about:blank"
                or not bootstrap and frame.url != action.target.page_url):
            raise _stale()
        if frame.url == "about:blank" and action.action_type != "navigate":
            raise _denied("Blank bootstrap observation permits navigation only")
        signature = link_url = coordinate = None
        locator = action.target.locator
        if action.action_type in _ELEMENT_ACTIONS:
            if isinstance(locator, CoordinateLocator):
                if (locator.width, locator.height) != (width, height) or (
                        locator.width, locator.height) != (_field(snapshot, "width"), _field(snapshot, "height")):
                    raise _stale()
                if locator.screenshot_evidence_id != _field(snapshot, "screenshot_evidence_id"):
                    raise _stale()
                await self._check_pixels(page, _field(snapshot, "screenshot_sha256"))
                _, _, _, after_pixels = await self._probe(page, frame, tab_id, frame_id)
                if after_pixels != fingerprint:
                    raise _stale()
                metadata = await self._coordinate(page, frame, locator)
                coordinate = (locator.x, locator.y)
            else:
                _, metadata = await self._unique(page, frame, locator)
            signature = _digest(metadata)
            if action.action_type in ("click", "download_attachment") and action.expected_effect == "read":
                link_url = metadata.get("href")
                if (metadata.get("tag") != "a" or not link_url or not self._same_site(frame.url, link_url)
                        or metadata.get("inline_handler")):
                    raise _denied("Read click requires a current permitted link", field="locator")
            if action.action_type == "download_attachment" and link_url != action.args.attachment_url:
                raise _denied("Attachment is not the current designated link", field="attachment_url")
            if action.action_type == "input" and (metadata.get("readonly") or metadata.get("tag") not in ("input", "textarea")):
                raise _denied("Target is not an editable form field", field="locator")
            if action.action_type == "select" and metadata.get("tag") != "select":
                raise _denied("Target is not a select control", field="locator")
        if action.action_type == "navigate" and not self._permits(action.args.url):
            raise _denied("Navigation is outside the source scope", field="url")
        if action.action_type == "switch_tab":
            await self._surface(token, action.args.tab_id)
        return PreparedTarget(action.action_type, tab_id, frame_id, fingerprint, frame.url,
                              signature, locator, _digest(action.model_dump(mode="json")), link_url, coordinate, trusted_write,
                              _field(snapshot, "screenshot_sha256") if coordinate is not None else None)

    async def execute(self, token, action, prepared, *, allowed_mutations=()):
        if self._recovery_token is not None or self._write_check_token is not None:
            raise _denied('Recovery cannot dispatch structured actions', field='action')
        action = self._action(action)
        if (not isinstance(prepared, PreparedTarget) or action.action_type != prepared.action_type
                or _digest(action.model_dump(mode="json")) != prepared.action_sha256):
            raise _stale()
        allowed = []
        if type(allowed_mutations) not in (tuple, list):
            raise _denied("Invalid trusted mutation endpoints")
        for item in allowed_mutations:
            if (type(item) not in (tuple, list) or len(item) != 2 or type(item[0]) is not str
                    or item[0] not in ("POST", "PUT", "PATCH", "DELETE") or not self._permits(item[1])):
                raise _denied("Invalid trusted mutation endpoints")
            allowed.append(tuple(item))
        if allowed and (not prepared.trusted_write or action.expected_effect != "write"):
            raise _denied("Mutation endpoints require trusted write authorization")
        async with self._dispatch_gate:
            async with AsyncExitStack() as stack:
                if action.expected_effect == "write":
                    await stack.enter_async_context(self.managed.gateway_dispatch_permission(self.session_id,
                        self.owner, execution_token=token, step_id=action.step_id,
                        operation_id=action.target.write_scope.operation_id))
                    self._dispatch_step_id, self._dispatch_operation_id = action.step_id, action.target.write_scope.operation_id
                try:
                    return await self._execute_prepared(token, action, prepared, allowed)
                finally:
                    self._dispatch_step_id = self._dispatch_operation_id = None
                    self._allowed_mutations = frozenset()

    async def _execute_prepared(self, token, action, prepared, allowed):
        page, frame, tab_id, frame_id = await self._surface(token, prepared.tab_id, prepared.frame_id)
        if prepared.coordinate is not None:
            await self._check_pixels(page, prepared.screenshot_sha256)
        _, _, _, fingerprint = await self._probe(page, frame, tab_id, frame_id)
        if fingerprint != prepared.page_version or frame.url != prepared.source_url:
            raise _stale()
        element = None
        if prepared.signature:
            if prepared.coordinate is not None:
                metadata = await self._coordinate(page, frame, prepared.locator)
                if action.action_type in _MUTATING:
                    # Never evaluate a main-world elementFromPoint to get
                    # an editable handle: the website can monkeypatch it.
                    attr = next((key for key in ("id", "data-testid", "name", "href")
                                 if metadata.get("attrs", {}).get(key)), None)
                    if attr is None:
                        raise _denied("Coordinate form target needs a stable exact DOM attribute", field="locator")
                    safe = DOMLocator(strategy="dom", attribute=attr, value=metadata["attrs"][attr])
                    element, resolved = await self._unique(page, frame, safe)
                    if resolved["node_id"] != metadata["node_id"]:
                        raise _stale()
            else:
                element, metadata = await self._unique(page, frame, prepared.locator)
            if _digest(metadata) != prepared.signature:
                raise _stale()
        blocked_before = self._critical_blocked_requests
        self._allowed_mutations = frozenset(allowed)
        try:
            result = {"action_type": action.action_type, "dispatch_completed": True}
            if action.action_type == "navigate":
                response = await frame.goto(action.args.url, wait_until="domcontentloaded")
                result["http_status"] = response.status if response else None
                if response is not None and response.status >= 400:
                    raise BusinessError("BROWSER_HTTP_ERROR", "Browser navigation received an error response", status=502, field="network")
            elif action.action_type == "click":
                if prepared.coordinate is not None:
                    await page.mouse.click(*prepared.coordinate)
                else:
                    await element.click()
            elif action.action_type == "input":
                await element.fill(action.args.text)
            elif action.action_type == "keypress":
                await element.press(action.args.key)
            elif action.action_type == "select":
                await element.select_option(label=action.args.option_label)
            elif action.action_type == "scroll":
                pixels = action.args.pixels
                dx = pixels if action.args.direction == "right" else -pixels if action.args.direction == "left" else 0
                dy = pixels if action.args.direction == "down" else -pixels if action.args.direction == "up" else 0
                await self._isolated(page, frame, _SCROLL, {"x": dx, "y": dy})
            elif action.action_type == "switch_tab":
                other, _, target_tab, _ = await self._surface(token, action.args.tab_id)
                await other.bring_to_front()
                self._selected_tab = target_tab
            elif action.action_type == "read_visible":
                capture = await self.capture(token, tab_id=tab_id, frame_id=frame_id)
                result.update(visible_text=capture.visible_text, visible_sha256=capture.visible_sha256)
            elif action.action_type == "screenshot":
                capture = await self.capture(token, tab_id=tab_id, frame_id=frame_id, include_screenshot=True)
                result.update(artifact_bytes=capture.screenshot, artifact_sha256=capture.screenshot_sha256,
                              artifact_kind="screenshot")
            elif action.action_type == "download_attachment":
                async with self.managed.download_permission(self.session_id, self.owner,
                        execution_token=token, page=page, attachment_url=action.args.attachment_url):
                    async with page.expect_download() as pending:
                        if prepared.coordinate is not None:
                            await page.mouse.click(*prepared.coordinate)
                        else:
                            await element.click()
                    download = await pending.value
                try:
                    if not self._permits(download.url) or download.url != action.args.attachment_url:
                        raise _denied("Downloaded attachment differs from the current link", field="attachment_url")
                    path = await download.path()
                    if path is None:
                        raise _denied("Attachment download is unavailable", field="attachment_url")
                    data = await asyncio.to_thread(self._read_download, path)
                    result.update(artifact_bytes=data, artifact_sha256=hashlib.sha256(data).hexdigest(),
                                  artifact_kind="attachment", artifact_size=len(data))
                finally:
                    await download.delete()
            if self._guard_failed or self._critical_blocked_requests != blocked_before:
                raise _denied("Browser action attempted a forbidden request", field="network")
            result['source_url'] = frame.url
            return result
        finally:
            self._allowed_mutations = frozenset()

    def _read_download(self, path):
        # This path comes exclusively from Playwright's generated download,
        # never from a model, action argument, page filename or suggested name.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_download_bytes:
                raise _denied("Attachment exceeds the bounded download limit", field="attachment_url")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(self.max_download_bytes + 1)
            if len(data) > self.max_download_bytes:
                raise _denied("Attachment exceeds the bounded download limit", field="attachment_url")
            return data
        finally:
            os.close(fd)


# Trusted adapters can explicitly provide alternatives; a model proposal with
# only one locator never receives invented fallback selectors.
def ordered_locators(locators):
    rank = {"semantic": 0, "dom": 1, "coordinate": 2}
    if any(not isinstance(item, (SemanticLocator, DOMLocator, CoordinateLocator)) for item in locators):
        raise _denied("Invalid trusted locator candidates", field="locator")
    return tuple(sorted(locators, key=lambda item: rank[item.strategy]))
