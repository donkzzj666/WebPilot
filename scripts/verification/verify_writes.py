#!/usr/bin/env python3
"""M1-23 / FR-03: isolated Chromium writes and independent-process recovery.

The external object belongs to this probe's HTTP server. Its SQLite log survives
worker SIGKILL and is also the visible source for read-only reconciliation. No
GitHub, user profile, paid provider or evaluator-only reference is accessed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import html
import inspect
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import traceback
from urllib.parse import parse_qs, urlsplit
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).resolve().parent)]
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

from verify_gateway import action, seed_run, synthetic_identity
from webagent.config import Settings, disable_external_tracing
from webagent.db import connect, migrate, transaction
from webagent.db.repository import canonical_json, create_run, utc_text
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore

CHANGE_SHA = hashlib.sha256(b'Owned M1-23 synthetic content v1').hexdigest()
PRECONDITION = 'owned-object-v1'
ADAPTER = 'owned-write-probe-v1'
FAULTS = ('intent_committed', 'physical_applied', 'check_captured')
CASES = ('intent-not-applied', 'physical-applied', 'physical-unknown',
         'object-changed', 'version-changed', 'during-check', 'cancelled-write')


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def error_details(error, seen=None):
    seen = set() if seen is None else seen
    if id(error) in seen:
        return {'type': type(error).__name__, 'cycle': True}
    seen.add(id(error))
    result = {'type': type(error).__name__, 'message': str(error), 'locations': [
        {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
        for frame in traceback.extract_tb(error.__traceback__)]}
    if isinstance(error, OSError):
        result['errno'] = error.errno
    if isinstance(error, BusinessError):
        result.update(code=error.code, field=error.field, status=error.status)
    if isinstance(error, BaseExceptionGroup):
        result['exceptions'] = [error_details(child, seen) for child in error.exceptions]
    if error.__cause__ is not None:
        result['cause'] = error_details(error.__cause__, seen)
    elif error.__context__ is not None and not error.__suppress_context__:
        result['context'] = error_details(error.__context__, seen)
    return result


def claim(identity):
    return {'identity_ref': identity, 'target': {
        'repository': 'Fixture/Gateway', 'branch': 'gateway-fixture',
        'base_sha': 'a' * 40, 'operation': 'edit_file', 'files': ['src/fixture.txt']},
        'expected_change_sha256': CHANGE_SHA, 'precondition_version': PRECONDITION,
        'adapter_id': ADAPTER}


class WriteFixture:
    """A real synthetic external object with its own durable mutation log."""
    def __init__(self, output):
        self.path = Path(output) / 'owned-external.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute("CREATE TABLE objects(alias TEXT PRIMARY KEY, business_key TEXT UNIQUE, facts TEXT NOT NULL, mode TEXT NOT NULL, content TEXT NOT NULL DEFAULT 'Synthetic original content')")
            db.execute('CREATE TABLE submissions(sequence INTEGER PRIMARY KEY, alias TEXT NOT NULL, business_key TEXT NOT NULL, applied INTEGER NOT NULL)')
            db.execute('CREATE TABLE receipts(business_key TEXT PRIMARY KEY, alias TEXT NOT NULL, receipt TEXT NOT NULL)')
        self.active, self.requests, self.errors = set(), [], []

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.port}'

    @property
    def url(self):
        return self.origin + '/fixture/' + self.seed_alias

    def bind(self, alias, task_id, identity):
        from webagent.writes.models import business_key
        facts = claim(identity)
        key = business_key(task_id, facts)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO objects(alias,business_key,facts,mode) VALUES(?,?,?,?)', (alias, key, canonical_json(facts), 'visible'))
        return key

    def change(self, alias, change):
        with sqlite3.connect(self.path) as db:
            facts = json.loads(db.execute('SELECT facts FROM objects WHERE alias=?', (alias,)).fetchone()[0])
            if change == 'object':
                facts['target']['repository'] = 'Fixture/Changed'
            elif change == 'version':
                facts['precondition_version'] = 'owned-object-v2'
            elif change != 'unknown':
                raise ValueError('Unknown fixed object change')
            db.execute('UPDATE objects SET facts=?,mode=? WHERE alias=?',
                (canonical_json(facts), 'unknown' if change == 'unknown' else 'visible', alias))

    def facts(self, alias):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT business_key,facts,mode FROM objects WHERE alias=?', (alias,)).fetchone()
            if row is None:
                raise ValueError('Unregistered owned object')
            found = db.execute('SELECT receipt FROM receipts WHERE business_key=?', (row[0],)).fetchone()
        body = json.loads(row[1])
        body.pop('adapter_id')
        body.update(outcome='UNKNOWN' if row[2] == 'unknown' else ('APPLIED' if found else 'NOT_APPLIED'),
            observed_version=body['precondition_version'], receipt=json.loads(found[0]) if found else None)
        return body

    def apply(self, alias, fields):
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT business_key,facts FROM objects WHERE alias=?', (alias,)).fetchone()
            if row is None or fields != {'business_key': [row[0]]}:
                raise ValueError('The form does not identify its owned stable intent')
            existing = db.execute('SELECT receipt FROM receipts WHERE business_key=?', (row[0],)).fetchone()
            receipt = json.loads(existing[0]) if existing else {
                'business_key': row[0], 'object_id': alias, 'change_sha256': CHANGE_SHA,
                'receipt_version': 'owned-receipt-v1'}
            db.execute('INSERT INTO submissions(alias,business_key,applied) VALUES(?,?,?)', (alias, row[0], int(existing is None)))
            if existing is None:
                db.execute('INSERT INTO receipts VALUES(?,?,?)', (row[0], alias, canonical_json(receipt)))
                db.execute('UPDATE objects SET content=? WHERE alias=?', ('Owned M1-23 synthetic content v1', alias))
        return receipt

    def counts(self, alias):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT count(*),COALESCE(sum(applied),0) FROM submissions WHERE alias=?', (alias,)).fetchone()
        return {'posts': row[0], 'applied': row[1]}

    def content(self, alias):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT content FROM objects WHERE alias=?', (alias,)).fetchone()[0]

    def page(self, alias, *, status=False):
        facts = self.facts(alias)
        with sqlite3.connect(self.path) as db:
            key = db.execute('SELECT business_key FROM objects WHERE alias=?', (alias,)).fetchone()[0]
        visible = canonical_json(facts)
        form = '' if status else ('<form action="/fixture/apply/' + html.escape(alias, quote=True)
            + '" method="post"><input type="hidden" name="business_key" value="' + html.escape(key, quote=True)
            + '"><button id="save" type="submit" aria-label="Save owned change"></button></form>')
        return ('<!doctype html><meta charset="utf-8"><meta name="fixture-account" content="fixture-user"><title>Owned M1-23 object</title>'
            '<pre id="facts">' + html.escape(visible) + '</pre>' + form).encode()

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            if len(raw) > 65536:
                raise ValueError('Request header exceeds fixture limit')
            lines = raw.decode('ascii').split('\r\n')
            method, target, _ = lines[0].split(' ', 2)
            headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip() for line in lines[1:] if ':' in line}
            size = int(headers.get('content-length', '0'))
            if not 0 <= size <= 4096:
                raise ValueError('Request body exceeds fixture limit')
            body = await asyncio.wait_for(reader.readexactly(size), 5) if size else b''
            path, status, extra = urlsplit(target).path, '200 OK', ''
            parts = path.strip('/').split('/')
            if method == 'POST' and len(parts) == 3 and parts[:2] == ['fixture', 'apply']:
                self.apply(parts[2], parse_qs(body.decode('ascii'), strict_parsing=True))
                status, response, extra = '303 See Other', b'', 'Location: /fixture/status/' + parts[2] + '\r\n'
            elif method == 'GET' and len(parts) == 3 and parts[:2] == ['fixture', 'status']:
                response = self.page(parts[2], status=True)
            elif method == 'GET' and len(parts) == 2 and parts[0] == 'fixture':
                response = self.page(parts[1])
            else:
                status, response = '404 Not Found', b'Owned M1-23 route unavailable'
            self.requests.append({'method': method, 'path': path, 'response_sha256': hashlib.sha256(response).hexdigest()})
            writer.write(('HTTP/1.1 ' + status + '\r\nContent-Type: text/html; charset=utf-8\r\n'
                'Cache-Control: no-store\r\nConnection: close\r\n' + extra + 'Content-Length: '
                + str(len(response)) + '\r\n\r\n').encode() + response)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as error:
            self.errors.append({'type': type(error).__name__})
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
        tasks = tuple(self.active)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class Fault:
    def __init__(self, directory, phase, run_id, point):
        if point is not None and point not in FAULTS:
            raise ValueError('Unknown fixed write boundary')
        self.path, self.run_id, self.point = directory / ('marker-' + phase + '.json'), run_id, point
        self.hit = False

    def stop(self, stage):
        if not self.hit and self.point == stage:
            self.hit = True
            write_json(self.path, {'pid': os.getpid(), 'run_id': self.run_id, 'stage': stage})
            os.kill(os.getpid(), signal.SIGSTOP)


class OwnedAuth:
    def load(self, reference, **bindings):
        assert bindings['site_id'] == 'local-fixture' and bindings['realm'] == 'webarena'
        return {'cookies': [], 'origins': []}


def ledger(directory, run_id):
    with connect(directory / 'business.sqlite3') as db:
        run = dict(db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone())
        tables = {}
        for name in ('steps', 'task_events', 'write_intents', 'resource_quarantines',
                     'gateway_attempts', 'budget_attempts', 'evidence', 'run_results'):
            tables[name] = [dict(row) for row in db.execute('SELECT * FROM ' + name)]
        for name in ('write_protocol_claims', 'write_protocol_checks', 'write_protocol_dispatches',
                     'write_protocol_check_evidence', 'write_protocol_links'):
            if db.execute('SELECT 1 FROM sqlite_schema WHERE type=\'table\' AND name=?', (name,)).fetchone():
                tables[name] = [dict(row) for row in db.execute('SELECT * FROM ' + name)]
        return {'run': run, 'tables': tables,
            'budget': dict(db.execute('SELECT * FROM run_budgets WHERE run_id=?', (run_id,)).fetchone()),
            'integrity': db.execute('PRAGMA integrity_check').fetchone()[0],
            'foreign_keys': [list(row) for row in db.execute('PRAGMA foreign_key_check')]}


def seed(directory, fixture, alias):
    directory.mkdir(parents=True)
    settings = Settings(directory)
    migrate(settings.business_db)
    store = SchedulerStore(settings.business_db, lease_seconds=180)
    identity = synthetic_identity(settings.business_db, fixture)
    fixture.seed_alias = alias
    run_id = 'writes-' + alias
    contract = seed_run(settings.business_db, store, fixture, run_id, identity_ref=identity,
                        limits={'max_actions': 25, 'max_active_seconds': 120})
    key = fixture.bind(alias, contract['task_id'], identity)
    write_json(directory / 'owned-input.json', {'alias': alias, 'identity_ref': identity,
        'run_id': run_id, 'task_id': contract['task_id'], 'claim': claim(identity), 'business_key': key})
    return run_id


def related_run(directory, source_run_id, *, reconcile=False):
    """Trusted fixture retry: same Task and frozen contract, fresh Run budget."""
    run_id = source_run_id + '-retry'
    path = directory / 'business.sqlite3'
    with connect(path) as db, transaction(db):
        old = db.execute('SELECT * FROM runs WHERE run_id=?', (source_run_id,)).fetchone()
        assert old['state'] in ('SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED')
        create_run(db, run_id=run_id, task_id=old['task_id'], contract_version=old['contract_version'],
            graph_version=old['graph_version'], graph_state_schema_version=old['graph_state_schema_version'],
            model_config_sha256=old['model_config_sha256'], runtime_config_sha256=old['runtime_config_sha256'],
            parent_run_id=source_run_id)
        resources = [Resource(row['resource_type'], row['resource_key'], bool(row['logical_hold']))
            for row in db.execute('SELECT * FROM scheduler_requirements WHERE run_id=?', (source_run_id,))
            if row['resource_type'] != 'browser_context']
        resources.append(Resource.browser_context(run_id))
    scheduler = SchedulerStore(path, lease_seconds=180)
    if reconcile:
        from webagent.sessions.store import SessionRegistry
        # Source was killed as a complete owned process group. This is the
        # normal metadata-only startup orphan recovery, with no page access.
        SessionRegistry(path).recover_orphans('owned-write-retry-registration')
        scheduler.enqueue_write_reconciliation(run_id, 0, resources, source_run_id=source_run_id)
    else:
        scheduler.enqueue(run_id, resources, expected_state_version=0, queue_class='webarena')
    return run_id


def cancel_crashed_run(directory, run_id):
    from webagent.controls.models import ControlRequest
    from webagent.controls.store import ControlStore
    from uuid import uuid4
    scheduler = SchedulerStore(directory / 'business.sqlite3', lease_seconds=180)
    # The fixed owned Worker identity restarts its own generation, revoking the
    # actually killed process before a tokenless idle control is completed.
    scheduler.start_worker('owned-write-worker')
    with connect(directory / 'business.sqlite3') as db:
        version = db.execute('SELECT state_version FROM runs WHERE run_id=?', (run_id,)).fetchone()[0]
    controls = ControlStore(directory, scheduler=scheduler)
    accepted = controls.request(run_id, 'cancel', ControlRequest(expected_state_version=version,
        contract_version=1, settings_version=0), uuid4().hex)['operation']
    applied = controls.apply_idle(run_id, expected_operation_id=accepted['operation_id'])
    assert applied['status'] == 'APPLIED' and applied['state'] == 'CANCELLED'
    write_json(directory / 'cancel-receipt.json', applied)
    return ledger(directory, run_id)


class WriteNodeState(TypedDict, total=False):
    run_id: str
    step_id: str
    epoch: int
    operation_id: str
    status: str
    reused: bool


async def invoke_write_node(directory, gateway, token, snapshot, scope, phase):
    """One actual durable LangGraph node over the production action gateway.

    Fresh node input never resumes an old click task. Tokens, adapters, page
    facts and action candidates remain in this process-only closure.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import StateGraph, START, END
    outcomes = {}
    async def dispatch_node(state):
        result = await gateway.dispatch(token, action(token, snapshot, state['step_id'], 'click',
            locator={'strategy': 'dom', 'attribute': 'id', 'value': 'save'}, write_scope=scope))
        outcomes['dispatch'] = result
        return {'operation_id': result['operation_id'], 'status': result['status'],
                'reused': bool(result.get('reused', result.get('duplicate', False)))}
    builder = StateGraph(WriteNodeState)
    builder.add_node('dispatch_write', dispatch_node)
    builder.add_edge(START, 'dispatch_write')
    builder.add_edge('dispatch_write', END)
    async with AsyncSqliteSaver.from_conn_string(str(directory / 'graph.sqlite3')) as saver:
        await saver.setup()
        await saver.conn.execute('PRAGMA synchronous=FULL')
        graph = builder.compile(checkpointer=saver, name='owned-write-protocol-node-v1')
        state = await graph.ainvoke({'run_id': token.run_id, 'step_id': phase + '-write', 'epoch': token.epoch},
            config={'configurable': {'thread_id': token.run_id}}, durability='sync')
        saved = await graph.aget_state({'configurable': {'thread_id': token.run_id}})
        assert not saved.next and saved.values == state
        outcomes['saved_node'] = dict(next=list(saved.next), values=state)
    return outcomes


