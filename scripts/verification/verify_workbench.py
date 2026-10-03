#!/usr/bin/env python3
"""M1-21 workbench acceptance with real UI, queue, graph, browser and SSE.

READY contracts and one explicit site gate are trusted, isolated ledger setup.
Start/pause/resume/cancel, budget exhaustion, observation, verification and SSE
are production paths. A transport wrapper can interrupt or duplicate/conflict
delivery for reconnect tests; it never changes SQLite business state.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).resolve().parent)]
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

from verify_controls import (ControlsFixture, Domain, OwnedSecrets, command_body,
    facts, owned_resources, seed_task)
from verify_startup import Process, TRACE_FLAGS, free_ports, now
from verify_task_entry import public_artifacts, safe_error, scan_secrets, wait_until
from webagent.api import create_app
from webagent.config import Settings, disable_external_tracing
from webagent.controls.store import ControlStore
from webagent.db import LATEST_VERSION, connect, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.events import WaitingEvent, append_event, read_events
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.scheduler.store import SchedulerStore
from webagent.security import LocalApiPolicy, load_or_create_token
from webagent.sse import encode_event
from webagent.tasks.models import TaskContract

import httpx
from playwright.async_api import async_playwright, expect
import uvicorn


def stream_attachment(scope: dict) -> dict:
    """Only bounded public IDs/cursor; never arbitrary headers or query values."""
    values = parse_qs(scope.get('query_string', b'').decode('ascii', errors='ignore'))
    headers = {key.lower(): value for key, value in scope.get('headers', [])}
    cursor = headers.get(b'last-event-id', b'').decode('ascii', errors='ignore')
    return {'task_id': values.get('task_id', [None])[0],
            'run_id': values.get('run_id', [None])[0],
            'last_event_id': cursor if cursor.isdecimal() and len(cursor) <= 19 else None}


def delivery_frames(event: dict, mode: str, *, lower_id: int | None = None) -> bytes:
    """Explicit stream delivery faults, preserving the original business row."""
    original = deepcopy(event)
    if mode == 'duplicate':
        return (encode_event(original) + encode_event(original)).encode()
    changed = deepcopy(original)
    if mode == 'conflict':
        changed['state_version'] += 1
    elif mode == 'out_of_order':
        if type(lower_id) is not int or not 0 < lower_id < int(event['event_id']):
            raise ValueError('An earlier positive delivery ID is required')
        changed['event_id'] = lower_id
    else:
        raise ValueError('Unknown owned stream delivery fault')
    return encode_event(changed).encode()


class StreamTransport:
    """ASGI transport fault seam outside the unchanged production SSE router."""
    def __init__(self, app):
        self.app = app
        self.attachments: list[dict] = []
        self.active: dict[int, asyncio.Event] = {}
        self.prefix: bytes = b''
        self.disconnections = 0

    def disconnect(self) -> int:
        count = len(self.active)
        for event in list(self.active.values()):
            event.set()
        self.disconnections += count
        return count

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or scope.get('path') != '/v1/events':
            return await self.app(scope, receive, send)
        self.attachments.append(stream_attachment(scope))
        signal = asyncio.Event()
        identity = id(signal)
        self.active[identity] = signal
        prefix, self.prefix = self.prefix, b''
        started = False
        ended = False

        async def deliver(message):
            nonlocal started, ended
            await send(message)
            if message['type'] == 'http.response.start':
                started = True
                if prefix and message['status'] == 200:
                    await send({'type': 'http.response.body', 'body': prefix, 'more_body': True})
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                ended = True

        task = asyncio.create_task(self.app(scope, receive, deliver))
        interrupt = asyncio.create_task(signal.wait())
        try:
            done, _ = await asyncio.wait((task, interrupt), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if started and not ended:
                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        finally:
            interrupt.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, interrupt, return_exceptions=True)
            self.active.pop(identity, None)


class WorkbenchFixture(ControlsFixture):
    def __init__(self, output, reasoning):
        super().__init__(output)
        self.reasoning = reasoning

    async def model_reply(self, request):
        reply = await super().model_reply(request)
        reply['choices'][0]['message']['reasoning_content'] = self.reasoning
        return reply


def additional_ready_task(path: Path, original_task_id: str, origin: str, alias: str,
                          *, actions: int | None = None) -> str:
    """Documented trusted preparation fixture, never forged execution state."""
    task_id = 'workbench-task-' + alias
    with connect(path) as db, transaction(db):
        raw = db.execute('SELECT content_json FROM contracts WHERE task_id=? AND contract_version=1',
                         (original_task_id,)).fetchone()[0]
        contract = json.loads(raw)
        contract.update(task_id=task_id, original_instruction='读取声明来源的年度财报营收与利润：' + alias,
                        created_at=utc_text(), start_urls=[origin + '/start/' + alias])
        if actions is not None:
            contract['budget_profile']['max_actions'] = actions
        contract = TaskContract.model_validate_json(canonical_json(contract)).model_dump(mode='json')
        create_task(db, task_id=task_id, instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        db.execute("UPDATE tasks SET preparation_status='READY',current_contract_version=1,requested_fields_json='[]',state_version=state_version+1 WHERE task_id=?", (task_id,))
    return task_id


def ledger_summary(path: Path, run_id: str) -> dict:
    """Counts and safe states only, excluding observations/model/physical args."""
    with connect(path) as db:
        db.execute('BEGIN')
        run = db.execute('SELECT task_id,state,state_version,contract_version FROM runs WHERE run_id=?', (run_id,)).fetchone()
        return {'run_id': run_id, **dict(run),
                'events': db.execute('SELECT count(*) FROM task_events WHERE run_id=?', (run_id,)).fetchone()[0],
                'graph_progress': db.execute('SELECT count(*) FROM graph_progress WHERE run_id=?', (run_id,)).fetchone()[0],
                'verified_checkpoints': db.execute('SELECT count(*) FROM run_checkpoints WHERE run_id=?', (run_id,)).fetchone()[0],
                'browser_actions': db.execute('SELECT count(*) FROM steps WHERE run_id=? AND status="COMPLETED"', (run_id,)).fetchone()[0],
                'controls': [dict(row) for row in db.execute('SELECT action,status FROM run_controls WHERE run_id=? ORDER BY operation_seq', (run_id,))],
                'quota_debits': db.execute('SELECT count(*) FROM quota_debits WHERE run_id=?', (run_id,)).fetchone()[0]}


def seed_filter_gap(path: Path, gap_task_id: str) -> None:
    """Use a dedicated task so an event fixture cannot poison UI start targets."""
    with connect(path) as db, transaction(db):
        create_run(db, run_id='owned-workbench-filter-gap', task_id=gap_task_id, contract_version=1,
            graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
        append_event(db, run_id='owned-workbench-filter-gap', expected_state_version=0,
                     payload=WaitingEvent(wait_id='owned-workbench-filter-gap-wait', reason='site'))


async def verify(output: Path, report: dict, *, headed: bool) -> None:
    disable_external_tracing()
    reasoning = 'SYNTHETIC_M121_REASONING_' + uuid4().hex
    fixture = WorkbenchFixture(output, reasoning)
    data = output / 'data'
    frontend = browser = context = playwright = server = serving = domain = None
    api_requests: list[dict] = []
    control_requests: list[dict] = []
    page_errors: list[dict] = []
    rejected_network: list[dict] = []
    transport = None
    worker_handles: list = []
    runs: dict[str, str] = {}
    token = None
    report.update(checks=[], processes=[], configuration={
        'trusted_ledger_fixtures': ['READY read-only contracts and model configuration', 'explicit site gate',
                                  'other-task event-only Run for global cursor gaps'],
        'provider': 'owned HTTP synthetic fixture', 'website': 'owned loopback source',
        'credential_store': 'memory-only synthetic', 'physical_repository_writes': 0,
        'production_paths': ['Chromium', 'Vite', 'FastAPI', 'SQLite', 'ControlStore', 'QueueWorker',
                             'GraphExecutor', 'ManagedBrowser', 'LangGraph', 'SSE'],
        'delivery_faults': 'trusted ASGI stream transport only; business ledger unchanged', 'headed': headed})

    def record(name: str, **details):
        report['checks'].append({'name': name, 'passed': True, 'at': now(), **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    try:
        await fixture.start()
        primary_task = seed_task(data, fixture.origin, 'live')
        budget_task = additional_ready_task(data / 'business.sqlite3', primary_task, fixture.origin,
                                            'budget', actions=1)
        cancel_task = additional_ready_task(data / 'business.sqlite3', primary_task, fixture.origin, 'cancel')
        blocked_task = additional_ready_task(data / 'business.sqlite3', primary_task, fixture.origin, 'blocked')
        gap_task = additional_ready_task(data / 'business.sqlite3', primary_task, fixture.origin, 'filter-gap')
        settings = Settings(data)
        token = load_or_create_token(data)
        canaries = {'provider_key': 'owned-controls-synthetic-provider-key', 'private_reasoning': reasoning,
                    'local_api_token': token}
        api_port, ui_port = free_ports()
        api_url, ui_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
        policy = LocalApiPolicy(token, frozenset({f'127.0.0.1:{api_port}'}), frozenset({api_url, ui_url}))
        app = create_app(settings, secret_store=OwnedSecrets(), local_api_policy=policy)
        app.state.control_store = ControlStore(settings.business_db, secret_store=OwnedSecrets(),
            resource_factory=lambda contract, run_id: owned_resources(fixture.origin, contract, run_id))

        @app.middleware('http')
        async def summarize(request, call_next):
            response = await call_next(request)
            api_requests.append({'method': request.method, 'path': request.url.path, 'status': response.status_code})
            return response

        transport = StreamTransport(app)
        server = uvicorn.Server(uvicorn.Config(transport, host='127.0.0.1', port=api_port,
            log_config=None, log_level='critical', access_log=False))
        serving = asyncio.create_task(server.serve())

        async def api_started():
            if serving.done():
                await serving
                raise RuntimeError('owned API exited before readiness')
            return server.started

        await wait_until(api_started, 'owned workbench API')
        env = {**os.environ, 'WEBAGENT_DATA_DIR': str(data), 'WEBAGENT_API_PORT': str(api_port),
               'WEBAGENT_UI_PORT': str(ui_port), 'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1'}
        for key in ('NODE_OPTIONS', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            env.pop(key, None)
        for key in TRACE_FLAGS:
            env[key] = 'false'
        frontend = Process('workbench-frontend', 'frontend', output, env)
        domain = Domain.__new__(Domain)
        # The actual API remains in-process for the labelled transport seam;
        # reuse only Domain's production Worker child lifecycle, not its start.
        domain.directory, domain.fixture, domain.alias = data, fixture, 'live'
        domain.task_id, domain.run_id = primary_task, None
        domain.worker = domain.api = domain.client = domain.listener = None
        domain.logs, domain.requests, domain.worker_pids = [], [], []
        domain.worker_group, domain.worker_kill_boundary = None, False
        async with httpx.AsyncClient(base_url=api_url, trust_env=False, timeout=10,
                                    headers={'Authorization': 'Bearer ' + token}) as client:
            async def ui_ready():
                try:
                    return (await client.get(ui_url)).status_code == 200
                except httpx.HTTPError:
                    return False
            await wait_until(ui_ready, 'owned workbench frontend', frontend=frontend)
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=not headed, args=[
                '--disable-background-networking', '--disable-component-update',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
            context = await browser.new_context(viewport={'width': 1280, 'height': 960}, service_workers='block')

            async def restrict(route):
                parsed = urlsplit(route.request.url)
                if parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port in (api_port, ui_port):
                    await route.continue_()
                else:
                    rejected_network.append({'method': route.request.method, 'allowed': False})
                    await route.abort()
            await context.route('**/*', restrict)
            page = await context.new_page()
            page.on('pageerror', lambda _: page_errors.append({'kind': 'pageerror'}))

            def capture_control(request):
                parsed = urlsplit(request.url)
                if request.method == 'POST' and parsed.path.endswith(('/start', '/pause', '/resume', '/cancel')):
                    body = request.post_data_json
                    control_requests.append({'path': parsed.path, 'idempotency_key': request.headers.get('idempotency-key'),
                        'expected_state_version': body.get('expected_state_version'),
                        'contract_version': body.get('contract_version'), 'settings_version': body.get('settings_version')})
            page.on('request', capture_control)

            async def workspace(task_id, run_id=None):
                response = await client.get('/v1/tasks/' + task_id + '/workspace',
                    params={'run_id': run_id} if run_id else None)
                assert response.status_code == 200
                return response.json()

            async def select(task_id):
                await page.goto(ui_url + '/?task=' + task_id, wait_until='domcontentloaded')
                await expect(page.get_by_test_id('task-detail')).to_contain_text(task_id)
                await expect(page.locator('#execution-workbench')).to_be_visible()

            async def start(task_id, alias):
                await select(task_id)
                await page.get_by_test_id('workbench-confirm').check()
                async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/tasks/' + task_id + '/start') as response_event:
                    await page.get_by_role('button', name='启动执行', exact=True).click()
                response = await response_event.value
                assert response.status == 202
                body = await response.json()
                assert body['operation']['status'] == 'PENDING'
                run_id = body['operation']['run_id']
                runs[alias] = run_id
                fixture.bind(alias, run_id)
                domain.task_id, domain.run_id, domain.alias = task_id, run_id, alias
                return body['operation']

            async def control(action, label):
                target = domain.run_id
                await page.get_by_test_id('workbench-confirm').check()
                async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/v1/runs/' + target + '/' + action) as response_event:
                    await page.get_by_role('button', name=label, exact=True).click()
                response = await response_event.value
                assert response.status == 202
                receipt = (await response.json())['operation']
                assert receipt['status'] == 'PENDING' and receipt['run_id'] == target
                return receipt

            async def operation_applied(operation_id):
                async def applied():
                    response = await client.get('/v1/operations/' + operation_id)
                    assert response.status_code == 200
                    operation = response.json()['operation']
                    if operation['status'] != 'PENDING':
                        assert operation['status'] == 'APPLIED'
                        return True
                    await domain.ensure_alive()
                    return False
                await wait_until(applied, 'owned control settlement', timeout=35)

            await select(primary_task)
            initial = await workspace(primary_task)
            assert initial['run'] is None and initial['budget'] is None and initial['queue'] is None
            await expect(page.get_by_role('button', name='启动执行', exact=True)).to_be_disabled()
            record('ready_preparation_is_distinct_from_run_and_requires_explicit_review', trusted_READY_fixture=True)
            started = await start(primary_task, 'live')
            await expect(page.get_by_test_id('workbench-operation')).to_contain_text('已受理，处理中')
            await expect(page.locator('#execution-workbench .workbench-notice')).to_contain_text('正在等待安全边界处理')
            await expect(page.get_by_test_id('workbench-queue')).to_contain_text('排队')
            queued = await workspace(primary_task)
            assert queued['run']['state'] == 'QUEUED' and queued['run']['settings_version'] == 1
            assert queued['queue']['status'] == 'QUEUED' and queued['budget']['initialized'] is False
            record('ui_start_uses_real_pending_202_and_persisted_queue_without_claiming_execution', run_id=domain.run_id)

            fixture.block_click.add(domain.run_id)
            await domain.start_worker()
            worker_handles.append(domain.worker)
            async def click_entered():
                await domain.ensure_alive()
                return fixture.click_entered.setdefault(domain.run_id, asyncio.Event()).is_set()
            await wait_until(click_entered, 'real browser read action in flight', timeout=35)
            await operation_applied(started['operation_id'])
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'RUNNING')
            active = await workspace(primary_task)
            # Production action admission invalidates the old page head before
            # dispatch. During this held physical click, the last captured page
            # is readable evidence but cannot be represented as a fresh page.
            assert active['observation'] and active['observation']['valid'] is False
            assert active['observation']['evidence'] and active['graph_progress']
            assert active['current_subgoal'] and active['budget']
            assert active['budget']['model_calls_used'] > 0
            assert active['budget']['remaining_actions'] < active['budget']['limits']['max_actions']
            await expect(page.get_by_test_id('workbench-subgoal')).not_to_be_empty()
            await expect(page.get_by_test_id('workbench-evidence')).to_contain_text(active['observation']['snapshot_id'])
            await expect(page.get_by_test_id('workbench-evidence')).to_contain_text('观察已失效，需重新读取')
            await expect(page.get_by_test_id('workbench-evidence')).to_contain_text('revenue')
            await expect(page.get_by_test_id('workbench-evidence')).to_contain_text('profit')
            text_evidence = [item['evidence_id'] for item in active['observation']['evidence']
                             if item['artifact_kind'] == 'text' and item['availability'] == 'AVAILABLE'
                             and item['redaction_status'] == 'FILTERED']
            assert text_evidence and any(request['method'] == 'GET' and request['status'] == 200
                and request['path'] == '/v1/evidence/' + evidence_id + '/content'
                for request in api_requests for evidence_id in text_evidence)
            record('real_worker_inflight_browser_action_projects_subgoal_invalidated_page_head_and_budget',
                   provider_calls=len(fixture.provider_requests), evidence_count=len(active['observation']['evidence']))
            record('latest_page_uses_actual_authenticated_filtered_text_derivative', original_screenshot_claimed=False)

            # A second task's committed event interleaves global IDs but cannot
            # execute: this explicit event-only fixture has no scheduler row.
            seed_filter_gap(settings.business_db, gap_task)

            paused_receipt = await control('pause', '暂停执行')
            await expect(page.get_by_test_id('workbench-operation')).to_contain_text('已受理，处理中')
            await expect(page.locator('#execution-workbench .workbench-notice')).to_contain_text('正在等待安全边界处理')
            current = facts(data, domain.run_id)
            assert current['run']['state'] == 'RUNNING' and any(step['status'] == 'INTENT' for step in current['steps'])
            record('pause_receipt_remains_pending_while_atomic_read_action_is_inflight')
            fixture.click_release[domain.run_id].set()
            await operation_applied(paused_receipt['operation_id'])
            await domain.await_facts(lambda value: value['run']['state'] == 'PAUSED')
            await domain.await_metrics(lambda value: value['active'].get(domain.run_id) == 0)
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'PAUSED')
            await expect(page.locator('#execution-workbench .workbench-notice')).to_contain_text('已应用')
            await expect(page.locator('#execution-workbench .workbench-notice')).not_to_contain_text('正在等待安全边界处理')
            paused = await workspace(primary_task)
            assert paused['checkpoint'] and paused['queue']['status'] == 'WAITING'
            assert fixture.clicks[domain.run_id] == 1
            await page.reload(wait_until='domcontentloaded')
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'PAUSED')
            assert (await workspace(primary_task))['run']['run_id'] == domain.run_id
            record('applied_pause_and_reload_restore_same_run_checkpoint_and_wait_state')

            configured = (await client.get('/v1/settings')).json()
            changed = await client.put('/v1/settings/model', json={'expected_version': configured['version'],
                'model': {**configured['model'], 'max_tokens': 2048}, 'accept_data_sharing': True})
            assert changed.status_code == 200 and changed.json()['version'] == 2
            await page.get_by_role('button', name='重新同步', exact=True).click()
            unchanged = await workspace(primary_task)
            assert unchanged['run']['settings_version'] == 1
            assert unchanged['budget']['limits'] == paused['budget']['limits']
            record('settings_update_does_not_replace_existing_run_frozen_settings_or_budget', current_settings_version=2, run_settings_version=1)

            # Delivery only: original SSE router still reads SQLite. A duplicate
            # cannot add history, and conflict/out-of-order force current API reads.
            await exercise_stream_faults(page, client, primary_task, domain.run_id, settings.business_db,
                                         transport, api_requests, record)

            fixture.phase[domain.run_id] = 'B'
            resume_receipt = await control('resume', '继续执行')
            assert control_requests[-1]['settings_version'] == 1
            await operation_applied(resume_receipt['operation_id'])
            succeeded = await domain.await_facts(lambda value: value['run']['state'] == 'SUCCEEDED')
            await domain.await_metrics(lambda value: value['active'].get(domain.run_id) == 0)
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'SUCCEEDED')
            await expect(page.locator('#execution-workbench .workbench-notice')).to_contain_text('已应用')
            await expect(page.locator('#execution-workbench .workbench-notice')).not_to_contain_text('正在等待安全边界处理')
            terminal = await workspace(primary_task)
            assert terminal['checkpoint']['verified_item_ids'] and all(item['verified'] for item in terminal['criteria'] if item['critical'])
            assert terminal['observation']['valid'] is True
            assert terminal['observation']['snapshot_id'] != active['observation']['snapshot_id']
            assert len(succeeded['quota_debits']) == 1 and fixture.clicks[domain.run_id] == 1
            assert terminal['run']['settings_version'] == 1
            record('resume_reuses_same_run_frozen_config_and_verifies_criteria_without_replaying_click', run_id=domain.run_id)
            record('control_banner_follows_persisted_pending_and_applied_receipts_without_stale_waiting_copy')
            await page.locator('#execution-workbench').screenshot(path=str(output / '01-workbench-desktop.png'))
            await domain.stop_worker()

            await start(cancel_task, 'cancel')
            await domain.start_worker(point='model_before')
            worker_handles.append(domain.worker)
            await domain.boundary()
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'RUNNING')
            cancel_receipt = await control('cancel', '取消执行')
            await expect(page.get_by_test_id('workbench-operation')).to_contain_text('已受理，处理中')
            (data / 'release-boundary').touch()
            await operation_applied(cancel_receipt['operation_id'])
            cancelled = await domain.await_facts(lambda value: value['run']['state'] == 'CANCELLED')
            await domain.await_metrics(lambda value: value['active'].get(domain.run_id) == 0)
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'CANCELLED')
            assert not cancelled['run_results'] or all(row['outcome'] == 'CANCELLED' for row in cancelled['run_results'])
            record('cancel_pending_receipt_settles_at_real_graph_boundary_without_success')
            await domain.stop_worker()

            await start(budget_task, 'budget')
            await domain.start_worker()
            worker_handles.append(domain.worker)
            exhausted = await domain.await_facts(lambda value: value['run']['state'] == 'FAILED')
            await domain.await_metrics(lambda value: value['active'].get(domain.run_id) == 0)
            failed = await workspace(budget_task)
            assert failed['budget']['exhausted'] is True and failed['budget']['reason'] == 'action_limit'
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'FAILED')
            await expect(page.get_by_test_id('workbench-budget')).to_contain_text('预算')
            assert exhausted['run']['blocked_reason']
            assert not exhausted['run_results'] or all(row['outcome'] != 'SUCCEEDED' for row in exhausted['run_results'])
            record('real_action_budget_exhaustion_displays_failed_state_and_stop_reason', constrained_READY_contract=True)
            await domain.stop_worker()

            await start(blocked_task, 'blocked')
            # Explicit isolated site gate only: scheduler must project a queue
            # blocker without invoking the provider or a website action.
            with connect(settings.business_db) as db, transaction(db):
                db.execute("INSERT INTO site_gates(site_id,state,blocked_reason,next_eligible_at,updated_at) VALUES(?,'BLOCKED',?,NULL,?)",
                           ('webarena:owned-http', 'owned_fixture_site_gate', utc_text()))
            before_calls = len(fixture.provider_requests)
            await domain.start_worker()
            worker_handles.append(domain.worker)
            await domain.await_facts(lambda value: value['queue']['reason'] == 'resource_conflict')
            blocked = await workspace(blocked_task)
            assert blocked['queue']['reason'] == 'resource_conflict' and blocked['run']['state'] == 'QUEUED'
            assert len(fixture.provider_requests) == before_calls
            await expect(page.get_by_test_id('workbench-queue')).to_contain_text('资源')
            record('explicit_site_gate_fixture_projects_queue_blocker_without_false_running_or_provider_call', trusted_site_gate=True)
            await domain.stop_worker()

            assert not page_errors and not rejected_network
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            for value in canaries.values():
                assert value not in await page.locator('body').inner_text() and value not in page.url
            assert len(control_requests) == 7
            assert len({entry['idempotency_key'] for entry in control_requests}) == len(control_requests)
            assert all(isinstance(entry['idempotency_key'], str) and entry['idempotency_key'] for entry in control_requests)
            record('all_controls_are_explicit_distinct_idempotent_posts_without_hidden_retries', control_posts=len(control_requests))
            await page.set_viewport_size({'width': 375, 'height': 900})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await expect(page.locator('#model-api-key')).to_have_value('')
            await page.locator('#execution-workbench').screenshot(path=str(output / '02-workbench-mobile.png'))
            record('workbench_mobile_layout_and_screenshots_contain_no_secret_or_private_reasoning', viewport_width=375)
            unauth = await client.get('/v1/tasks/' + primary_task + '/workspace', headers={'Authorization': ''})
            assert unauth.status_code == 401
            denied = await client.get('/v1/tasks/' + primary_task + '/workspace', headers={'Origin': 'https://untrusted.example'})
            assert denied.status_code == 403
            record('workbench_read_routes_preserve_local_auth_and_origin_boundary')

        assert not fixture.errors
        report['passed'] = True
    finally:
        cleanup = []
        if context is not None:
            try: await context.close()
            except Exception as error: cleanup.append(safe_error(error))
        if browser is not None:
            try: await browser.close()
            except Exception as error: cleanup.append(safe_error(error))
        if playwright is not None:
            try: await playwright.stop()
            except Exception as error: cleanup.append(safe_error(error))
        if domain is not None:
            try: await domain.close()
            except Exception as error: cleanup.append(safe_error(error))
        if frontend is not None:
            try:
                closed = frontend.stop()
                report['processes'].append(closed)
                assert not closed['forced_kill']
            except Exception as error: cleanup.append(safe_error(error))
        if serving is not None:
            try:
                transport.disconnect()
                server.should_exit = True
                await asyncio.wait_for(serving, 12)
            except Exception as error: cleanup.append(safe_error(error))
        try: await fixture.close()
        except Exception as error: cleanup.append(safe_error(error))
        report['cleanup_errors'] = cleanup
        if cleanup:
            report['passed'] = False
        summaries = []
        if (data / 'business.sqlite3').exists():
            with connect(data / 'business.sqlite3') as db:
                report['storage'] = {'schema_version': db.execute('PRAGMA user_version').fetchone()[0],
                    'integrity': db.execute('PRAGMA integrity_check').fetchone()[0],
                    'foreign_key_errors': len(db.execute('PRAGMA foreign_key_check').fetchall()),
                    'write_intents': db.execute('SELECT count(*) FROM write_intents').fetchone()[0],
                    'active_workers': db.execute("SELECT count(*) FROM scheduler_workers WHERE state='ACTIVE'").fetchone()[0],
                    'active_browser_sessions': db.execute("SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING')").fetchone()[0]}
            for run_id in runs.values():
                summaries.append(ledger_summary(data / 'business.sqlite3', run_id))
        graph_path = data / 'graph.sqlite3'
        report['graph_storage'] = None
        if graph_path.exists():
            with closing(sqlite3.connect(graph_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
                db.execute('PRAGMA query_only=ON')
                report['graph_storage'] = {'integrity': db.execute('PRAGMA integrity_check').fetchone()[0],
                    'foreign_key_errors': len(db.execute('PRAGMA foreign_key_check').fetchall()),
                    'checkpoints': db.execute('SELECT count(*) FROM checkpoints').fetchone()[0]}
        report['owned_lifecycle'] = {'worker_handles': [{'pid': child.pid, 'exit_code': child.returncode}
                                                       for child in worker_handles],
            'all_workers_awaited': bool(worker_handles) and all(child.returncode == 0 for child in worker_handles),
            'api_awaited': serving is not None and serving.done(),
            'frontend_exited': frontend is not None and not frontend.alive(),
            'browser_disconnected': browser is not None and not browser.is_connected()}
        report['runs'] = summaries
        report['scope_counts'] = {'checks': len(report['checks']), 'worker_processes': len(domain.worker_pids) if domain else 0,
            'http_model_calls': len(fixture.provider_requests), 'browser_clicks': sum(fixture.clicks.values()),
            'control_posts': len(control_requests), 'stream_attachments': len(transport.attachments) if transport else 0,
            'forced_stream_disconnections': transport.disconnections if transport else 0}
        (output / 'request-summary.json').write_text(json.dumps({'api': api_requests, 'controls': control_requests,
            'stream_attachments': transport.attachments if transport else [], 'page_errors': page_errors,
            'rejected_network': rejected_network, 'http_model_requests': fixture.provider_requests}, indent=2) + '\n')
        if token is not None:
            scan = scan_secrets(output, {'provider_key': 'owned-controls-synthetic-provider-key',
                                       'private_reasoning': reasoning, 'local_api_token': token})
            report['secret_scan'] = scan
            if not scan['passed']:
                report['passed'] = False
        if report['passed']:
            assert report['storage']['schema_version'] == LATEST_VERSION
            assert report['storage']['integrity'] == 'ok' and report['storage']['foreign_key_errors'] == 0
            assert report['storage']['write_intents'] == 0
            assert report['storage']['active_workers'] == 0 and report['storage']['active_browser_sessions'] == 0
            assert report['graph_storage'] and report['graph_storage']['integrity'] == 'ok'
            assert report['graph_storage']['foreign_key_errors'] == 0 and report['graph_storage']['checkpoints'] > 0
            assert all(report['owned_lifecycle'][key] for key in ('all_workers_awaited', 'api_awaited', 'frontend_exited', 'browser_disconnected'))
            assert not cleanup and not fixture.active and not transport.active
            record('all_owned_processes_browser_streams_exit_before_ledger_integrity_and_secret_hashing')
            report['scope_counts']['checks'] = len(report['checks'])


async def exercise_stream_faults(page, client, task_id, run_id, path, transport, api_requests, record):
    async def active_stream():
        return bool(transport.active)
    await wait_until(active_stream, 'owned active event stream')
    events = read_events(path, after=0, task_id=task_id, run_id=run_id, limit=100)
    assert len(events) > 2
    assert any(right['event_id'] > left['event_id'] + 1 for left, right in zip(events, events[1:]))
    assert all(event['task_id'] == task_id and event['run_id'] == run_id for event in events)
    await expect(page.get_by_test_id('workbench-connection')).to_have_text('已同步')
    await expect(page.get_by_test_id('workbench-confirm')).to_be_enabled()
    record('filtered_global_id_gap_is_normal_for_selected_run_and_keeps_connection_synchronized', trusted_other_task_event=True)
    original = events[-1]
    database_before = [(item['event_id'], canonical_json(item)) for item in events]
    before = len(transport.attachments)
    transport.prefix = delivery_frames(original, 'duplicate')
    assert transport.disconnect() > 0
    async def attached():
        return len(transport.attachments) > before and bool(transport.active)
    await wait_until(attached, 'SSE reconnect with original duplicate replay')
    newest = transport.attachments[-1]
    assert newest['run_id'] == run_id and newest['task_id'] == task_id
    assert newest['last_event_id'] == str(original['event_id'])
    await expect(page.get_by_test_id('workbench-connection')).to_have_text('已同步')
    await asyncio.sleep(1.2)
    assert len(transport.attachments) == before + 1
    await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'PAUSED')
    identifiers = await page.get_by_test_id('workbench-event').evaluate_all('(elements) => elements.map((item) => item.dataset.eventId)')
    assert identifiers and len(identifiers) == len(set(identifiers))
    record('forced_stream_disconnect_reconnects_with_exact_last_event_id_and_ignores_duplicate_delivery', delivery_injected=True)

    def workspace_count():
        return sum(item['method'] == 'GET' and item['path'] == '/v1/tasks/' + task_id + '/workspace' for item in api_requests)
    count = workspace_count()
    before = len(transport.attachments)
    transport.prefix = delivery_frames(original, 'conflict')
    assert transport.disconnect() > 0
    async def resynced():
        return len(transport.attachments) >= before + 2 and workspace_count() >= count + 2
    await wait_until(resynced, 'conflicting SSE delivery authoritative resync')
    await expect(page.get_by_test_id('workbench-connection')).to_have_text('已同步')
    await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'PAUSED')
    record('same_id_conflicting_stream_delivery_queries_current_snapshot_without_changing_business_state', delivery_injected=True)

    # Global IDs are shared across tasks. A filtered gap is normal. The first
    # older global ID belongs to another task, but fault delivery deliberately
    # changes only the sequence metadata of a selected-run event.
    with connect(path) as db:
        candidates = [row[0] for row in db.execute('SELECT event_id FROM task_events WHERE event_id<? AND run_id<>? ORDER BY event_id DESC',
                                                  (original['event_id'], run_id))]
    lower = next((value for value in candidates if value not in {event['event_id'] for event in events}), None)
    assert lower is not None
    count = workspace_count()
    before = len(transport.attachments)
    transport.prefix = delivery_frames(original, 'out_of_order', lower_id=lower)
    assert transport.disconnect() > 0
    await wait_until(resynced, 'out-of-order unknown SSE ID authoritative resync')
    await expect(page.get_by_test_id('workbench-connection')).to_have_text('已同步')
    await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'PAUSED')
    record('unknown_out_of_order_stream_id_queries_current_snapshot', delivery_injected=True)
    after = read_events(path, after=0, task_id=task_id, run_id=run_id, limit=100)
    assert database_before == [(item['event_id'], canonical_json(item)) for item in after]
    assert any(request['method'] == 'GET' and request['status'] == 200
               and request['path'] == '/v1/runs/' + run_id + '/events' for request in api_requests)
    record('stream_delivery_faults_do_not_mutate_persistent_event_ledger')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('workbench-%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT / 'artifacts' / 'verification' / 'M1-21' / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-21', 'probe': 'workbench-ui-live-graph-sse', 'started_at': now(), 'passed': False,
        'scope': 'Owned real Chromium/Vite/FastAPI/SQLite/ControlStore/QueueWorker/GraphExecutor/LangGraph/ManagedBrowser/SSE. READY preparation and site-gate rows are explicit constrained fixtures; stream delivery faults alter transport only. No paid provider, user credentials, real site or repository write.'}
    try:
        async def bounded():
            async with asyncio.timeout(300):
                await verify(output, report, headed=args.headed)
        asyncio.run(bounded())
    except BaseException as error:
        report['passed'] = False
        report['error'] = safe_error(error)
    finally:
        report['finished_at'] = now()
        report['artifact_sha256'] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in public_artifacts(output)}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report.get('checks', [])), 'report': str(output / 'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
