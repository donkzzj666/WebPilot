#!/usr/bin/env python3
"""M1-13 gateway acceptance against an owned, registered WebArena fixture.

The probe creates a new SQLite data domain and a disposable managed Chromium.
Only its synthetic HTTP origin is registered. It never opens a user profile,
calls a real account, or sends browser writes to a production website.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')


FIXTURE = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="fixture-repository" content="Fixture/Gateway"><meta name="fixture-branch" content="gateway-fixture">
<meta name="fixture-base-sha" content="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
<meta name="fixture-account" content="fixture-user">
<title>M1-13 owned gateway fixture</title><style>body{font:18px system-ui;margin:40px}
main{max-width:750px}label,button,a,input,select{margin:8px;padding:8px;display:inline-block}
iframe{width:700px;height:130px}*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;outline:none!important}
button,input,textarea,select{appearance:none;-webkit-appearance:none;border:1px solid #777;border-radius:0;box-shadow:none;background:#f2f2f2;color:#111}
a:link,a:visited,a:hover,a:active{color:#125ea9;text-decoration:underline;background:transparent}</style><main><h1>Structured gateway fixture</h1>
<p id="visible">Synthetic fixture data only.</p>
<form action="/fixture/search" method="get"><label for="query">Search fixture</label>
<input id="query" name="q"><select name="category" aria-label="Category">
<option>All</option><option>Research</option></select><button type="submit">Search</button></form>
<a id="next" href="/fixture/next">Read next page</a>
<a id="popup" href="/fixture/next" target="_blank">Open fixture tab</a>
<a id="attachment" href="/fixture/attachment" download="synthetic.txt">Download synthetic attachment</a>
<a id="outside" href="/outside">Outside contract</a>
<form action="/fixture/save" method="post"><label for="editor">Fixture editor</label>
<textarea id="editor" name="body">Synthetic original content</textarea>
<button id="save" type="submit">Save fixture write</button></form>
<button id="ambiguous" onclick="document.body.dataset.changed='yes'">Unknown button</button>
<a id='literal"[]value' href="/fixture/next">Exact DOM attribute link</a>
<iframe title="Synthetic frame" src="/fixture/frame"></iframe>
<p style="margin-top:1100px">Scroll end</p></main></html>'''
FRAME = b'''<!doctype html><title>Synthetic frame</title><style>
*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;outline:none!important}
a:link,a:visited,a:hover,a:active{color:#125ea9;text-decoration:underline;background:transparent}</style>
<p>Owned child frame.</p><a id="frame-link" href="/fixture/next">Frame next</a>'''
COORDINATE_FIXTURE = b'''<!doctype html><title>Synthetic coordinate fixture</title><style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#fff}
iframe{position:absolute;left:40px;top:40px;width:700px;height:130px;border:0;display:block}</style>
<iframe title="Synthetic coordinate frame" src="/fixture/coordinate-frame"></iframe>'''
COORDINATE_FRAME = b'''<!doctype html><title>Synthetic coordinate frame</title><style>
html,body{margin:0;overflow:hidden;background:#fff}
a:link,a:visited,a:hover,a:active{position:absolute;left:20px;top:40px;width:140px;height:40px;display:block;
font:16px/40px Arial;color:#125ea9;background:#dfeafd;text-decoration:underline;outline:none}</style>
<a id="frame-link" href="/fixture/next">Frame next</a>'''
NEXT = b'''<!doctype html><title>Synthetic next page</title>
<main><h1>Next fixture page</h1><a href="/fixture">Return</a></main>'''
OPTIONAL_RESOURCES = b'''<!doctype html><meta charset="utf-8"><title>Owned optional resource fixture</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/static/optional.css"><script src="/static/optional.js"></script>
<main><h1 id="main-document">Readable scoped main document</h1>
<img src="/icons/optional.png" alt="Synthetic optional image"><p>Title: Synthetic fixture</p></main>'''


def active_request_fixture(method):
    """The owned synchronous XHR settles before DOMContentLoaded, without a retry."""
    assert method in ('GET', 'POST')
    return ('''<!doctype html><title>Owned critical request fixture</title><script>
try {const xhr=new XMLHttpRequest();xhr.open('%s','/outside-network/active-%s',false);xhr.send(null);}
catch(error) {document.documentElement.dataset.denied='yes';}
</script><main>Scoped document with a forbidden active request.</main>''' % (method, method.lower())).encode()


class GatewayFixture:
    """Record only method/path and synthetic write counts, never request headers."""
    def __init__(self):
        self.requests = []
        self.write_count = 0
        self.active = set()
        self.hang = asyncio.Event()

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.port}'

    @property
    def url(self):
        return self.origin + '/fixture'

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            method, target, _ = raw.split(b'\r\n', 1)[0].decode('ascii', 'replace').split(' ', 2)
            headers = {}
            for header in raw.split(b'\r\n')[1:]:
                if b':' in header:
                    name, value = header.split(b':', 1)
                    headers[name.lower()] = value.strip()
            size = int(headers.get(b'content-length', b'0'))
            if size:
                await asyncio.wait_for(reader.readexactly(size), 5)
            path = urlsplit(target).path
            self.requests.append({'method': method, 'path': path})
            status = b'200 OK'
            extra = b''
            if method == 'POST' and path == '/fixture/save':
                self.write_count += 1
                body = b'<!doctype html><title>Synthetic save receipt</title><p>Saved synthetic content.</p>'
            elif method != 'GET':
                status, body = b'403 Forbidden', b'unsupported synthetic operation'
            elif path in ('/fixture', '/fixture/search'):
                body = FIXTURE
            elif path == '/fixture/frame':
                body = FRAME
            elif path == '/fixture/coordinates':
                body = COORDINATE_FIXTURE
            elif path == '/fixture/coordinate-frame':
                body = COORDINATE_FRAME
            elif path == '/fixture/network/optional':
                body = OPTIONAL_RESOURCES
            elif path in ('/fixture/network/active-get', '/fixture/network/active-post'):
                body = active_request_fixture('POST' if path.endswith('-post') else 'GET')
            elif path == '/fixture/network/passive-redirect':
                body = b'<!doctype html><title>Owned passive redirect</title><script src="/fixture/network/redirect-resource"></script><main>Scoped document.</main>'
            elif path == '/fixture/network/redirect-resource':
                status, body = b'302 Found', b''
                extra = b'Location: /outside-network/passive-redirect\r\n'
            elif path in ('/fixture/next', '/outside'):
                body = NEXT
            elif path == '/fixture/attachment':
                body = b'Synthetic attachment data only.\n'
                extra = b'Content-Disposition: attachment; filename=synthetic.txt\r\n'
            elif path == '/fixture/redirect':
                status, body = b'302 Found', b''
                extra = b'Location: /outside\r\n'
            elif path == '/fixture/hang':
                await self.hang.wait()
                body = NEXT
            else:
                status, body = b'404 Not Found', b'missing synthetic page'
            writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Type: text/html; charset=utf-8\r\n'
                         b'Cache-Control: no-store\r\nConnection: close\r\n' + extra + b'Content-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.active.discard(task)

    async def start(self):
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.hang.set()
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*tuple(self.active), return_exceptions=True)


def seed_run(path, store, fixture, name, *, identity_ref=None, limits=None, extra_source=False,
             source_prefix='/fixture', start_url=None):
    from webagent.db import connect, transaction
    from webagent.db.repository import add_contract, create_run, create_task, utc_text
    from webagent.scheduler.models import Resource
    from webagent.tasks.compiler import compile_draft
    now = utc_text()
    contract = compile_draft(
        {'instruction': 'Inspect the synthetic gateway acceptance fixture', 'scenario': 'research',
         'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixture'],
          'topic_criteria': ['synthetic fixture data'], 'cutoff_at': now, 'max_items': 3}},
        task_id='task-' + name, version=1, created_at=now,
        provenance=[{'origin': 'explicit_test_configuration', 'reference': 'gateway-acceptance-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    contract['sources'] = [{'source_id': 'local-fixture', 'site_id': 'local-fixture',
                            'origin': fixture.origin, 'path_prefix': source_prefix}]
    if extra_source:
        contract['sources'].append({'source_id': 'unleased-fixture', 'site_id': 'other-logical-site',
                                    'origin': fixture.origin, 'path_prefix': '/outside'})
    contract['start_urls'] = [start_url or fixture.url]
    contract['budget_profile'].update(limits or {})
    resources = [Resource.site_identity('local-fixture', identity_ref, realm='webarena'),
                 Resource.browser_context(name)]
    if identity_ref:
        contract['scenario'], contract['identity_ref'] = 'operations', identity_ref
        contract['targets'] = [{'object_id': 'fixture-repository', 'kind': 'repository',
                                'canonical_name': 'Fixture/Gateway'}]
        contract['parameters'] = {'scenario': 'operations', 'operation_kind': 'code_repair',
                                  'repository': 'Fixture/Gateway', 'base_sha': 'a' * 40,
                                  'branch': 'gateway-fixture', 'failure_run_id': 'synthetic-check',
                                  'required_checks': ['synthetic-check'], 'independent_rules_ref': 'fixture-rules'}
        contract['action_policy'] = {'mode': 'repository_write', 'repository': 'Fixture/Gateway',
                                     'base_branch': 'main', 'branch': 'gateway-fixture', 'base_sha': 'a' * 40,
                                     'task_kind': 'ordinary_repair', 'allowed_files': ['src/fixture.txt'],
                                     'workflow_exception_files': [], 'protected_patterns': ['tests/*', '.github/*'],
                                     'required_checks': ['synthetic-check'], 'independent_rules_ref': 'fixture-rules',
                                     'allowed_operations': ['edit_file']}
        resources.append(Resource.repository_write('Fixture/Gateway'))
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'],
                    requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=name, task_id=contract['task_id'], contract_version=1,
                   graph_version='gateway-fixture-v1', graph_state_schema_version='fixture-v1',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
    store.enqueue(name, resources, expected_state_version=0, queue_class='webarena')
    return contract


def synthetic_identity(path, fixture):
    """Publish synthetic metadata without OS credentials or a production login."""
    from webagent.identities.store import IdentityStore
    from webagent.sessions.models import SessionOwner
    from webagent.sessions.store import SessionRegistry
    store, registry = IdentityStore(path), SessionRegistry(path)
    login = store.create(site_id='local-fixture', realm='webarena', origin=fixture.origin,
                         expected_account='fixture-user')
    session = registry.reserve('synthetic-identity-manager', SessionOwner('login', login.login_id,
                               'local-fixture', realm='webarena'))
    registry.opened(session.session_id, session.manager_id)
    login = store.attach_session(login.login_id, login.state_version, session.session_id, session.manager_id)
    login = store.begin_confirm(login.login_id, login.state_version)
    login = store.identity_candidate(login.login_id, login.state_version, 'fixture-user')
    verified = store.finalize_verified(login.login_id, login.state_version,
        identity_ref=login.candidate_identity_ref, normalized_account='fixture-user',
        auth_ref=str(uuid4()), auth_sha256='a' * 64, verification_origin=fixture.origin,
        adapter_id='synthetic-gateway-adapter-v1', evidence_sha256='e' * 64)
    registry.closing(session.session_id, session.manager_id)
    registry.closed(session.session_id, session.manager_id)
    return verified.identity_ref


def action(token, snapshot, step, kind, *, locator=None, args=None, write_scope=None):
    return {'run_id': token.run_id, 'step_id': step, 'epoch': token.epoch,
            'snapshot_id': snapshot['snapshot_id'], 'action_type': kind,
            'expected_effect': 'write' if write_scope else 'read',
            'target': {'page_url': snapshot['source_url'], 'tab_id': snapshot['tab_id'],
                       'frame_id': snapshot['frame_id'], 'locator': locator, 'write_scope': write_scope},
            'args': args or {}}


async def rejected(awaitable):
    from webagent.errors import BusinessError
    try:
        await awaitable
    except BusinessError as error:
        return {'code': error.code, 'field': error.field, 'status': error.status}
    raise AssertionError('Synthetic boundary did not reject the action')


async def form_driver_probes(base, fixture, observations, check):
    """Each local form driver gets a separate identity and data domain.

    No local edit is silently granted a second operation. The gateway records
    each supplied write declaration as UNKNOWN, then quarantines that Run.
    """
    from webagent.config import Settings
    from webagent.db import connect, migrate
    from webagent.gateway.permissions import WriteAuthorization
    from webagent.gateway.service import BrowserGateway
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.scheduler.store import SchedulerStore
    from webagent.sessions.manager import ManagedBrowser
    from webagent.sessions.models import SessionOwner
    cases = (
        ('input', 'editor', {'text': 'Synthetic gateway edit'}),
        ('keypress', 'editor', {'key': 'Tab'}),
        ('select', 'category', {'option_label': 'Research'}),
    )
    observations['local_form_drivers'] = {}
    for kind, element_id, args in cases:
        directory = base / ('driver-' + kind)
        directory.mkdir()
        settings = Settings(directory)
        migrate(settings.business_db)
        store = SchedulerStore(settings.business_db)
        identity_ref = synthetic_identity(settings.business_db, fixture)
        seed_run(settings.business_db, store, fixture, 'driver-' + kind, identity_ref=identity_ref)
        manager = ManagedBrowser(settings, headless=False,
            network_config=NetworkConfig(webarena_endpoints=(Endpoint('http', '127.0.0.1', fixture.port),)))
        try:
            await manager.start()
            generation = store.start_worker(manager.manager_id)
            token = store.claim(manager.manager_id, generation)
            owner = SessionOwner('run', token.run_id, 'local-fixture', identity_ref, realm='webarena')
            session = await manager.create(owner, execution_token=token)
            context = await manager.context(session.session_id, owner, execution_token=token)
            page = context.pages[0]
            async def authorize(value, snapshot, prepared):
                locator = value.target.locator
                current_control = (locator.strategy == 'semantic' and locator.label == 'Fixture editor'
                                   if kind == 'input' else locator.strategy == 'dom' and locator.value == element_id)
                if page.url != fixture.url or value.action_type != kind or not current_control:
                    raise ValueError('Synthetic form driver target changed')
                facts = {}
                for key in ('repository', 'branch', 'base-sha', 'account'):
                    facts[key] = await page.locator('meta[name="fixture-' + key + '"]').get_attribute('content')
                assert facts == {'repository': 'Fixture/Gateway', 'branch': 'gateway-fixture',
                                 'base-sha': 'a' * 40, 'account': 'fixture-user'}
                return WriteAuthorization(facts['repository'], facts['branch'], facts['base-sha'], 'edit_file',
                                          ('src/fixture.txt',), identity_ref, ())
            gateway = BrowserGateway.from_managed(manager, session, scheduler=store, write_authorizer=authorize)
            await gateway.navigate(token, fixture.url, kind + '-bootstrap')
            await page.wait_for_load_state('load')
            snapshot = await gateway.observe(token)
            scope = {'repository': 'Fixture/Gateway', 'branch': 'gateway-fixture', 'base_sha': 'a' * 40,
                     'operation': 'edit_file', 'files': ['src/fixture.txt'], 'operation_id': kind + '-operation',
                     'identity_ref': identity_ref, 'target_rechecked_at': snapshot['captured_at']}
            locator = ({'strategy': 'semantic', 'role': None, 'accessible_name': None, 'label': 'Fixture editor'}
                       if kind == 'input' else {'strategy': 'dom', 'attribute': 'id' if kind == 'keypress' else 'name',
                                               'value': element_id})
            result = await gateway.dispatch(token, action(token, snapshot, kind + '-actual-driver', kind,
                                                         locator=locator, args=args, write_scope=scope))
            check(kind + '_real_browser_driver_keeps_write_uncertainty', result['status'] == 'UNKNOWN'
                  and store.budgets.status(token.run_id)['actions_used'] == 2)
            if kind == 'input':
                check('semantic_label_input_changes_only_synthetic_editor', await page.locator('#editor').input_value() == args['text'])
            elif kind == 'keypress':
                check('bounded_keypress_changes_synthetic_keyboard_focus', await page.evaluate('document.activeElement.id') == 'save')
            else:
                check('bounded_select_changes_only_synthetic_control', await page.locator('select[name="category"]').input_value() == 'Research')
            with connect(settings.business_db) as db:
                check(kind + '_driver_does_not_confirm_an_external_operation',
                      db.execute('SELECT status FROM write_intents').fetchone()[0] == 'UNKNOWN')
            observations['local_form_drivers'][kind] = {'atomic_dispatches': 2, 'status': result['status'],
                                                       'synthetic_http_writes': fixture.write_count}
        finally:
            await manager.aclose()


async def denied_resource_probes(base, fixture, observations, check):
    """Real browser denials never reach the owned HTTP receiver.

    Only the explicitly scoped main document may finish after a direct optional
    resource denial. Synchronous active requests and a passive redirect must
    still fence the atomic navigation. All data and browser authority are new.
    """
    from webagent.config import Settings
    from webagent.db import connect, migrate
    from webagent.gateway.service import BrowserGateway
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.scheduler.store import SchedulerStore
    from webagent.sessions.manager import ManagedBrowser
    from webagent.sessions.models import SessionOwner
    directory = base / 'denied-resources'
    directory.mkdir()
    settings = Settings(directory)
    migrate(settings.business_db)
    store = SchedulerStore(settings.business_db)
    manager = ManagedBrowser(settings, headless=False,
        network_config=NetworkConfig(webarena_endpoints=(Endpoint('http', '127.0.0.1', fixture.port),)))
    observations['denied_resources'] = {}
    try:
        await manager.start()
        generation = store.start_worker(manager.manager_id)
        for case in ('optional', 'active-get', 'active-post', 'passive-redirect'):
            name, url = 'denied-resource-' + case, fixture.origin + '/fixture/network/' + case
            prefix = '/fixture/network' if case == 'passive-redirect' else '/fixture/network/' + case
            seed_run(settings.business_db, store, fixture, name, source_prefix=prefix, start_url=url)
            token = store.claim(manager.manager_id, generation)
            owner = SessionOwner('run', token.run_id, 'local-fixture', realm='webarena')
            session = await manager.create(owner, execution_token=token)
            gateway = BrowserGateway.from_managed(manager, session, scheduler=store)
            denial_events = []
            record_denial = gateway.browser._record_request_denial
            async def observed_denial(method, destination, **metadata):
                event = {'method': method, 'path': urlsplit(destination).path,
                    **{key: metadata.get(key) for key in ('resource_type', 'is_navigation',
                        'is_redirect', 'is_main_frame', 'guard_ready')}}
                denial_events.append(event)
                await record_denial(method, destination, **metadata)
            gateway.browser._record_request_denial = observed_denial
            if case != 'optional':
                await asyncio.sleep(3.05)
                token = store.heartbeat(token)
            initial_requests, step = len(fixture.requests), name + '-navigation'
            old_token = token
            if case == 'optional':
                result = await gateway.navigate(token, url, step)
                context = await manager.context(session.session_id, owner, execution_token=token)
                await context.pages[0].wait_for_load_state('load')
                check('denied_optional_assets_preserve_real_main_navigation', result['status'] == 'COMPLETED'
                    and await context.pages[0].locator('#main-document').inner_text() == 'Readable scoped main document')
                with connect(settings.business_db) as db:
                    check('optional_main_navigation_has_durable_completed_step',
                        db.execute('SELECT status FROM steps WHERE step_id=?', (step,)).fetchone()[0] == 'COMPLETED')
                counts = gateway.browser
                receiver_paths = {row['path'] for row in fixture.requests[initial_requests:]}
                observations['denied_resources'][case] = {'status': result['status'],
                    'optional_denials': counts._optional_blocked_requests,
                    'critical_denials': counts._critical_blocked_requests,
                    'denial_events': denial_events,
                    'receiver_requests': fixture.requests[initial_requests:]}
                check('real_optional_requests_are_classified_without_forwarding',
                    counts._optional_blocked_requests >= 3 and counts._critical_blocked_requests == 0)
                check('optional_css_script_image_never_reach_owned_receiver',
                    not receiver_paths.intersection({'/static/optional.css', '/static/optional.js', '/icons/optional.png'}))
                store.finish(token, target='CANCELLED')
                await manager.close(session.session_id, owner)
            else:
                failure = await rejected(gateway.navigate(token, url, step))
                with connect(settings.business_db) as db:
                    check(case + '_remains_durable_uncertain_navigation',
                        db.execute('SELECT status FROM steps WHERE step_id=?', (step,)).fetchone()[0] == 'UNKNOWN')
                try:
                    store.validate(old_token)
                except Exception as error:
                    check(case + '_fences_original_execution_epoch', getattr(error, 'status', None) == 409)
                else:
                    raise AssertionError('Critical resource denial retained original executor')
                check(case + '_request_never_reaches_owned_receiver',
                    not any(row['path'].startswith('/outside-network/') for row in fixture.requests[initial_requests:]))
                check(case + '_is_critical_even_under_read_navigation', gateway.browser._critical_blocked_requests > 0)
                observations['denied_resources'][case] = {'rejection': failure,
                    'denial_events': denial_events,
                    'optional_denials': gateway.browser._optional_blocked_requests,
                    'critical_denials': gateway.browser._critical_blocked_requests}
                await manager.close(session.session_id, owner)
                recovered = store.claim(manager.manager_id, generation)
                assert recovered is not None and recovered.run_id == name
                store.finish(recovered, target='CANCELLED')
        observations['denied_resources']['receiver_requests'] = [row for row in fixture.requests
            if row['path'].startswith(('/fixture/network/', '/outside-network/', '/static/', '/icons/'))]
    finally:
        await manager.aclose()


async def verify(output, report):
    from webagent.config import Settings, disable_external_tracing
    from webagent.db import connect, migrate
    from webagent.gateway.permissions import WriteAuthorization
    from webagent.gateway.service import BrowserGateway
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.scheduler.store import SchedulerStore
    from webagent.sessions.manager import ManagedBrowser
    from webagent.sessions.models import SessionOwner
    disable_external_tracing()
    observations = {'scope': 'Synthetic fixture only', 'rejections': {}}
    def check(name, condition=True):
        assert condition, name
        report['checks'][name] = True
    fixture, manager = await GatewayFixture().start(), None
    with tempfile.TemporaryDirectory(prefix='webpilot-gateway-') as directory:
        settings = Settings(Path(directory).resolve())
        try:
            migrate(settings.business_db)
            store = SchedulerStore(settings.business_db)
            manager = ManagedBrowser(settings, headless=False,
                network_config=NetworkConfig(webarena_endpoints=(Endpoint('http', '127.0.0.1', fixture.port),)))
            await manager.start()
            generation = store.start_worker(manager.manager_id)
            seed_run(settings.business_db, store, fixture, 'gateway-read')
            token = store.claim(manager.manager_id, generation)
            owner = SessionOwner('run', token.run_id, 'local-fixture', realm='webarena')
            session = await manager.create(owner, execution_token=token, gateway_downloads=True)
            gateway = BrowserGateway.from_managed(manager, session, scheduler=store)
            result = await gateway.navigate(token, fixture.url, 'initial-navigation')
            check('initial_navigation_uses_atomic_gateway_and_budget', result['status'] == 'COMPLETED'
                  and store.budgets.status(token.run_id)['actions_used'] == 1
                  and gateway.local_result('initial-navigation')['http_status'] == 200)
            context = await manager.context(session.session_id, owner, execution_token=token)
            page = context.pages[0]
            await page.wait_for_load_state('load')
            await asyncio.sleep(.05)
            check('registered_fixture_page_reaches_browser_through_authenticated_proxy', await page.locator('#visible').count() == 1)
            snapshot = await gateway.observe(token, include_screenshot=True)
            check('snapshot_is_local_metadata_blocked_until_evidence_filtering', snapshot['redaction_status'] == 'BLOCKED'
                  and snapshot['visible_excerpt'] == '' and snapshot['screenshot_evidence_id'])

            # Raw page access below belongs only to this independent fixture
            # evaluator, to create stale surfaces and inspect request counts.
            stale = replace(token, epoch=token.epoch + 1)
            before = store.budgets.status(token.run_id)['actions_used']
            observations['rejections']['old_epoch'] = await rejected(gateway.dispatch(stale,
                action(token, snapshot, 'old-epoch', 'scroll', args={'direction': 'down', 'pixels': 1})))
            check('old_epoch_rejected_before_browser_or_budget', store.budgets.status(token.run_id)['actions_used'] == before)

            for name, kind, locator, args in (
                ('javascript', 'javascript', None, {'script': 'alert(1)'}),
                ('outside_scope', 'navigate', None, {'url': fixture.origin + '/outside'}),
                ('read_disguised_write', 'click', {'strategy': 'dom', 'attribute': 'id', 'value': 'save'}, {}),
                ('ambiguous_button', 'click', {'strategy': 'dom', 'attribute': 'id', 'value': 'ambiguous'}, {}),
                ('selector_injection', 'click', {'strategy': 'dom', 'attribute': 'id', 'value': 'next\"] , button[id=\"save'}, {}),
                ('read_editor_input', 'input', {'strategy': 'dom', 'attribute': 'id', 'value': 'editor'}, {'text': 'SYNTHETIC_DENIED'}),
            ):
                observations['rejections'][name] = await rejected(gateway.dispatch(token,
                    action(token, snapshot, 'denied-' + name, kind, locator=locator, args=args)))
                check(name + '_rejected_before_dispatch', store.budgets.status(token.run_id)['actions_used'] == before)
            check('untrusted_read_hint_cannot_send_fixture_write', fixture.write_count == 0)

            # A website-owned namespace must have no authority over the fixed
            # isolated-world observation probe or its DOM-change fingerprint.
            await page.evaluate("window.__webpilotGatewayProbeV1={version:1,capture:()=>({revision:0,text:'forged'})}")
            snapshot = await gateway.observe(token, include_screenshot=True)
            await page.locator('#visible').evaluate("element => element.textContent='Changed synthetic DOM'")
            observations['rejections']['mutated_page'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'old-page', 'scroll', args={'direction': 'down', 'pixels': 1})))
            check('website_namespace_cannot_hide_dom_changes', store.budgets.status(token.run_id)['actions_used'] == before)

            snapshot = await gateway.observe(token, include_screenshot=True)
            await page.set_viewport_size({'width': 1100, 'height': 750})
            observations['rejections']['viewport_changed'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'old-viewport', 'scroll', args={'direction': 'down', 'pixels': 1})))
            check('changed_viewport_rejects_old_observation', store.budgets.status(token.run_id)['actions_used'] == before)
            snapshot = await gateway.observe(token, include_screenshot=True)
            coord = {'strategy': 'coordinate', 'screenshot_evidence_id': 'stale-screenshot',
                     'snapshot_id': snapshot['snapshot_id'], 'tab_id': snapshot['tab_id'],
                     'frame_id': snapshot['frame_id'], 'width': snapshot['width'], 'height': snapshot['height'],
                     'x': 10, 'y': 10}
            observations['rejections']['old_screenshot'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'old-screenshot', 'click', locator=coord)))
            check('old_screenshot_reference_cannot_dispatch', store.budgets.status(token.run_id)['actions_used'] == before)

            await page.evaluate("""() => {const canvas=document.createElement('canvas');canvas.id='synthetic-pixels';
                canvas.width=100;canvas.height=40;document.body.prepend(canvas);
                const context=canvas.getContext('2d');context.fillStyle='red';context.fillRect(0,0,100,40);} """)
            snapshot = await gateway.observe(token, include_screenshot=True)
            capture = gateway.local_result(snapshot['snapshot_id'])
            link = next(item for item in capture['elements'] if item['attrs'].get('id') == 'next')
            bounds = link['bounds']
            coord = {'strategy': 'coordinate', 'screenshot_evidence_id': snapshot['screenshot_evidence_id'],
                     'snapshot_id': snapshot['snapshot_id'], 'tab_id': snapshot['tab_id'],
                     'frame_id': snapshot['frame_id'], 'width': snapshot['width'], 'height': snapshot['height'],
                     'x': int(bounds['x'] + bounds['width'] / 2), 'y': int(bounds['y'] + bounds['height'] / 2)}
            await page.evaluate("""() => {const context=document.getElementById('synthetic-pixels').getContext('2d');
                context.fillStyle='blue';context.fillRect(0,0,100,40);} """)
            current = await gateway.browser.capture(token)
            check('canvas_change_has_same_dom_fingerprint', current.page_version == snapshot['page_version'])
            observations['rejections']['canvas_pixels_changed'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'old-canvas-pixels', 'click', locator=coord)))
            check('pixel_change_rejects_coordinate_with_unchanged_dom', store.budgets.status(token.run_id)['actions_used'] == before)

            child_frame = next(frame for frame in page.frames if frame != page.main_frame)
            child_id = gateway.browser._register_frame(child_frame)
            snapshot = await gateway.observe(token, frame_id=child_id)
            value = action(token, snapshot, 'wrong-frame', 'scroll', args={'direction': 'down', 'pixels': 1})
            value['target']['frame_id'] = gateway.browser._register_frame(page.main_frame)
            observations['rejections']['frame_mismatch'] = await rejected(gateway.dispatch(token, value))
            check('changed_frame_binding_rejected', store.budgets.status(token.run_id)['actions_used'] == before)

            snapshot = await gateway.observe(token)
            other = await context.new_page()
            other_id = gateway.browser._register_page(other)
            mismatch = action(token, snapshot, 'wrong-tab', 'scroll', args={'direction': 'down', 'pixels': 1})
            mismatch['target']['tab_id'] = other_id
            observations['rejections']['tab_mismatch'] = await rejected(gateway.dispatch(token, mismatch))
            check('changed_tab_binding_rejected', store.budgets.status(token.run_id)['actions_used'] == before)
            other_snapshot = await gateway.observe(token, tab_id=other_id)
            other_navigation = action(token, other_snapshot, 'new-tab-navigation', 'navigate',
                                      args={'url': fixture.origin + '/fixture/next'})
            other_navigation['target']['page_url'] = fixture.origin + '/fixture/next'
            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            other_result = await gateway.dispatch(token, other_navigation)
            check('new_tab_navigation_is_guarded_and_counted', other_result['status'] == 'COMPLETED')
            snapshot = await gateway.observe(token, tab_id=snapshot['tab_id'])
            switched = await gateway.dispatch(token, action(token, snapshot, 'switch-tab', 'switch_tab', args={'tab_id': other_id}))
            check('switch_tab_is_a_structured_atomic_action', switched['status'] == 'COMPLETED')
            other_snapshot = await gateway.observe(token)
            switched_back = await gateway.dispatch(token, action(token, other_snapshot, 'switch-tab-back', 'switch_tab',
                args={'tab_id': snapshot['tab_id']}))
            check('switch_tab_returns_to_bound_run_context', switched_back['status'] == 'COMPLETED')
            await other.close()

            atomic_before = store.budgets.status(token.run_id)['actions_used']
            builders = [lambda snapshot, index=index: action(token, snapshot, f'compound-{index}', 'scroll',
                        args={'direction': 'down' if index % 2 == 0 else 'up', 'pixels': 10}) for index in range(10)]
            results = await gateway.sequence(token, builders)
            check('ten_atom_composite_is_ten_browser_dispatches', len(results) == 10
                  and all(value['status'] == 'COMPLETED' for value in results)
                  and store.budgets.status(token.run_id)['actions_used'] - atomic_before == 10)

            snapshot = await gateway.observe(token)
            read = await gateway.dispatch(token, action(token, snapshot, 'visible-read', 'read_visible'))
            check('visible_read_uses_separate_observation_counter', read['status'] == 'COMPLETED'
                  and store.budgets.status(token.run_id)['actions_used'] == atomic_before + 10)
            snapshot = await gateway.observe(token)
            screenshot = await gateway.dispatch(token, action(token, snapshot, 'explicit-screenshot', 'screenshot'))
            check('screenshot_uses_separate_counter_and_local_bytes', screenshot['status'] == 'COMPLETED'
                  and bool(gateway.local_result('explicit-screenshot')['artifact_bytes']))

            snapshot = await gateway.observe(token)
            capture = gateway.local_result(snapshot['snapshot_id'])
            if hasattr(capture, 'elements'):
                capture = vars(capture)
            attachment_url = fixture.origin + '/fixture/attachment'
            attachment_link = next(item for item in capture['links'] if item['href'] == attachment_url)
            link_reference = attachment_link['link_evidence_id']
            missing_url = fixture.origin + '/fixture/missing-attachment'
            observations['rejections']['unlinked_attachment'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'unlinked-download', 'download_attachment',
                       locator={'strategy': 'dom', 'attribute': 'id', 'value': 'attachment'},
                       args={'attachment_url': missing_url, 'link_evidence_id': link_reference})))
            check('attachment_must_match_current_page_link', fixture.write_count == 0)
            attachment = await gateway.dispatch(token, action(token, snapshot, 'designated-download', 'download_attachment',
                locator={'strategy': 'dom', 'attribute': 'id', 'value': 'attachment'},
                args={'attachment_url': attachment_url, 'link_evidence_id': link_reference}))
            check('designated_attachment_uses_browser_download', attachment['status'] == 'COMPLETED'
                  and gateway.local_result('designated-download')['artifact_bytes'] == b'Synthetic attachment data only.\n')

            # Coordinate checks use a static page without form controls or
            # scrollbars. Native widget raster changes elsewhere on the main
            # fixture must not turn a hit-test rejection into a pixel rejection.
            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            coordinate_navigation_before = store.budgets.status(token.run_id)['actions_used']
            coordinate_navigation = await gateway.navigate(token, fixture.origin + '/fixture/coordinates',
                                                           'coordinate-fixture-navigation')
            assert coordinate_navigation['status'] == 'COMPLETED'
            await page.wait_for_load_state('load')
            child_frame = next(frame for frame in page.frames if frame != page.main_frame)
            child_id = gateway.browser._register_frame(child_frame)
            observations['coordinate_fixture'] = {'navigation_actions':
                store.budgets.status(token.run_id)['actions_used'] - coordinate_navigation_before}

            # The independent evaluator creates the parent overlay and waits
            # for its paint before observing. Only the native hit test can
            # establish that the fresh point belongs to the wrong frame.
            frame_box = await child_frame.locator('#frame-link').bounding_box()
            await page.evaluate("""async box => {const overlay=document.createElement('div');overlay.id='synthetic-overlay';
                Object.assign(overlay.style,{position:'fixed',left:box.x+'px',top:box.y+'px',width:box.width+'px',
                height:box.height+'px',background:'orange',zIndex:'9999'});document.body.append(overlay);
                await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));} """, frame_box)
            snapshot = await gateway.observe(token, frame_id=child_id, include_screenshot=True)
            coordinate = {'strategy': 'coordinate', 'screenshot_evidence_id': snapshot['screenshot_evidence_id'],
                          'snapshot_id': snapshot['snapshot_id'], 'tab_id': snapshot['tab_id'],
                          'frame_id': snapshot['frame_id'], 'width': snapshot['width'], 'height': snapshot['height'],
                          'x': int(frame_box['x'] + frame_box['width'] / 2),
                          'y': int(frame_box['y'] + frame_box['height'] / 2)}
            counter_before = store.budgets.status(token.run_id)['actions_used']
            observations['rejections']['iframe_coordinate_overlay'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'iframe-overlay', 'click', locator=coordinate)))
            check('fresh_iframe_coordinate_cannot_hit_parent_overlay',
                  observations['rejections']['iframe_coordinate_overlay']['field'] == 'locator'
                  and store.budgets.status(token.run_id)['actions_used'] == counter_before)
            await page.locator('#synthetic-overlay').evaluate('''async element => {element.remove();
                await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));}''')
            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            snapshot = await gateway.observe(token, frame_id=child_id, include_screenshot=True)
            coordinate.update(screenshot_evidence_id=snapshot['screenshot_evidence_id'], snapshot_id=snapshot['snapshot_id'])
            budget_before = store.budgets.status(token.run_id)
            coordinate_result = await gateway.dispatch(token, action(token, snapshot, 'fresh-iframe-coordinate', 'click', locator=coordinate))
            await child_frame.wait_for_url(fixture.origin + '/fixture/next')
            budget_after = store.budgets.status(token.run_id)
            check('fresh_iframe_coordinate_passes_native_hit_test', coordinate_result['status'] == 'COMPLETED')
            check('coordinate_rechecks_count_two_additional_screenshots',
                  budget_after['screenshots_used'] - budget_before['screenshots_used'] == 2
                  and budget_after['actions_used'] - budget_before['actions_used'] == 1)

            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            coordinate_return_before = store.budgets.status(token.run_id)['actions_used']
            coordinate_return = await gateway.navigate(token, fixture.url, 'coordinate-fixture-return')
            assert coordinate_return['status'] == 'COMPLETED'
            await page.wait_for_load_state('load')
            observations['coordinate_fixture']['return_navigation_actions'] = (
                store.budgets.status(token.run_id)['actions_used'] - coordinate_return_before)
            snapshot = await gateway.observe(token)
            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            pages_before = store.budgets.status(token.run_id)['content_pages_used']
            semantic = {'strategy': 'semantic', 'role': 'link', 'accessible_name': 'Read next page', 'label': None}
            navigated = await gateway.dispatch(token, action(token, snapshot, 'semantic-navigation', 'click', locator=semantic))
            check('semantic_locator_reads_current_link', navigated['status'] == 'COMPLETED' and page.url == fixture.origin + '/fixture/next')
            check('link_navigation_counts_content_page_and_pacing', store.budgets.status(token.run_id)['content_pages_used'] == pages_before + 1)
            observations['rejections']['navigation_pacing'] = await rejected(gateway.navigate(token, fixture.url, 'too-soon-navigation'))
            check('logical_site_spacing_cannot_be_bypassed_with_click', observations['rejections']['navigation_pacing']['code'] == 'SITE_THROTTLED')

            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            await gateway.navigate(token, fixture.url, 'return-navigation')
            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            # This fixture includes a child frame. The gateway navigation ends
            # at DOM readiness. Complete pacing and fixture readiness before
            # freezing the exact-locator observation, then dispatch immediately.
            await page.wait_for_load_state('load')
            await page.get_by_role('link', name='Exact DOM attribute link', exact=True).wait_for(state='visible')
            exact_frames = [frame for frame in page.frames if frame != page.main_frame
                            and frame.url == fixture.origin + '/fixture/frame']
            assert len(exact_frames) == 1, 'Exact DOM fixture requires one ready child frame'
            await exact_frames[0].wait_for_load_state('load')
            await exact_frames[0].locator('#frame-link').wait_for(state='visible')
            await page.evaluate('''() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))''')
            snapshot = await gateway.observe(token)
            exact = {'strategy': 'dom', 'attribute': 'id', 'value': 'literal"[]value'}
            exact_result = await gateway.dispatch(token, action(token, snapshot, 'exact-dom-value', 'click', locator=exact))
            check('dom_attribute_values_are_exact_escaped_data', exact_result['status'] == 'COMPLETED')
            token = store.heartbeat(token)
            store.finish(token, target='CANCELLED')
            await manager.close(session.session_id, owner)

            # These two independent Runs deliberately end in uncertainty. The
            # probe cancels them only after reacquiring a fenced recovery token;
            # no original executor or browser response can authorize a retry.
            for name, destination, limits in (
                ('gateway-redirect', fixture.origin + '/fixture/redirect', None),
                ('gateway-timeout', fixture.origin + '/fixture/hang', {'action_timeout_seconds': 1}),
            ):
                contract = seed_run(settings.business_db, store, fixture, name, limits=limits,
                                    extra_source=name == 'gateway-redirect')
                token = store.claim(manager.manager_id, generation)
                if name == 'gateway-redirect':
                    from webagent.scheduler.models import Resource
                    from webagent.tasks.models import SourceScope
                    unleased_source = SourceScope.model_validate(contract['sources'][-1])
                    check('redirect_destination_is_declared_but_logical_site_is_unleased',
                          unleased_source.permits(fixture.origin + '/outside')
                          and Resource.site_identity('other-logical-site', realm='webarena').resource_key
                          not in token.resources)
                owner = SessionOwner('run', token.run_id, 'local-fixture', realm='webarena')
                session = await manager.create(owner, execution_token=token)
                gateway = BrowserGateway.from_managed(manager, session, scheduler=store)
                await asyncio.sleep(3.05)
                token = store.heartbeat(token)
                old_token = token
                step = name + '-navigation'
                observations['rejections'][name] = await rejected(gateway.navigate(token, destination, step))
                with connect(settings.business_db) as db:
                    current_step = db.execute('SELECT status FROM steps WHERE step_id=?', (step,)).fetchone()
                    check(name + '_persists_uncertain_atomic_result', current_step['status'] == 'UNKNOWN')
                try:
                    store.validate(old_token)
                except Exception as error:
                    check(name + '_revokes_original_execution_epoch', getattr(error, 'status', None) == 409)
                else:
                    raise AssertionError('Original synthetic executor was still qualified')
                check(name + '_counts_attempt_without_late_result', store.budgets.status(name)['actions_used'] == 1
                      and gateway.local_result(step) is None)
                if name == 'gateway-redirect':
                    check('redirect_to_declared_unleased_site_is_blocked_before_receiver',
                          not any(row['path'] == '/outside' for row in fixture.requests))
                else:
                    check('real_browser_hang_uses_tightened_one_second_timeout', observations['rejections'][name]['code'] == 'TIMEOUT')
                    fixture.hang.set()
                    await asyncio.sleep(.05)
                    check('late_fixture_response_cannot_overwrite_timeout_record', gateway.local_result(step) is None)
                await manager.close(session.session_id, owner)
                recovered = store.claim(manager.manager_id, generation)
                assert recovered is not None and recovered.run_id == name
                store.finish(recovered, target='CANCELLED')

            # Production contains no default writer. This explicit fixture
            # adapter reads only fixed synthetic signals from the current page.
            identity_ref = synthetic_identity(settings.business_db, fixture)
            seed_run(settings.business_db, store, fixture, 'gateway-write', identity_ref=identity_ref)
            token = store.claim(manager.manager_id, generation)
            owner = SessionOwner('run', token.run_id, 'local-fixture', identity_ref, realm='webarena')
            session = await manager.create(owner, execution_token=token)
            context = await manager.context(session.session_id, owner, execution_token=token)
            page = context.pages[0]
            async def fixture_authorizer(value, snapshot, prepared):
                if page.url != fixture.url or value.target.locator.strategy != 'dom' or value.target.locator.value != 'save':
                    raise ValueError('Synthetic writer target changed')
                facts = {}
                for key in ('repository', 'branch', 'base-sha', 'account'):
                    facts[key] = await page.locator('meta[name="fixture-' + key + '"]').get_attribute('content')
                assert facts == {'repository': 'Fixture/Gateway', 'branch': 'gateway-fixture',
                                 'base-sha': 'a' * 40, 'account': 'fixture-user'}
                return WriteAuthorization(facts['repository'], facts['branch'], facts['base-sha'], 'edit_file',
                    ('src/fixture.txt',), identity_ref, (('POST', fixture.origin + '/fixture/save'),))
            gateway = BrowserGateway.from_managed(manager, session, scheduler=store, write_authorizer=fixture_authorizer)
            await asyncio.sleep(3.05)
            token = store.heartbeat(token)
            await gateway.navigate(token, fixture.url, 'write-bootstrap')
            snapshot = await gateway.observe(token)
            scope = {'repository': 'Fixture/Gateway', 'branch': 'gateway-fixture', 'base_sha': 'a' * 40,
                     'operation': 'edit_file', 'files': ['src/fixture.txt'], 'operation_id': 'fixture-write-one',
                     'identity_ref': identity_ref, 'target_rechecked_at': snapshot['captured_at']}
            denied_scope = dict(scope, files=['tests/acceptance.py'])
            observations['rejections']['protected_write_scope'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'protected-file', 'click', locator={'strategy': 'dom', 'attribute': 'id', 'value': 'save'},
                       write_scope=denied_scope)))
            check('protected_files_denied_before_fixture_write', fixture.write_count == 0)
            for label, changed_scope in (
                ('repository', dict(scope, repository='Fixture/Other')),
                ('identity', dict(scope, identity_ref='untrusted-identity')),
                ('base_sha', dict(scope, base_sha='b' * 40)),
            ):
                action_count = store.budgets.status(token.run_id)['actions_used']
                observations['rejections']['write_' + label] = await rejected(gateway.dispatch(token,
                    action(token, snapshot, 'wrong-write-' + label, 'click',
                        locator={'strategy': 'dom', 'attribute': 'id', 'value': 'save'}, write_scope=changed_scope)))
                check('trusted_fixture_facts_deny_wrong_' + label, fixture.write_count == 0
                      and store.budgets.status(token.run_id)['actions_used'] == action_count)
            result = await gateway.dispatch(token, action(token, snapshot, 'fixture-write', 'click',
                locator={'strategy': 'dom', 'attribute': 'id', 'value': 'save'}, write_scope=scope))
            check('authorized_fixture_write_is_dispatched_once', fixture.write_count == 1)
            check('browser_return_does_not_claim_confirmed_write', result['status'] == 'UNKNOWN')
            with connect(settings.business_db) as db:
                intent = db.execute("SELECT * FROM write_intents WHERE operation_id='fixture-write-one'").fetchone()
                quarantines = [dict(row) for row in db.execute('SELECT * FROM resource_quarantines')]
                check('unconfirmed_write_has_durable_unknown_and_quarantine', intent['status'] == 'UNKNOWN' and quarantines)
                observations['write'] = {'physical_writes': fixture.write_count, 'status': intent['status'],
                                          'quarantined_resources': len(quarantines)}
                observations['steps'] = [dict(row) for row in db.execute('SELECT step_id,status,sequence,error_code FROM steps')]
            observations['rejections']['unconfirmed_retry'] = await rejected(gateway.dispatch(token,
                action(token, snapshot, 'retry-unconfirmed-write', 'click', locator={'strategy': 'dom', 'attribute': 'id', 'value': 'save'},
                       write_scope=scope)))
            check('unconfirmed_fixture_write_cannot_automatically_repeat', fixture.write_count == 1)
            observations['request_counts'] = {'total': len(fixture.requests),
                'outside_contract_requests': sum(row['path'] == '/outside' for row in fixture.requests),
                'synthetic_post_writes': fixture.write_count}
            check('no_request_leaves_registered_contract_fixture', observations['request_counts']['outside_contract_requests'] == 0)
            await manager.aclose()
            manager = None
            await form_driver_probes(settings.data_dir, fixture, observations, check)
            await denied_resource_probes(settings.data_dir, fixture, observations, check)
        finally:
            original_error = sys.exc_info()[1]
            (output / 'observations.json').write_text(json.dumps(observations, indent=2) + '\n')
            await fixture.close()
            if manager is not None:
                try:
                    await manager.aclose()
                except Exception as cleanup_error:
                    report['cleanup_error'] = {'type': type(cleanup_error).__name__,
                        'code': getattr(cleanup_error, 'code', None), 'field': getattr(cleanup_error, 'field', None)}
                    if original_error is None:
                        raise
    (output / 'observations.json').write_text(json.dumps(observations, indent=2) + '\n')
    report['scope_counts'] = {'owned_managed_browsers': 5, 'synthetic_http_origins': 1, 'physical_fixture_writes': fixture.write_count}
    report['passed'] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-13', 'passed': False, 'checks': {},
              'created_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'New temporary SQLite and owned managed Chromium; browser actions and writes '
                       'only against one explicitly registered synthetic loopback WebArena origin.'}
    try:
        asyncio.run(verify(args.output_dir, report))
    except Exception as error:
        import traceback
        report['error_type'] = type(error).__name__
        if hasattr(error, 'code'):
            report['error'] = {'code': error.code, 'field': error.field, 'status': error.status}
        report['error_locations'] = [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                     'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]
    report['artifact_sha256'] = {str(path.relative_to(args.output_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(args.output_dir.rglob('*')) if path.is_file()}
    path = args.output_dir / 'report.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(path)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
