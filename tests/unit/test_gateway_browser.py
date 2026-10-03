"""Fixed browser backend boundary tests; real Chromium has a separate probe."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import copy
from datetime import datetime, timezone
import hashlib
import re
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from webagent.errors import BusinessError
from webagent.gateway import browser as module
from webagent.gateway.browser import BrowserBackend, ordered_locators
from webagent.models.schema import CoordinateLocator, DOMLocator, SemanticLocator
from webagent.scheduler.models import ExecutionToken, Resource
from webagent.sessions.models import SessionOwner
from webagent.tasks.models import SourceScope

URL = "https://fixture.example/page"
SCOPE = SourceScope(source_id="fixture", site_id="fixture", origin="https://fixture.example")
OWNER = SessionOwner("run", "run", "fixture")
TOKEN = ExecutionToken("run", "worker", 1, 1, 0, "2026-09-30T00:00:00.000000Z",
                       (Resource.site_identity("fixture").resource_key,))


def run(awaitable):
    return asyncio.run(awaitable)


def node(identifier=1, *, tag="a", href="https://fixture.example/next", visible=True, disabled=False, name="Next", **attrs):
    return {"node_id": identifier, "tag": tag, "attrs": {"id": str(identifier), **attrs},
            "role": {"a": "link", "button": "button", "input": "textbox", "select": "combobox"}.get(tag),
            "name": name, "visible": visible, "disabled": disabled, "readonly": False,
            "inline_handler": False, "href": href if tag == "a" else None,
            "form_method": None, "form_action": None,
            "bounds": {"x": 10, "y": 10, "width": 100, "height": 20}}


class Locator:
    def __init__(self, frame, nodes):
        self.frame, self.nodes = frame, nodes

    async def count(self): return len(self.nodes)
    def nth(self, index): return Locator(self.frame, [self.nodes[index]])
    async def is_visible(self): return self.nodes[0]["visible"]
    async def is_enabled(self): return not self.nodes[0]["disabled"]
    async def evaluate(self, script):
        raise AssertionError("Element metadata must come from the isolated world")
    def and_(self, other): return Locator(self.frame, [n for n in self.nodes if n in other.nodes])
    def or_(self, other): return Locator(self.frame, list({n["node_id"]: n for n in self.nodes + other.nodes}.values()))
    async def click(self): self.frame.calls.append(("click", self.nodes[0]["node_id"]))
    async def fill(self, text): self.frame.calls.append(("fill", text))
    async def press(self, key): self.frame.calls.append(("press", key))
    async def select_option(self, *, label): self.frame.calls.append(("select_option", label))


class Frame:
    def __init__(self, page, url=URL):
        self.page, self.url, self.detached = page, url, False
        self.name = ""
        self.nodes = [node()]
        self.revision = 0
        self.calls = []
        self.hit = self.nodes[0]
        self.offset = {"x": 20, "y": 30, "width": 200, "height": 100}
        self.hit_points = []
        self.text_truncated = False

    def is_detached(self): return self.detached
    async def evaluate(self, script, args=None):
        if script == module._CAPTURE:
            viewport = {"width": 800, "height": 600, "scroll_x": 0, "scroll_y": 0, "dpr": 1}
            if self != self.page.main_frame:
                viewport.update(width=self.offset["width"], height=self.offset["height"])
            return {"revision": self.revision, "title": "Fixture", "text": "Public visible text",
                    "text_truncated": self.text_truncated,
                    "viewport": viewport,
                    "elements": copy.deepcopy(self.nodes)}
        if script == module._HIT:
            self.hit_points.append(args)
            return copy.deepcopy(self.hit)
        if script == module._SCROLL:
            self.calls.append(("scroll", args)); self.revision += 1; return None
        if script == module._MATCH:
            return copy.deepcopy([n for n in self.nodes if n["visible"] and (
                n["attrs"].get(args["attribute"]) == args["value"] if args["strategy"] == "dom" else
                (not args.get("role") or n["role"] == args["role"]) and
                (not args.get("accessible_name") or n["name"] == args["accessible_name"]) and
                (not args.get("label") or n.get("label") == args["label"]))])
        raise AssertionError("Nonfixed script")
    def get_by_role(self, role, *, name=None, exact=None):
        self.calls.append(("role", role, name, exact))
        return Locator(self, [n for n in self.nodes if n["role"] == role and (name is None or n["name"] == name)])
    def get_by_label(self, label, *, exact):
        self.calls.append(("label", label, exact))
        return Locator(self, [n for n in self.nodes if n.get("label") == label])
    def get_by_text(self, text, *, exact):
        return Locator(self, [n for n in self.nodes if n["name"] == text])
    def locator(self, selector):
        self.calls.append(("dom", selector))
        match = re.fullmatch(r'\[([\w-]+)="(.*)"\]', selector)
        assert match is not None
        attribute, encoded = match.groups()
        value = "".join(chr(int(code, 16)) for code in re.findall(r"\\([a-f0-9]+) ", encoded))
        return Locator(self, [n for n in self.nodes if n["attrs"].get(attribute) == value])
    async def goto(self, url, **options):
        self.calls.append(("goto", url, options))
        self.url = url
        if self is self.page.main_frame: self.page.url = url
        self.page.emit("framenavigated", self)
        return type("Response", (), {"status": 200})()
    async def frame_element(self):
        frame = self
        class Element:
            async def bounding_box(self): return frame.offset
            async def evaluate(self, script):
                assert script == "e => ({x:e.clientLeft,y:e.clientTop})"
                return {"x": 2, "y": 3}
        return Element()


class Page:
    def __init__(self, url=URL):
        self.url, self.closed, self.handlers = url, False, {}
        self.main_frame = Frame(self, url)
        self.frames = [self.main_frame]
        self.viewport_size = {"width": 800, "height": 600}
        self.mouse = self
        self.calls = []
        self.screenshot_mutates = False
        self.download = None
        self.screenshot_bytes = b"fixture-png"
    def on(self, event, fn): self.handlers.setdefault(event, []).append(fn)
    def emit(self, event, *args):
        for fn in self.handlers.get(event, []): fn(*args)
    def is_closed(self): return self.closed
    async def screenshot(self, **options):
        self.calls.append(("screenshot", options))
        if self.screenshot_mutates: self.main_frame.revision += 1
        return self.screenshot_bytes
    async def click(self, x, y): self.calls.append(("mouse_click", x, y))
    async def bring_to_front(self): self.calls.append(("front",))
    def expect_download(self):
        page = self
        class Pending:
            async def __aenter__(self):
                self.value = asyncio.get_running_loop().create_future()
                self.value.set_result(page.download)
                return self
            async def __aexit__(self, *_): pass
        return Pending()


class Context:
    def __init__(self, page):
        self.pages, self.handlers, self.routes = [page], {}, []
        page.context = self
    def on(self, event, fn): self.handlers.setdefault(event, []).append(fn)
    async def route(self, pattern, fn): self.routes.append((pattern, fn))
    async def new_cdp_session(self, page):
        if isinstance(page, Frame):
            raise RuntimeError("This frame does not have a separate CDP session, it is a part of the parent frame's session")
        return CDP(page)


class CDP:
    def __init__(self, page):
        self.page, self.worlds, self.last_frame = page, {}, None
        self.handlers, self.protocol_calls, self.detached = {}, [], False
    def on(self, event, fn): self.handlers[event] = fn
    async def detach(self): self.detached = True
    async def send(self, method, args=None):
        self.protocol_calls.append((method, args))
        if method == "Fetch.enable": return {}
        if method in ("Fetch.continueRequest", "Fetch.failRequest", "Fetch.continueWithAuth"): return {}
        if method == "Page.getFrameTree":
            frames = [{"frame": {"id": str(id(frame)), "name": frame.name, "url": frame.url, "loaderId": str(id(frame)) + ':' + frame.url}}
                      for frame in self.page.frames]
            return {"frameTree": {**frames[0], "childFrames": frames[1:]}}
        if method == "Page.createIsolatedWorld":
            self.worlds[args["frameId"]] = next(f for f in self.page.frames if str(id(f)) == args["frameId"])
            self.last_frame = args["frameId"]
            return {"executionContextId": args["frameId"]}
        if method == "DOM.getNodeForLocation": return {"frameId": getattr(self.page, "actual_hit_frame", self.last_frame), "backendNodeId": 1}
        if method == "DOM.getFrameOwner": return {"backendNodeId": args["frameId"]}
        if method == "DOM.getBoxModel":
            frame = next(f for f in self.page.frames if str(id(f)) == args["backendNodeId"])
            box = frame.offset
            x, y, w, h = box["x"] + 2, box["y"] + 3, box["width"], box["height"]
            return {"model": {"content": [x,y,x+w,y,x+w,y+h,x,y+h]}}
        if method == "Runtime.evaluate":
            expression = args["expression"]
            if expression == "(" + module._INSTALL + ")()": return {"result": {"value": None}}
            for script in (module._CAPTURE, module._HIT, module._MATCH, module._SCROLL):
                prefix = "(" + script + ")(" 
                if expression.startswith(prefix):
                    import json
                    value = await self.worlds[args["contextId"]].evaluate(script, json.loads(expression[len(prefix):-1]))
                    return {"result": {"value": value}}
        raise AssertionError(method)


class Managed:
    def __init__(self, context): self.current, self.calls = context, []
    async def context(self, session_id, owner, *, execution_token):
        assert isinstance(execution_token, ExecutionToken)
        self.calls.append((session_id, owner, execution_token))
        return self.current
    async def gateway_proxy_credentials(self, session_id, owner, *, execution_token, step_id=None, operation_id=None):
        assert execution_token is TOKEN
        return {"server": "http://127.0.0.1:54321", "username": "generated-proxy-user", "password": "generated-proxy-password"}
    async def gateway_context(self, session_id, owner, *, execution_token, step_id, operation_id):
        assert step_id == "step" and operation_id == "operation"
        return await self.context(session_id, owner, execution_token=execution_token)
    @asynccontextmanager
    async def gateway_dispatch_permission(self, session_id, owner, *, execution_token, step_id, operation_id):
        assert execution_token is TOKEN and step_id == "step" and operation_id == "operation"
        yield
    @asynccontextmanager
    async def download_permission(self, session_id, owner, *, execution_token, page, attachment_url):
        assert execution_token is TOKEN and attachment_url == page.download.url
        yield


def backend(url=URL):
    page = Page(url)
    context = Context(page)
    managed = Managed(context)
    browser = BrowserBackend(managed, "session", OWNER, allowed_sources=(SCOPE,))
    return browser, page, context, managed


async def observe(browser, **options):
    capture = await browser.capture(TOKEN, **options)
    value = asdict(capture)
    value.update(snapshot_id="snapshot", source_url=capture.page_url, screenshot_evidence_id="shot" if capture.screenshot else None)
    return value


def action(snapshot, kind="click", *, strategy="semantic", effect="read", locator=None, args=None):
    if locator is None and kind in ("click", "input", "keypress", "select", "download_attachment"):
        locator = {"strategy": strategy, "role": "link", "accessible_name": "Next", "label": None}
    return {"run_id": "run", "step_id": "step", "epoch": 1, "snapshot_id": snapshot["snapshot_id"],
            "target": {"page_url": snapshot["source_url"], "tab_id": snapshot["tab_id"], "frame_id": snapshot["frame_id"],
                       "locator": locator, "write_scope": {"repository": "owner/repo", "branch": "fix", "base_sha": "a" * 40,
                           "operation": "edit_file", "files": ["src/a.py"], "operation_id": "operation", "identity_ref": "identity",
                           "target_rechecked_at": datetime(2026, 9, 30, tzinfo=timezone.utc)} if effect == "write" else None},
            "expected_effect": effect, "action_type": kind, "args": args or {}}


def coordinate(snapshot, *, x=15, y=15):
    return {"strategy": "coordinate", "screenshot_evidence_id": "shot", "snapshot_id": "snapshot",
            "tab_id": snapshot["tab_id"], "frame_id": snapshot["frame_id"], "width": 800, "height": 600, "x": x, "y": y}


def test_capture_is_bounded_fixed_probe_and_preserves_context_boundary():
    async def exercise():
        browser, page, context, managed = backend()
        capture = await browser.capture(TOKEN, include_screenshot=True)
        assert capture.visible_text == "Public visible text"
        assert capture.screenshot_sha256 == hashlib.sha256(b"fixture-png").hexdigest()
        assert capture.links[0]["href"] == "https://fixture.example/next"
        assert len(context.routes) == 1
        await browser.capture(TOKEN)
        assert len(context.routes) == 1
        assert len(managed.calls) == 2
    run(exercise())


@pytest.mark.parametrize('flag', [False, True])
def test_capture_preserves_explicit_completeness_signal(flag):
    async def exercise():
        browser, page, *_ = backend()
        page.main_frame.text_truncated = flag
        capture = await browser.capture(TOKEN)
        assert capture.text_truncated is flag
    run(exercise())


@pytest.mark.parametrize('nodes,limit,expected,truncated', [
    (['abc', 'def'], 7, 'abc\ndef', False),
    (['abc', 'def', 'ghi'], 8, 'abc\ndef', True),  # Shorter than the bound, but still truncated.
    (['abcdef'], 3, 'abc', True),
    (['', 'abc', '  '], 4, 'abc', False),
])
def test_fixed_dom_probe_reports_actual_text_truncation(nodes, limit, expected, truncated):
    bundled = Path(__file__).resolve().parents[2] / '.runtime/node/bin/node'
    node = str(bundled) if bundled.is_file() else shutil.which('node')
    assert node is not None, 'The project bootstrap must provide its pinned JavaScript runtime'
    # Execute the actual developer-owned capture closure over a tiny synthetic
    # DOM. No browser, model, website, credentials or default data are involved.
    harness = '''class Element { closest() { return null; }
      getBoundingClientRect() { return {width:1,height:1}; } }
    global.window={addEventListener(){}};
    global.getComputedStyle=()=>({display:'block',visibility:'visible'});
    global.MutationObserver=class{observe(){}};
    global.NodeFilter={SHOW_TEXT:4};
    global.innerWidth=1;global.innerHeight=1;global.scrollX=0;global.scrollY=0;global.devicePixelRatio=1;
    global.document={title:'Fixture',body:new Element(),querySelectorAll:()=>[],
      createTreeWalker(){let i=0;return{nextNode:()=>i<nodes.length?{parentElement:new Element(),textContent:nodes[i++]}:null};}};
    '''
    script = 'const nodes=' + json.dumps(nodes) + ';' + harness + '\n(' + module._INSTALL + ')();\n' + (
        'process.stdout.write(JSON.stringify(window.__webpilotGatewayProbeV1.capture(' + str(limit) + ')));')
    result = subprocess.run([node], input=script, capture_output=True, text=True, timeout=5, check=True)
    capture = json.loads(result.stdout)
    assert capture['text'] == expected
    assert capture['text_truncated'] is truncated


def test_capture_rejects_dom_change_during_screenshot():
    async def exercise():
        browser, page, *_ = backend()
        page.screenshot_mutates = True
        with pytest.raises(BusinessError, match="no longer current"):
            await browser.capture(TOKEN, include_screenshot=True)
    run(exercise())


@pytest.mark.parametrize("change", ["revision", "url", "viewport", "generation", "closed", "detached"])
def test_snapshot_changes_reject_before_browser_dispatch(change):
    async def exercise():
        browser, page, *_ = backend()
        snapshot = await observe(browser)
        proposed = action(snapshot)
        if change == "revision": page.main_frame.revision += 1
        elif change == "url": page.main_frame.url = "https://fixture.example/changed"
        elif change == "viewport": page.viewport_size["width"] += 1
        elif change == "generation": page.emit("framenavigated", page.main_frame)
        elif change == "closed": page.closed = True
        elif change == "detached": page.main_frame.detached = True
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, proposed, snapshot)
        assert not any(c[0] == "click" for c in page.main_frame.calls)
    run(exercise())


@pytest.mark.parametrize("condition", ["ambiguous", "hidden", "disabled", "many"])
def test_semantic_requires_unique_visible_enabled_element(condition):
    async def exercise():
        browser, page, *_ = backend()
        if condition == "ambiguous": page.main_frame.nodes.append(node(2))
        elif condition == "hidden": page.main_frame.nodes[0]["visible"] = False
        elif condition == "disabled": page.main_frame.nodes[0]["disabled"] = True
        elif condition == "many": page.main_frame.nodes = [node(i) for i in range(257)]
        snapshot = await observe(browser)
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, action(snapshot), snapshot)
    run(exercise())


def test_hidden_duplicate_does_not_block_unique_visible_semantic_match():
    async def exercise():
        browser, page, *_ = backend()
        page.main_frame.nodes.append(node(2, visible=False))
        snapshot = await observe(browser)
        prepared = await browser.prepare(TOKEN, action(snapshot), snapshot)
        result = await browser.execute(TOKEN, action(snapshot), prepared)
        assert result == {"action_type": "click", "dispatch_completed": True,
                          "source_url": page.main_frame.url}
        assert ("click", 1) in page.main_frame.calls
    run(exercise())


def test_exact_dom_attribute_does_not_allow_css_injection():
    async def exercise():
        browser, page, *_ = backend()
        malicious = 'a"] button,[id="b'
        page.main_frame.nodes[0]["attrs"]["id"] = malicious
        snapshot = await observe(browser)
        proposed = action(snapshot, locator={"strategy": "dom", "attribute": "id", "value": malicious})
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        await browser.execute(TOKEN, proposed, prepared)
        selectors = [c[1] for c in page.main_frame.calls if c[0] == "dom"]
        assert all('button' not in selector and '\\22 ' in selector for selector in selectors)
    run(exercise())


@pytest.mark.parametrize("bad", ["css", "xpath", "evaluate", "shell", "request", "refresh"])
def test_arbitrary_action_or_locator_capabilities_are_rejected(bad):
    async def exercise():
        browser, *_ = backend()
        snapshot = await observe(browser)
        proposed = action(snapshot)
        if bad in ("css", "xpath"): proposed["target"]["locator"] = {"strategy": bad, "value": "*"}
        else: proposed["action_type"] = bad
        with pytest.raises(BusinessError) as error: await browser.prepare(TOKEN, proposed, snapshot)
        assert error.value.code == "INVALID_PARAMETER"
    run(exercise())


@pytest.mark.parametrize("kind,args", [("input", {"text": "hello"}), ("keypress", {"key": "Enter"}), ("select", {"option_label": "Main"})])
def test_read_effect_cannot_grant_form_interaction(kind, args):
    async def exercise():
        browser, *_ = backend()
        snapshot = await observe(browser)
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, action(snapshot, kind, args=args), snapshot, trusted_write=True)
    run(exercise())


@pytest.mark.parametrize("condition", ["button", "outside", "inline"])
def test_read_click_only_current_permitted_links(condition):
    async def exercise():
        browser, page, *_ = backend()
        if condition == "button": page.main_frame.nodes[0] = node(tag="button")
        elif condition == "outside": page.main_frame.nodes[0]["href"] = "https://other.example/write"
        else: page.main_frame.nodes[0]["inline_handler"] = True
        snapshot = await observe(browser)
        proposed = action(snapshot, locator={"strategy": "dom", "attribute": "id", "value": "1"})
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, proposed, snapshot)
    run(exercise())


@pytest.mark.parametrize("change", ["evidence", "width", "frame", "hit"])
def test_coordinate_binds_screenshot_viewport_frame_and_current_hit(change):
    async def exercise():
        browser, page, *_ = backend()
        snapshot = await observe(browser, include_screenshot=True)
        locator = coordinate(snapshot)
        if change == "evidence": locator["screenshot_evidence_id"] = "another-shot"
        elif change == "width": locator["width"] = 801
        elif change == "frame": locator["frame_id"] = "another-frame"
        elif change == "hit": page.main_frame.hit = None
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, action(snapshot, locator=locator), snapshot)
    run(exercise())


def test_coordinate_rehit_before_dispatch_rejects_replaced_same_looking_node():
    async def exercise():
        browser, page, *_ = backend()
        snapshot = await observe(browser, include_screenshot=True)
        proposed = action(snapshot, locator=coordinate(snapshot))
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        page.main_frame.hit = node(99)
        with pytest.raises(BusinessError): await browser.execute(TOKEN, proposed, prepared)
        assert not any(call[0] == "mouse_click" for call in page.calls)
    run(exercise())


def test_coordinate_in_iframe_uses_current_frame_offset_and_border():
    async def exercise():
        browser, page, *_ = backend()
        child = Frame(page)
        page.frames.append(child)
        frame_id = browser._register_frame(child)
        snapshot = await observe(browser, frame_id=frame_id, include_screenshot=True)
        proposed = action(snapshot, locator=coordinate(snapshot, x=40, y=50))
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        await browser.execute(TOKEN, proposed, prepared)
        assert child.hit_points == [{"x": 18, "y": 17}, {"x": 18, "y": 17}]
        assert ("mouse_click", 40, 50) in page.calls
    run(exercise())


def test_prepared_action_cannot_be_swapped_after_authorization():
    async def exercise():
        browser, page, *_ = backend()
        snapshot = await observe(browser)
        proposed = action(snapshot, "navigate", args={"url": "https://fixture.example/next"})
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        proposed["args"]["url"] = "https://other.example/write"
        with pytest.raises(BusinessError): await browser.execute(TOKEN, proposed, prepared)
        assert not any(c[0] == "goto" for c in page.main_frame.calls)
    run(exercise())


def test_bootstrap_blank_only_navigation_and_with_declared_url():
    async def exercise():
        browser, page, *_ = backend("about:blank")
        snapshot = await observe(browser)
        proposed = action(snapshot, "navigate", args={"url": URL})
        proposed["target"]["page_url"] = URL
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        await browser.execute(TOKEN, proposed, prepared)
        assert page.url == URL
    run(exercise())


@pytest.mark.parametrize("kind,args", [("read_visible", {}), ("screenshot", {}), ("scroll", {"direction": "down", "pixels": 10})])
def test_blank_bootstrap_disallows_non_navigation(kind, args):
    async def exercise():
        browser, *_ = backend("about:blank")
        snapshot = await observe(browser)
        proposed = action(snapshot, kind, args=args)
        proposed["target"]["page_url"] = URL
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, proposed, snapshot)
    run(exercise())


@pytest.mark.parametrize("direction,expected", [("up", {"x": 0, "y": -12}), ("down", {"x": 0, "y": 12}), ("left", {"x": -12, "y": 0}), ("right", {"x": 12, "y": 0})])
def test_scroll_only_declared_bounded_direction(direction, expected):
    async def exercise():
        browser, page, *_ = backend()
        snapshot = await observe(browser)
        proposed = action(snapshot, "scroll", args={"direction": direction, "pixels": 12})
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        await browser.execute(TOKEN, proposed, prepared)
        assert ("scroll", expected) in page.main_frame.calls
    run(exercise())


def test_switch_tab_read_and_screenshot_dispatch_without_business_outcome():
    async def exercise():
        browser, page, context, _ = backend()
        other = Page()
        other.context = context
        context.pages.append(other)
        snapshot = await observe(browser)
        other_tab = browser._page_ids[id(other)]
        for kind, args in [("switch_tab", {"tab_id": other_tab}), ("read_visible", {}), ("screenshot", {})]:
            proposed = action(snapshot, kind, args=args)
            prepared = await browser.prepare(TOKEN, proposed, snapshot)
            result = await browser.execute(TOKEN, proposed, prepared)
            assert result["dispatch_completed"] is True
            assert "success" not in result and "outcome" not in result
        assert other.calls == [("front",)]
        assert browser._selected_tab == other_tab
    run(exercise())


@pytest.mark.parametrize("kind,tag,args,expected", [("input", "input", {"text": "hello"}, ("fill", "hello")),
    ("keypress", "input", {"key": "Enter"}, ("press", "Enter")),
    ("select", "select", {"option_label": "Main"}, ("select_option", "Main"))])
def test_write_form_actions_require_trusted_authorization(kind, tag, args, expected):
    async def exercise():
        browser, page, *_ = backend()
        page.main_frame.nodes = [node(tag=tag)]
        snapshot = await observe(browser)
        proposed = action(snapshot, kind, effect="write", args=args, locator={"strategy": "dom", "attribute": "id", "value": "1"})
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, proposed, snapshot)
        prepared = await browser.prepare(TOKEN, proposed, snapshot, trusted_write=True)
        await browser.execute(TOKEN, proposed, prepared, allowed_mutations=(("POST", "https://fixture.example/save"),))
        assert expected in page.main_frame.calls
        assert browser._allowed_mutations == frozenset()
    run(exercise())


class Route:
    def __init__(self, method, url, *, resource_type=None, frame=None, navigation=False, redirected_from=None):
        self.request = type("Request", (), {"method": method, "url": url})()
        self.request.is_navigation_request = lambda: navigation
        self.request.resource_type = resource_type
        self.request.redirected_from = redirected_from
        if frame is not None:
            self.request.frame = frame
        self.calls = []
    async def fallback(self): self.calls.append("fallback")
    async def abort(self, reason): self.calls.append(("abort", reason))


@pytest.mark.parametrize('route_type,native_type', [('stylesheet', 'Stylesheet'), ('image', 'Image'),
    ('font', 'Font'), ('media', 'Media'), ('script', 'Script')])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_denied_optional_resource_stays_aborted_under_both_guards(method, route_type, native_type):
    async def exercise():
        browser, page, context, *_ = backend()
        await observe(browser)
        outside = 'https://outside.example/optional-resource'
        route = Route(method, outside, resource_type=route_type, frame=page.main_frame)
        await context.routes[0][1](route)
        assert route.calls == [('abort', 'blockedbyclient')]
        assert (browser._blocked_requests, browser._optional_blocked_requests,
                browser._critical_blocked_requests) == (1, 1, 0)
        cdp = browser._cdps[id(page)]
        await cdp.handlers['Fetch.requestPaused']({'requestId': 'optional', 'resourceType': native_type,
            'frameId': str(id(page.main_frame)), 'request': {'method': method, 'url': outside}})
        assert cdp.protocol_calls[-1][0] == 'Fetch.failRequest'
        assert (browser._blocked_requests, browser._optional_blocked_requests,
                browser._critical_blocked_requests) == (2, 2, 0)
    run(exercise())


@pytest.mark.parametrize('route_type,native_type', [('document', 'Document'), ('xhr', 'XHR'),
    ('fetch', 'Fetch'), ('ping', 'Ping'), ('other', 'Other'), (None, None), ([], [])])
def test_denied_active_document_or_unknown_resource_remains_critical(route_type, native_type):
    async def exercise():
        browser, page, context, *_ = backend()
        await observe(browser)
        route = Route('GET', 'https://outside.example/private', resource_type=route_type, frame=page.main_frame)
        await context.routes[0][1](route)
        cdp = browser._cdps[id(page)]
        await cdp.handlers['Fetch.requestPaused']({'requestId': 'critical', 'resourceType': native_type,
            'frameId': str(id(page.main_frame)), 'request': {'method': 'GET', 'url': route.request.url}})
        assert route.calls == [('abort', 'blockedbyclient')]
        assert cdp.protocol_calls[-1][0] == 'Fetch.failRequest'
        assert browser._critical_blocked_requests == 2 and browser._optional_blocked_requests == 0
    run(exercise())


@pytest.mark.parametrize('metadata', ['redirect', 'malformed-redirect', 'missing-route-redirect', 'same-url-child'])
def test_passive_redirect_or_child_frame_is_never_optional(metadata):
    async def exercise():
        browser, page, context, *_ = backend()
        await observe(browser)
        frame = page.main_frame
        if metadata == 'same-url-child':
            frame = Frame(page, url=page.main_frame.url)
            frame.name = 'same-url-child'
            page.frames.append(frame)
        route = Route('GET', 'https://outside.example/image', resource_type='image', frame=frame,
            redirected_from=object() if metadata in ('redirect', 'malformed-redirect') else None)
        if metadata == 'missing-route-redirect':
            del route.request.redirected_from
        await context.routes[0][1](route)
        cdp = browser._cdps[id(page)]
        event = {'requestId': 'passive-not-optional', 'resourceType': 'Image',
            'frameId': str(id(frame)), 'request': {'method': 'GET', 'url': route.request.url}}
        if metadata == 'redirect':
            event['redirectedRequestId'] = 'previous-request'
        elif metadata in ('malformed-redirect', 'missing-route-redirect'):
            event['redirectedRequestId'] = None
        await cdp.handlers['Fetch.requestPaused'](event)
        assert route.calls == [('abort', 'blockedbyclient')]
        assert cdp.protocol_calls[-1][0] == 'Fetch.failRequest'
        assert browser._critical_blocked_requests == 2 and browser._optional_blocked_requests == 0
    run(exercise())


@pytest.mark.parametrize('problem', ['navigation', 'missing-frame', 'broken-frame', 'unknown-native-frame',
    'undeclared-page', 'declared-unleased', 'revoked', 'POST', 'PUT', 'PATCH', 'DELETE'])
def test_passive_type_cannot_hide_missing_frame_lease_or_control(problem):
    async def exercise():
        browser, page, context, managed = backend()
        await observe(browser)
        method, url = (problem, 'https://outside.example/resource') if problem in ('POST', 'PUT', 'PATCH', 'DELETE') else (
            'GET', 'https://outside.example/resource')
        if problem == 'declared-unleased':
            browser.allowed_sources += (SourceScope(source_id='outside', site_id='outside', origin='https://outside.example'),)
        if problem == 'revoked':
            async def revoked(*args, **kwargs): raise BusinessError('RESOURCE_CONFLICT', 'Revoked fixture')
            managed.context = revoked
        frame = None if problem == 'missing-frame' else page.main_frame
        if problem == 'broken-frame':
            frame = object()
        if problem == 'undeclared-page':
            page.url = page.main_frame.url = 'https://unleased.example/page'
        route = Route(method, url, resource_type='image', frame=frame, navigation=problem == 'navigation')
        await context.routes[0][1](route)
        cdp = browser._cdps[id(page)]
        native_type = 'Document' if problem == 'navigation' else 'Image'
        native_frame = None if problem in ('missing-frame', 'broken-frame') else 'unknown' if problem == 'unknown-native-frame' else str(id(page.main_frame))
        await cdp.handlers['Fetch.requestPaused']({'requestId': 'passive-critical', 'resourceType': native_type,
            'frameId': native_frame, 'request': {'method': method, 'url': url}})
        assert route.calls == [('abort', 'blockedbyclient')]
        assert cdp.protocol_calls[-1][0] == 'Fetch.failRequest'
        expected = 1 if problem == 'unknown-native-frame' else 2
        assert browser._critical_blocked_requests == expected
        assert browser._optional_blocked_requests == 2 - expected
    run(exercise())


@pytest.mark.parametrize('path', ['execute', 'recovery', 'write-check'])
@pytest.mark.parametrize('resource_type,should_fail', [('image', False), ('fetch', True), ('document', True)])
def test_navigation_paths_share_denied_request_classification(path, resource_type, should_fail):
    async def exercise():
        browser, page, context, managed = backend()
        snapshot = await observe(browser)
        original = page.main_frame.goto
        async def resource_navigation(url, **kwargs):
            response = await original(url, **kwargs)
            await context.routes[0][1](Route('GET', 'https://outside.example/resource',
                resource_type=resource_type, frame=page.main_frame, navigation=resource_type == 'document'))
            return response
        page.main_frame.goto = resource_navigation
        async def checked_context(*args, **kwargs): return await managed.context(*args, execution_token=kwargs['execution_token'])
        if path == 'recovery':
            managed.recovery_context = checked_context
            call = browser.recovery_navigate(TOKEN, URL)
        elif path == 'write-check':
            managed.write_check_context = checked_context
            call = browser.write_check_navigate(TOKEN, URL, 'fixture-operation')
        else:
            proposed = action(snapshot, 'navigate', args={'url': URL}, locator=None)
            prepared = await browser.prepare(TOKEN, proposed, snapshot)
            call = browser.execute(TOKEN, proposed, prepared)
        if should_fail:
            with pytest.raises(BusinessError): await call
            assert browser._critical_blocked_requests == 1
        else:
            result = await call
            assert result['source_url'] == URL
            assert browser._critical_blocked_requests == 0 and browser._optional_blocked_requests == 1
    run(exercise())


@pytest.mark.parametrize('path', ['execute', 'recovery', 'write-check'])
def test_optional_abort_protocol_failure_rejects_current_navigation(path):
    async def exercise():
        browser, page, _, managed = backend()
        snapshot = await observe(browser)
        cdp = browser._cdps[id(page)]
        original_send = cdp.send
        async def failing_send(method, args=None):
            if method == 'Fetch.failRequest':
                raise RuntimeError('Owned synthetic protocol failure')
            return await original_send(method, args)
        cdp.send = failing_send
        original_goto = page.main_frame.goto
        async def resource_navigation(url, **kwargs):
            response = await original_goto(url, **kwargs)
            await cdp.handlers['Fetch.requestPaused']({'requestId': 'failed-optional-abort',
                'resourceType': 'Image', 'frameId': str(id(page.main_frame)),
                'request': {'method': 'GET', 'url': 'https://outside.example/image'}})
            return response
        page.main_frame.goto = resource_navigation
        async def checked_context(*args, **kwargs): return await managed.context(*args, execution_token=kwargs['execution_token'])
        if path == 'recovery':
            managed.recovery_context = checked_context
            operation = browser.recovery_navigate(TOKEN, URL)
        elif path == 'write-check':
            managed.write_check_context = checked_context
            operation = browser.write_check_navigate(TOKEN, URL, 'owned-operation')
        else:
            proposed = action(snapshot, 'navigate', args={'url': URL}, locator=None)
            prepared = await browser.prepare(TOKEN, proposed, snapshot)
            operation = browser.execute(TOKEN, proposed, prepared)
        with pytest.raises(BusinessError):
            await operation
        assert browser._guard_failed is True and browser._critical_blocked_requests == 0
        assert browser._optional_blocked_requests == 1
    run(exercise())


@pytest.mark.parametrize("method,url,grants,permitted", [("GET", URL, (), True), ("HEAD", URL, (), True),
    ("POST", URL, (), False), ("GET", "https://other.example/page", (), False),
    ("POST", URL, (("POST", URL),), True), ("POST", URL + "?different=1", (("POST", URL),), False),
    ("DELETE", URL, (("POST", URL),), False)])
def test_request_fence_exact_method_url_and_preserves_network_boundary(method, url, grants, permitted):
    async def exercise():
        browser, _, context, _ = backend()
        await observe(browser)
        browser._allowed_mutations = frozenset(grants)
        route = Route(method, url)
        await context.routes[0][1](route)
        assert route.calls == (["fallback"] if permitted else [("abort", "blockedbyclient")])
    run(exercise())


def test_script_event_on_read_link_cannot_borrow_mutation_authority():
    async def exercise():
        browser, page, context, _ = backend()
        snapshot = await observe(browser)
        proposed = action(snapshot)
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        original = Locator.click
        async def attack(self):
            await original(self)
            await context.routes[0][1](Route("POST", "https://fixture.example/save"))
        Locator.click = attack
        try:
            with pytest.raises(BusinessError, match="forbidden request"):
                await browser.execute(TOKEN, proposed, prepared)
            assert browser._allowed_mutations == frozenset()
        finally: Locator.click = original
    run(exercise())


@pytest.mark.parametrize("bad", [(("POST", "https://other.example/save"),), (("GET", URL),), (("POST", URL, "extra"),), True])
def test_invalid_mutation_grants_fail_before_action(bad):
    async def exercise():
        browser, page, *_ = backend()
        snapshot = await observe(browser)
        proposed = action(snapshot)
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        with pytest.raises(BusinessError): await browser.execute(TOKEN, proposed, prepared, allowed_mutations=bad)
        assert not any(c[0] == "click" for c in page.main_frame.calls)
    run(exercise())


def test_attachment_only_current_link_bounded_bytes_and_browser_temp_cleanup(tmp_path):
    async def exercise():
        browser, page, *_ = backend()
        payload = b"fixture attachment"
        path = tmp_path / "browser-generated"
        path.write_bytes(payload)
        class Download:
            url = "https://fixture.example/next"
            deleted = False
            async def path(self): return path
            async def delete(self): self.deleted = True
        page.download = Download()
        snapshot = await observe(browser)
        proposed = action(snapshot, "download_attachment", args={"attachment_url": page.download.url, "link_evidence_id": "current-link"})
        prepared = await browser.prepare(TOKEN, proposed, snapshot)
        result = await browser.execute(TOKEN, proposed, prepared)
        assert result["artifact_bytes"] == payload
        assert result["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
        assert page.download.deleted is True
        assert "path" not in result
    run(exercise())


@pytest.mark.parametrize("condition", ["large", "symlink", "directory"])
def test_download_rejects_oversize_or_non_regular_browser_temp(tmp_path, condition):
    browser, *_ = backend()
    browser.max_download_bytes = 4
    path = tmp_path / "browser-temp"
    if condition == "large": path.write_bytes(b"12345")
    elif condition == "directory": path.mkdir()
    else:
        real = tmp_path / "real"; real.write_bytes(b"123"); path.symlink_to(real)
    with pytest.raises((BusinessError, OSError)): browser._read_download(path)


def test_trusted_locator_candidates_are_ordered_without_inventing_fallbacks():
    semantic = SemanticLocator(strategy="semantic", role="link", accessible_name="Next", label=None)
    dom = DOMLocator(strategy="dom", attribute="id", value="one")
    point = CoordinateLocator(strategy="coordinate", screenshot_evidence_id="shot", snapshot_id="snapshot", tab_id="tab", frame_id="frame", width=10, height=10, x=1, y=1)
    assert ordered_locators((point, dom, semantic)) == (semantic, dom, point)
    assert ordered_locators((semantic,)) == (semantic,)
    with pytest.raises(BusinessError): ordered_locators(({"strategy": "css"},))


def test_source_configuration_cannot_be_absent_or_unvalidated():
    browser, page, context, managed = backend()
    for scopes in ((), ({"origin": "https://fixture.example"},)):
        with pytest.raises(BusinessError): BrowserBackend(managed, "session", OWNER, allowed_sources=scopes)


@pytest.mark.parametrize("phase", ["prepare", "execute"])
def test_canvas_pixel_change_rejects_coordinate_without_dom_mutation(phase):
    async def exercise():
        browser, page, *_ = backend()
        snapshot = await observe(browser, include_screenshot=True)
        proposed = action(snapshot, locator=coordinate(snapshot))
        prepared = await browser.prepare(TOKEN, proposed, snapshot) if phase == "execute" else None
        page.screenshot_bytes = b"changed-canvas-with-same-dom"
        with pytest.raises(BusinessError):
            if phase == "prepare": await browser.prepare(TOKEN, proposed, snapshot)
            else: await browser.execute(TOKEN, proposed, prepared)
        assert not any(call[0] == "mouse_click" for call in page.calls)
    run(exercise())


def test_iframe_coordinate_rejects_native_hit_in_parent_overlay():
    async def exercise():
        browser, page, *_ = backend()
        child = Frame(page)
        page.frames.append(child)
        frame_id = browser._register_frame(child)
        snapshot = await observe(browser, frame_id=frame_id, include_screenshot=True)
        page.actual_hit_frame = str(id(page.main_frame))
        with pytest.raises(BusinessError, match="covered"):
            await browser.prepare(TOKEN, action(snapshot, locator=coordinate(snapshot, x=40, y=50)), snapshot)
    run(exercise())


def test_duplicate_child_frame_mapping_fails_closed():
    async def exercise():
        browser, page, *_ = backend()
        one, two = Frame(page), Frame(page)
        page.frames.extend([one, two])
        with pytest.raises(BusinessError): await observe(browser, frame_id=browser._register_frame(one))
    run(exercise())


def test_read_link_cannot_cross_logical_sites_even_when_both_sources_allowed():
    async def exercise():
        browser, page, *_ = backend()
        browser.allowed_sources = (SCOPE, SourceScope(source_id="other", site_id="different-site", origin="https://other.example"))
        page.main_frame.nodes[0]["href"] = "https://other.example/page"
        snapshot = await observe(browser)
        with pytest.raises(BusinessError): await browser.prepare(TOKEN, action(snapshot), snapshot)
    run(exercise())


def test_coordinate_input_uses_exact_current_dom_control_not_main_world_hit_handle():
    async def exercise():
        browser, page, *_ = backend()
        page.main_frame.nodes = [node(tag="input")]
        page.main_frame.hit = page.main_frame.nodes[0]
        snapshot = await observe(browser, include_screenshot=True)
        proposed = action(snapshot, "input", effect="write", args={"text": "new value"}, locator=coordinate(snapshot))
        prepared = await browser.prepare(TOKEN, proposed, snapshot, trusted_write=True)
        await browser.execute(TOKEN, proposed, prepared)
        assert ("fill", "new value") in page.main_frame.calls
        assert sum(call[0] == "screenshot" for call in page.calls) == 3
    run(exercise())


@pytest.mark.parametrize("url,permitted", [(URL, True), ("https://outside.example/page", False)])
def test_native_guard_intercepts_each_redirect_request_before_network(url, permitted):
    async def exercise():
        browser, page, *_ = backend()
        await observe(browser)
        cdp = browser._cdps[id(page)]
        await cdp.handlers["Fetch.requestPaused"]({"requestId": "redirect-request", "request": {"method": "GET", "url": url}})
        method, args = cdp.protocol_calls[-1]
        assert method == ("Fetch.continueRequest" if permitted else "Fetch.failRequest")
        assert args["requestId"] == "redirect-request"
    run(exercise())


def test_native_guard_revalidates_qualification_for_late_background_requests():
    async def exercise():
        browser, page, _, managed = backend()
        await observe(browser)
        async def revoked(*args, **kwargs):
            raise BusinessError("RESOURCE_CONFLICT", "Synthetic revoked token", status=409)
        managed.context = revoked
        cdp = browser._cdps[id(page)]
        await cdp.handlers["Fetch.requestPaused"]({"requestId": "late", "request": {"method": "GET", "url": URL}})
        assert cdp.protocol_calls[-1][0] == "Fetch.failRequest"
    run(exercise())


@pytest.mark.parametrize("source,origin,permitted", [("Proxy", "http://127.0.0.1:54321", True),
    ("Proxy", "http://127.0.0.1:54322", False), ("Server", "http://127.0.0.1:54321", False)])
def test_native_authentication_only_exact_managed_proxy(source, origin, permitted):
    async def exercise():
        browser, page, *_ = backend()
        await observe(browser)
        cdp = browser._cdps[id(page)]
        event = {"requestId": "auth", "request": {"method": "GET", "url": URL},
                 "authChallenge": {"source": source, "origin": origin}}
        await cdp.handlers["Fetch.authRequired"](event)
        method, args = cdp.protocol_calls[-1]
        assert method == "Fetch.continueWithAuth"
        auth = args["authChallengeResponse"]
        assert auth["response"] == ("ProvideCredentials" if permitted else "CancelAuth")
        if permitted:
            await cdp.handlers["Fetch.authRequired"](event)
            assert cdp.protocol_calls[-1][1]["authChallengeResponse"] == {"response": "CancelAuth"}
        else: assert set(auth) == {"response"}
    run(exercise())


def test_popup_first_navigation_is_denied_until_native_guard_installed():
    async def exercise():
        browser, _, context, _ = backend()
        await observe(browser)
        popup = Page()
        route = Route("GET", URL)
        route.request.frame = popup.main_frame
        route.request.is_navigation_request = lambda: True
        await context.routes[0][1](route)
        assert route.calls == [("abort", "blockedbyclient")]
    run(exercise())


def test_browser_close_releases_cdp_sessions_and_retains_denial_fence():
    async def exercise():
        browser, page, context, _ = backend()
        await observe(browser)
        cdp = browser._cdps[id(page)]
        await browser.aclose()
        assert cdp.detached is True
        route = Route("GET", URL)
        await context.routes[0][1](route)
        assert route.calls == [("abort", "blockedbyclient")]
        with pytest.raises(BusinessError): await observe(browser)
    run(exercise())


def test_redirect_to_another_declared_site_requires_its_resource_lease():
    async def exercise():
        browser, page, *_ = backend()
        browser.allowed_sources += (SourceScope(source_id="other", site_id="other", origin="https://other.example"),)
        await observe(browser)
        cdp = browser._cdps[id(page)]
        await cdp.handlers["Fetch.requestPaused"]({"requestId": "redirect", "request": {"method": "GET", "url": "https://other.example/page"}})
        assert cdp.protocol_calls[-1][0] == "Fetch.failRequest"
    run(exercise())


def test_declared_second_site_with_complete_typed_lease_can_make_read_request():
    async def exercise():
        browser, page, *_ = backend()
        browser.allowed_sources += (SourceScope(source_id="other", site_id="other", origin="https://other.example"),)
        qualified = replace(TOKEN, resources=(Resource.site_identity("fixture").resource_key,
                                            Resource.site_identity("other").resource_key))
        await browser.capture(qualified)
        cdp = browser._cdps[id(page)]
        await cdp.handlers["Fetch.requestPaused"]({"requestId": "scoped", "request": {"method": "GET", "url": "https://other.example/page"}})
        assert cdp.protocol_calls[-1][0] == "Fetch.continueRequest"
    run(exercise())


def test_exact_mutation_endpoint_grant_does_not_replace_site_lease():
    async def exercise():
        browser, page, *_ = backend()
        browser.allowed_sources += (SourceScope(source_id="other", site_id="other", origin="https://other.example"),)
        await observe(browser)
        browser._allowed_mutations = frozenset({("POST", "https://other.example/save")})
        cdp = browser._cdps[id(page)]
        await cdp.handlers["Fetch.requestPaused"]({"requestId": "write", "request": {"method": "POST", "url": "https://other.example/save"}})
        assert cdp.protocol_calls[-1][0] == "Fetch.failRequest"
    run(exercise())