async def ordinary_navigation_ready(gateway, token, url):
    """Use the production pacing journal and clock before a normal GET.

    Query GETs are independently counted by their own gateway. Waiting here
    retains the original Run budget; no interval, debit or permission is reset.
    """
    contract, _ = await asyncio.to_thread(gateway._qualified, token)
    source = next(source for source in contract.sources if source.permits(url))
    budgets = gateway.scheduler.budgets
    def paced():
        site = budgets._site(token, source.site_id)
        with connect(gateway.path) as db:
            row = db.execute('SELECT * FROM site_pacing WHERE site_id=?', (site,)).fetchone()
        return row is not None and budgets._paced(row['last_utc'], row['last_mono_ns'],
            row['clock_domain'], budgets._stamp(),
            max(row['interval_seconds'], contract.budget_profile.min_site_interval_seconds))
    while True:
        token, status = await asyncio.to_thread(gateway.scheduler.runtime_budget, token)
        if status['exhausted']:
            await asyncio.to_thread(gateway.scheduler.expire_budget, token.run_id, status['reason'])
            raise BusinessError('BUDGET_EXCEEDED', 'Owned write Run exhausted while waiting for site pacing', status=409)
        if not await asyncio.to_thread(paced):
            return token
        await asyncio.sleep(.1)


