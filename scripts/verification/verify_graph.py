#!/usr/bin/env python3
"""M1-16 real Chromium, HTTP provider and durable LangGraph acceptance.

All documents, endpoints and credentials are owned synthetic fixtures. The
model substitute derives proposals from the bytes in its HTTP request, while
the verifier separately rereads the captured original browser evidence.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import traceback
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

from webagent.config import Settings, disable_external_tracing
from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.models.transport import DeepSeekTransport, ModelConfig
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.tasks.models import TaskContract


class OwnedGraphFixture:
    """Serve generated disclosures and an independent HTTP model substitute."""

    def __init__(self):
        self.requests, self.provider_requests, self.errors = [], [], []
        self.active = set()
        self.recovery_phase = 'A'
        self.complete_missing = False
        self.calls = {}
        self.now = utc_text()
        self.values = []
        for field in ('revenue', 'profit'):
            value = format(Decimal(secrets.randbelow(900000) + 100000) / Decimal('100'), '.2f')
            self.values.append({'field_id': field, 'entity_id': 'owned-fixture-entity',
                'report_version': 'owned-disclosure-v1', 'period_start': '2025-01-01T00:00:00Z',
                'period_end': '2025-12-31T23:59:59Z', 'period_type': 'annual',
                'metric_definition': field, 'currency': 'USD', 'raw_value': value,
                'disclosed_unit': 'unit', 'normalized_value': value, 'value_origin': 'disclosed',
                'formula': None, 'rounding_rule': 'exact', 'rounding_lower': value,
                'rounding_upper': value, 'channel': 'owned-http'})

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.port}'

    def document(self, missing=False):
        return {'values': deepcopy(self.values[:1] if missing else self.values),
            'coverage': {'searched_sources': ['owned-http'], 'queries': [], 'cutoff_at': None,
                'content_pages': 1, 'unread_candidates': [],
                'gaps': ['Await the profit disclosure'] if missing else [], 'complete': not missing}}

    def page(self, path):
        if path == '/wrong':
            document = {'notice': 'This is a different report', 'next_url': self.origin + '/full'}
        else:
            document = self.document(path == '/partial' or path == '/missing' and not self.complete_missing)
        script = ''
        if path == '/missing':
            script = '<script>setInterval(async()=>{try{let r=await fetch("/facts");document.querySelector("pre").textContent=JSON.stringify(await r.json())}catch{}},100)</script>'
        # No other visible text may be appended to the original JSON disclosure.
        return ('<!doctype html><meta charset="utf-8"><title>Owned graph disclosure</title>'
                '<pre style="white-space:pre-wrap">' + html.escape(canonical_json(document))
                + '</pre>' + script).encode()

    def reply(self, request):
        assert len(request['messages']) == 2 and 'tools' not in request and 'tool_choice' not in request
        payload = json.loads(request['messages'][1]['content'])
        assert 'observation' in payload and 'verified_checkpoint' in payload
        assert set(payload) == {'run_id', 'contract', 'observation', 'verified_checkpoint',
            'image_evidence_ids', 'allowed_action_schema_ref', 'selected_flow_versions'}
        assert not ({'history', 'messages', 'execution_history', 'originals', 'summary'} & set(payload))
        run_id = payload['run_id']
        number = self.calls.get(run_id, 0) + 1
        self.calls[run_id] = number
        observation, checkpoint = payload['observation'], payload['verified_checkpoint']
        document = json.loads(observation['visible_excerpt'])
        refs = observation['evidence_ids']
        assert refs and observation['redaction_status'] == 'FILTERED'
        if run_id == 'graph-wrong' and 'next_url' in document:
            output = {'type': 'Action', 'action': {'run_id': run_id,
                'step_id': 'model-navigation-' + str(number), 'epoch': checkpoint['epoch'],
                'snapshot_id': observation['snapshot_id'], 'expected_effect': 'read',
                'action_type': 'navigate', 'target': {'page_url': observation['source_url'],
                    'tab_id': observation['tab_id'], 'frame_id': observation['frame_id'],
                    'locator': None, 'write_scope': None}, 'args': {'url': document['next_url']}}}
        elif run_id == 'graph-evidence':
            output = {'type': 'RequestEvidence',
                'criterion_ids': [payload['contract']['acceptance_criteria'][0]['criterion_id']],
                'source_ids': ['owned-http'], 'needed': 'Obtain an independent current disclosure'}
        elif run_id in ('graph-input', 'graph-queued-input') or run_id == 'graph-restore' and self.recovery_phase == 'A':
            output = {'type': 'RequestInput', 'requested_fields': ['user_confirmation'],
                      'reason': 'Owned recovery fixture requires explicit control return'}
        else:
            output = {'type': 'ProposeResult', 'items': {'scenario': 'finance',
                'values': [{**item, 'evidence_ids': refs} for item in document['values']]},
                'coverage': document['coverage'], 'evidence_ids': refs,
                'unresolved': [], 'existing_operation_ids': []}
            if run_id == 'graph-missing' and len(document['values']) == 1:
                # This changes the HTTP source, never the captured original.
                # The next page-owned poll makes the new disclosure observable.
                self.complete_missing = True
        self.provider_requests.append({'run_id': run_id, 'number': number,
            'provider_phase': self.recovery_phase if run_id == 'graph-restore' else 'single',
            'input_sha256': hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
            'observation_sha256': hashlib.sha256(observation['visible_excerpt'].encode()).hexdigest(),
            'snapshot_id': observation['snapshot_id'], 'checkpoint_id': checkpoint['checkpoint_id'],
            'epoch': checkpoint['epoch'], 'output_type': output['type'],
            'supplied_facts': len(document.get('values', [])), 'tool_authority_absent': True})
        return {'id': 'owned-graph-' + str(number), 'model': 'deepseek-flash',
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': canonical_json(output)}}],
            'usage': {'prompt_tokens': 23, 'completion_tokens': 17, 'total_tokens': 40}}

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            assert len(header) < 65536
            lines = header.decode('ascii').split('\r\n')
            method, target, _ = lines[0].split(' ', 2)
            headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                       for line in lines[1:] if ':' in line}
            size = int(headers.get('content-length', '0'))
            assert 0 <= size <= 2 * 1024 * 1024
            body = await asyncio.wait_for(reader.readexactly(size), 5) if size else b''
            path, status = urlsplit(target).path, '200 OK'
            content_type = 'text/html; charset=utf-8'
            if method == 'POST' and path == '/chat/completions':
                response, content_type = canonical_json(self.reply(json.loads(body))).encode(), 'application/json'
            elif method == 'GET' and path == '/facts':
                response, content_type = canonical_json(self.document(not self.complete_missing)).encode(), 'application/json'
            elif method == 'GET' and path in ('/full', '/wrong', '/missing', '/partial'):
                response = self.page(path)
            else:
                status, response = '404 Not Found', b'Owned fixture route unavailable'
            self.requests.append({'method': method, 'path': path,
                'response_sha256': hashlib.sha256(response).hexdigest()})
            writer.write(('HTTP/1.1 ' + status + '\r\nContent-Type: ' + content_type +
                '\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: '
                + str(len(response)) + '\r\n\r\n').encode() + response)
            await writer.drain()
        except Exception as error:
            self.errors.append({'type': type(error).__name__, 'phase': 'fixture'})
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
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*tuple(self.active), return_exceptions=True)


def seed(directory, origin, run_id, config, *, route='/full', max_pages=10, recoveries=3):
    from webagent.tasks.compiler import compile_draft
    path = directory / 'business.sqlite3'
    migrate(path)
    contract = compile_draft({'instruction': 'Read the owned annual disclosure and verify both metrics',
        'scenario': 'finance', 'source_ids': ['local-fixture'], 'parameters': {
            'entity_id': 'owned-fixture-entity', 'report_version': 'owned-disclosure-v1',
            'period_type': 'annual', 'metrics': ['revenue', 'profit'], 'currency': 'USD'}},
        task_id='task-' + run_id, version=1, created_at=utc_text(), provenance=[{
            'origin': 'explicit_test_configuration', 'reference': 'graph-acceptance-fixture-v1',
            'content_sha256': 'a' * 64, 'authorizes_execution': True}]).contract
    contract['sources'] = [{'source_id': 'owned-http', 'site_id': 'owned-http', 'origin': origin, 'path_prefix': '/'}]
    contract['start_urls'] = [origin + route]
    contract['output_schema'] = [{'field_id': field, 'required': True,
        'description': 'Requested original ' + field} for field in ('revenue', 'profit')]
    contract['time_scope'] = {'start': '2025-01-01T00:00:00Z', 'end': '2025-12-31T23:59:59Z',
                             'basis': 'Explicit owned HTTP annual disclosure period'}
    contract['budget_profile'].update({'max_content_pages': max_pages, 'max_actions': 12,
        'max_active_seconds': 120, 'max_recoveries_per_obstacle': recoveries})
    contract = TaskContract.model_validate_json(canonical_json(contract)).model_dump(mode='json')
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=run_id, task_id=contract['task_id'], contract_version=1,
            graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256=config.config_sha256, runtime_config_sha256='b' * 64)
    store = SchedulerStore(path, lease_seconds=180)
    store.enqueue(run_id, [Resource.site_identity('owned-http', realm='webarena'),
                          Resource.browser_context(run_id)], expected_state_version=0, queue_class='webarena')
    return contract


def facts(directory, run_id):
    with connect(directory / 'business.sqlite3') as db:
        run = dict(db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone())
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        payload = {'run': {key: run[key] for key in ('run_id', 'state', 'state_version', 'contract_sha256',
                    'graph_version', 'graph_state_schema_version', 'model_config_sha256')},
            'budget': dict(db.execute('SELECT * FROM run_budgets WHERE run_id=?', (run_id,)).fetchone()),
            'steps': [dict(row) for row in db.execute('SELECT step_id,status,sequence,error_code FROM steps WHERE run_id=? ORDER BY sequence', (run_id,))],
            'progress': [dict(row) for row in db.execute('SELECT * FROM graph_progress WHERE run_id=? ORDER BY progress_id', (run_id,))] if 'graph_progress' in tables else [],
            'event_types': [row[0] for row in db.execute('SELECT event_type FROM task_events WHERE run_id=? ORDER BY event_id', (run_id,))]}
        if 'run_results' in tables:
            payload['results'] = [dict(row) for row in db.execute('SELECT * FROM run_results WHERE run_id=?', (run_id,))]
        if 'run_verifications' in tables:
            payload['verifications'] = [dict(row) for row in db.execute('SELECT * FROM run_verifications WHERE run_id=?', (run_id,))]
        payload['budget_status'] = SchedulerStore(directory / 'business.sqlite3').budgets.status(run_id)
        return payload


async def execute(directory, origin, run_id, *, phase='single', fresh=True, route='/full', max_pages=10, recoveries=3):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from webagent.gateway.service import BrowserGateway
    from webagent.graph.runtime import StateGraphAdapter
    from webagent.models.adapter import ModelAdapter
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.sessions.manager import ManagedBrowser
    from webagent.sessions.models import SessionOwner
    from webagent.verification.service import VerificationService

    directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(directory.resolve())
    config = ModelConfig(base_url=origin, connect_seconds=1.0, read_seconds=5.0, total_seconds=8.0,
                         max_tokens=8192)
    if fresh:
        seed(directory, origin, run_id, config, route=route, max_pages=max_pages, recoveries=recoveries)
    store = SchedulerStore(settings.business_db, lease_seconds=180)
    manager = ManagedBrowser(settings, headless=True, network_config=NetworkConfig(
        webarena_endpoints=(Endpoint('http', '127.0.0.1', urlsplit(origin).port),)))
    provider = DeepSeekTransport(config, 'owned-graph-synthetic-provider-' + phase, allow_test_loopback=True)
    try:
        await manager.start()
        if not fresh:
            with connect(settings.business_db) as db:
                current = db.execute('SELECT state_version,state FROM runs WHERE run_id=?', (run_id,)).fetchone()
            assert current['state'] == 'PAUSED'
            store.resume(run_id, current['state_version'])
        generation = store.start_worker(manager.manager_id)
        token = store.claim(manager.manager_id, generation)
        assert token is not None and token.run_id == run_id
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute('PRAGMA busy_timeout=5000')
            await saver.conn.execute('PRAGMA synchronous=FULL')
            recovery, plan = None, None
            if not fresh:
                from webagent.graph.recovery import RecoveryStore
                from webagent.graph.recovery_state import load_saved_graph
                recovery = RecoveryStore(directory, budgets=store.budgets)
                plan = recovery.begin(token, await load_saved_graph(saver, run_id))
                assert plan['allowed'], plan['reason']
            owner = SessionOwner('run', run_id, 'owned-http', realm='webarena')
            session = await manager.create(owner, execution_token=token, gateway_downloads=True)
            gateway = BrowserGateway.from_managed(manager, session, scheduler=store)
            if recovery is not None:
                proof = await gateway.recovery_observe(token, plan['recovery_id'])
                if proof['source_url'] == 'about:blank':
                    await gateway.recovery_navigate(token, plan['restore_url'], plan['recovery_id'])
                    proof = await gateway.recovery_observe(token, plan['recovery_id'])
                recovery.complete(token, proof['snapshot_id'],
                    **recovery.observed_facts(token, proof['snapshot_id']))
                token = store.reconcile(run_id, token.state_version)
            adapter = ModelAdapter(settings.business_db, provider)
            verifier = VerificationService(directory, scheduler=store)
            graph = StateGraphAdapter(directory, gateway, adapter, verifier, checkpointer=saver)
            result = await asyncio.wait_for(graph.run(token), 115)
            snapshot = await graph.graph.aget_state({'configurable': {'thread_id': run_id}}) if hasattr(graph, 'graph') else None
            checkpoint = {'next': list(snapshot.next), 'values': snapshot.values} if snapshot else None
        payload = facts(directory, run_id)
        payload.update({'pid': os.getpid(), 'phase': phase, 'framework_checkpoint': checkpoint,
                        'returned_state': result if isinstance(result, dict) else None})
        return payload
    finally:
        await provider.aclose()
        await manager.aclose()


async def child(directory, origin, phase):
    payload = await execute(directory, origin, 'graph-restore', phase=phase, fresh=phase == 'A')
    destination = directory.parent / ('process-' + phase + '.json')
    destination.write_text(json.dumps(payload, indent=2) + '\n')
    print(json.dumps({'passed': True, 'pid': os.getpid(), 'phase': phase, 'state': payload['run']['state']}))


async def process(directory, origin, phase):
    command = [sys.executable, str(Path(__file__).resolve()), '--child-dir', str(directory), '--origin', origin, '--phase', phase]
    env = dict(os.environ, PYTHONUTF8='1')
    proc = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 125)
    except BaseException:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 8)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        raise
    (directory.parent / ('process-' + phase + '.stdout')).write_bytes(stdout)
    (directory.parent / ('process-' + phase + '.stderr')).write_bytes(stderr)
    assert proc.returncode == 0, ('graph child failed', phase, proc.returncode)
    return json.loads((directory.parent / ('process-' + phase + '.json')).read_text())


async def queued(directory, origin, run_id='graph-queued', *, waiting=False):
    """Use the production QueueWorker and GraphExecutor composition together."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from webagent.graph.executor import GraphExecutor
    from webagent.graph.runtime import StateGraphAdapter
    from webagent.graph.store import GraphStore
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.scheduler.worker import QueueWorker
    from webagent.sessions.manager import ManagedBrowser
    directory.mkdir(parents=True)
    settings = Settings(directory.resolve())
    config = ModelConfig(base_url=origin, connect_seconds=1.0, read_seconds=5.0, total_seconds=8.0,
                         max_tokens=8192)
    seed(directory, origin, run_id, config)
    store = SchedulerStore(settings.business_db, lease_seconds=180)
    manager = ManagedBrowser(settings, headless=True, network_config=NetworkConfig(
        webarena_endpoints=(Endpoint('http', '127.0.0.1', urlsplit(origin).port),)))
    stopped, graph_finished, executor_finished, pump = asyncio.Event(), asyncio.Event(), asyncio.Event(), None
    graphs, provider_instances = [], []
    def provider_factory(requested_run_id):
        assert requested_run_id == run_id
        provider = DeepSeekTransport(config, 'owned-queued-synthetic-key', allow_test_loopback=True)
        provider_instances.append(provider)
        return provider
    def graph_factory(*args, **kwargs):
        graph = StateGraphAdapter(*args, **kwargs)
        original_run = graph.run
        async def monitored_run(token):
            result = await original_run(token)
            graph_finished.set()  # Only after synchronous framework persistence.
            return result
        graph.run = monitored_run
        graphs.append(graph)
        return graph
    try:
        await manager.start()
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute('PRAGMA busy_timeout=5000')
            await saver.conn.execute('PRAGMA synchronous=FULL')
            executor = GraphExecutor(settings, manager, checkpointer=saver, scheduler=store,
                secret_store=object(), provider_factory=provider_factory, graph_factory=graph_factory)
            async def monitored_executor(token):
                result = await executor(token)
                executor_finished.set()  # Includes provider/browser cleanup.
                return result
            worker = QueueWorker(store, manager.manager_id, executor=monitored_executor, poll_seconds=.02,
                                 heartbeat_seconds=.2, deadline_seconds=.05)
            pump = asyncio.create_task(worker.run(stopped))
            async with asyncio.timeout(30):
                while True:
                    if pump.done():
                        await pump
                        raise AssertionError('Worker exited before terminal aggregation')
                    state = await asyncio.to_thread(GraphStore(settings.business_db).load_state, run_id)
                    business_done = state['route'] == 'wait' if waiting else state['completed']
                    if business_done and graph_finished.is_set() and executor_finished.is_set():
                        break
                    await asyncio.sleep(.05)
            stopped.set()
            await pump
            snapshot = await graphs[0].graph.aget_state({'configurable': {'thread_id': run_id}})
            payload = facts(directory, run_id)
            payload.update({'framework_checkpoint': {'next': list(snapshot.next), 'values': snapshot.values},
                'executor_instances': len(graphs), 'provider_instances': len(provider_instances),
                'worker_failed': worker._failed,
                'graph_returned': graph_finished.is_set(), 'executor_returned': executor_finished.is_set(),
                'providers_closed': all(provider._client.is_closed for provider in provider_instances)})
            return payload
    finally:
        stopped.set()
        if pump is not None and not pump.done():
            await asyncio.wait_for(pump, 8)
        await manager.aclose()


