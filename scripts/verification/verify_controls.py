#!/usr/bin/env python3
"""M1-18: owned HTTP/API/Chromium/Worker control acceptance, fail fast.

This script is prepared for the single authorized formal verification batch.
It never talks to paid providers, user services, or evaluator-only resources.
Trusted constructor hooks and persistent local fixtures control the boundaries;
the public API cannot install these hooks or submit arbitrary browser actions.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import traceback
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).resolve().parent)]
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

from verify_recovery import RecoveryFixture, SyntheticAuth
from webagent.config import Settings, disable_external_tracing
from webagent.db import LATEST_VERSION, connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_task, utc_text
from webagent.errors import BusinessError
from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
from webagent.models.transport import DeepSeekTransport, ModelConfig
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def error_details(error, seen=None):
    """Keep the actual failed operation even when teardown also fails."""
    seen = set() if seen is None else seen
    if id(error) in seen:return {'type':type(error).__name__,'cycle':True}
    seen.add(id(error))
    result = {'type':type(error).__name__,'message':str(error),
        'locations':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
            for frame in traceback.extract_tb(error.__traceback__)]}
    if isinstance(error,OSError):result['errno'] = error.errno
    if isinstance(error,BaseExceptionGroup):
        result['exceptions'] = [error_details(item,seen) for item in error.exceptions]
    if error.__cause__ is not None:result['cause'] = error_details(error.__cause__,seen)
    elif error.__context__ is not None and not error.__suppress_context__:
        result['context'] = error_details(error.__context__,seen)
    return result


class OwnedSecrets:
    """Synthetic fixture credential, memory only; never accesses Keychain."""
    def put(self, reference, secret):
        from webagent.settings.secrets import validate_reference
        validate_reference(reference)

    def get(self, reference):
        from pydantic import SecretStr
        from webagent.settings.secrets import validate_reference
        validate_reference(reference)
        return SecretStr('owned-controls-synthetic-provider-key')

    def delete(self, reference):
        from webagent.settings.secrets import validate_reference
        validate_reference(reference)


class OwnedProvider:
    """Preserve logical frozen config while replacing its HTTP transport.

    The settings endpoint intentionally permits only the official endpoint.
    This trusted test adapter exposes that frozen logical config for business
    validation, and sends every actual HTTP request to the owned fixture.
    """
    def __init__(self, logical, origin):
        self.config = logical
        physical = ModelConfig.model_validate({**logical.model_dump(mode='json'), 'base_url':origin})
        self.transport = DeepSeekTransport(physical, 'owned-controls-synthetic-provider-key', allow_test_loopback=True)
        self.sensitive_literals = ('owned-controls-synthetic-provider-key',)

    async def complete(self, *args, **kwargs):
        return await self.transport.complete(*args, **kwargs)

    async def complete_verification(self, *args, **kwargs):
        return await self.transport.complete_verification(*args, **kwargs)

    async def aclose(self):
        await self.transport.aclose()


class ControlsFixture(RecoveryFixture):
    def __init__(self, output):
        super().__init__(output)
        self.aliases, self.modes, self.block_click = {}, {}, set()
        self.click_entered, self.click_release = {}, {}

    def bind(self, alias, run_id, mode='action'):
        self.aliases[alias] = run_id
        self.modes[run_id] = mode
        self.phase[run_id] = 'A'

    def source_page(self, path):
        alias = path.split('/')[-1]
        run_id = self.aliases.get(alias, alias)
        return super().source_page('/start/' + run_id if path.startswith('/start/') else '/data/' + run_id)

    async def model_reply(self, request):
        payload = json.loads(request['messages'][1]['content'])
        run_id = payload['run_id']
        if self.modes.get(run_id) == 'input' and self.phase.get(run_id) == 'A':
            assert len(request['messages']) == 2 and 'tools' not in request
            assert set(payload) == {'run_id','contract','observation','verified_checkpoint',
                'image_evidence_ids','allowed_action_schema_ref','selected_flow_versions'}
            observation = payload['observation']
            assert observation['redaction_status']=='FILTERED' and observation['evidence_ids']
            output = {'type':'RequestInput','requested_fields':['source_document'],
                'reason':'An explicitly requested additional source is needed'}
            self.calls[run_id] = self.calls.get(run_id,0) + 1
            self.provider_requests.append({'run_id':run_id,'phase':'A','output_type':'RequestInput',
                'input_sha256':hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
                'snapshot_id':observation['snapshot_id'],'tool_authority_absent':True,'original_history_absent':True})
            return {'id':'owned-controls-input','model':'deepseek-flash',
                'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':canonical_json(output)}}],
                'usage':{'prompt_tokens':29,'completion_tokens':19,'total_tokens':48}}
        return await super().model_reply(request)

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5)
            assert len(raw)<65536
            lines = raw.decode('ascii').split('\r\n')
            method,target,_ = lines[0].split(' ',2)
            headers = {line.split(':',1)[0].lower():line.split(':',1)[1].strip()
                for line in lines[1:] if ':' in line}
            size = int(headers.get('content-length','0'))
            assert 0<=size<=2*1024*1024
            body = await asyncio.wait_for(reader.readexactly(size),5) if size else b''
            path,status,extra,content_type = urlsplit(target).path,'200 OK','','text/html; charset=utf-8'
            if method=='POST' and path=='/chat/completions':
                response = canonical_json(await self.model_reply(json.loads(body))).encode()
                content_type = 'application/json'
            elif method=='GET' and path.startswith('/clicked/'):
                run_id = path.split('/')[-1]
                self.clicks[run_id] = self.clicks.get(run_id,0) + 1
                self.click_entered.setdefault(run_id,asyncio.Event()).set()
                if run_id in self.block_click:
                    await asyncio.wait_for(self.click_release.setdefault(run_id,asyncio.Event()).wait(),20)
                status,response,extra = '302 Found',b'','Location: /data/'+run_id+'\r\n'
            elif method=='GET' and (path.startswith('/start/') or path.startswith('/data/')):
                response = self.source_page(path)
            else:
                status,response = '404 Not Found',b'Owned controls fixture route unavailable'
            self.requests.append({'method':method,'path':path,'response_sha256':hashlib.sha256(response).hexdigest()})
            writer.write(('HTTP/1.1 '+status+'\r\nContent-Type: '+content_type+'\r\n'
                'Cache-Control: no-store\r\nConnection: close\r\n'+extra+'Content-Length: '+str(len(response))+'\r\n\r\n').encode()+response)
            await writer.drain()
        except (ConnectionError,asyncio.IncompleteReadError):
            pass
        except Exception as error:
            self.errors.append({'type':type(error).__name__,'phase':'fixture'})
        finally:
            writer.close()
            try:await writer.wait_closed()
            except ConnectionError:pass
            self.active.discard(task)


def seed_task(directory, origin, alias):
    """Trusted READY task/settings fixture; actual Run is created by POST start."""
    from pydantic import SecretStr
    from webagent.settings.models import ModelConnection, ModelSettingsRequest
    from webagent.settings.service import update_model
    from webagent.tasks.compiler import compile_draft
    from webagent.tasks.models import TaskContract
    directory.mkdir(parents=True)
    path = directory/'business.sqlite3'
    migrate(path)
    logical = ModelConnection(connect_seconds=1.0,read_seconds=10.0,total_seconds=15.0,max_tokens=8192)
    update_model(path,OwnedSecrets(),ModelSettingsRequest(expected_version=0,model=logical,
        api_key=SecretStr('owned-controls-synthetic-provider-key'),accept_data_sharing=True))
    task_id = 'controls-task-'+alias
    contract = compile_draft({'instruction':'Read the declared owned disclosure and retain control history',
        'scenario':'finance','source_ids':['local-fixture'],'parameters':{
            'entity_id':'owned-fixture-entity','report_version':'owned-disclosure-v1',
            'period_type':'annual','metrics':['revenue','profit'],'currency':'USD'}},
        task_id=task_id,version=1,created_at=utc_text(),provenance=[{
            'origin':'explicit_test_configuration','reference':'controls-acceptance-fixture-v1',
            'content_sha256':'a'*64,'authorizes_execution':True}]).contract
    contract['sources'] = [{'source_id':'owned-http','site_id':'owned-http','origin':origin,'path_prefix':'/'}]
    contract['start_urls'] = [origin+'/start/'+alias]
    contract['output_schema'] = [{'field_id':field,'required':True,'description':'Original '+field}
        for field in ('revenue','profit')]
    contract['time_scope'] = {'start':'2025-01-01T00:00:00Z','end':'2025-12-31T23:59:59Z','basis':'Explicit declared annual period'}
    contract['budget_profile'].update({'max_actions':12,'max_active_seconds':120})
    contract = TaskContract.model_validate_json(canonical_json(contract)).model_dump(mode='json')
    with connect(path) as db,transaction(db):
        create_task(db,task_id=task_id,instruction=contract['original_instruction'],requested_fields=['contract'])
        add_contract(db,contract)
        db.execute("UPDATE tasks SET preparation_status='READY',current_contract_version=1,requested_fields_json='[]',state_version=state_version+1 WHERE task_id=?",(task_id,))
    return task_id


def command_body(directory, *, task_id=None, run_id=None):
    with connect(directory/'business.sqlite3') as db:
        if run_id is not None:
            row = db.execute('SELECT state_version,contract_version FROM runs WHERE run_id=?',(run_id,)).fetchone()
            version_row = db.execute('SELECT settings_version FROM run_config_snapshots WHERE run_id=?',(run_id,)).fetchone()
            return {'expected_state_version':row['state_version'],'contract_version':row['contract_version'],
                'settings_version':version_row[0] if version_row else 0}
        row = db.execute('SELECT state_version,current_contract_version FROM tasks WHERE task_id=?',(task_id,)).fetchone()
        return {'expected_state_version':row['state_version'],'contract_version':row['current_contract_version'],
            'settings_version':db.execute('SELECT MAX(version) FROM model_settings_versions').fetchone()[0]}


def facts(directory, run_id):
    with connect(directory/'business.sqlite3') as db:
        db.execute('BEGIN')
        run = dict(db.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone())
        result = {'run':run}
        for table,condition,values,order in (
            ('steps','run_id=?',(run_id,),'sequence'),('task_events','run_id=?',(run_id,),'event_id'),
            ('run_budgets','run_id=?',(run_id,),'run_id'),('quota_debits','run_id=?',(run_id,),'debit_id'),
            ('run_controls','run_id=?',(run_id,),'operation_seq'),('graph_progress','run_id=?',(run_id,),'progress_id'),
            ('run_retry_operations','run_id=?',(run_id,),'operation_id'),
            ('budget_attempts','run_id=?',(run_id,),'attempt_id'),
            ('graph_recoveries','run_id=?',(run_id,),'recovery_seq'),('browser_sessions','owner_kind=? AND owner_id=?',('run',run_id),'created_at'),
            ('resource_leases','holder_run_id=?',(run_id,),'resource_key'),('write_intents','task_id=?',(run['task_id'],),'operation_id'),
            ('run_results','run_id=?',(run_id,),'run_id')):
            result[table] = [dict(row) for row in db.execute('SELECT * FROM '+table+' WHERE '+condition+' ORDER BY '+order,values)]
        result['queue'] = dict(db.execute('SELECT * FROM scheduler_queue WHERE run_id=?',(run_id,)).fetchone())
        result['integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
        result['foreign_keys'] = [list(row) for row in db.execute('PRAGMA foreign_key_check')]
    return result


class FixedHook:
    POINTS = frozenset({'verify_return_before','model_before','control_applied','graph_returned'})
    def __init__(self, directory, point=None, *, kill=False):
        if point is not None and point not in self.POINTS:raise ValueError('Unknown fixed controls fault')
        self.directory,self.point,self.kill,self.hit = directory,point,kill,False

    async def hook(self, stage, run_id):
        if self.hit or stage!=self.point:return
        self.hit = True
        write_json(self.directory/'boundary.json',{'stage':stage,'run_id':run_id,'pid':os.getpid()})
        if self.kill:
            os.kill(os.getpid(),signal.SIGSTOP)
        else:
            async with asyncio.timeout(25):
                while not (self.directory/'release-boundary').exists():await asyncio.sleep(.02)


def record_wait(directory, store, token, target):
    """Persistent CI/site/handoff fixture, not an implementation of those flows."""
    from webagent.events import WaitingEvent, append_event
    from webagent.graph.store import GraphStore
    due = datetime.now(timezone.utc)+timedelta(hours=1)
    kwargs = {'handoff_deadline':due,'control_owner':'human'} if target=='WAITING_HANDOFF' else {'next_eligible_at':due}
    store.defer(token,target,**kwargs)
    wait_id = 'owned-wait-'+token.run_id
    with connect(directory/'business.sqlite3') as db,transaction(db):
        state_version = db.execute('SELECT state_version FROM runs WHERE run_id=?',(token.run_id,)).fetchone()[0]
        reason = {'WAITING_CI':'ci','WAITING_SITE':'site','WAITING_HANDOFF':'handoff','PAUSED':'pause'}[target]
        append_event(db,run_id=token.run_id,expected_state_version=state_version,
            payload=WaitingEvent(wait_id=wait_id,reason=reason,deadline=due))
    GraphStore(directory/'business.sqlite3').record_wait_progress(token.run_id,wait_id,
        expected_state_version=state_version)


async def worker_process(directory, origin, *, point=None, kill=False, wait_state=None):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from webagent.graph.executor import GraphExecutor
    from webagent.graph.runtime import StateGraphAdapter
    from webagent.graph.store import GraphStore
    from webagent.gateway.service import BrowserGateway
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.scheduler.worker import QueueWorker
    from webagent.sessions.manager import ManagedBrowser
    from webagent.settings.service import load_run_config
    disable_external_tracing()
    settings = Settings(directory.resolve())
    store = SchedulerStore(settings.business_db,lease_seconds=180)
    manager = ManagedBrowser(settings,headless=True,auth_store=SyntheticAuth(),network_config=NetworkConfig(
        webarena_endpoints=(Endpoint('http','127.0.0.1',urlsplit(origin).port),)))
    hook = FixedHook(directory,point,kill=kill)
    metrics = {'pid':os.getpid(),'ticks':0,'active':{},'max_active':{},'invocations':{},'returned':{},'providers':0,'closed_providers':0}
    stopped = asyncio.Event()
    pump = None
    providers = []
    def publish():
        metrics['closed_providers'] = sum(provider.transport._client.is_closed for provider in providers)
        write_json(directory/'worker-metrics.json',metrics)
    try:
        await manager.start()
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute('PRAGMA synchronous=FULL')
            await saver.conn.execute('PRAGMA busy_timeout=5000')
            def provider_factory(run_id):
                logical = load_run_config(settings.business_db,OwnedSecrets(),run_id).model
                provider = OwnedProvider(logical,origin)
                providers.append(provider)
                metrics['providers'] += 1
                return provider
            def graph_factory(*args,**kwargs):
                graph = StateGraphAdapter(*args,**kwargs,fault_hook=hook.hook)
                original = graph.run
                async def observed(token):
                    result = await original(token)
                    saved = await graph.graph.aget_state({'configurable':{'thread_id':token.run_id}})
                    write_json(directory/('saved-'+token.run_id+'.json'),{'next':list(saved.next),'values':saved.values})
                    await hook.hook('graph_returned',token.run_id)
                    return result
                graph.run = observed
                return graph
            executor = GraphExecutor(settings,manager,checkpointer=saver,scheduler=store,secret_store=OwnedSecrets(),
                provider_factory=provider_factory,graph_factory=graph_factory)
            fixture_wait_done = False
            async def monitored(token):
                nonlocal fixture_wait_done
                run_id = token.run_id
                metrics['active'][run_id] = metrics['active'].get(run_id,0)+1
                metrics['max_active'][run_id] = max(metrics['max_active'].get(run_id,0),metrics['active'][run_id])
                metrics['invocations'][run_id] = metrics['invocations'].get(run_id,0)+1
                publish()
                try:
                    if wait_state and not fixture_wait_done:
                        fixture_wait_done = True
                        contract = GraphStore(settings.business_db).load_run(run_id)['contract']
                        from webagent.sessions.models import SessionOwner
                        owner = SessionOwner('run',run_id,'owned-http',realm='webarena')
                        session = await manager.create(owner,execution_token=token,gateway_downloads=True)
                        gateway = BrowserGateway.from_managed(manager,session,scheduler=store)
                        await gateway.observe(token)
                        await gateway.navigate(token,contract.start_urls[0],'owned-wait-navigation-'+run_id)
                        await gateway.observe(token)
                        await gateway.browser.aclose()
                        record_wait(directory,store,token,wait_state)
                        return
                    return await executor(token)
                finally:
                    metrics['active'][run_id] -= 1
                    metrics['returned'][run_id] = metrics['returned'].get(run_id,0)+1
                    publish()
            worker = QueueWorker(store,manager.manager_id,executor=monitored,poll_seconds=.02,
                heartbeat_seconds=.2,deadline_seconds=.05,
                controls=executor.controls,control_settler=executor.settle_control)
            original_tick = worker.tick
            async def tick():
                result = await original_tick()
                metrics['ticks'] += 1
                publish()
                return result
            worker.tick = tick
            pump = asyncio.create_task(worker.run(stopped))
            async with asyncio.timeout(90):
                while not (directory/'stop-worker').exists():
                    if pump.done():
                        await pump
                        raise AssertionError('Worker exited before owned stop')
                    await asyncio.sleep(.02)
            stopped.set()
            await pump
            metrics['worker_failed'] = worker._failed
            publish()
    finally:
        stopped.set()
        if pump is not None and not pump.done():await asyncio.wait_for(pump,8)
        for provider in providers:await provider.aclose()
        await manager.aclose()
        publish()


def owned_resources(origin, contract, run_id):
    assert all(source.origin==origin and source.site_id=='owned-http' for source in contract.sources)
    return [Resource.site_identity(source.site_id,contract.identity_ref,realm='webarena') for source in contract.sources]+[
        Resource.browser_context(run_id)]


async def api_process(directory, fd, origin):
    import uvicorn
    from webagent.api import create_app
    from webagent.controls.store import ControlStore
    disable_external_tracing()
    settings = Settings(directory.resolve())
    secrets = OwnedSecrets()
    app = create_app(settings,secret_store=secrets)
    app.state.control_store = ControlStore(settings.business_db,secret_store=secrets,
        resource_factory=lambda contract,run_id:owned_resources(origin,contract,run_id))
    server = uvicorn.Server(uvicorn.Config(app,fd=fd,access_log=False,log_level='warning'))
    await server.serve()


class Domain:
    def __init__(self, directory, fixture, alias):
        self.directory,self.fixture,self.alias = directory,fixture,alias
        self.task_id = seed_task(directory,fixture.origin,alias)
        self.worker,self.api,self.client,self.listener = None,None,None,None
        self.logs,self.requests,self.worker_pids = [],[],[]
        self.run_id = None
        self.worker_group,self.worker_kill_boundary = None,False

    async def start(self, *, mode='action'):
        import httpx
        from webagent.security import load_or_create_token
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1',0))
        self.listener.listen(128)
        port = self.listener.getsockname()[1]
        base = 'http://127.0.0.1:'+str(port)
        log = (self.directory/'api.log').open('wb')
        self.logs.append(log)
        self.api = await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).resolve()),
            '--api-dir',str(self.directory),'--api-fd',str(self.listener.fileno()),'--origin',self.fixture.origin,cwd=ROOT,
            pass_fds=(self.listener.fileno(),),stdout=log,stderr=asyncio.subprocess.STDOUT,
            env={**os.environ,'PYTHONUTF8':'1','WEBAGENT_API_PORT':str(port)})
        self.client = httpx.AsyncClient(base_url=base,trust_env=False,timeout=5,
            headers={'Authorization':'Bearer '+load_or_create_token(self.directory)})
        async with asyncio.timeout(20):
            while True:
                if self.api.returncode is not None:raise AssertionError('Owned API exited before ready')
                try:
                    response = await self.client.get('/health')
                    if response.status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.05)
        status,reply = await self.post('start',body=command_body(self.directory,task_id=self.task_id),key='start-'+self.alias)
        assert status==202 and reply['operation']['status']=='PENDING'
        self.run_id = reply['operation']['run_id']
        self.fixture.bind(self.alias,self.run_id,mode)
        return self

    async def post(self, action, *, body=None, key=None, run_id=None):
        run_id = run_id or self.run_id
        path = '/v1/tasks/'+self.task_id+'/'+action if action in ('start','retry') else '/v1/runs/'+run_id+'/'+action
        body = body or command_body(self.directory,run_id=run_id)
        response = await self.client.post(path,json=body,headers={'Idempotency-Key':key or str(uuid4())})
        reply = response.json()
        self.requests.append({'method':'POST','path':path,'status':response.status_code,
            'body':body,'operation_id':reply.get('operation',{}).get('operation_id'),'code':reply.get('code')})
        return response.status_code,reply

    async def operation(self, operation_id):
        response = await self.client.get('/v1/operations/'+operation_id)
        assert response.status_code==200
        return response.json()['operation']

    async def await_operation(self, operation_id, expected='APPLIED'):
        async with asyncio.timeout(35):
            while True:
                operation = await self.operation(operation_id)
                if operation['status']!='PENDING':
                    assert operation['status']==expected
                    return operation
                await self.ensure_alive()
                await asyncio.sleep(.03)

    async def start_worker(self, *, point=None, kill=False, wait_state=None):
        (self.directory/'stop-worker').unlink(missing_ok=True)
        (self.directory/'release-boundary').unlink(missing_ok=True)
        (self.directory/'boundary.json').unlink(missing_ok=True)
        command = [sys.executable,str(Path(__file__).resolve()),'--worker-dir',str(self.directory),
            '--origin',self.fixture.origin]
        if point:command += ['--point',point]
        if kill:command += ['--kill-boundary']
        if wait_state:command += ['--wait-state',wait_state]
        log = (self.directory/('worker-'+str(len(self.worker_pids))+'.log')).open('wb')
        self.logs.append(log)
        self.worker = await asyncio.create_subprocess_exec(*command,cwd=ROOT,stdout=log,stderr=asyncio.subprocess.STDOUT,
            env={**os.environ,'PYTHONUTF8':'1'},start_new_session=True)
        self.worker_pids.append(self.worker.pid)
        self.worker_group,self.worker_kill_boundary = self.worker.pid,kill

    async def ensure_alive(self):
        if self.worker is not None and self.worker.returncode is not None:
            raise AssertionError('Owned Worker exited unexpectedly')
        if self.api is not None and self.api.returncode is not None:
            raise AssertionError('Owned API exited unexpectedly')

    async def await_facts(self, predicate, run_id=None):
        run_id = run_id or self.run_id
        async with asyncio.timeout(40):
            while True:
                value = facts(self.directory,run_id)
                if predicate(value):return value
                await self.ensure_alive()
                await asyncio.sleep(.03)

    async def await_metrics(self, predicate):
        async with asyncio.timeout(35):
            while True:
                path = self.directory/'worker-metrics.json'
                if path.exists():
                    value = json.loads(path.read_text())
                    if predicate(value):return value
                await self.ensure_alive()
                await asyncio.sleep(.03)

    async def boundary(self):
        async with asyncio.timeout(35):
            while not (self.directory/'boundary.json').exists():
                await self.ensure_alive()
                await asyncio.sleep(.02)
        result = json.loads((self.directory/'boundary.json').read_text())
        assert result['pid']==self.worker.pid and result['run_id']==self.run_id
        return result

    async def _force_worker_exit(self):
        process = self.worker
        if process is None:return
        errors = []
        if process.returncode is None:
            try:
                # Only a live child created here with start_new_session may
                # authorize its private group. Never signal historical PIDs.
                if (process.pid not in self.worker_pids or self.worker_group!=process.pid
                        or os.getpgid(process.pid)!=self.worker_group):
                    raise AssertionError('Owned Worker process group changed')
                os.killpg(self.worker_group,signal.SIGKILL)
            except ProcessLookupError:pass
            except Exception as error:errors.append(error)
            # A group signal failure must not strand the stopped Python child
            # or prevent the independent API/client/socket cleanup below.
            try:process.kill()
            except ProcessLookupError:pass
            except Exception as error:errors.append(error)
        try:await asyncio.wait_for(process.wait(),10)
        except Exception as error:errors.append(error)
        if process.returncode is not None:
            self.worker,self.worker_group = None,None
        if errors:raise ExceptionGroup('Owned Worker termination failed',errors)
        return process.returncode

    async def kill_worker(self):
        assert self.worker is not None
        assert await self._force_worker_exit()==-signal.SIGKILL

    async def crash_snapshot(self):
        boundary = await self.boundary()
        # A whole-process SIGSTOP can freeze an independent SQLite writer
        # thread. Reap the killed process before opening a fresh WAL reader.
        await self.kill_worker()
        before = facts(self.directory,self.run_id)
        write_json(self.directory/'before-crash.json',{'boundary':boundary,
            'observed_after_sigkill':True,'business':before})
        return boundary,before

    async def stop_worker(self):
        if self.worker is not None and self.worker.returncode is None:
            (self.directory/'stop-worker').touch()
            await asyncio.wait_for(self.worker.wait(),12)
            assert self.worker.returncode==0
        self.worker = None
        self.worker_group = None

    async def close(self):
        errors = []
        try:
            if self.worker is not None and self.worker.returncode is None:
                if self.worker_kill_boundary:
                    # SIGSTOP suspends all threads; files cannot wake it.
                    await self._force_worker_exit()
                else:
                    (self.directory/'release-boundary').touch()
                    self.fixture.click_release.setdefault(self.run_id,asyncio.Event()).set()
                    await self.stop_worker()
        except Exception as error:
            errors.append(error)
        if self.worker is not None and self.worker.returncode is None:
            try:await self._force_worker_exit()
            except Exception as error:errors.append(error)
        if self.client is not None:
            try:await self.client.aclose()
            except Exception as error:errors.append(error)
        if self.api is not None and self.api.returncode is None:
            try:
                self.api.terminate()
                await asyncio.wait_for(self.api.wait(),8)
            except Exception as error:
                errors.append(error)
                try:
                    self.api.kill()
                    await asyncio.wait_for(self.api.wait(),8)
                except Exception as error:errors.append(error)
        if self.listener is not None:
            try:self.listener.close()
            except Exception as error:errors.append(error)
        for log in self.logs:
            try:log.close()
            except Exception as error:errors.append(error)
        try:write_json(self.directory/'api-requests.json',self.requests)
        except Exception as error:errors.append(error)
        try:
            write_json(self.directory/'process-exit.json',{'api_returncode':self.api.returncode if self.api else None,
                'worker_pids':self.worker_pids,'cleanup_errors':[error_details(error) for error in errors]})
        except Exception as error:errors.append(error)
        if errors:raise ExceptionGroup('Owned process cleanup failed',errors)


def assert_completed_event(value, operation):
    matches = [row for row in value['task_events'] if row['event_type']=='operation_completed'
        and json.loads(row['payload_json']).get('operation_id')==operation['operation_id']]
    assert len(matches)==1
    assert matches[0]['event_id']==operation['completed_event_id']
    payload = json.loads(matches[0]['payload_json'])
    assert payload['action']==operation['action'] and payload['status']==operation['status']
    assert payload['result_ref']==operation['operation_id']


def assert_resume_preserved(before, after):
    """Falsifiable ledger check for the same Run, including crash recovery."""
    from verify_recovery import assert_preserved
    assert len(before['run_budgets'])==len(after['run_budgets'])==1
    assert_preserved({**before,'budget':before['run_budgets'][0]},
                     {**after,'budget':after['run_budgets'][0]})


def assert_source_result(value, fixture, run_id):
    assert value['run']['state']=='SUCCEEDED' and len(value['run_results'])==1
    result = json.loads(value['run_results'][0]['result_json'])
    assert result['generated_by']=='business_aggregator' and result['run_id']==run_id
    assert [item['normalized_value'] for item in result['items']['values']] == [
        item['normalized_value'] for item in fixture.disclosure(run_id)['values']]


def add_write_fixture(directory, run_id, status):
    """Preserve trusted historical rows; no external write or M1-23 logic."""
    with connect(directory/'business.sqlite3') as db,transaction(db):
        task_id = db.execute('SELECT task_id FROM runs WHERE run_id=?',(run_id,)).fetchone()[0]
        operation_id = 'owned-side-effect-'+status.lower()
        now = utc_text()
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(operation_id,operation_id,task_id,run_id,
                'owned-fixture-target','create_pr','owned-synthetic-identity','owned-v1',status,now,now))
        if status=='UNKNOWN':
            key = Resource.site_identity('owned-http',realm='webarena').resource_key
            db.execute('INSERT INTO resource_quarantines VALUES(?,?,?)',(key,operation_id,now))
        return dict(db.execute('SELECT * FROM write_intents WHERE operation_id=?',(operation_id,)).fetchone())


async def verify(output, report):
    disable_external_tracing()
    fixture = await ControlsFixture(output).start()
    domains, outcomes = [], {}
    primary_error = None
    def check(name,condition=True):
        if not condition:
            report['failed_check'] = name
            raise AssertionError(name)
        report['checks'][name] = True
    async def domain(name,mode='action'):
        value = Domain(output/'domains'/name,fixture,name)
        domains.append(value)
        return await value.start(mode=mode)
    try:
        # One real in-flight browser click is held by the owned HTTP origin.
        active = await domain('inflight-pause-resume')
        fixture.block_click.add(active.run_id)
        await active.start_worker()
        async with asyncio.timeout(30):
            while not fixture.click_entered.setdefault(active.run_id,asyncio.Event()).is_set():
                await active.ensure_alive()
                await asyncio.sleep(.02)
        body = command_body(active.directory,run_id=active.run_id)
        status,accepted = await active.post('pause',body=body,key='owned-pause-key')
        check('pause_202_is_durable_acceptance_while_current_action_is_still_inflight',status==202
            and accepted['operation']['status']=='PENDING'
            and any(row['status']=='INTENT' for row in facts(active.directory,active.run_id)['steps']))
        status,replayed = await active.post('pause',body=body,key='owned-pause-key')
        check('identical_post_reuses_one_durable_operation',status==202 and replayed==accepted)
        status,_ = await active.post('pause',body={**body,'expected_state_version':body['expected_state_version']+1},key='owned-pause-key')
        check('same_key_different_body_is_rejected',status==409)
        status,_ = await active.post('pause',body={**body,'expected_state_version':body['expected_state_version']-1},key='owned-stale-key')
        check('old_state_version_is_rejected',status==409)
        fixture.click_release[active.run_id].set()
        pause_op = await active.await_operation(accepted['operation']['operation_id'])
        paused = await active.await_facts(lambda v:v['run']['state']=='PAUSED')
        await active.await_metrics(lambda m:m['returned'].get(active.run_id,0)==1 and m['active'].get(active.run_id)==0)
        saved = json.loads((active.directory/('saved-'+active.run_id+'.json')).read_text())
        check('current_atomic_action_settles_before_pause_and_interrupt',
            any(row['status']=='COMPLETED' and json.loads(row['action_json'])['action_type']=='click' for row in paused['steps'])
            and saved['next']==['wait'] and fixture.clicks[active.run_id]==1)
        check('pause_preserves_owned_session_and_account_reservation_but_releases_active_slot',
            any(row['state']=='OPEN' for row in paused['browser_sessions'])
            and any(row['resource_type']=='site_identity' for row in paused['resource_leases'])
            and not any(row['resource_type']=='active_slot' for row in paused['resource_leases']))
        assert_completed_event(paused,pause_op)
        fixture.phase[active.run_id] = 'B'
        status,resumed = await active.post('resume',key='owned-resume-key')
        check('resume_is_accepted_asynchronously',status==202)
        resume_op = await active.await_operation(resumed['operation']['operation_id'])
        finished = await active.await_facts(lambda v:v['run']['state']=='SUCCEEDED')
        metrics = await active.await_metrics(lambda m:m['returned'].get(active.run_id,0)==2 and m['active'].get(active.run_id)==0)
        check('same_run_and_thread_resume_has_only_one_invocation_at_a_time',
            finished['run']['thread_id']==active.run_id and metrics['max_active'][active.run_id]==1)
        check('resume_obtains_new_read_proof_and_never_replays_the_last_click',
            any(row['phase']=='COMPLETE' for row in finished['graph_recoveries']) and fixture.clicks[active.run_id]==1)
        check('same_run_resume_does_not_repeat_daily_quota',len(finished['quota_debits'])==1
            and finished['quota_debits']==paused['quota_debits'])
        assert_resume_preserved(paused,finished)
        assert_source_result(finished,fixture,active.run_id)
        check('resumed_result_matches_fresh_dynamic_source_and_preserves_the_original_ledger')
        assert_completed_event(finished,resume_op)
        status,_ = await active.post('resume',key='owned-terminal-resume')
        check('terminal_resume_is_rejected',status==409)
        outcomes['inflight-pause-resume'] = finished
        await active.stop_worker()

        # Actual graph boundaries hold RUNNING and VERIFYING for API cancel.
        for name,point,expected in (('cancel-running','model_before','RUNNING'),
                                    ('cancel-verifying','verify_return_before','VERIFYING')):
            item = await domain(name)
            await item.start_worker(point=point)
            await item.boundary()
            before = facts(item.directory,item.run_id)
            check(name+'_reaches_declared_real_graph_state',before['run']['state']==expected)
            status,reply = await item.post('cancel',key='owned-'+name)
            check(name+'_returns_pending_202',status==202 and reply['operation']['status']=='PENDING')
            (item.directory/'release-boundary').touch()
            op = await item.await_operation(reply['operation']['operation_id'])
            after = await item.await_facts(lambda v:v['run']['state']=='CANCELLED')
            await item.await_metrics(lambda m:m['active'].get(item.run_id)==0)
            assert_completed_event(after,op)
            check(name+'_cannot_aggregate_a_success_after_cancel',not after['run_results']
                or all(row['outcome']=='CANCELLED' for row in after['run_results']))
            outcomes[name] = after
            await item.stop_worker()

        # CI/site/handoff are explicit persistent fixture waits. The API and
        # idle Worker cancellation are production paths, with a real context.
        for target in ('WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED'):
            item = await domain('cancel-'+target.lower())
            await item.start_worker(wait_state=target)
            before = await item.await_facts(lambda v:v['run']['state']==target)
            await item.await_metrics(lambda m:m['active'].get(item.run_id)==0)
            request_index = len(fixture.requests)
            status,reply = await item.post('cancel')
            check(target.lower()+'_cancel_is_accepted',status==202)
            op = await item.await_operation(reply['operation']['operation_id'])
            after = await item.await_facts(lambda v:v['run']['state']=='CANCELLED')
            assert_completed_event(after,op)
            check(target.lower()+'_cancel_performs_no_browser_or_model_work',len(fixture.requests)==request_index)
            check(target.lower()+'_cancel_keeps_original_quota_and_history',before['quota_debits']==after['quota_debits']
                and all(row in after['task_events'] for row in before['task_events']))
            outcomes[target] = after
            await item.stop_worker()

        # Real OS death on either side of the durable wait/framework boundary.
        for name,point in (('wait-commit-crash','control_applied'),('interrupt-saved-crash','graph_returned')):
            item = await domain(name)
            fixture.block_click.add(item.run_id)
            await item.start_worker(point=point,kill=True)
            async with asyncio.timeout(30):
                while not fixture.click_entered.setdefault(item.run_id,asyncio.Event()).is_set():
                    await item.ensure_alive()
                    await asyncio.sleep(.02)
            status,pause_reply = await item.post('pause')
            check(name+'_uses_a_real_pending_user_pause',status==202 and pause_reply['operation']['status']=='PENDING')
            fixture.click_release[item.run_id].set()
            boundary,before = await item.crash_snapshot()
            pause_op = await item.await_operation(pause_reply['operation']['operation_id'])
            assert_completed_event(before,pause_op)
            check(name+'_business_wait_survives_actual_sigkill',before['run']['state']=='PAUSED'
                and any(row['event_type']=='wait_registered' for row in before['task_events'])
                and fixture.clicks[item.run_id]==1)
            if point=='graph_returned':
                saved = json.loads((item.directory/('saved-'+item.run_id+'.json')).read_text())
                check('interrupt_is_saved_before_second_crash',saved['next']==['wait'])
            fixture.phase[item.run_id] = 'B'
            status,reply = await item.post('resume')
            check(name+'_resume_is_accepted_for_same_run',status==202 and reply['operation']['run_id']==item.run_id)
            await item.start_worker()
            op = await item.await_operation(reply['operation']['operation_id'])
            after = await item.await_facts(lambda v:v['run']['state']=='SUCCEEDED')
            await item.await_metrics(lambda m:m['active'].get(item.run_id)==0)
            assert_completed_event(after,op)
            check(name+'_fresh_process_uses_business_proof_and_preserves_wait_and_debit',
                len(item.worker_pids)==2 and len(set(item.worker_pids))==2
                and any(row['phase']=='COMPLETE' for row in after['graph_recoveries'])
                and before['quota_debits']==after['quota_debits']
                and fixture.clicks[item.run_id]==1
                and len([row for row in after['task_events'] if row['event_type']=='wait_registered'])==1)
            assert_resume_preserved(before,after)
            assert_source_result(after,fixture,item.run_id)
            check(name+'_retains_original_budget_attempts_and_verifies_dynamic_result')
            outcomes[name] = {'boundary':boundary,'before':before,'after':after,'worker_pids':item.worker_pids}
            await item.stop_worker()

        # Reuse cancellation rather than manufacture a terminal business Run.
        for status in ('NOT_APPLIED','UNKNOWN'):
            item = await domain('retry-'+status.lower(),mode='input')
            await item.start_worker()
            await item.await_facts(lambda v:v['run']['state']=='PAUSED')
            await item.await_metrics(lambda m:m['active'].get(item.run_id)==0)
            old_write = add_write_fixture(item.directory,item.run_id,status)
            http_status,reply = await item.post('cancel')
            check(status.lower()+'_original_cancel_is_accepted',http_status==202)
            cancel_op = await item.await_operation(reply['operation']['operation_id'])
            before = await item.await_facts(lambda v:v['run']['state']=='CANCELLED')
            check(status.lower()+'_cancel_keeps_and_reports_the_original_side_effect',
                old_write in before['write_intents'] and any(
                    row['operation_id']==old_write['operation_id'] and row['status']==status
                    for row in cancel_op['result']['side_effects']))
            assert_completed_event(before,cancel_op)
            request_index,model_index = len(fixture.requests),len(fixture.provider_requests)
            http_status,reply = await item.post('retry')
            check(status.lower()+'_retry_is_an_accepted_new_run',http_status==202)
            child_run_id = reply['operation']['run_id']
            assert child_run_id!=item.run_id
            fixture.phase[child_run_id] = 'B'
            fixture.aliases[item.alias] = child_run_id
            retry_op = await item.await_operation(reply['operation']['operation_id'])
            if status=='UNKNOWN':
                prior_metrics = json.loads((item.directory/'worker-metrics.json').read_text())
                await item.await_metrics(lambda m:m['ticks']>=prior_metrics['ticks']+3)
                child = facts(item.directory,child_run_id)
                check('unknown_inherited_write_blocks_new_run_without_replay',
                    child['run']['state'] not in ('SUCCEEDED','PARTIAL')
                    and not child['quota_debits'] and len(fixture.requests)==request_index
                    and len(fixture.provider_requests)==model_index)
            else:
                child = await item.await_facts(lambda v:v['run']['state'] in ('SUCCEEDED','PARTIAL','FAILED'),child_run_id)
                await item.await_metrics(lambda m:m['active'].get(child_run_id)==0)
                check('linked_retry_spends_a_new_quota_only_for_the_new_claim',
                    len(child['quota_debits'])==1 and len(before['quota_debits'])==1
                    and child['quota_debits'][0]['debit_id']!=before['quota_debits'][0]['debit_id'])
            check(status.lower()+'_retry_links_new_thread_and_preserves_original_write_rows',
                child['run']['parent_run_id']==item.run_id and child['run']['thread_id']==child_run_id
                and child['run']['contract_sha256']==before['run']['contract_sha256']
                and old_write in child['write_intents'])
            check(status.lower()+'_retry_records_the_real_inherited_operation_reference',
                any(row['operation_id']==old_write['operation_id']
                    and row['originating_run_id']==item.run_id and row['recorded_status']==status
                    for row in child['run_retry_operations']))
            assert_completed_event(child,retry_op)
            outcomes['retry-'+status.lower()] = {'before':before,'child':child,'write':old_write}
            await item.stop_worker()

        check('all_owned_model_calls_use_filtered_inputs_without_tool_or_history_authority',
            bool(fixture.provider_requests) and all(row['tool_authority_absent'] and row['original_history_absent']
                for row in fixture.provider_requests))
        check('no_browser_write_is_sent',not any(row['method']=='POST' and row['path']!='/chat/completions' for row in fixture.requests))
        check('owned_fixture_has_no_errors',not fixture.errors)
        report['passed'] = True
    except Exception as error:
        primary_error = error
        report['primary_failure'] = error_details(error)
        raise
    finally:
        cleanup_errors = []
        for item in reversed(domains):
            try:await item.close()
            except Exception as error:cleanup_errors.append({'domain':item.alias,**error_details(error)})
        try:await fixture.close()
        except Exception as error:cleanup_errors.append({'domain':'http-fixture',**error_details(error)})
        report['scope_counts'] = {'isolated_domains':len(domains),'worker_processes':sum(len(d.worker_pids) for d in domains),
            'worker_pids':[pid for item in domains for pid in item.worker_pids],
            'api_processes':len(domains),'api_pids':[item.api.pid for item in domains if item.api is not None],
            'sigkill_windows':sum(
                (item.directory/'before-crash.json').exists() for item in domains),
            'http_model_requests':len(fixture.provider_requests)}
        write_json(output/'outcomes.json',outcomes)
        write_json(output/'http-records.json',{'requests':fixture.requests,'model_requests':fixture.provider_requests,'errors':fixture.errors})
        write_json(output/'generated-source.json',fixture.document())
        if cleanup_errors:
            report['passed'] = False
            report['cleanup_errors'] = cleanup_errors
            if primary_error is not None:raise primary_error
            raise AssertionError('Owned controls resources did not cleanly exit')
        post_exit = []
        for item in domains:
            with connect(item.directory/'business.sqlite3') as db:
                business = {'schema_version':db.execute('PRAGMA user_version').fetchone()[0],
                    'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],
                    'foreign_keys':[list(row) for row in db.execute('PRAGMA foreign_key_check')]}
            graph_path = item.directory/'graph.sqlite3'
            graph = None
            if graph_path.exists():
                with closing(sqlite3.connect(graph_path)) as db:
                    graph = {'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],
                        'foreign_keys':[list(row) for row in db.execute('PRAGMA foreign_key_check')]}
            post_exit.append({'domain':item.alias,'business':business,'graph':graph,
                'api_exited':item.api is not None and item.api.returncode is not None,
                'worker_exited':item.worker is None or item.worker.returncode is not None})
        write_json(output/'post-exit.json',post_exit)
        if report['passed']:
            check('all_owned_api_and_worker_processes_exit_before_artifact_hashing',
                all(row['api_exited'] and row['worker_exited'] for row in post_exit))
            check('both_database_kinds_are_consistent_after_all_processes_exit',all(
                row['business']['schema_version']==LATEST_VERSION and row['business']['integrity']=='ok'
                and not row['business']['foreign_keys'] and row['graph'] is not None
                and row['graph']['integrity']=='ok' and not row['graph']['foreign_keys'] for row in post_exit))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--worker-dir',type=Path)
    parser.add_argument('--api-dir',type=Path)
    parser.add_argument('--api-fd',type=int)
    parser.add_argument('--origin')
    parser.add_argument('--point',choices=sorted(FixedHook.POINTS))
    parser.add_argument('--kill-boundary',action='store_true')
    parser.add_argument('--wait-state',choices=('WAITING_CI','WAITING_SITE','WAITING_HANDOFF','PAUSED'))
    args = parser.parse_args()
    if args.api_dir:
        asyncio.run(api_process(args.api_dir.resolve(),args.api_fd,args.origin))
        return 0
    if args.worker_dir:
        asyncio.run(worker_process(args.worker_dir.resolve(),args.origin,point=args.point,kill=args.kill_boundary,wait_state=args.wait_state))
        return 0
    assert args.output_dir is not None
    output = args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=False)
    report = {'task':'M1-18','passed':False,'checks':{},'created_at':datetime.now(timezone.utc).isoformat(),
        'versions':{'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,'graph':GRAPH_VERSION,
            'state_schema':STATE_SCHEMA_VERSION,**{name:version(name) for name in ('langgraph','langgraph-checkpoint-sqlite','playwright')}},
        'scope':'Owned dynamic HTTP source, trusted HTTP provider replacement, authenticated API processes, production QueueWorker/GraphExecutor, independent business and async graph SQLite. Persistent CI/site/handoff and write rows are explicit fixture states; no M1-23 reconciliation is implemented or claimed.'}
    try:
        asyncio.run(verify(output,report))
    except Exception as error:
        report['passed'] = False
        report['error_type'] = type(error).__name__
        report['failure'] = error_details(error)
        if isinstance(error,BusinessError):report['error'] = {'code':error.code,'field':error.field,'status':error.status}
        report['error_locations'] = [{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
            for frame in traceback.extract_tb(error.__traceback__)]
    # Only after every owned child, browser, saver, API and fixture has exited.
    report['artifact_sha256'] = {str(path.relative_to(output)):hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob('*')) if path.is_file() and '.security' not in path.parts}
    write_json(output/'report.json',report)
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'report':str(output/'report.json')}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