async def child(directory, origin, run_id, phase, point=None, *, dispatch=False, finish=False):
    from webagent.gateway.permissions import WriteAuthorization
    from webagent.gateway.service import BrowserGateway
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.sessions.manager import ManagedBrowser
    from webagent.sessions.models import SessionOwner
    from webagent.writes.service import WriteCheckCapture
    from webagent.writes.store import WriteProtocolStore
    disable_external_tracing()
    settings, fault = Settings(directory), Fault(directory, phase, run_id, point)
    inputs = json.loads((directory / 'owned-input.json').read_text())
    store = SchedulerStore(settings.business_db, lease_seconds=180)
    manager = ManagedBrowser(settings, headless=True, auth_store=OwnedAuth(), network_config=NetworkConfig(
        webarena_endpoints=(Endpoint('http', '127.0.0.1', urlsplit(origin).port),)))
    gateway = token = None
    error, outcomes = None, {}
    read_edit_page, latest_snapshot = False, None
    try:
        await manager.start()
        generation = store.start_worker('owned-write-worker')
        token = store.claim('owned-write-worker', generation)
        assert token is not None and token.run_id == run_id, 'No current write-query qualification'
        owner = SessionOwner('run', run_id, 'local-fixture', inputs['identity_ref'], realm='webarena')
        session = await manager.create(owner, execution_token=token)
        # Adapter reads real controlled DOM facts; no model/page content can
        # install this callable or choose an endpoint outside the frozen scope.
        async def authorize(value, snapshot, prepared):
            context = await manager.context(session.session_id, owner, execution_token=token)
            page = context.pages[0]
            facts = json.loads(await page.locator('#facts').inner_text())
            if await page.locator('meta[name="fixture-account"]').get_attribute('content') != 'fixture-user':
                raise BusinessError('STATE_CONFLICT', 'Owned account changed', status=409)
            expected = inputs['claim']
            if (facts['identity_ref'] != expected['identity_ref'] or facts['target'] != expected['target']
                    or facts['precondition_version'] != expected['precondition_version']
                    or facts['expected_change_sha256'] != expected['expected_change_sha256']):
                raise BusinessError('STATE_CONFLICT', 'Owned write object changed', status=409)
            target = expected['target']
            return WriteAuthorization(target['repository'], target['branch'], target['base_sha'], target['operation'],
                tuple(target['files']), expected['identity_ref'], (('POST', origin + '/fixture/apply/' + inputs['alias']),),
                adapter_id=ADAPTER, expected_change_sha256=CHANGE_SHA, precondition_version=PRECONDITION)
        async def verify(operation, surface):
            nonlocal latest_snapshot
            snapshot = (await surface.observe() if read_edit_page else
                await surface.navigate(origin + '/fixture/status/' + inputs['alias']))
            latest_snapshot = snapshot
            capture = surface.capture(snapshot['snapshot_id'])
            if inspect.isawaitable(capture):
                capture = await capture
            facts = json.loads(capture['visible_text'])
            facts['snapshot_id'] = snapshot['snapshot_id']
            fault.stop('check_captured')
            if point == 'check_captured':
                # Parent changes the owned object while this process is stopped.
                # A new real read publishes the changed head before old facts
                # are returned, exercising the proof/commit version boundary.
                await surface.navigate(origin + '/fixture/status/' + inputs['alias'])
            return WriteCheckCapture(data=canonical_json(facts).encode(), source_url=snapshot['source_url'],
                                     snapshot_id=snapshot['snapshot_id'])
        gateway = BrowserGateway.from_managed(manager, session, scheduler=store,
                                             write_authorizer=authorize, write_verifier=verify)
        protocol = WriteProtocolStore(settings.business_db)
        with connect(settings.business_db) as db:
            operation = db.execute('SELECT operation_id FROM write_intents WHERE task_id=?', (inputs['task_id'],)).fetchone()
        if operation is not None:
            outcomes['check'] = await gateway.reconcile_write(token, operation[0])
            with connect(settings.business_db) as db:
                status = db.execute('SELECT status FROM write_intents WHERE operation_id=?', (operation[0],)).fetchone()[0]
            if status == 'UNKNOWN' or status == 'INTENT':
                try:
                    await gateway.navigate(token, origin + '/fixture/' + inputs['alias'], phase + '-forbidden')
                except BusinessError as rejection:
                    outcomes['normal_dispatch_rejected'] = rejection.code
                else:
                    raise AssertionError('Unknown write admitted normal browser dispatch')
                try:
                    store.reconcile(run_id, token.state_version)
                except BusinessError as rejection:
                    outcomes['success_authority_rejected'] = rejection.code
                else:
                    raise AssertionError('Unknown write admitted success execution authority')
                return
            if ledger(directory, run_id)['run']['state'] == 'RECONCILING':
                token = store.reconcile(run_id, token.state_version)
                assert store.validate(token)['state'] == 'RUNNING'
        if dispatch:
            edit_url = origin + '/fixture/' + inputs['alias']
            token = await ordinary_navigation_ready(gateway, token, edit_url)
            await gateway.navigate(token, edit_url, phase + '-bootstrap')
            snapshot = await gateway.observe(token)
            if operation is not None:
                with connect(settings.business_db) as db:
                    known_status = db.execute('SELECT status FROM write_intents WHERE operation_id=?', (operation[0],)).fetchone()[0]
                if known_status == 'CONFIRMED':
                    read_edit_page = True
                    outcomes['current_edit_check'] = await gateway.reconcile_write(token, operation[0])
                    snapshot = latest_snapshot
            scope = {**inputs['claim']['target'], 'identity_ref': inputs['identity_ref'],
                'operation_id': phase + '-model-hint', 'target_rechecked_at': snapshot['captured_at']}
            prepare = gateway.store.prepare
            def prepare_with_crash(*args, **kwargs):
                intent = prepare(*args, **kwargs)
                if intent.get('dispatch_allowed'):
                    fault.stop('intent_committed')
                return intent
            gateway.store.prepare = prepare_with_crash
            execute = gateway.browser.execute
            async def execute_with_crash(*args, **kwargs):
                result = await execute(*args, **kwargs)
                fault.stop('physical_applied')
                return result
            gateway.browser.execute = execute_with_crash
            outcomes.update(await invoke_write_node(directory, gateway, token, snapshot, scope, phase))
            with connect(settings.business_db) as db:
                op = db.execute('SELECT operation_id FROM write_intents WHERE task_id=?', (inputs['task_id'],)).fetchone()[0]
            outcomes['operation'] = protocol.get(op)
            outcomes['post_dispatch_check'] = await gateway.reconcile_write(token, op)
        if finish:
            store.finish(token, 'FAILED')
    except Exception as caught:
        error = error_details(caught)
    finally:
        cleanup_errors = []
        if gateway is not None:
            try:
                await gateway.browser.aclose()
            except Exception as caught:
                cleanup_errors.append(error_details(caught))
        if token is not None:
            try:
                store.abandon(token)
            except BusinessError:
                pass
        try:
            await manager.aclose()
        except Exception as caught:
            cleanup_errors.append(error_details(caught))
        payload = ledger(directory, run_id)
        payload.update(pid=os.getpid(), error=error, cleanup_errors=cleanup_errors, outcomes=outcomes, phase=phase)
        write_json(directory / ('process-' + phase + '.json'), payload)