async def api_reads(directory, output, run_id='graph-queued'):
    """An actual API process reads completed graph progress under local auth."""
    import httpx
    from webagent.security import load_or_create_token
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    directory = directory.resolve()
    token = load_or_create_token(directory)
    headers = {'Authorization': 'Bearer ' + token}
    log = (output / (run_id + '-api.log')).open('wb')
    process = None
    records = []
    try:
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'uvicorn',
            'webagent.api:create_app', '--factory', '--fd', str(listener.fileno()), '--no-access-log',
            cwd=ROOT, pass_fds=(listener.fileno(),), stdout=log, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONPATH': str(ROOT / 'backend'),
                 'WEBAGENT_DATA_DIR': str(directory.resolve()), 'WEBAGENT_API_PORT': str(port)})
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=5) as client:
            async with asyncio.timeout(15):
                while True:
                    assert process.returncode is None, 'API exited during startup'
                    try:
                        response = await client.get('/health', headers=headers)
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)
            health = response.json()
            records.append({'path': '/health', 'status': response.status_code, 'response': health})
            unauthorized = await client.get('/v1/runs/' + run_id + '/progress')
            records.append({'path': '/v1/runs/' + run_id + '/progress', 'authenticated': False,
                            'status': unauthorized.status_code})
            first = await client.get('/v1/runs/' + run_id + '/progress?limit=2', headers=headers)
            assert first.status_code == 200
            body = first.json()
            later = await client.get('/v1/runs/' + run_id + '/progress?after=' + str(body['next_after']), headers=headers)
            assert later.status_code == 200
            result = await client.get('/v1/runs/' + run_id + '/result', headers=headers)
            assert result.status_code == (404 if run_id.endswith('input') else 200)
            invalid = await client.get('/v1/runs/' + run_id + '/progress?limit=0', headers=headers)
            mutation = await client.post('/v1/runs/' + run_id + '/progress', headers=headers, json={'route': 'aggregate'})
            for path, response in (('progress-first', first), ('progress-after', later),
                ('result', result), ('progress-invalid', invalid), ('progress-mutation', mutation)):
                records.append({'path': path, 'status': response.status_code, 'response': response.json()})
            return {'health': health, 'unauthenticated_status': unauthorized.status_code,
                'first': body, 'later': later.json(), 'result': result.json(),
                'invalid_status': invalid.status_code, 'mutation_status': mutation.status_code}
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
        listener.close()
        log.close()
        (output / (run_id + '-api-exchanges.json')).write_text(json.dumps(records, indent=2) + '\n')


