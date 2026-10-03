#!/usr/bin/env python3
"""M1-17 / FR-02 real process crash and dual-checkpoint recovery acceptance.

The owned source and HTTP model are independent of child workers. Fault hooks
are developer-injected constructor dependencies, never task/model/API input.
A stopped child is killed by its parent with SIGKILL, leaving real SQLite/WAL
and AsyncSqliteSaver state for a different OS process to reconcile.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import html
from importlib.metadata import version
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import secrets
import traceback
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).resolve().parent)]
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

from verify_graph import OwnedGraphFixture
from webagent.config import Settings, disable_external_tracing
from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.models.transport import DeepSeekTransport, ModelConfig
from webagent.scheduler.models import ExecutionToken, Resource, ordered_resources
from webagent.scheduler.store import SchedulerStore
from webagent.tasks.models import TaskContract

CRASH_POINTS = ('model_before', 'model_after', 'action_before', 'action_after',
    'gateway_before_physical', 'gateway_after_physical', 'evidence_before_commit',
    'evidence_after_commit', 'business_before', 'business_after',
    'dispatch_return_before', 'saver_before', 'saver_after')
FAULT_POINTS = CRASH_POINTS + ('saver_error', 'disk_error', 'gateway_execution_error', 'verify_return_before')


class RecoveryFixture(OwnedGraphFixture):
    def __init__(self, output):
        super().__init__()
        self.output = output
        self.phase = {}
        self.changed = {}
        self.clicks = {}
        self.identities = {}
        self.gateway_host = None

    def disclosure(self, run_id):
        body = self.document()
        change = self.changed.get(run_id)
        object_id = 'changed-entity' if change == 'object' else 'owned-fixture-entity'
        object_version = 'owned-disclosure-v2' if change == 'version' else 'owned-disclosure-v1'
        for item in body['values']:
            item['entity_id'], item['report_version'] = object_id, object_version
        body['recovery_context'] = {'object_id': object_id, 'object_version': object_version}
        if run_id in self.identities:
            body['recovery_context']['normalized_account'] = 'changed-user' if change == 'account' else 'fixture-user'
            body['recovery_context']['identity_ref'] = self.identities[run_id]
        return body

    def source_page(self, path):
        run_id = path.split('/')[-1]
        body = self.disclosure(run_id)
        anchor = ''
        if path.startswith('/start/'):
            # A real visible read-only link with no extra body text; the visible
            # disclosure remains one complete JSON document for original parsing.
            anchor = '<a id="read-report" aria-label="Read report" style="display:block;width:180px;height:42px;border:1px solid #345" href="/clicked/' + run_id + '"></a>'
        return ('<!doctype html><meta charset="utf-8"><title>Owned recovery disclosure</title>'
                '<pre style="white-space:pre-wrap">' + html.escape(canonical_json(body)) + '</pre>' + anchor).encode()

    async def model_reply(self, request):
        payload = json.loads(request['messages'][1]['content'])
        assert len(request['messages']) == 2 and 'tools' not in request and 'tool_choice' not in request
        assert set(payload) == {'run_id', 'contract', 'observation', 'verified_checkpoint',
            'image_evidence_ids', 'allowed_action_schema_ref', 'selected_flow_versions'}
        run_id = payload['run_id']
        observation, checkpoint = payload['observation'], payload['verified_checkpoint']
        document = json.loads(observation['visible_excerpt'])
        number = self.calls.get(run_id, 0) + 1
        self.calls[run_id] = number
        phase = self.phase.get(run_id, 'A')
        refs = observation['evidence_ids']
        assert refs and observation['redaction_status'] == 'FILTERED'
        if urlsplit(observation['source_url']).path.startswith('/start/'):
            kind = 'click' if phase == 'A' else 'navigate'
            args = {} if kind == 'click' else {'url': self.origin + '/data/' + run_id}
            locator = {'strategy': 'dom', 'attribute': 'id', 'value': 'read-report'} if kind == 'click' else None
            output = {'type': 'Action', 'action': {'run_id': run_id,
                'step_id': ('original-click-' if kind == 'click' else 'replanned-navigation-') + run_id,
                'epoch': checkpoint['epoch'], 'snapshot_id': observation['snapshot_id'],
                'expected_effect': 'read', 'action_type': kind,
                'target': {'page_url': observation['source_url'], 'tab_id': observation['tab_id'],
                    'frame_id': observation['frame_id'], 'locator': locator, 'write_scope': None}, 'args': args}}
            if phase == 'A':
                # Real HTTP model thinking time, charged to the original Run.
                # Navigation pacing still belongs to the production gateway.
                await asyncio.sleep(3.05)
        else:
            output = {'type': 'ProposeResult', 'items': {'scenario': 'finance',
                'values': [{**item, 'evidence_ids': refs} for item in document['values']]},
                'coverage': document['coverage'], 'evidence_ids': refs,
                'unresolved': [], 'existing_operation_ids': []}
        self.provider_requests.append({'run_id': run_id, 'number': number, 'phase': phase,
            'input_sha256': hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
            'snapshot_id': observation['snapshot_id'], 'epoch': checkpoint['epoch'],
            'output_type': output['type'], 'tool_authority_absent': True,
            'original_history_absent': True})
        return {'id': 'owned-recovery-' + str(number), 'model': 'deepseek-flash',
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': canonical_json(output)}}],
            'usage': {'prompt_tokens': 29, 'completion_tokens': 19, 'total_tokens': 48}}

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            assert len(raw) < 65536
            lines = raw.decode('ascii').split('\r\n')
            method, target, _ = lines[0].split(' ', 2)
            headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                       for line in lines[1:] if ':' in line}
            size = int(headers.get('content-length', '0'))
            assert 0 <= size <= 2 * 1024 * 1024
            request_body = await asyncio.wait_for(reader.readexactly(size), 5) if size else b''
            path, status, extra = urlsplit(target).path, '200 OK', ''
            content_type = 'text/html; charset=utf-8'
            if method == 'POST' and path == '/chat/completions':
                response, content_type = canonical_json(await self.model_reply(json.loads(request_body))).encode(), 'application/json'
            elif method == 'GET' and path.startswith('/clicked/'):
                run_id = path.split('/')[-1]
                self.clicks[run_id] = self.clicks.get(run_id, 0) + 1
                status, response, extra = '302 Found', b'', 'Location: /data/' + run_id + '\r\n'
            elif method == 'GET' and (path.startswith('/start/') or path.startswith('/data/')):
                response = self.source_page(path)
            else:
                status, response = '404 Not Found', b'Owned fixture route unavailable'
            self.requests.append({'method': method, 'path': path,
                'response_sha256': hashlib.sha256(response).hexdigest()})
            writer.write(('HTTP/1.1 ' + status + '\r\nContent-Type: ' + content_type + '\r\n'
                'Cache-Control: no-store\r\nConnection: close\r\n' + extra + 'Content-Length: '
                + str(len(response)) + '\r\n\r\n').encode() + response)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # Expected when a genuinely killed HTTP client disappears.
        except Exception as error:
            self.errors.append({'type': type(error).__name__, 'phase': 'fixture'})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.active.discard(task)


class FaultController:
    """Owned child only; marker is durable before a parent-issued SIGKILL."""
    def __init__(self, directory, run_id, point=None):
        if point is not None and point not in FAULT_POINTS:
            raise ValueError('Unknown fixed probe fault')
        self.directory, self.run_id, self.point = Path(directory), run_id, point
        self.hit = False

    def stop(self, stage):
        if self.hit or stage != self.point:
            return
        self.hit = True
        marker = self.directory / 'fault-marker.json'
        with marker.open('w') as handle:
            handle.write(canonical_json({'run_id': self.run_id, 'stage': stage, 'pid': os.getpid()}))
            handle.flush()
            os.fsync(handle.fileno())
        os.kill(os.getpid(), signal.SIGSTOP)

    async def hook(self, stage, run_id):
        assert run_id == self.run_id
        self.stop(stage)


class SyntheticAuth:
    """An owned empty login state; no actual credential or OS Keychain use."""
    def load(self, ref, **bindings):
        assert bindings['site_id'] == 'owned-http' and bindings['realm'] == 'webarena'
        return {'cookies': [], 'origins': []}


def seed(directory, origin, run_id, config, *, account=False, max_actions=12):
    from webagent.tasks.compiler import compile_draft
    path = directory / 'business.sqlite3'
    migrate(path)
    identity_ref = None
    if account:
        from webagent.identities.store import IdentityStore
        # Seed a separate owned-http scoped identity, without a real login.
        from webagent.sessions.store import SessionRegistry
        from webagent.sessions.models import SessionOwner
        identity = IdentityStore(path)
        registry = SessionRegistry(path)
        login = identity.create(site_id='owned-http', realm='webarena', origin=origin, expected_account='fixture-user')
        session = registry.reserve('owned-login-probe', SessionOwner('login', login.login_id, 'owned-http', realm='webarena'))
        registry.opened(session.session_id, session.manager_id)
        login = identity.attach_session(login.login_id, login.state_version, session.session_id, session.manager_id)
        login = identity.begin_confirm(login.login_id, login.state_version)
        login = identity.identity_candidate(login.login_id, login.state_version, 'fixture-user')
        login = identity.finalize_verified(login.login_id, login.state_version,
            identity_ref=login.candidate_identity_ref, normalized_account='fixture-user', auth_ref=str(uuid4()),
            auth_sha256='a' * 64, verification_origin=origin, adapter_id='owned-recovery-adapter-v1', evidence_sha256='e' * 64)
        registry.closing(session.session_id, session.manager_id)
        registry.closed(session.session_id, session.manager_id)
        identity_ref = login.identity_ref
    draft = {'instruction': 'Read and verify the owned annual disclosure after recovery',
        'scenario': 'finance', 'source_ids': ['local-fixture'], 'parameters': {
            'entity_id': 'owned-fixture-entity', 'report_version': 'owned-disclosure-v1',
            'period_type': 'annual', 'metrics': ['revenue', 'profit'], 'currency': 'USD'}}
    if identity_ref:
        draft['identity_ref'] = identity_ref
    contract = compile_draft(draft, task_id='task-' + run_id, version=1, created_at=utc_text(), provenance=[{
        'origin': 'explicit_test_configuration', 'reference': 'recovery-acceptance-fixture-v1',
        'content_sha256': 'a' * 64, 'authorizes_execution': True}]).contract
    contract['sources'] = [{'source_id': 'owned-http', 'site_id': 'owned-http', 'origin': origin, 'path_prefix': '/'}]
    contract['start_urls'] = [origin + '/start/' + run_id]
    contract['output_schema'] = [{'field_id': field, 'required': True,
        'description': 'Requested original ' + field} for field in ('revenue', 'profit')]
    contract['time_scope'] = {'start': '2025-01-01T00:00:00Z', 'end': '2025-12-31T23:59:59Z',
                             'basis': 'Explicit owned annual disclosure period'}
    contract['budget_profile'].update({'max_actions': max_actions, 'max_active_seconds': 120})
    contract = TaskContract.model_validate_json(canonical_json(contract)).model_dump(mode='json')
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=run_id, task_id=contract['task_id'], contract_version=1,
            graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256=config.config_sha256, runtime_config_sha256='b' * 64)
    # Exercise actual ordinary debit semantics with an explicitly registered
    # synthetic browser realm, rather than asserting an empty quota ledger.
    store = SchedulerStore(path, lease_seconds=180)
    store.enqueue(run_id, [Resource.site_identity('owned-http', identity_ref, realm='webarena'),
        Resource.browser_context(run_id)], expected_state_version=0, queue_class='ordinary')
    return contract


def ledger(directory, run_id):
    with connect(directory / 'business.sqlite3') as db:
        run = dict(db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone())
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        payload = {'run': {k:run[k] for k in ('run_id','state','state_version','contract_sha256',
            'graph_version','graph_state_schema_version','model_config_sha256')},
            'budget': dict(db.execute('SELECT * FROM run_budgets WHERE run_id=?', (run_id,)).fetchone())}
        for table, order in (('steps','sequence'),('task_events','event_id'),('quota_debits','debit_id'),
                            ('budget_attempts','attempt_id'),('model_attempts','request_id'),
                            ('graph_progress','progress_id'),('run_results','run_id'),('run_verifications','verification_id'),
                            ('graph_recoveries','recovery_id')):
            if table in tables:
                payload[table] = [dict(r) for r in db.execute('SELECT * FROM ' + table + ' WHERE run_id=? ORDER BY ' + order, (run_id,))]
        payload['integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
        payload['foreign_keys'] = [list(row) for row in db.execute('PRAGMA foreign_key_check')]
    return payload


def gateway_factory(fault):
    from webagent.gateway.service import BrowserGateway
    def factory(manager, session, **kwargs):
        gateway = BrowserGateway.from_managed(manager, session, **kwargs)
        execute = gateway.browser.execute
        async def execute_with_fault(*args, **call_kwargs):
            action = args[1]
            if action.action_type == 'click':
                fault.stop('gateway_before_physical')
                if fault.point == 'gateway_execution_error' and not fault.hit:
                    fault.hit = True
                    raise BusinessError('SERVICE_UNAVAILABLE', 'Owned injected browser failure', status=503)
            result = await execute(*args, **call_kwargs)
            if action.action_type == 'click':
                fault.stop('gateway_after_physical')
            return result
        gateway.browser.execute = execute_with_fault
        publisher = gateway.evidence.publish_observation
        def publish_with_fault(observation, *args, **call_kwargs):
            result = publisher(observation, *args, **call_kwargs)
            fault.stop('evidence_after_commit')
            return result
        gateway.evidence.publish_observation = publish_with_fault
        def storage_hook(stage):
            if stage == 'before_commit':
                fault.stop('evidence_before_commit')
            if stage == 'mid_write' and fault.point == 'disk_error' and not fault.hit:
                fault.hit = True
                raise OSError(errno.ENOSPC, 'Owned injected evidence disk failure')
        gateway.evidence.store.fault_hook = storage_hook
        gateway.evidence.store.files.fault_hook = storage_hook
        return gateway
    return factory


def install_saver_fault(saver, fault):
    original = saver.aput
    async def aput(config, checkpoint, metadata, new_versions):
        route = checkpoint.get('channel_values', {}).get('route')
        selected = route == 'confirm'
        if selected:
            fault.stop('saver_before')
            if fault.point == 'saver_error' and not fault.hit:
                fault.hit = True
                raise sqlite3.OperationalError('database or disk is full')
        result = await original(config, checkpoint, metadata, new_versions)
        if selected:
            fault.stop('saver_after')
        return result
    saver.aput = aput


def decode_token(value):
    value = dict(value)
    value['resources'] = tuple(value['resources'])
    return ExecutionToken(**value)


def rpc_value(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if hasattr(value, '__dataclass_fields__'):
        return asdict(value)
    return value


class LiveBrowserHost:
    """Parent owns an actual browser while child graph processes are killed.

    A private Unix socket is a probe-only dependency seam. Commands are fixed
    and every actual browser operation uses the production qualified gateway.
    No socket operation is exposed to a task contract, model or application API.
    """
    def __init__(self, directory, origin, run_id):
        from webagent.network.config import NetworkConfig
        from webagent.network.policy import Endpoint
        from webagent.sessions.manager import ManagedBrowser
        self.directory, self.run_id = directory, run_id
        self.manager = ManagedBrowser(Settings(directory), headless=True, auth_store=SyntheticAuth(),
            network_config=NetworkConfig(webarena_endpoints=(Endpoint('http','127.0.0.1',urlsplit(origin).port),)))
        self.socket = Path('/private/tmp/wp-recovery-' + secrets.token_hex(8) + '.sock')
        self.gateway, self.commands = None, []

    async def start(self):
        await self.manager.start()
        self.server = await asyncio.start_unix_server(self.handle, str(self.socket))
        self.socket.chmod(0o600)
        (self.directory / 'live-manager.json').write_text(canonical_json({'manager_id':self.manager.manager_id})+'\n')
        return self

    async def handle(self, reader, writer):
        from webagent.gateway.service import BrowserGateway
        from webagent.sessions.models import SessionOwner
        try:
            data = await asyncio.wait_for(reader.readline(),15)
            assert 0 < len(data) <= 2*1024*1024
            request = json.loads(data)
            method, args = request['method'], request.get('args',{})
            self.commands.append({'method':method})
            token = decode_token(args.pop('token')) if 'token' in args else None
            if method == 'create':
                owner = SessionOwner(**args.pop('owner'))
                assert owner.owner_id == self.run_id and token.run_id == self.run_id
                result = await self.manager.create(owner,execution_token=token,**args)
            elif method == 'close':
                owner = SessionOwner(**args.pop('owner'))
                assert owner.owner_id == self.run_id
                result = await self.manager.close(args['session_id'],owner)
            elif method == 'gateway':
                sessions = [session for session in self.manager.registry.list_owned(self.manager.manager_id)
                            if session.session_id == args['session_id'] and session.owner.owner_id == self.run_id]
                assert len(sessions) == 1
                if self.gateway is not None:
                    await self.gateway.browser.aclose()
                self.gateway = BrowserGateway.from_managed(self.manager,sessions[0],scheduler=SchedulerStore(self.directory/'business.sqlite3'))
                result = True
            elif method == 'detach':
                if self.gateway is not None:
                    await self.gateway.browser.aclose()
                result = True
            elif method in ('observe','navigate','dispatch','recovery_observe','recovery_navigate'):
                assert token.run_id == self.run_id and self.gateway is not None
                result = await getattr(self.gateway,method)(token,**args)
            else:
                raise ValueError('Unknown fixed probe socket command')
            response = {'result':rpc_value(result)}
        except BusinessError as error:
            response = {'error':{'code':error.code,'field':error.field,'status':error.status}}
        except Exception as error:
            response = {'error':{'type':type(error).__name__}}
        writer.write(canonical_json(response).encode()+b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        if self.gateway is not None:
            await self.gateway.browser.aclose()
        await self.manager.aclose()
        self.socket.unlink(missing_ok=True)
        (self.directory/'live-host-commands.json').write_text(json.dumps(self.commands,indent=2)+'\n')


class LiveManagerProxy:
    def __init__(self, settings, socket):
        from webagent.sessions.store import SessionRegistry
        self.settings, self.socket = settings, socket
        self.registry = SessionRegistry(settings.business_db)
        self.manager_id = json.loads((settings.data_dir/'live-manager.json').read_text())['manager_id']

    async def rpc(self, method, **args):
        reader, writer = await asyncio.open_unix_connection(str(self.socket),limit=2*1024*1024)
        try:
            writer.write(canonical_json({'method':method,'args':{k:rpc_value(v) for k,v in args.items()}}).encode()+b'\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(),35))
            if 'error' in response:
                error = response['error']
                if 'code' in error:
                    raise BusinessError(error['code'],'Owned gateway RPC refused',field=error['field'],status=error['status'])
                raise AssertionError('Owned gateway RPC failed: '+error['type'])
            return response['result']
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self):
        return self

    async def aclose(self):
        pass  # The parent retains ownership across child death.

    async def create(self, owner, *, execution_token, **kwargs):
        from webagent.sessions.models import SessionInfo, SessionOwner
        result = await self.rpc('create',owner=owner,token=execution_token,**kwargs)
        result['owner'] = SessionOwner(**result['owner'])
        return SessionInfo(**result)

    async def close(self, session_id, owner):
        return await self.rpc('close',session_id=session_id,owner=owner)


class LiveGatewayProxy:
    def __init__(self, manager, session, scheduler):
        from webagent.gateway.service import BrowserGateway
        self.manager = manager
        # Local qualification reads use production SQLite/session checks too.
        backend = SimpleNamespace(owner=session.owner,session_id=session.session_id,
            managed=SimpleNamespace(manager_id=manager.manager_id))
        self.local = BrowserGateway(manager.settings.business_db,backend,scheduler=scheduler)
        self.browser = SimpleNamespace(aclose=self.detach)

    def _qualified(self, token, **kwargs):
        return self.local._qualified(token,**kwargs)

    async def detach(self):
        return await self.manager.rpc('detach')

    async def observe(self, token, **kwargs):
        return await self.manager.rpc('observe',token=token,**kwargs)

    async def navigate(self, token, url, step_id):
        return await self.manager.rpc('navigate',token=token,url=url,step_id=step_id)

    async def dispatch(self, token, value):
        return await self.manager.rpc('dispatch',token=token,action=value)

    async def recovery_observe(self, token, recovery_id):
        return await self.manager.rpc('recovery_observe',token=token,recovery_id=recovery_id)

    async def recovery_navigate(self, token, url, recovery_id):
        return await self.manager.rpc('recovery_navigate',token=token,url=url,recovery_id=recovery_id)


async def child(directory, origin, run_id, phase, point=None, gateway_socket=None):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from webagent.graph.executor import GraphExecutor
    from webagent.graph.runtime import StateGraphAdapter
    from webagent.graph.store import GraphStore
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.sessions.manager import ManagedBrowser
    from webagent.verification.service import VerificationService
    disable_external_tracing()
    settings = Settings(directory.resolve())
    config = ModelConfig(base_url=origin, connect_seconds=1.0, read_seconds=5.0, total_seconds=8.0, max_tokens=8192)
    fault = FaultController(directory, run_id, point)
    store = SchedulerStore(settings.business_db, lease_seconds=180)
    manager, token, provider = None, None, None
    status, error = 'completed', None
    try:
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute('PRAGMA busy_timeout=5000')
            await saver.conn.execute('PRAGMA synchronous=FULL')
            install_saver_fault(saver, fault)
            run = GraphStore(settings.business_db).load_run(run_id)
            if run['state'] in ('SUCCEEDED','PARTIAL','FAILED','CANCELLED'):
                runtime = StateGraphAdapter(directory, None, None, VerificationService(directory), checkpointer=saver)
                await runtime.repair_terminal(run_id)
            else:
                manager = LiveManagerProxy(settings,gateway_socket) if gateway_socket else ManagedBrowser(
                    settings, headless=True, auth_store=SyntheticAuth(), network_config=NetworkConfig(
                    webarena_endpoints=(Endpoint('http', '127.0.0.1', urlsplit(origin).port),)))
                await manager.start()
                generation = store.start_worker(manager.manager_id)
                token = store.claim(manager.manager_id, generation)
                if token is None:
                    status = 'blocked-no-qualification'
                else:
                    provider = DeepSeekTransport(config, 'owned-recovery-provider-' + phase, allow_test_loopback=True)
                    def graph_factory(*args, **kwargs):
                        return StateGraphAdapter(*args, **kwargs, fault_hook=fault.hook)
                    async def identity_preparer(gateway, qualification, contract, identity):
                        # Synthetic setup proof is only for the initial Run;
                        # M1-17 independently verifies the restored page account.
                        return identity.normalized_account == 'fixture-user'
                    async def live_gateway_factory(manager,session,**kwargs):
                        await manager.rpc('gateway',session_id=session.session_id)
                        return LiveGatewayProxy(manager,session,kwargs['scheduler'])
                    executor = GraphExecutor(settings, manager, checkpointer=saver, scheduler=store,
                        secret_store=object(), provider_factory=lambda requested: provider,
                        gateway_factory=live_gateway_factory if gateway_socket else gateway_factory(fault), graph_factory=graph_factory,
                        identity_preparer=identity_preparer)
                    await asyncio.wait_for(executor(token), 110)
    except BusinessError as caught:
        status, error = 'blocked', {'code':caught.code,'field':caught.field,'status':caught.status}
    except Exception as caught:
        status, error = 'fault', {'type':type(caught).__name__, 'locations':[
            {'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
            for frame in traceback.extract_tb(caught.__traceback__)]}
    finally:
        if token is not None:
            try:
                store.abandon(token)
            except BusinessError:
                pass
        if provider is not None:
            await provider.aclose()
        if manager is not None:
            await manager.aclose()
    payload = ledger(directory, run_id)
    payload.update({'phase':phase,'pid':os.getpid(),'status':status,'error':error})
    destination = directory / ('process-' + phase + '.json')
    destination.write_text(json.dumps(payload, indent=2) + '\n')
    print(json.dumps({'status':status,'state':payload['run']['state'],'phase':phase,'pid':os.getpid()}))


async def queued_child(directory, origin, run_id, peer_run_id):
    """A blocked recovery must leave the production Worker free for other work."""
    disable_external_tracing()
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from webagent.graph.executor import GraphExecutor
    from webagent.graph.store import GraphStore
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.scheduler.worker import QueueWorker
    from webagent.sessions.manager import ManagedBrowser
    settings = Settings(directory.resolve())
    config = ModelConfig(base_url=origin,connect_seconds=1.0,read_seconds=5.0,total_seconds=8.0,max_tokens=8192)
    store = SchedulerStore(settings.business_db,lease_seconds=180)
    manager = ManagedBrowser(settings,headless=True,auth_store=SyntheticAuth(),network_config=NetworkConfig(
        webarena_endpoints=(Endpoint('http','127.0.0.1',urlsplit(origin).port),)))
    stopped, peer_done, pump = asyncio.Event(), asyncio.Event(), None
    calls, providers = [], []
    try:
        await manager.start()
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute('PRAGMA synchronous=FULL')
            def provider_factory(requested):
                assert requested==peer_run_id
                provider=DeepSeekTransport(config,'owned-worker-continuation-provider',allow_test_loopback=True)
                providers.append(provider)
                return provider
            async def initial_identity(gateway,token,contract,identity):
                return identity.normalized_account=='fixture-user'
            executor=GraphExecutor(settings,manager,checkpointer=saver,scheduler=store,
                secret_store=object(),provider_factory=provider_factory,identity_preparer=initial_identity)
            async def monitored(token):
                calls.append(token.run_id)
                result=await executor(token)
                if token.run_id==peer_run_id:peer_done.set()
                return result
            worker=QueueWorker(store,manager.manager_id,executor=monitored,poll_seconds=.02,
                heartbeat_seconds=.2,deadline_seconds=.05)
            pump=asyncio.create_task(worker.run(stopped))
            async with asyncio.timeout(40):
                while not peer_done.is_set():
                    if pump.done():
                        await pump
                        raise AssertionError('Worker exited before independent queued task')
                    await asyncio.sleep(.05)
            stopped.set()
            await pump
            peer=ledger(directory,peer_run_id)
            assert peer['run']['state']=='SUCCEEDED'
            saved=await saver.aget_tuple({'configurable':{'thread_id':peer_run_id}})
            assert saved.checkpoint['channel_values']['completed']
            payload=ledger(directory,run_id)
            payload.update({'phase':'C','pid':os.getpid(),'status':'completed','error':None,
                'peer':peer,'executor_calls':calls,'worker_failed':worker._failed,
                'providers_closed':all(provider._client.is_closed for provider in providers)})
            (directory/'process-C.json').write_text(json.dumps(payload,indent=2)+'\n')
    finally:
        stopped.set()
        if pump is not None and not pump.done():await asyncio.wait_for(pump,8)
        await manager.aclose()


async def launch(directory, origin, run_id, phase, *, point=None, kill=False, gateway_socket=None, peer_run_id=None):
    command = [sys.executable, str(Path(__file__).resolve()), '--child-dir', str(directory),
        '--origin', origin, '--run-id', run_id, '--phase', phase]
    if point:
        command += ['--fault-point', point]
    if gateway_socket:
        command += ['--gateway-socket',str(gateway_socket)]
    if peer_run_id:
        command += ['--queue-peer-run-id',peer_run_id]
    proc = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env={**os.environ,'PYTHONUTF8':'1'},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        if kill:
            async with asyncio.timeout(25):
                marker = directory / 'fault-marker.json'
                while not marker.exists():
                    if proc.returncode is not None:
                        out, err = await proc.communicate()
                        (directory / 'process-A.stderr').write_bytes(err)
                        raise AssertionError('Child exited before requested crash boundary')
                    await asyncio.sleep(.02)
            observed = json.loads(marker.read_text())
            assert observed == {'run_id':run_id,'stage':point,'pid':proc.pid}
            before = ledger(directory, run_id)
            os.kill(proc.pid, signal.SIGKILL)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
            assert proc.returncode == -signal.SIGKILL
            before['crash'] = {'pid':proc.pid,'signal':'SIGKILL','stage':point}
            (directory / 'before-crash.json').write_text(json.dumps(before, indent=2) + '\n')
            result = before
        else:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 120)
            (directory / ('process-' + phase + '.stdout')).write_bytes(stdout)
            (directory / ('process-' + phase + '.stderr')).write_bytes(stderr)
            assert proc.returncode == 0
            result = json.loads((directory / ('process-' + phase + '.json')).read_text())
        (directory / ('process-' + phase + '.stdout')).write_bytes(stdout)
        (directory / ('process-' + phase + '.stderr')).write_bytes(stderr)
        return result
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def assert_preserved(before, after):
    for field in ('run_id','contract_sha256','graph_version','graph_state_schema_version','model_config_sha256'):
        assert before['run'][field]==after['run'][field],field
    assert before['budget']['budget_record_id'] == after['budget']['budget_record_id']
    for name in ('actions_used','content_pages_used','observations_used','screenshots_used','model_calls_used','active_ms','ci_wait_ms'):
        assert after['budget'][name] >= before['budget'][name], name
    old_counts=json.loads(before['budget']['recovery_counts_json'])
    new_counts=json.loads(after['budget']['recovery_counts_json'])
    assert all(new_counts.get(key,0)>=count for key,count in old_counts.items())
    assert len(before['quota_debits']) == len(after['quota_debits']) == 1
    assert before['quota_debits'] == after['quota_debits']
    for table, key in (('task_events','event_id'),('budget_attempts','attempt_id')):
        previous = {row[key]:row for row in before[table]}
        current = {row[key]:row for row in after[table]}
        assert previous.items() <= current.items(), table
    failed = {row['step_id']:row for row in before['steps']}
    current = {row['step_id']:row for row in after['steps']}
    assert failed.items() <= current.items()
    assert after['integrity'] == 'ok' and not after['foreign_keys']
    if after['run_results']:
        assert len([row for row in after['task_events'] if row['event_type'] == 'result_ready']) == 1


async def mutate_graph(directory, run_id, change):
    from langgraph.checkpoint.base import create_checkpoint
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    async with AsyncSqliteSaver.from_conn_string(str(directory / 'graph.sqlite3')) as saver:
        current = await saver.aget_tuple({'configurable':{'thread_id':run_id}})
        assert current is not None
        checkpoint = create_checkpoint(current.checkpoint, None, current.metadata.get('step',0) + 1)
        if change == 'graph-version':
            checkpoint['channel_values']['graph_version'] = 'owned-incompatible-graph-v2'
        elif change == 'event-missing':
            checkpoint['channel_values']['business_event_id'] = 2**62
        elif change == 'state-version':
            checkpoint['channel_values']['state_version'] += 100
        else:
            raise ValueError('Unknown graph mutation')
        await saver.aput(current.config, checkpoint, current.metadata, checkpoint['channel_versions'])


def current_token(directory, run_id):
    with connect(directory / 'business.sqlite3') as db:
        q = db.execute('SELECT * FROM scheduler_queue WHERE run_id=?',(run_id,)).fetchone()
        resources = tuple(item.resource_key for item in ordered_resources([
            Resource(row[0].split(':',1)[0],row[0]) for row in db.execute(
                'SELECT resource_key FROM resource_leases WHERE holder_run_id=?',(run_id,))]))
        return ExecutionToken(run_id,q['worker_id'],q['worker_generation'],q['epoch'],q['run_state_version'],q['expires_at'],resources)


def add_blocker(directory, run_id, change):
    store = SchedulerStore(directory / 'business.sqlite3', lease_seconds=180)
    if change == 'human':
        token = current_token(directory, run_id)
        store.defer(token,'WAITING_HANDOFF',handoff_deadline=datetime.now(timezone.utc)+timedelta(hours=24),control_owner='human')
    elif change == 'unknown-write':
        with connect(directory / 'business.sqlite3') as db, transaction(db):
            task_id = db.execute('SELECT task_id FROM runs WHERE run_id=?',(run_id,)).fetchone()[0]
            now = utc_text()
            db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
                target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',('owned-unknown', 'owned-unknown', task_id, run_id,
                    'owned-fixture-target','create_pr','owned-synthetic-identity','owned-v1','UNKNOWN',now,now))
            key = Resource.site_identity('owned-http',realm='webarena').resource_key
            db.execute('INSERT INTO resource_quarantines(resource_key,operation_id,created_at) VALUES(?,?,?)',
                (key,'owned-unknown',now))
    elif change in ('evidence-missing','evidence-corrupt'):
        with connect(directory / 'business.sqlite3') as db:
            row = db.execute('SELECT artifact_path FROM evidence WHERE run_id=? AND original_evidence_id IS NULL LIMIT 1',(run_id,)).fetchone()
        assert row is not None
        if change=='evidence-missing':(directory / row[0]).unlink()
        else:(directory / row[0]).write_bytes(b'Owned deliberately corrupted original bytes')
    else:
        raise ValueError('Unknown business blocker')


async def verify(output, report, selected_cases=None):
    disable_external_tracing()
    def check(name, condition=True):
        assert condition,name
        report['checks'][name] = True
    fixture = await RecoveryFixture(output).start()
    summaries = []
    try:
        config = ModelConfig(base_url=fixture.origin, connect_seconds=1.0, read_seconds=5.0,total_seconds=8.0,max_tokens=8192)
        cases = [(point, point, None, True) for point in CRASH_POINTS]
        cases += [('saver-error','saver_error',None,False),('disk-error','disk_error',None,False),
            ('original-failure','gateway_execution_error',None,False)]
        cases += [(change,'verify_return_before' if change in ('evidence-missing','evidence-corrupt') else 'action_after',change,True)
            for change in ('graph-version','state-version','event-missing','evidence-missing','evidence-corrupt','object','version','account','human','unknown-write')]
        cases += [('budget-exhausted','action_after',None,True)]
        cases += [('account-control','action_after',None,True)]
        cases += [('live-browser','action_after',None,True)]
        cases += [('queued-continuation','action_after','graph-version',True)]
        if selected_cases:
            assert set(selected_cases) <= {case[0] for case in cases}
            cases = [case for case in cases if case[0] in selected_cases]
        for name, point, change, killed in cases:
            directory = output / 'domains' / name
            directory.mkdir(parents=True)
            run_id = 'recovery-' + name
            contract = seed(directory, fixture.origin, run_id, config, account=change=='account' or name=='account-control',
                 max_actions=2 if name=='budget-exhausted' else 12)
            if contract['identity_ref']:
                fixture.identities[run_id] = contract['identity_ref']
            if name=='queued-continuation':
                peer_run_id='recovery-independent-peer'
                peer_contract=seed(directory,fixture.origin,peer_run_id,config,account=True)
                fixture.identities[peer_run_id]=peer_contract['identity_ref']
                fixture.phase[peer_run_id]='B'
            fixture.phase[run_id] = 'A'
            host = await LiveBrowserHost(directory,fixture.origin,run_id).start() if name=='live-browser' else None
            try:
                before = await launch(directory, fixture.origin, run_id,'A',point=point,kill=killed,
                                      gateway_socket=host.socket if host else None)
                original_clicks = fixture.clicks.get(run_id,0)
                if host:
                    check('browser_survives_killed_graph_process',host.manager._browser.is_connected()
                          and any(not entry.physically_closed for entry in host.manager._contexts.values()))
                if change in ('graph-version','state-version','event-missing'):
                    await mutate_graph(directory,run_id,change)
                elif change in ('human','unknown-write','evidence-missing','evidence-corrupt'):
                    add_blocker(directory,run_id,change)
                elif change in ('object','version','account'):
                    fixture.changed[run_id] = change
                fixture.phase[run_id] = 'B'
                request_start = len(fixture.requests)
                after = await launch(directory,fixture.origin,run_id,'B',gateway_socket=host.socket if host else None)
            finally:
                if host:
                    await host.close()
            assert_preserved(before,after)
            check(name + '_preserves_original_business_ledgers_and_budget')
            assert before.get('crash',{}).get('pid',before.get('pid'))!=after['pid']
            check(name + '_never_replays_last_click', fixture.clicks.get(run_id,0) == original_clicks
                and fixture.clicks.get(run_id,0) <= 1)
            if change or name in ('disk-error','budget-exhausted'):
                check(name + '_cannot_promote_unreconciled_state',after['run']['state'] not in ('SUCCEEDED','PARTIAL'))
                if change in ('human','unknown-write','graph-version','state-version','event-missing','evidence-missing','evidence-corrupt') or name in ('disk-error','budget-exhausted'):
                    check(name + '_has_no_recovery_browser_reads',not any(row['method']=='GET' for row in fixture.requests[request_start:]))
            else:
                check(name + '_recovery_finishes_with_independently_verified_result',after['run']['state']=='SUCCEEDED' and after['run_results'])
                result = json.loads(after['run_results'][0]['result_json'])
                check(name + '_result_matches_current_visible_dynamic_source',
                    [item['normalized_value'] for item in result['items']['values']]
                    == [item['normalized_value'] for item in fixture.disclosure(run_id)['values']])
            if name=='business_after':
                check('business_committed_terminal_repair_does_not_request_any_new_browser_or_model_work',
                    not fixture.requests[request_start:] and before['task_events']==after['task_events']
                    and before['budget']==after['budget'] and before['steps']==after['steps'])
            if name == 'original-failure':
                check('original_failed_browser_attempt_and_error_are_retained',any(
                    row['status']=='UNKNOWN' and row['error_code'] for row in before['steps'])
                    and before['error']['code']=='SERVICE_UNAVAILABLE'
                    and all(row in after['steps'] for row in before['steps'] if row['status']=='UNKNOWN'))
            if name=='queued-continuation':
                continuation_request_start=len(fixture.requests)
                continuation_model_start=len(fixture.provider_requests)
                continuation=await launch(directory,fixture.origin,run_id,'C',peer_run_id=peer_run_id)
                check('blocked_recovery_does_not_hot_loop_or_block_independent_queued_work',
                    continuation['executor_calls'].count(run_id)<=1
                    and continuation['executor_calls'].count(peer_run_id)==1
                    and set(continuation['executor_calls'])<={run_id,peer_run_id}
                    and not continuation['worker_failed'] and continuation['providers_closed']
                    and continuation['peer']['run']['state']=='SUCCEEDED'
                    and all(row['run_id']==peer_run_id for row in fixture.provider_requests[continuation_model_start:])
                    and not any(row['path'].endswith('/'+run_id) for row in fixture.requests[continuation_request_start:]))
                assert_preserved(after,continuation)
            summaries.append({'case':name,'before_state':before['run']['state'],'after_state':after['run']['state'],
                'clicks_before':original_clicks,'clicks_after':fixture.clicks.get(run_id,0),
                'child_a_pid':before.get('crash',{}).get('pid',before.get('pid')),'child_b_pid':after['pid'],
                'recovery_error':after['error']})
        check('all_model_calls_use_owned_http_without_history_or_tools',bool(fixture.provider_requests)
            and all(row['tool_authority_absent'] and row['original_history_absent'] for row in fixture.provider_requests))
        check('no_browser_write_is_sent',not any(row['method']=='POST' and row['path']!='/chat/completions' for row in fixture.requests))
        check('owned_fixture_has_no_errors',not fixture.errors)
        report['scope_counts'] = {'isolated_domains':len(cases),'os_processes':len(cases)*2+int(any(case[0]=='queued-continuation' for case in cases)),
            'sigkill_windows':len([case for case in cases if case[3]]),'http_model_requests':len(fixture.provider_requests)}
        report['passed'] = True
    finally:
        await fixture.close()
        (output / 'matrix.json').write_text(json.dumps(summaries,indent=2)+'\n')
        (output / 'http-records.json').write_text(json.dumps({'requests':fixture.requests,
            'provider_requests':fixture.provider_requests,'errors':fixture.errors},indent=2)+'\n')
        (output / 'generated-source.json').write_text(canonical_json(fixture.document())+'\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--child-dir',type=Path)
    parser.add_argument('--origin')
    parser.add_argument('--run-id')
    parser.add_argument('--phase',choices=('A','B','C'))
    parser.add_argument('--fault-point',choices=FAULT_POINTS)
    parser.add_argument('--gateway-socket',type=Path,help='Owned live-browser probe dependency only')
    parser.add_argument('--case',action='append',help='Run only fixed owned acceptance cases during development')
    parser.add_argument('--queue-peer-run-id',help='Owned production Worker continuation dependency only')
    args = parser.parse_args()
    if args.child_dir:
        if args.queue_peer_run_id:
            asyncio.run(queued_child(args.child_dir.resolve(),args.origin,args.run_id,args.queue_peer_run_id))
        else:
            asyncio.run(child(args.child_dir.resolve(),args.origin,args.run_id,args.phase,args.fault_point,args.gateway_socket))
        return 0
    assert args.output_dir is not None
    output = args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=False)
    report = {'task':'M1-17','passed':False,'checks':{},'created_at':datetime.now(timezone.utc).isoformat(),
        'versions':{'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,
            'graph':GRAPH_VERSION,'state_schema':STATE_SCHEMA_VERSION,
            **{name:version(name) for name in ('langgraph','langgraph-checkpoint','langgraph-checkpoint-sqlite','aiosqlite','playwright')}},
        'api_coverage':'Authenticated read-only recovery diagnostic API is covered separately by production API unit tests; this probe exercises actual GraphExecutor and QueueWorker recovery.',
        'scope':'Owned dynamic HTTP documents/model, independent SQLite business/graph databases, actual worker OS SIGKILL and new-process recovery; no evaluator-only resources, user services or accounts.'}
    try:
        asyncio.run(verify(output,report,args.case))
    except Exception as error:
        report['error_type'] = type(error).__name__
        if isinstance(error,BusinessError):
            report['error'] = {'code':error.code,'field':error.field,'status':error.status}
        report['error_locations'] = [{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
            for frame in traceback.extract_tb(error.__traceback__)]
    report['artifact_sha256'] = {str(path.relative_to(output)):hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob('*')) if path.is_file() and '.security' not in path.parts}
    (output / 'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'report':str(output / 'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