async def launch(directory, origin, run_id, phase, *, point=None, kill=False, dispatch=False, finish=False, fixture=None):
    command = [sys.executable, str(Path(__file__).resolve()), '--child-dir', str(directory), '--origin', origin,
               '--run-id', run_id, '--phase', phase]
    if point:
        command += ['--fault-point', point]
    if dispatch:
        command += ['--dispatch']
    if finish:
        command += ['--finish']
    proc = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env={**os.environ, 'PYTHONUTF8': '1'},
        start_new_session=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        if point:
            async with asyncio.timeout(30):
                marker = directory / ('marker-' + phase + '.json')
                while not marker.exists():
                    if proc.returncode is not None:
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
                        (directory / ('process-' + phase + '.stdout')).write_bytes(stdout)
                        (directory / ('process-' + phase + '.stderr')).write_bytes(stderr)
                        payload = directory / ('process-' + phase + '.json')
                        details = json.loads(payload.read_text()).get('error') if payload.exists() else None
                        raise AssertionError('Owned child exited before the requested write boundary: ' + canonical_json(details))
                    await asyncio.sleep(.02)
            assert json.loads(marker.read_text()) == {'pid': proc.pid, 'run_id': run_id, 'stage': point}
            assert os.getpgid(proc.pid) == proc.pid
            if kill:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                assert point == 'check_captured' and fixture is not None
                inputs = json.loads((directory / 'owned-input.json').read_text())
                fixture.change(inputs['alias'], 'object')
                os.kill(proc.pid, signal.SIGCONT)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 50)
        (directory / ('process-' + phase + '.stdout')).write_bytes(stdout)
        (directory / ('process-' + phase + '.stderr')).write_bytes(stderr)
        if kill:
            assert proc.returncode == -signal.SIGKILL
            # The baseline is read after actual OS exit, never from a process
            # merely stopped at a convenient pre-crash observation point.
            before = ledger(directory, run_id)
            before['crash'] = {'pid': proc.pid, 'stage': point, 'signal': 'SIGKILL'}
            write_json(directory / ('before-' + phase + '.json'), before)
            return before
        assert proc.returncode == 0, 'Owned write child failed'
        return json.loads((directory / ('process-' + phase + '.json')).read_text())
    finally:
        if proc.returncode is None:
            primary = sys.exc_info()[1]
            try:
                # Only a still-live Popen child whose current group matches
                # this launch's dedicated session may receive cleanup signals.
                if os.getpgid(proc.pid) != proc.pid:
                    raise RuntimeError('Owned child process group changed')
                os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.wait_for(proc.wait(), 10)
            except ProcessLookupError:
                await asyncio.wait_for(proc.wait(), 10)
            except Exception as cleanup_error:
                write_json(directory / ('cleanup-' + phase + '.json'), error_details(cleanup_error))
                if primary is None:
                    raise