async def verify(output, report):
    disable_external_tracing()
    def check(name, condition=True):
        assert condition, name
        report['checks'][name] = True
    fixture = await OwnedGraphFixture().start()
    outcomes = {}
    try:
        for name, route, limit, recoveries in (('success', '/full', 10, 3), ('wrong', '/wrong', 10, 3),
            ('missing', '/missing', 10, 3), ('evidence', '/full', 3, 3), ('input', '/full', 10, 3),
            ('partial', '/partial', 10, 0)):
            run_id = 'graph-' + name
            payload = await execute(output / 'domains' / name, fixture.origin, run_id,
                                    route=route, max_pages=limit, recoveries=recoveries)
            outcomes[name] = payload
            (output / (name + '.json')).write_text(json.dumps(payload, indent=2) + '\n')
            check(name + '_framework_and_business_versions_are_fixed',
                payload['run']['graph_version'] == GRAPH_VERSION and payload['run']['graph_state_schema_version'] == STATE_SCHEMA_VERSION)
        check('actual_page_facts_can_succeed_only_through_verifier', outcomes['success']['run']['state'] == 'SUCCEEDED'
              and outcomes['success']['results'])
        check('wrong_page_action_is_recorded_before_real_navigation', outcomes['wrong']['run']['state'] == 'SUCCEEDED'
              and any(row['step_id'].startswith('model-navigation-') and row['status'] == 'COMPLETED' for row in outcomes['wrong']['steps']))
        missing_phases = [row['phase'] for row in outcomes['missing']['progress']]
        check('missing_critical_field_requires_verification_and_new_observation', outcomes['missing']['run']['state'] == 'SUCCEEDED'
              and missing_phases.count('verify') >= 2 and missing_phases.count('observe') >= 2)
        check('persistent_evidence_request_is_stopped_by_original_run_budget', outcomes['evidence']['run']['state'] == 'FAILED'
              and outcomes['evidence']['budget_status']['reason'] == 'recovery_limit'
              and max(json.loads(outcomes['evidence']['budget']['recovery_counts_json']).values()) == 3)
        check('input_request_has_durable_wait_and_never_success', outcomes['input']['run']['state'] == 'PAUSED'
              and 'wait_registered' in outcomes['input']['event_types']
              and outcomes['input']['framework_checkpoint']['next'] == ['wait'])
        check('framework_saves_only_refs_without_model_reply_or_authority', all(
            not ({'output', 'action', 'visible_excerpt', 'provider', 'token', 'api_key'}
                 & set(payload['framework_checkpoint']['values'])) for payload in outcomes.values()))
        verified_values = json.loads(outcomes['success']['results'][0]['result_json'])['items']['values']
        check('final_output_matches_fresh_independent_source_values',
              [item['normalized_value'] for item in verified_values] == [item['normalized_value'] for item in fixture.values])
        partial_result = json.loads(outcomes['partial']['results'][0]['result_json'])
        check('exhausted_supplement_keeps_verified_deliverable_and_aggregates_partial',
              outcomes['partial']['run']['state'] == 'PARTIAL' and partial_result['outcome'] == 'PARTIAL'
              and partial_result['generated_by'] == 'business_aggregator' and partial_result['unresolved']
              and outcomes['partial']['budget_status']['reason'] == 'recovery_limit')
        check('partial_aggregation_preserves_original_failed_critical_checks',
              any(item['verdict'] != 'PASS' for item in partial_result['checks'])
              and outcomes['partial']['verifications'])
        directory = output / 'domains' / 'restore'
        first = await process(directory, fixture.origin, 'A')
        check('first_os_process_persists_wait_and_exits', first['run']['state'] == 'PAUSED'
              and first['framework_checkpoint']['next'] == ['wait'])
        fixture.recovery_phase = 'B'
        second = await process(directory, fixture.origin, 'B')
        check('second_os_process_uses_same_run_and_succeeds', first['pid'] != second['pid']
              and first['run']['run_id'] == second['run']['run_id'] and second['run']['state'] == 'SUCCEEDED')
        restore_calls = [row for row in fixture.provider_requests if row['run_id'] == 'graph-restore']
        check('provider_is_replaced_and_observation_is_fresh_after_restore', {row['provider_phase'] for row in restore_calls} == {'A', 'B'}
              and len({row['snapshot_id'] for row in restore_calls}) >= 2)
        check('restored_process_reuses_immutable_configuration', first['run']['contract_sha256'] == second['run']['contract_sha256']
              and first['run']['model_config_sha256'] == second['run']['model_config_sha256'])
        check('waiting_action_is_never_replayed_after_new_process', len([row for row in second['steps']
              if row['step_id'].startswith('model-navigation-')]) == 0
              and second['steps'] == first['steps']
              and second['budget_status']['observations_used'] > first['budget_status']['observations_used'])
        check('restore_retains_original_budget_and_adds_actual_calls',
              second['budget']['budget_record_id'] == first['budget']['budget_record_id']
              and second['budget']['model_calls_used'] == first['budget']['model_calls_used'] + 1
              and second['budget']['active_ms'] >= first['budget']['active_ms'])
        queue_payload = await queued(output / 'domains' / 'queued', fixture.origin)
        (output / 'queued.json').write_text(json.dumps(queue_payload, indent=2) + '\n')
        check('queue_worker_dispatches_production_executor_and_graph', queue_payload['run']['state'] == 'SUCCEEDED'
              and queue_payload['executor_instances'] == 1 and queue_payload['provider_instances'] == 1
              and not queue_payload['worker_failed'])
        check('worker_completion_closes_provider_and_finishes_queue', queue_payload['providers_closed']
              and queue_payload['framework_checkpoint']['next'] == []
              and queue_payload['graph_returned'] and queue_payload['executor_returned'])
        queued_wait = await queued(output / 'domains' / 'queued-input', fixture.origin, 'graph-queued-input', waiting=True)
        (output / 'queued-input.json').write_text(json.dumps(queued_wait, indent=2) + '\n')
        check('worker_wait_drains_graph_interrupt_before_settling', queued_wait['run']['state'] == 'PAUSED'
              and queued_wait['framework_checkpoint']['next'] == ['wait']
              and queued_wait['graph_returned'] and queued_wait['executor_returned'])
        check('worker_wait_closes_provider_and_retains_durable_registration', queued_wait['providers_closed']
              and not queued_wait['worker_failed'] and 'wait_registered' in queued_wait['event_types'])
        api = await api_reads(output / 'domains' / 'queued', output)
        check('api_declares_worker_graph_and_authenticated_progress', api['health']['stage'] == 'M1-25'
              and api['health']['graph'] == 'worker_custom_stategraph'
              and api['health']['graph_progress'] == 'authenticated_read_only'
              and api['health']['task_execution_enabled'] is True)
        check('progress_requires_local_api_authentication', api['unauthenticated_status'] == 401)
        check('progress_read_uses_bounded_monotonic_cursor', len(api['first']['progress']) == 2
              and all(row['progress_id'] > api['first']['next_after'] for row in api['later']['progress']))
        check('progress_cannot_invoke_or_mutate_graph', api['invalid_status'] == 422 and api['mutation_status'] == 405)
        check('authenticated_result_has_business_aggregator_outcome', api['result']['result']['outcome'] == 'SUCCEEDED'
              and api['result']['result']['generated_by'] == 'business_aggregator')
        input_api = await api_reads(output / 'domains' / 'input', output, 'graph-input')
        check('input_request_field_names_survive_authenticated_process_read',
              input_api['first']['input_request']['requested_fields'] == ['user_confirmation']
              and input_api['first']['input_request']['wait_id'] == outcomes['input']['returned_state']['wait_id'])
        check('all_model_outbound_calls_use_owned_http_without_tools', bool(fixture.provider_requests)
              and all(row['tool_authority_absent'] for row in fixture.provider_requests))
        check('no_browser_write_is_sent', not any(row['method'] == 'POST' and row['path'] != '/chat/completions' for row in fixture.requests))
        check('fixture_server_has_no_errors', not fixture.errors)
        report['scope_counts'] = {'managed_graph_domains': 9, 'real_child_processes': 4,
            'model_http_requests': len(fixture.provider_requests), 'synthetic_origins': 1}
        report['passed'] = True
    finally:
        await fixture.close()
        (output / 'http-records.json').write_text(json.dumps({'requests': fixture.requests,
            'provider_requests': fixture.provider_requests, 'errors': fixture.errors}, indent=2) + '\n')
        (output / 'generated-source.json').write_text(canonical_json(fixture.document()) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--child-dir', type=Path)
    parser.add_argument('--origin')
    parser.add_argument('--phase', choices=('A', 'B'))
    args = parser.parse_args()
    if args.child_dir:
        asyncio.run(child(args.child_dir, args.origin, args.phase))
        return 0
    assert args.output_dir is not None
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-16', 'passed': False, 'checks': {}, 'created_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'New isolated SQLite, managed headless Chromium, owned HTTP source and provider; safe durable wait restored in a new OS process. This probe covers safe wait recovery; crash-window coverage is in verify_recovery.py.'}
    try:
        asyncio.run(verify(args.output_dir, report))
    except Exception as error:
        report['error_type'] = type(error).__name__
        if hasattr(error, 'code'):
            report['error'] = {'code': error.code, 'field': error.field, 'status': error.status}
        report['error_locations'] = [{'file': Path(frame.filename).name, 'line': frame.lineno,
            'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]
    report['artifact_sha256'] = {str(path.relative_to(args.output_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(args.output_dir.rglob('*')) if path.is_file() and '.security' not in path.parts}
    (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(args.output_dir / 'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
