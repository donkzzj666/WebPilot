#!/usr/bin/env python3
"""M1-20 UI → natural compilation/settings/login → SQLite acceptance.

Uses a real Vite proxy, FastAPI, Chromium, natural compiler and login RPC/browser
service. Provider HTTP and the identity website are owned synthetic fixtures;
credentials/vault keys are in memory. No QueueWorker, live website, paid provider
or physical repository write is started. Request bodies, HAR, traces, login DOM
and screenshots are never exported.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from verify_startup import Process, TRACE_FLAGS, free_ports, now
from verify_settings import SyntheticStore
from verify_natural_tasks import FixtureProvider, LoopbackTransport
from verify_browser_sessions import SyntheticKeyStore, network_config
from verify_login_sessions import LoginFixture, PASSWORD, OTP, COOKIE
from webagent.api import create_app
from webagent.config import Settings
from webagent.db import LATEST_VERSION, connect
from webagent.identities.rpc import LoginServer
from webagent.identities.service import LoginService
from webagent.identities.sites import FixtureSiteAdapter, SiteCatalog
from webagent.security import LocalApiPolicy, load_or_create_token
from webagent.sessions.auth import AuthStateStore
from webagent.sessions.manager import ManagedBrowser
from webagent.settings import service as settings_service
from webagent.tasks import natural_service

import httpx
from playwright.async_api import expect, async_playwright
import uvicorn

PARAMETERS = {'entity_id': 'ACME', 'report_version': '2025', 'period_type': 'annual',
              'metrics': ['revenue'], 'currency': 'USD'}
CLEAR_INSTRUCTION = '读取 ACME 的 2025 年度财报营收，以 USD 美元计量。'
PARTIAL_INSTRUCTION = '读取某家企业最近一期年度财报营收，以 USD 美元计量。'


def request_summary(method: str, path: str, status: int) -> dict:
    """Export route/status only, excluding query/header/body/model payloads."""
    if type(method) is not str or method not in {'GET', 'POST', 'PUT', 'OPTIONS'}:
        return {'kind': 'other_request', 'status': status if type(status) is int else 0}
    parsed = urlsplit(path)
    if not parsed.path.startswith(('/v1/', '/health')):
        return {'kind': 'other_request', 'method': method, 'status': status if type(status) is int else 0}
    return {'method': method, 'path': parsed.path, 'status': status if type(status) is int else 0}


def public_artifacts(output: Path) -> list[Path]:
    return [path for path in sorted(output.rglob('*')) if path.is_file()
            and '.security' not in path.parts and '.private' not in path.parts and path.name != 'report.json']


def scan_secrets(output: Path, canaries: dict[str, str]) -> dict:
    """Return names/counts only; never expose the matched credential bytes."""
    matched = []
    files = public_artifacts(output)
    for path in files:
        raw = path.read_bytes()
        names = sorted(name for name, value in canaries.items() if value.encode() in raw)
        if names:
            matched.append({'artifact': str(path.relative_to(output)), 'canary_names': names})
    return {'passed': not matched, 'scanned_files': len(files), 'canary_count': len(canaries), 'matches': matched}


def safe_error(error: BaseException) -> dict:
    return {'type': type(error).__name__, 'message': 'Verification failed; inspect completed checks and source location.',
            'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                       for frame in traceback.extract_tb(error.__traceback__)]}


class SyntheticModel:
    """Production transport talks actual HTTP exclusively to this listener."""
    def __init__(self, key: str, reasoning: str):
        self.key, self.reasoning = key, reasoning
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self.active: set = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            headers_raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            lines = headers_raw.decode('ascii').split('\r\n')
            headers = {k.lower(): v for k, v in (line.split(': ', 1) for line in lines[1:] if ': ' in line)}
            length = int(headers['content-length'])
            assert 0 < length <= 500_000
            body = json.loads(await asyncio.wait_for(reader.readexactly(length), 5))
            payload = json.loads(body['messages'][1]['content'])
            summary = {'number': len(self.requests) + 1,
                'json_mode': body.get('response_format') == {'type': 'json_object'},
                'tools_absent': 'tools' not in body, 'stream_disabled': body.get('stream') is False,
                'compiler_prompt': 'm1-06-compiler-v1' in body['messages'][0]['content'],
                'synthetic_authorization_matched': headers.get('authorization') == 'Bearer ' + self.key,
                'payload_fields': sorted(payload)}
            assert all(summary[k] for k in ('json_mode', 'tools_absent', 'stream_disabled', 'compiler_prompt', 'synthetic_authorization_matched'))
            assert set(payload) == {'instruction', 'explicit_parameters', 'explicit_scenario', 'web_context'}
            self.requests.append(summary)
            proposal = {'scenario': 'finance', 'parameters': PARAMETERS, 'ambiguous_fields': []}
            response = {'id': 'chatcmpl-synthetic-task-entry', 'model': body['model'], 'choices': [{
                'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': json.dumps(proposal), 'reasoning_content': self.reasoning}}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}}
            raw = json.dumps(response).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '
                         + str(len(raw)).encode() + b'\r\n\r\n' + raw)
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.active.discard(task)

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*self.active, return_exceptions=True)


async def wait_until(check, label: str, *, frontend=None, timeout=25):
    async with asyncio.timeout(timeout):
        while True:
            if frontend is not None:
                frontend.assert_alive()
            if await check():
                return
            await asyncio.sleep(.05)


async def verify(output: Path, report: dict, *, headed: bool) -> None:
    data = output / 'data'
    data.mkdir()
    settings = Settings(data)
    store = SyntheticStore()
    model_key = 'SYNTHETIC_M120_MODEL_' + uuid4().hex
    reasoning = 'SYNTHETIC_M120_REASONING_' + uuid4().hex
    token = load_or_create_token(data)
    canaries = {'model_key': model_key, 'private_reasoning': reasoning, 'local_api_token': token,
                'login_password': PASSWORD, 'login_otp': OTP, 'login_cookie': COOKIE}
    model = SyntheticModel(model_key, reasoning)
    fixture = LoginFixture()
    api_port, ui_port = free_ports()
    api_url, ui_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
    policy = LocalApiPolicy(token, frozenset({f'127.0.0.1:{api_port}'}), frozenset({api_url, ui_url}))
    app = create_app(settings, secret_store=store, local_api_policy=policy)
    api_requests, browser_requests, page_errors, request_keys = [], [], [], []

    @app.middleware('http')
    async def summarize_request(request, call_next):
        response = await call_next(request)
        api_requests.append(request_summary(request.method, request.url.path, response.status_code))
        return response

    original_factory = natural_service.provider_for_compilation

    def factory(path, secret_store):
        with connect(path) as db:
            row = settings_service._latest(db)
        configured, runtime = settings_service._snapshot(row)
        secret = settings_service._ready_secret(row, secret_store)
        resolved = settings_service.ResolvedRunConfig(row['version'], configured, runtime, secret)
        client = httpx.AsyncClient(transport=LoopbackTransport(model.port), trust_env=False, follow_redirects=False)
        return resolved, FixtureProvider(configured, secret, client)

    natural_service.provider_for_compilation = factory
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=api_port,
                            log_config=None, log_level='critical', access_log=False))
    serving = None
    manager = rpc = frontend = None
    browser = context = playwright = None
    env = {**os.environ, 'WEBAGENT_DATA_DIR': str(data), 'WEBAGENT_API_PORT': str(api_port),
           'WEBAGENT_UI_PORT': str(ui_port), 'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1'}
    for name in ('NODE_OPTIONS', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    for flag in TRACE_FLAGS:
        env[flag] = 'false'
    report.update(checks=[], processes=[], configuration={
        'api_url': api_url, 'ui_url': ui_url, 'data_dir': str(data), 'credential_store': 'isolated-synthetic-memory',
        'model_provider_http': 'owned-loopback-fixture', 'login_site': None,
        'queue_worker_started': False, 'real_provider_requests': False, 'physical_write_dispatch': False,
        'synthetic_login_realm': 'webarena',
        'headed': headed})

    def record(name: str, **details):
        report['checks'].append({'name': name, 'passed': True, 'at': now(), **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    async def stop_api():
        if serving is None:
            return
        if not serving.done():
            server.should_exit = True
            await asyncio.wait_for(serving, 12)
        await serving

    try:
        await model.start()
        await fixture.start()
        report['configuration']['login_site'] = fixture.origin
        serving = asyncio.create_task(server.serve())
        async def api_started():
            if serving.done():
                await serving
                raise RuntimeError('owned_api_stopped_early')
            return server.started
        await wait_until(api_started, 'isolated task-entry API')
        vault = AuthStateStore(data / 'auth', key_store=SyntheticKeyStore())
        manager = ManagedBrowser(settings, auth_store=vault, network_config=network_config(fixture.port), headless=not headed)
        catalog = SiteCatalog(adapters=(FixtureSiteAdapter(site_id='fixture-github', origin=fixture.origin),))
        login_service = LoginService(settings, manager, catalog=catalog)
        await login_service.start()
        rpc = await LoginServer(settings, login_service).start()
        frontend = Process('task-entry-frontend', 'frontend', output, env)
        async with httpx.AsyncClient(base_url=api_url, headers={'Authorization': 'Bearer ' + token},
                                    trust_env=False, timeout=15) as client:
            async def ui_ready():
                try:
                    return (await client.get(ui_url)).status_code == 200
                except httpx.HTTPError:
                    return False
            await wait_until(ui_ready, 'task-entry frontend', frontend=frontend)
            initial = await client.get('/v1/settings')
            assert initial.status_code == 200 and initial.json()['version'] == 0
            assert initial.json()['readiness']['credential_status'] == 'not_configured'
            record('isolated_settings_initially_missing')
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=not headed, args=[
                '--disable-background-networking', '--disable-component-update',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
            context = await browser.new_context(viewport={'width': 1280, 'height': 1000}, service_workers='block')

            async def restrict_network(route):
                parsed = urlsplit(route.request.url)
                allowed = parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port in (api_port, ui_port)
                if not allowed:
                    browser_requests.append({'method': route.request.method, 'allowed': False})
                    await route.abort()
                else:
                    await route.continue_()

            def summarize_browser_request(request):
                parsed = urlsplit(request.url)
                if parsed.path.startswith('/api/v1/') and request.method in ('POST', 'PUT'):
                    body = request.post_data_json
                    item = {'method': request.method, 'path': parsed.path, 'allowed': True}
                    if isinstance(body, dict):
                        item.update(compiler_mode=body.get('compiler_mode'), api_key_supplied='api_key' in body,
                                    contract_version=body.get('contract_version'), expected_version=body.get('expected_version'))
                    browser_requests.append(item)
                    request_keys.append((parsed.path, request.headers.get('idempotency-key')))

            await context.route('**/*', restrict_network)
            page = await context.new_page()
            page.on('request', summarize_browser_request)
            page.on('pageerror', lambda _: page_errors.append({'kind': 'pageerror'}))
            await page.goto(ui_url, wait_until='networkidle')
            await expect(page.get_by_text('本机模型配置尚未就绪', exact=True)).to_be_visible()
            await expect(page.get_by_role('button', name='提交任务', exact=True)).to_be_disabled()
            record('ui_distinguishes_missing_configuration_from_task_readiness')
            key = page.locator('#model-api-key')
            consent = page.locator('#model-data-consent')
            disclosure = page.locator('#model-data-disclosure')
            for phrase in ('任务正文', '过滤后的网页内容', '选中的截图', 'DeepSeek'):
                assert phrase in await disclosure.inner_text()
            assert (await disclosure.bounding_box())['y'] < (await key.bounding_box())['y']
            await expect(page.get_by_role('button', name='保存模型设置', exact=True)).to_be_disabled()
            await key.fill(model_key)
            await consent.check()
            await page.locator('#model-pricing-enabled').check()
            await page.locator('#model-price-input').fill('2')
            await page.locator('#model-price-output').fill('3')
            async with page.expect_response(lambda response: response.url.endswith('/api/v1/settings/model')) as save_event:
                await page.get_by_role('button', name='保存模型设置', exact=True).click()
            assert (await save_event.value).status == 200
            await expect(key).to_have_value('')
            configured = (await client.get('/v1/settings')).json()
            assert configured['version'] == 1 and configured['readiness']['ready'] is True
            assert configured['model']['pricing']['input_per_million'] == '2'
            assert configured['model']['pricing']['output_per_million'] == '3'
            assert configured['model']['price_version'].startswith('price-')
            assert store.contains_only(model_key) and store.put_count == 1 and len(model.requests) == 0
            record('provider_disclosure_consent_and_versioned_pricing_saved_without_provider_call')

            # Remaining production UI assertions are kept together below. Every
            # request uses real HTTP; no intercepted status or fake response is used.
            await page.get_by_role('button', name='刷新模型就绪状态', exact=True).click()
            await exercise_tasks(page, client, model, report, record, output)
            await exercise_settings(page, client, store, model_key, record)
            await exercise_identities(page, client, manager, login_service, fixture, record)

            # Sources/tokens from a website cannot call the local control API.
            denied = await client.get('/v1/tasks', headers={'Origin': 'https://untrusted.example'})
            assert denied.status_code == 403
            unauth = await client.get('/v1/tasks', headers={'Authorization': ''})
            assert unauth.status_code == 401
            record('local_api_origin_and_authentication_boundaries_retained')
            assert not page_errors and not [item for item in browser_requests if not item['allowed']]
            task_keys = [(path, key) for path, key in request_keys if path.startswith('/api/v1/tasks')]
            assert len(task_keys) == 7 and all(isinstance(key, str) and 0 < len(key) <= 200
                and all(33 <= ord(char) <= 126 for char in key) for _, key in task_keys)
            assert len({key for _, key in task_keys}) == 7
            record('ui_mutations_use_explicit_distinct_idempotency_keys_without_hidden_retries', task_mutations=7)
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            for value in canaries.values():
                assert value not in await page.locator('body').inner_text()
                assert value not in page.url
            record('browser_storage_url_dom_and_errors_do_not_expose_credentials')
            await page.set_viewport_size({'width': 375, 'height': 900})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await expect(key).to_have_value('')
            await page.screenshot(path=str(output / '03-mobile.png'), full_page=True)
            record('mobile_layout_has_no_horizontal_overflow', viewport_width=375, screenshot='03-mobile.png')

        with connect(settings.business_db) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == LATEST_VERSION
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM write_intents').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM task_compilations WHERE status="STARTED"').fetchone()[0] == 0
            compilations = db.execute('SELECT COUNT(*) FROM task_compilations').fetchone()[0]
            assert compilations == len(model.requests)
            dump = '\n'.join(db.iterdump())
            assert all(value not in dump for value in canaries.values())
        assert not (data / 'graph.sqlite3').exists()
        assert not model.errors and not model.active
        record('sqlite_contracts_and_login_metadata_are_durable_without_execution_or_writes',
               schema_version=LATEST_VERSION, provider_calls=compilations, runs=0, physical_writes=0)
        report['passed'] = True
    finally:
        cleanup_errors = []
        for name, obj in (('ui-context', context), ('ui-browser', browser), ('ui-playwright', playwright),
                          ('login-rpc', rpc), ('managed-login-browser', manager)):
            if obj is None:
                continue
            try:
                if name == 'ui-playwright':
                    await asyncio.wait_for(obj.stop(), 10)
                elif name in ('login-rpc', 'managed-login-browser'):
                    await asyncio.wait_for(obj.aclose(), 15)
                else:
                    await asyncio.wait_for(obj.close(), 10)
                report['processes'].append({'name': name, 'stopped': True})
            except Exception:
                cleanup_errors.append(name + '_did_not_stop')
        try:
            await stop_api()
            report['processes'].append({'name': 'task-entry-api', 'stopped': True})
        except Exception:
            cleanup_errors.append('owned_api_did_not_stop')
        if frontend is not None:
            try:
                result = await asyncio.to_thread(frontend.stop)
                report['processes'].append(result)
                if result['forced_kill']:
                    cleanup_errors.append('frontend_required_forced_kill')
            except Exception:
                cleanup_errors.append('owned_frontend_did_not_stop')
        natural_service.provider_for_compilation = original_factory
        for name, obj in (('synthetic-model-http', model), ('synthetic-login-http', fixture)):
            if not hasattr(obj, 'server'):
                continue
            try:
                await obj.close()
                report['processes'].append({'name': name, 'stopped': True})
            except Exception:
                cleanup_errors.append(name + '_did_not_stop')
        store.clear()
        report['cleanup_errors'] = cleanup_errors
        report['http_requests'] = api_requests
        report['browser_requests'] = browser_requests
        report['browser_page_errors'] = page_errors
        report['provider_request_summary'] = model.requests
        report['secret_scan'] = scan_secrets(output, canaries)
        if cleanup_errors or not report['secret_scan']['passed']:
            report['passed'] = False


async def exercise_tasks(page, client, model, report, record, output):
    async def fill_source(*, start='/reports/annual'):
        await page.locator('#source-id').fill('m120-finance')
        await page.locator('#site-id').fill('m120-finance-site')
        await page.locator('#source-origin').fill('https://finance.example.test')
        await page.locator('#source-path').fill('/reports')
        await page.locator('#start-url').fill('https://finance.example.test' + start)
        await page.locator('#source-authorization').check()

    async def submit_create():
        async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/tasks'
                                        and response.request.method == 'POST') as event:
            await page.get_by_role('button', name='提交任务', exact=True).click()
        return await event.value

    await page.locator('#task-instruction').fill(CLEAR_INSTRUCTION)
    await fill_source()
    response = await submit_create()
    assert response.status == 201
    clear = await response.json()
    clear_id = clear['task']['task_id']
    assert clear['task']['preparation_status'] == 'READY'
    assert clear['contract']['parameters'] == {'scenario': 'finance', **PARAMETERS}
    assert clear['contract']['action_policy'] == {'mode': 'read_only'}
    assert clear['contract']['identity_ref'] is None
    assert clear['current_run'] is None
    assert clear['compiler']['prompt_version'] == 'm1-06-compiler-v1'
    assert clear['contract']['sources'] == [{'source_id': 'm120-finance', 'site_id': 'm120-finance-site',
        'origin': 'https://finance.example.test', 'path_prefix': '/reports'}]
    await expect(page.get_by_test_id('task-detail')).to_contain_text('ACME')
    await expect(page.get_by_test_id('task-detail').get_by_test_id('task-policy')).to_contain_text('只读')
    await expect(page.get_by_test_id('task-status')).to_contain_text('契约已就绪')
    await expect(page.get_by_test_id('task-detail')).to_contain_text('2025')
    await page.screenshot(path=str(output / '01-clear-task.png'), full_page=True)
    record('natural_ui_submission_shows_actual_contract_sources_parameters_and_read_only_actions',
           task_id=clear_id, contract_version=1, compiler='m1-06-compiler-v1', runs=0, screenshot='01-clear-task.png')

    await page.reload(wait_until='networkidle')
    await expect(page.get_by_test_id('task-list')).to_contain_text('ACME')
    await expect(page.get_by_test_id('task-detail')).to_contain_text(clear_id)
    durable = (await client.get('/v1/tasks/' + clear_id)).json()
    assert durable['contract'] == clear['contract'] and durable['current_run'] is None
    record('page_reload_reads_persisted_task_list_and_contract')

    # Frontend validates URL syntax; only the backend owns source/path semantics.
    # The invalid URL is well formed but lies outside the authorized /reports.
    await page.get_by_role('button', name='新建任务', exact=True).click()
    await page.locator('#task-instruction').fill(CLEAR_INSTRUCTION)
    await fill_source(start='/private')
    before = len(model.requests)
    invalid = await submit_create()
    assert invalid.status == 422 and (await invalid.json())['code'] == 'INVALID_PARAMETER'
    await expect(page.locator('#task-instruction')).to_have_value(CLEAR_INSTRUCTION)
    await expect(page.locator('#start-url')).to_have_value('https://finance.example.test/private')
    assert len(model.requests) == before
    await expect(page.get_by_role('alert').filter(has_text='输入未通过校验')).to_be_visible()
    record('real_422_preserves_task_draft_and_does_not_call_provider')
    await page.locator('#start-url').fill('https://finance.example.test/reports/annual')
    await page.locator('#source-authorization').check()
    corrected = await submit_create()
    assert corrected.status == 201
    assert (await corrected.json())['contract']['action_policy'] == {'mode': 'read_only'}
    record('corrected_draft_creates_new_valid_request_without_permission_expansion')

    await page.get_by_role('button', name='新建任务', exact=True).click()
    await page.locator('#task-instruction').fill(PARTIAL_INSTRUCTION)
    await fill_source()
    incomplete = await submit_create()
    assert incomplete.status == 201
    partial = await incomplete.json()
    partial_id = partial['task']['task_id']
    assert partial['task']['preparation_status'] == 'NEEDS_INPUT'
    assert set(partial['missing_fields']) == {'parameters.entity_id', 'parameters.report_version'}
    assert partial['draft']['action_policy'] == {'mode': 'read_only'}
    await expect(page.get_by_test_id('task-clarifications')).to_be_visible()
    await page.locator('#clarify-parameters-entity_id').fill('ACME')
    await page.locator('#clarify-parameters-report_version').fill('2025')
    await page.locator('#clarify-confirmation').check()

    # Another local page commits one currently requested field, leaving version2.
    # The held UI still submits version1. This is a real DB/CAS conflict, not a
    # mocked response and no provider retry is permitted for clarification.
    before = len(model.requests)
    external = await client.post('/v1/tasks/' + partial_id + '/clarifications',
        headers={'Idempotency-Key': 'm120-external-clarification-' + uuid4().hex},
        json={'contract_version': 1, 'values': {'parameters.entity_id': 'ACME'}})
    assert external.status_code == 200 and external.json()['contract_version'] == 2
    async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/tasks/' + partial_id + '/clarifications') as conflict_event:
        await page.get_by_role('button', name='确认补充信息', exact=True).click()
    conflict = await conflict_event.value
    assert conflict.status == 409 and (await conflict.json())['code'] == 'CONTRACT_VERSION_CONFLICT'
    await expect(page.locator('#clarify-parameters-report_version')).to_have_value('2025')
    await expect(page.get_by_role('button', name='确认补充信息', exact=True)).to_be_disabled()
    await expect(page.get_by_role('button', name='我已核对最新任务', exact=True)).to_be_visible()
    assert len(model.requests) == before
    current = (await client.get('/v1/tasks/' + partial_id)).json()
    assert current['contract_version'] == 2 and current['draft']['action_policy'] == {'mode': 'read_only'}
    record('real_409_refreshes_persisted_version_keeps_input_and_requires_explicit_reconfirmation',
           task_id=partial_id, latest_revision=2)

    await page.get_by_role('button', name='我已核对最新任务', exact=True).click()
    await page.locator('#clarify-confirmation').check()
    async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/tasks/' + partial_id + '/clarifications') as completed_event:
        await page.get_by_role('button', name='确认补充信息', exact=True).click()
    completed_response = await completed_event.value
    assert completed_response.status == 200
    completed = await completed_response.json()
    assert completed['task']['preparation_status'] == 'READY'
    assert completed['contract']['contract_version'] == 3
    assert completed['contract']['action_policy'] == {'mode': 'read_only'}
    assert completed['contract']['sources'] == clear['contract']['sources']
    assert completed['contract']['identity_ref'] is None
    assert len(model.requests) == before
    await expect(page.get_by_test_id('task-detail').get_by_test_id('task-policy')).to_contain_text('只读')
    await expect(page.get_by_test_id('task-status')).to_contain_text('契约已就绪')
    await page.screenshot(path=str(output / '02-clarified-task.png'), full_page=True)
    record('central_clarification_only_fills_requested_fields_and_preserves_authority',
           task_id=partial_id, contract_version=3, extra_provider_calls=0, screenshot='02-clarified-task.png')

    # Even an authenticated API caller cannot use clarification as a recursive
    # permission patch. The UI offers only declared missing fields.
    rejected = await client.post('/v1/tasks/' + partial_id + '/clarifications',
        headers={'Idempotency-Key': 'm120-rejected-permission-' + uuid4().hex},
        json={'contract_version': 3, 'values': {'action_policy': {'mode': 'repository_write'}}})
    assert rejected.status_code == 422
    assert (await client.get('/v1/tasks/' + partial_id)).json()['contract'] == completed['contract']
    record('clarification_cannot_patch_permissions_or_completed_fields')

    await page.reload(wait_until='networkidle')
    await expect(page.get_by_test_id('task-detail')).to_contain_text(partial_id)
    await expect(page.get_by_test_id('task-status')).to_contain_text('契约已就绪')
    await expect(page.get_by_test_id('task-detail').get_by_test_id('task-policy')).to_contain_text('只读')
    report['task_ids'] = {'clear': clear_id, 'clarified': partial_id}
    record('clarified_task_remains_persistent_after_page_reload')

    await page.get_by_role('button', name='修改任务', exact=True).click()
    await expect(page.locator('#task-permission')).to_have_value('read_only')
    await expect(page.locator('#source-authorization')).not_to_be_checked()
    await page.locator('#task-permission').select_option('repository_write')
    await expect(page.locator('#write-authorization')).not_to_be_checked()
    assert (await client.get('/v1/tasks/' + partial_id)).json()['contract']['action_policy'] == {'mode': 'read_only'}
    record('revision_permission_changes_require_a_separate_explicit_write_authorization')
    await page.locator('#task-permission').select_option('read_only')
    await page.locator('#task-instruction').fill(CLEAR_INSTRUCTION)
    await fill_source()
    await page.locator('#task-revision-confirmation').check()
    async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/tasks/' + partial_id + '/revisions'
                                    and response.request.method == 'POST') as revision_event:
        await page.get_by_role('button', name='确认提交任务修订', exact=True).click()
    revised_response = await revision_event.value
    assert revised_response.status == 200
    revised = await revised_response.json()
    assert revised['contract']['contract_version'] == 4
    assert revised['contract']['action_policy'] == {'mode': 'read_only'}
    assert revised['contract']['identity_ref'] is None
    assert {contract['contract_version'] for contract in revised['contract_history']} == {3, 4}
    await page.reload(wait_until='networkidle')
    await expect(page.get_by_test_id('task-detail')).to_contain_text(partial_id)
    await expect(page.get_by_test_id('task-detail').get_by_test_id('task-policy')).to_contain_text('只读')
    assert (await client.get('/v1/tasks/' + partial_id)).json()['contract'] == revised['contract']
    record('explicit_revision_is_persistent_and_preserves_contract_history', task_id=partial_id, contract_version=4)
    await page.get_by_role('button', name='新建任务', exact=True).click()
    await expect(page.locator('#task-permission')).to_have_value('read_only')
    await expect(page.locator('#source-authorization')).not_to_be_checked()
    record('new_task_defaults_to_read_only_and_requires_new_source_authorization')


async def exercise_settings(page, client, store, model_key, record):
    max_tokens = page.locator('#model-max-tokens')
    key = page.locator('#model-api-key')
    consent = page.locator('#model-data-consent')
    await max_tokens.fill('2048')
    await consent.check()
    current = (await client.get('/v1/settings')).json()
    external = await client.put('/v1/settings/model', json={
        'expected_version': current['version'], 'model': {**current['model'], 'max_tokens': 3072},
        'accept_data_sharing': True})
    assert external.status_code == 200
    newest = external.json()['version']
    async with page.expect_response(lambda response: response.url.endswith('/api/v1/settings/model')) as conflict_event:
        await page.get_by_role('button', name='保存模型设置', exact=True).click()
    assert (await conflict_event.value).status == 409
    await expect(max_tokens).to_have_value('2048')
    await expect(key).to_have_value('')
    await expect(page.get_by_role('button', name='保存模型设置', exact=True)).to_be_disabled()
    assert (await client.get('/v1/settings')).json()['model']['max_tokens'] == 3072
    record('settings_409_preserves_nonsecret_draft_without_overwriting_latest_version', latest_version=newest)
    await page.get_by_role('button', name='重新载入最新配置', exact=True).click()
    await expect(max_tokens).to_have_value('3072')
    await expect(consent).not_to_be_checked()
    await expect(page.locator('#model-price-input')).to_have_value('2')
    await expect(page.locator('#model-price-output')).to_have_value('3')
    await expect(key).to_have_value('')
    assert store.put_count == 1 and store.contains_only(model_key)
    record('explicit_settings_refresh_reads_persistent_pricing_and_preserves_existing_credential')
    store.fault('locked')
    await page.get_by_role('button', name='重新载入配置', exact=True).click()
    await expect(page.get_by_text('系统凭据库已锁定，请解锁后重试。', exact=True)).to_be_visible()
    await expect(page.get_by_text('本机模型配置尚未就绪', exact=True)).to_be_visible()
    locked = (await client.get('/v1/settings')).json()
    assert locked['readiness']['ready'] is False and locked['readiness']['credential_status'] == 'locked'
    record('credential_lock_is_a_configuration_blocker_without_claiming_task_failure')
    store.fault(None)
    await page.get_by_role('button', name='重新载入配置', exact=True).click()
    await expect(page.get_by_text('本机模型配置就绪', exact=True)).to_be_visible()
    await expect(key).to_have_value('')


async def exercise_identities(page, client, manager, service, fixture, record):
    await expect(page.get_by_test_id('identity-readiness')).to_be_visible()
    await page.locator('#identity-site').select_option('fixture-github')
    await page.locator('#identity-expected-account').fill('alice')
    async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/identities/login-sessions'
                                    and response.request.method == 'POST') as created_event:
        await page.locator('#identity-create').click()
    response = await created_event.value
    assert response.status == 201
    login = await response.json()
    login_id = login['login_session_id']
    assert login['state'] == 'AWAITING_USER' and login['identity_ref'] is None and login['capture_blocked'] is True
    assert login['expected_account'] == 'alice'
    record('ui_creates_real_managed_login_preparation_without_password_or_otp_inputs', login_session_id=login_id)

    async def sign_in(account):
        current = service.store.get(login_id)
        managed_context = await manager.login_context(login['session_id'], service._owner(current))
        managed_page = managed_context.pages[0]
        await managed_page.goto(fixture.origin + '/login')
        await managed_page.locator('[name=account]').fill(account)
        await managed_page.locator('[name=password]').fill(PASSWORD)
        await managed_page.locator('[name=otp]').fill(OTP)
        async with managed_page.expect_navigation():
            await managed_page.get_by_role('button', name='Sign in', exact=True).click()

    async def confirm_ui():
        async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/identities/login-sessions/' + login_id + '/confirm'
                                        and response.request.method == 'POST') as event:
            await page.locator('#identity-confirm').click()
        response = await event.value
        assert response.status == 200
        return await response.json()

    await sign_in('bob')
    wrong = await confirm_ui()
    assert wrong['state'] == 'NEEDS_LOGIN' and wrong['reason'] == 'account_mismatch' and wrong['identity_ref'] is None
    await expect(page.get_by_test_id('identity-login-reason')).to_contain_text('账号')
    assert (await client.get('/v1/identities')).json() == []
    record('real_wrong_account_confirmation_remains_needs_login_and_publishes_no_identity')
    await page.reload(wait_until='networkidle')
    await expect(page.locator('#identity-login-session-id')).to_have_value(login_id)
    await expect(page.get_by_test_id('identity-login-reason')).to_contain_text('账号')
    await expect(page.get_by_test_id('identity-login-state')).to_have_text('需要重新登录并核对')
    assert login_id in page.url
    record('login_failure_reload_fetches_persistent_session_without_claiming_account_ready')
    await sign_in('alice')
    verified = await confirm_ui()
    identity_ref = verified['identity_ref']
    assert verified['state'] == 'VERIFIED' and identity_ref and verified['capture_blocked'] is True
    await expect(page.get_by_test_id('identity-record-' + identity_ref)).to_contain_text('alice')
    identity = (await client.get('/v1/identities')).json()[0]
    assert identity['state'] == 'VERIFIED' and identity['requires_identity_check'] is True
    assert identity['requires_business_check'] is True
    assert 'auth_ref' not in identity and 'auth_sha256' not in identity
    record('real_verified_account_metadata_preserves_recheck_requirements', identity_ref=identity_ref)
    async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/identities/login-sessions/' + login_id + '/close') as close_event:
        await page.locator('#identity-close').click()
    closed_response = await close_event.value
    assert closed_response.status == 200 and (await closed_response.json())['state'] == 'CLOSED'
    record('ui_explicitly_closes_managed_login_window')
    await page.reload(wait_until='networkidle')
    await expect(page.get_by_test_id('identity-record-' + identity_ref)).to_contain_text('alice')
    assert (await client.get('/v1/identities')).json()[0]['identity_ref'] == identity_ref
    record('verified_account_metadata_remains_after_reload_but_is_not_task_success')
    await page.get_by_role('button', name='新建任务', exact=True).click()
    await page.locator('#source-id').fill('m120-identity-boundary')
    await page.locator('#site-id').fill('fixture-github')
    await page.locator('#source-origin').fill(fixture.origin)
    await page.locator('#source-path').fill('/')
    await page.locator('#start-url').fill(fixture.origin + '/identity')
    option = page.locator('#task-identity option[value="' + identity_ref + '"]')
    await expect(option).to_be_attached()
    # Playwright's enabled assertion follows the enclosing LABEL to its SELECT.
    # Verify this native OPTION's own disabled property and attribute instead.
    await expect(option).to_have_attribute('disabled', '')
    assert await option.evaluate('(element) => element.disabled === true')
    await expect(page.locator('#task-identity')).to_have_value('')
    record('synthetic_evaluation_identity_cannot_be_selected_for_a_public_task', realm='webarena', identity_ref=identity_ref)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='New evidence directory; existing directories are refused')
    parser.add_argument('--headed', action='store_true', help='Show only the owned Chromium windows')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('task-entry-%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT / 'artifacts' / 'verification' / 'M1-20' / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-20', 'probe': 'task-entry-ui-http', 'started_at': now(), 'passed': False,
        'scope': 'Real Chromium/Vite/FastAPI/natural compiler/SQLite and real login RPC/ManagedBrowser with owned synthetic HTTP model and login website, in-memory credential/vault key stores. No QueueWorker, live provider, user credentials, task execution, physical write, HAR, trace or login capture.'}
    try:
        async def run_bounded():
            async with asyncio.timeout(300):
                await verify(output, report, headed=args.headed)
        asyncio.run(run_bounded())
    except BaseException as error:
        report['passed'] = False
        report['error'] = safe_error(error)
    finally:
        report['finished_at'] = now()
        report['artifact_sha256'] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in public_artifacts(output)}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'report': str(output / 'report.json')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