def preserve(before, after):
    for key in ('run_id', 'task_id', 'contract_sha256', 'graph_version',
                'graph_state_schema_version', 'model_config_sha256', 'runtime_config_sha256'):
        assert before['run'][key] == after['run'][key]
    assert before['budget']['budget_record_id'] == after['budget']['budget_record_id']
    for key in ('actions_used', 'content_pages_used', 'observations_used', 'screenshots_used',
                'model_calls_used', 'active_ms', 'ci_wait_ms'):
        assert after['budget'][key] >= before['budget'][key]
    for table, key in (('task_events', 'event_id'), ('budget_attempts', 'attempt_id'), ('gateway_attempts', 'step_id')):
        previous = {row[key]: row for row in before['tables'][table]}
        current = {row[key]: row for row in after['tables'][table]}
        assert previous.items() <= current.items(), table
    for table in ('write_protocol_claims', 'write_protocol_dispatches', 'write_protocol_checks'):
        assert all(row in after['tables'].get(table, []) for row in before['tables'].get(table, []))
    current_steps = {row['step_id']: row for row in after['tables']['steps']}
    assert all(current_steps[row['step_id']] == row for row in before['tables']['steps'] if row['status'] != 'INTENT')
    current_ops = {row['operation_id']: row for row in after['tables']['write_intents']}
    for row in before['tables']['write_intents']:
        for key in ('business_key', 'task_id', 'originating_run_id', 'target', 'expected_change', 'identity_ref', 'precondition_version'):
            assert row[key] == current_ops[row['operation_id']][key]
    assert after['integrity'] == 'ok' and not after['foreign_keys']


def assert_dispatch_budgets(value):
    attempts = {(row['run_id'], row['attempt_id']): row for row in value['tables']['budget_attempts']}
    writes = [row for row in value['tables']['gateway_attempts'] if row['external_write']]
    dispatches = value['tables'].get('write_protocol_dispatches', [])
    assert {row['step_id'] for row in writes} == {row['step_id'] for row in dispatches}
    for row in writes:
        key = 'gateway-' + hashlib.sha256(canonical_json([row['run_id'], row['step_id']]).encode()).hexdigest()
        counted = attempts[(row['run_id'], key)]
        assert counted['kind'] == 'action' and counted['actions'] == 1 and counted['epoch'] == row['epoch']
    run_id = value['run']['run_id']
    assert value['budget']['actions_used'] == sum(row['actions'] for row in attempts.values() if row['run_id'] == run_id)
    return len(writes)


def require_clean_process(value):
    assert value['error'] is None, 'Owned child failed: ' + canonical_json(value['error'])
    assert not value.get('cleanup_errors'), 'Owned child cleanup failed: ' + canonical_json(value.get('cleanup_errors'))


async def verify(output, report, selected=None):
    disable_external_tracing()
    def check(name, condition):
        assert condition, name
        report['checks'][name] = True
    fixture = await WriteFixture(output).start()
    matrix = []
    try:
        for case in CASES:
            if selected and case not in selected:
                continue
            directory = output / 'domains' / case
            run_id = seed(directory, fixture, case)
            point = 'intent_committed' if case in ('intent-not-applied', 'version-changed') else 'physical_applied'
            before = await launch(directory, fixture.origin, run_id, 'A', point=point, kill=True, dispatch=True)
            check(case + '_has_one_committed_intent', len(before['tables']['write_intents']) == 1
                  and before['tables']['write_intents'][0]['status'] == 'INTENT')
            expected_posts = int(point == 'physical_applied')
            check(case + '_actual_crash_window', fixture.counts(case) == {'posts': expected_posts, 'applied': expected_posts})
            if case == 'physical-unknown':
                fixture.change(case, 'unknown')
            elif case == 'object-changed':
                fixture.change(case, 'object')
            elif case == 'version-changed':
                fixture.change(case, 'version')
            cancelled = None
            observed_run = run_id
            if case == 'cancelled-write':
                cancelled = cancel_crashed_run(directory, run_id)
                check('cancel_does_not_roll_back_applied_external_data',
                    cancelled['run']['state'] == 'CANCELLED'
                    and cancelled['tables']['write_intents'][0]['status'] == 'UNKNOWN'
                    and fixture.content(case) == 'Owned M1-23 synthetic content v1'
                    and fixture.counts(case) == {'posts': 1, 'applied': 1})
                observed_run = related_run(directory, run_id, reconcile=True)
            after = await launch(directory, fixture.origin, observed_run, 'B',
                point='check_captured' if case == 'during-check' else None,
                dispatch=case in ('intent-not-applied', 'physical-applied'), fixture=fixture)
            if cancelled is None:
                preserve(before, after)
            else:
                check('cancelled_run_receipt_and_unknown_step_history_retained',
                    ledger(directory, run_id)['run']['state'] == 'CANCELLED'
                    and all(row in after['tables']['steps'] for row in cancelled['tables']['steps']))
            check(case + '_different_process_preserves_ledgers', before['crash']['pid'] != after['pid'])
            status = after['tables']['write_intents'][0]['status']
            if case in ('intent-not-applied', 'physical-applied', 'cancelled-write'):
                require_clean_process(after)
                check(case + '_confirmed_without_duplicate_post', status == 'CONFIRMED'
                      and fixture.counts(case) == {'posts': 1, 'applied': 1} and after['error'] is None)
            else:
                check(case + '_unknown_never_replays_or_succeeds', status == 'UNKNOWN'
                      and fixture.counts(case) == {'posts': expected_posts, 'applied': expected_posts}
                      and after['run']['state'] not in ('SUCCEEDED', 'PARTIAL')
                      and bool(after['tables']['resource_quarantines']) and not after['tables']['run_results'])
                if case != 'during-check':
                    check(case + '_query_authority_cannot_become_execution_authority',
                        bool(after['outcomes'].get('normal_dispatch_rejected'))
                        and bool(after['outcomes'].get('success_authority_rejected')))
            writes = assert_dispatch_budgets(after)
            check(case + '_each_actual_dispatch_has_an_independent_budget_debit',
                writes == (2 if case == 'intent-not-applied' else 1))
            if case in ('intent-not-applied', 'physical-applied'):
                operation_id = after['tables']['write_intents'][0]['operation_id']
                reentered = await launch(directory, fixture.origin, run_id, 'C', dispatch=True, finish=True)
                require_clean_process(reentered)
                preserve(after, reentered)
                check(case + '_node_reentry_reuses_semantic_operation_without_write',
                    reentered['error'] is None and fixture.counts(case) == {'posts': 1, 'applied': 1}
                    and len(reentered['tables']['write_intents']) == 1
                    and reentered['tables']['write_intents'][0]['operation_id'] == operation_id
                    and assert_dispatch_budgets(reentered) == writes)
                new_run = related_run(directory, run_id)
                retried = await launch(directory, fixture.origin, new_run, 'D', dispatch=True)
                require_clean_process(retried)
                check(case + '_new_run_reuses_same_business_key_without_replay',
                    retried['error'] is None and fixture.counts(case) == {'posts': 1, 'applied': 1}
                    and len(retried['tables']['write_intents']) == 1
                    and retried['tables']['write_intents'][0]['operation_id'] == operation_id
                    and retried['tables']['write_intents'][0]['business_key'] == before['tables']['write_intents'][0]['business_key']
                    and assert_dispatch_budgets(retried) == writes)
            matrix.append({'case': case, 'operation_status': status, 'physical': fixture.counts(case),
                           'pid_a': before['crash']['pid'], 'pid_b': after['pid'], 'error': after['error']})
        check('owned_http_has_no_unexpected_errors', not fixture.errors)
        phases = [json.loads(path.read_text()) for path in output.glob('domains/*/process-*.json')]
        crashes = [json.loads(path.read_text()) for path in output.glob('domains/*/before-*.json')]
        report['scope_counts'] = {'isolated_domains': len(matrix),
            'worker_processes': len({item['pid'] for item in phases} | {item['crash']['pid'] for item in crashes}),
            'sigkill_windows': len(crashes), 'http_posts': sum(row['method'] == 'POST' for row in fixture.requests)}
        report['passed'] = True
    finally:
        await fixture.close()
        write_json(output / 'matrix.json', matrix)
        write_json(output / 'http-records.json', {'requests': fixture.requests, 'errors': fixture.errors})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--child-dir', type=Path)
    parser.add_argument('--origin')
    parser.add_argument('--run-id')
    parser.add_argument('--phase', choices=('A', 'B', 'C', 'D'))
    parser.add_argument('--fault-point', choices=FAULTS)
    parser.add_argument('--dispatch', action='store_true')
    parser.add_argument('--finish', action='store_true')
    parser.add_argument('--case', choices=CASES, action='append')
    args = parser.parse_args()
    if args.child_dir:
        asyncio.run(child(args.child_dir.resolve(), args.origin, args.run_id, args.phase, args.fault_point,
                          dispatch=args.dispatch, finish=args.finish))
        return 0
    assert args.output_dir is not None
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-23', 'fault_requirement': 'FR-03', 'passed': False, 'checks': {},
        'created_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Owned HTTP/Chromium/SQLite mutations only; no user services, accounts, paid models or evaluator-only truth.'}
    try:
        asyncio.run(verify(output, report, args.case))
    except Exception as error:
        report['error'] = error_details(error)
    report['artifact_sha256'] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob('*')) if path.is_file() and '.security' not in path.parts}
    write_json(output / 'report.json', report)
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(output / 'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
