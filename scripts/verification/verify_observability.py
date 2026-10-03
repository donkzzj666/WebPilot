#!/usr/bin/env python3
"""M1-19: owned HTTP/model/Chromium/Worker diagnostics and outbound audit.

Only generated fixtures are used. Application logs are scanned separately from
restricted input/evidence artifacts. Network audits record actual calls rather
than inferring network behavior from environment variables.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import traceback
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).resolve().parent)]
TRACE_FLAGS = ('LANGSMITH_TRACING', 'LANGSMITH_TRACING_V2', 'LANGCHAIN_TRACING', 'LANGCHAIN_TRACING_V2')
CANARIES = {name: 'OBS_' + name.upper() + '_PRIVATE_LITERAL_9f873be169a14eb1' for name in
            ('prompt', 'page', 'cookie', 'model_key', 'provider_error', 'framework_payload')}
CANARIES['api_token'] = 'OBS_API_TOKEN_PRIVATE_' + '8' * 42
RUNS = ('obs-success', 'obs-failed')


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def artifact_hashes(output):
    # The clean-install evidence copier deliberately omits private credential
    # and original-input directories. Public manifests must remain verifiable.
    return {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob('*')) if path.is_file() and path.name != 'report.json'
            and not {'.security', '.private'} & set(path.relative_to(output).parts)}


def error_details(error, seen=None):
    seen = set() if seen is None else seen
    if id(error) in seen:
        return {'type': type(error).__name__, 'cycle': True}
    seen.add(id(error))
    value = {'type': type(error).__name__, 'message': str(error), 'locations': [
        {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
        for frame in traceback.extract_tb(error.__traceback__)]}
    if isinstance(error, OSError):
        value['errno'] = error.errno
    if isinstance(error, BaseExceptionGroup):
        value['exceptions'] = [error_details(item, seen) for item in error.exceptions]
    if error.__cause__ is not None:
        value['cause'] = error_details(error.__cause__, seen)
    elif error.__context__ is not None and not error.__suppress_context__:
        value['context'] = error_details(error.__context__, seen)
    return value


def is_loopback(host):
    value = str(host).lower().rstrip('.')
    if '://' in value:
        value = urlsplit(value).hostname or ''
    if value.startswith('['):
        value = value[1:].split(']', 1)[0]
    elif value.count(':') == 1:
        value = value.split(':', 1)[0]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == 'localhost'


class PythonAudit:
    def __init__(self, path):
        self.stream = Path(path).open('a', encoding='utf-8', buffering=1)

    def __call__(self, event, args):
        value = None
        if event in ('socket.connect', 'socket.sendto'):
            sock, address = args[0], args[1] if event == 'socket.connect' else args[-1]
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                value = {'event': event, 'host': address[0], 'port': address[1],
                         'loopback': is_loopback(address[0])}
        elif event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'):
            value = {'event': event, 'host': str(args[0]), 'loopback': is_loopback(args[0])}
        if value:
            self.stream.write(json.dumps({'pid': os.getpid(), **value}) + '\n')


def install_audits(directory, role):
    directory.mkdir(parents=True, exist_ok=True)
    inherited = {flag: os.environ.get(flag) for flag in TRACE_FLAGS}
    assert all(value == 'true' for value in inherited.values()), 'Probe must inherit enabled trace flags'
    write_json(directory / (role + '-inherited-tracing.json'), inherited)
    sys.addaudithook(PythonAudit(directory / (role + '-python-network.jsonl')))
    # This is a preload on the real Playwright Node process, not a fake driver.
    os.environ['NODE_OPTIONS'] = '--require=' + str(ROOT / 'scripts/verification/network-audit.cjs')
    os.environ['WEBAGENT_NETWORK_AUDIT'] = str(directory / 'node-network.jsonl')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')


class OwnedFixture:
    def __init__(self):
        self.active, self.requests, self.calls, self.errors = set(), [], {}, []
        self.provider_entered, self.provider_release = asyncio.Event(), asyncio.Event()
        self.values = [{'field_id': field, 'entity_id': 'owned-fixture-entity',
            'report_version': 'owned-disclosure-v1', 'period_start': '2025-01-01T00:00:00Z',
            'period_end': '2025-12-31T23:59:59Z', 'period_type': 'annual',
            'metric_definition': field, 'currency': 'USD', 'raw_value': amount,
            'disclosed_unit': 'unit', 'normalized_value': amount, 'value_origin': 'disclosed',
            'formula': None, 'rounding_rule': 'exact', 'rounding_lower': amount,
            'rounding_upper': amount, 'channel': 'owned-http'}
            for field, amount in (('revenue', '731.24'), ('profit', '96.57'))]

    @property
    def origin(self):
        return 'http://127.0.0.1:' + str(self.port)

    def document(self):
        return {'values': deepcopy(self.values), 'private_note': CANARIES['page'],
                'coverage': {'searched_sources': ['owned-http'], 'queries': [], 'cutoff_at': None,
                             'content_pages': 1, 'unread_candidates': [], 'gaps': [], 'complete': True}}

    def page(self, path):
        from webagent.db.repository import canonical_json
        document = ({'next_url': self.origin + '/full', 'private_note': CANARIES['page']}
                    if path == '/start' else self.document())
        return ('<!doctype html><meta charset="utf-8"><title>Owned observability source</title><pre>'
                + html.escape(canonical_json(document)) + '</pre>').encode()

    async def model_reply(self, request):
        from webagent.db.repository import canonical_json
        assert len(request['messages']) == 2 and 'tools' not in request
        payload = json.loads(request['messages'][1]['content'])
        run_id = payload['run_id']
        assert run_id in RUNS
        count = self.calls.get(run_id, 0) + 1
        self.calls[run_id] = count
        observation, checkpoint = payload['observation'], payload['verified_checkpoint']
        self.requests.append({'kind': 'model', 'run_id': run_id, 'number': count,
            'prompt_canary_seen': CANARIES['prompt'] in canonical_json(payload),
            'page_canary_seen': CANARIES['page'] in canonical_json(payload),
            'request_sha256': hashlib.sha256(canonical_json(request).encode()).hexdigest()})
        self.provider_entered.set()
        await self.provider_release.wait()
        if run_id == 'obs-failed' and count > 1:
            return '500 Internal Server Error', {'error': {'message': CANARIES['provider_error']}}
        if run_id == 'obs-failed':
            output = {'type': 'RequestEvidence', 'criterion_ids': [payload['contract']['acceptance_criteria'][0]['criterion_id']],
                      'source_ids': ['owned-http'], 'needed': 'Read another independent declared disclosure'}
        elif 'next_url' in json.loads(observation['visible_excerpt']):
            output = {'type': 'Action', 'action': {'run_id': run_id, 'step_id': 'obs-model-navigation',
                'epoch': checkpoint['epoch'], 'snapshot_id': observation['snapshot_id'], 'expected_effect': 'read',
                'action_type': 'navigate', 'target': {'page_url': observation['source_url'],
                    'tab_id': observation['tab_id'], 'frame_id': observation['frame_id'],
                    'locator': None, 'write_scope': None}, 'args': {'url': self.origin + '/full'}}}
        else:
            document = json.loads(observation['visible_excerpt'])
            refs = observation['evidence_ids']
            output = {'type': 'ProposeResult', 'items': {'scenario': 'finance',
                      'values': [{**value, 'evidence_ids': refs} for value in document['values']]},
                      'coverage': document['coverage'], 'evidence_ids': refs, 'unresolved': [], 'existing_operation_ids': []}
        return '200 OK', {'id': 'owned-observability-' + str(count), 'model': 'deepseek-flash',
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': canonical_json(output)}}],
            'usage': {'prompt_tokens': 23, 'completion_tokens': 17, 'total_tokens': 40}}

    async def serve(self, reader, writer):
        from webagent.db.repository import canonical_json
        task = asyncio.current_task()
        self.active.add(task)
        try:
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            assert len(head) < 65536
            lines = head.decode('ascii').split('\r\n')
            method, target, _ = lines[0].split(' ', 2)
            headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                       for line in lines[1:] if ':' in line}
            size = int(headers.get('content-length', '0'))
            assert 0 <= size <= 2 * 1024 * 1024
            body = await asyncio.wait_for(reader.readexactly(size), 5) if size else b''
            path, extra = urlsplit(target).path, ''
            if method == 'POST' and path == '/chat/completions':
                assert headers.get('authorization') == 'Bearer ' + CANARIES['model_key']
                status, data = await self.model_reply(json.loads(body))
                data, kind = canonical_json(data).encode(), 'application/json'
            elif method == 'GET' and path in ('/start', '/full'):
                status, data, kind = '200 OK', self.page(path), 'text/html; charset=utf-8'
                extra = 'Set-Cookie: obs_private=' + CANARIES['cookie'] + '; HttpOnly; SameSite=Strict\r\n'
            else:
                status, data, kind = '404 Not Found', b'Owned route absent', 'text/plain'
            self.requests.append({'kind': 'http', 'method': method, 'path': path, 'status': status[:3],
                'cookie_canary_received': CANARIES['cookie'] in headers.get('cookie', ''),
                'response_sha256': hashlib.sha256(data).hexdigest()})
            writer.write(('HTTP/1.1 ' + status + '\r\nContent-Type: ' + kind + '\r\n' + extra
                + 'Cache-Control: no-store\r\nConnection: close\r\nContent-Length: ' + str(len(data))
                + '\r\n\r\n').encode() + data)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
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
        self.provider_release.set()
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*tuple(self.active), return_exceptions=True)


def seed(directory, origin):
    from webagent.db import connect, migrate, transaction
    from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
    from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
    from webagent.models.transport import ModelConfig
    from webagent.scheduler.models import Resource
    from webagent.scheduler.store import SchedulerStore
    from webagent.tasks.compiler import compile_draft
    from webagent.tasks.models import TaskContract
    directory.mkdir(parents=True)
    path = directory / 'business.sqlite3'
    migrate(path)
    configs = {}
    for run_id in RUNS:
        config = ModelConfig(base_url=origin, connect_seconds=1.0, read_seconds=5.0, total_seconds=8.0,
            max_tokens=8192, **({'pricing': {'currency': 'USD', 'input_per_million': '2',
                                           'output_per_million': '3'}} if run_id == 'obs-success' else {}))
        configs[run_id] = config.model_dump(mode='json')
        contract = compile_draft({'instruction': 'Read both original annual metrics. ' + CANARIES['prompt'],
            'scenario': 'finance', 'source_ids': ['local-fixture'], 'parameters': {
                'entity_id': 'owned-fixture-entity', 'report_version': 'owned-disclosure-v1',
                'period_type': 'annual', 'metrics': ['revenue', 'profit'], 'currency': 'USD'}},
            task_id='task-' + run_id, version=1, created_at=utc_text(), provenance=[{
                'origin': 'explicit_test_configuration', 'reference': 'observability-acceptance-v1',
                'content_sha256': 'a' * 64, 'authorizes_execution': True}]).contract
        contract['sources'] = [{'source_id': 'owned-http', 'site_id': 'owned-http', 'origin': origin, 'path_prefix': '/'}]
        contract['start_urls'] = [origin + ('/start' if run_id == 'obs-success' else '/full')]
        contract['output_schema'] = [{'field_id': field, 'required': True, 'description': 'Original ' + field}
                                     for field in ('revenue', 'profit')]
        contract['time_scope'] = {'start': '2025-01-01T00:00:00Z', 'end': '2025-12-31T23:59:59Z', 'basis': 'Owned annual period'}
        contract['budget_profile'].update(max_actions=12, max_active_seconds=120, max_recoveries_per_obstacle=2)
        contract = TaskContract.model_validate_json(canonical_json(contract)).model_dump(mode='json')
        with connect(path) as db, transaction(db):
            create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'], requested_fields=['contract'])
            add_contract(db, contract)
            create_run(db, run_id=run_id, task_id=contract['task_id'], contract_version=1,
                graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
                model_config_sha256=config.config_sha256, runtime_config_sha256='b' * 64)
        SchedulerStore(path, lease_seconds=180).enqueue(run_id, [Resource.site_identity('owned-http', realm='webarena'),
            Resource.browser_context(run_id)], expected_state_version=0, queue_class='webarena')
    write_json(directory / 'owned-model-configs.json', configs)
    secure = directory / '.security'
    secure.mkdir(mode=0o700)
    token = secure / 'local-api-token'
    token.write_text(CANARIES['api_token'])
    token.chmod(0o600)


def ledger(directory, run_id):
    from webagent.db import connect
    with connect(directory / 'business.sqlite3') as db:
        db.execute('BEGIN')
        return {'run': dict(db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()),
            'events': [dict(row) for row in db.execute('SELECT * FROM task_events WHERE run_id=? ORDER BY event_id', (run_id,))],
            'progress': [dict(row) for row in db.execute('SELECT * FROM graph_progress WHERE run_id=? ORDER BY progress_id', (run_id,))],
            'steps': [dict(row) for row in db.execute('SELECT * FROM steps WHERE run_id=? ORDER BY sequence', (run_id,))],
            'models': [dict(row) for row in db.execute('SELECT rowid AS model_cursor,* FROM model_attempts WHERE run_id=? ORDER BY rowid', (run_id,))],
            'budget': dict(db.execute('SELECT * FROM run_budgets WHERE run_id=?', (run_id,)).fetchone())}


def assert_diagnostics(value, original):
    assert value['source'] == 'persistent_business_ledgers'
    for field in ('run_id', 'task_id', 'thread_id', 'graph_version', 'graph_state_schema_version', 'state', 'state_version'):
        assert value['run'][field] == original['run'][field]
    assert [event['event_id'] for event in value['events']] == [event['event_id'] for event in original['events']]
    progress = {row['progress_id']: row for row in original['progress']}
    steps = {row['step_id']: row for row in original['steps']}
    for event, raw in zip(value['events'], original['events']):
        assert event['event_type'] == raw['event_type'] and event['state_version'] == raw['state_version']
        payload = json.loads(raw['payload_json'])
        assert event['step_id'] == payload.get('step_id')
        if event['step_id']:
            assert event['current_step_status'] == steps[event['step_id']]['status']
        else:
            assert event['operation_id'] == payload.get('operation_id')
        for link in event['graph']:
            p = progress[link['progress_id']]
            assert p['business_event_id'] == event['event_id']
            assert link['node'] == p['phase'] and link['checkpoint_id'] == p['checkpoint_id']
    assert {link['progress_id'] for event in value['events'] for link in event['graph']} == set(progress)
    assert [item['model_cursor'] for item in value['model_attempts']] == [row['model_cursor'] for row in original['models']]
    for model, row in zip(value['model_attempts'], original['models']):
        record = json.loads(row['record_json']) if row['record_json'] else {}
        assert model['request_id'] == row['request_id'] and model['status'] == row['status']
        assert model['error_class'] == record.get('error_class')
        assert model['price_version'] == record.get('price_version')
        assert model['estimated_cost'] == record.get('estimated_cost')
    for counter in ('actions_used', 'content_pages_used', 'active_ms', 'model_calls_used', 'observations_used', 'screenshots_used'):
        assert value['budget'][counter] == original['budget'][counter]


def assert_costs(success, failed):
    assert success['model_attempts'] and failed['model_attempts']
    for item in success['model_attempts']:
        assert item['usage_known'] and item['usage_complete']
        assert item['price_version'].startswith('price-') and item['cost_known']
        assert item['cost_currency'] == 'USD'
        expected = (Decimal(item['usage']['input_tokens']) * 2 + Decimal(item['usage']['output_tokens']) * 3) / 1000000
        assert Decimal(item['estimated_cost']) == expected
    known = [item for item in failed['model_attempts'] if item['usage_known']]
    unknown = [item for item in failed['model_attempts'] if not item['usage_known']]
    assert known and unknown, 'Failure must distinguish known unpriced and unknown usage'
    assert all(item['price_version'] is None and item['estimated_cost'] is None and not item['cost_known']
               for item in failed['model_attempts'])
    assert all(item['error_class'] == 'provider_error' for item in unknown)
    assert failed['model_summary']['unknown_usage_attempts'] == len(unknown)


def scan_logs(paths, canaries=CANARIES):
    files, records = 0, 0
    for path in paths:
        data = Path(path).read_bytes()
        files += 1
        for name, literal in canaries.items():
            assert literal.encode() not in data, 'Sensitive literal appeared in application log: ' + name
        if '.jsonl' in path.name and not path.name.endswith('.lock'):
            for line in data.splitlines():
                value = json.loads(line)
                assert isinstance(value, dict) and 'event' in value
                assert not ({'prompt', 'messages', 'cookie', 'authorization', 'api_key', 'payload', 'reasoning', 'content'} & set(value))
                records += 1
    assert files and records, 'Empty logs cannot prove redaction'
    return {'files_scanned': files, 'structured_records': records, 'canary_categories': sorted(canaries)}


def assert_log_correlations(paths, originals):
    records = [json.loads(line) for path in paths for line in path.read_bytes().splitlines()]
    for run_id in RUNS:
        rows = [row for row in records if row.get('run_id') == run_id and row['event'].startswith('graph_')]
        assert rows and any(row['event'] == 'graph_checkpoint_saved' for row in rows)
        for row in rows:
            assert row['thread_id'] == run_id and row['task_id'] == originals[run_id]['run']['task_id']
            assert row['graph_version'] == originals[run_id]['run']['graph_version']
            event = next(event for event in originals[run_id]['events'] if event['event_id'] == row['event_id'])
            assert row['state_version'] == event['state_version']
            if row.get('step_id'):
                assert row['step_id'] in {step['step_id'] for step in originals[run_id]['steps']}
            if row.get('progress_id'):
                progress = next(item for item in originals[run_id]['progress'] if item['progress_id'] == row['progress_id'])
                assert progress['business_event_id'] == row['event_id']
                assert progress['checkpoint_id'] == row.get('business_checkpoint_id')
                assert progress.get('verification_id') == row.get('verification_id')
    assert any(row.get('node') == 'verify' and row.get('run_id') == 'obs-success' for row in records)
    assert any(row.get('run_id') == 'obs-failed' and row.get('error_class') for row in records)


def read_framework_history(path):
    """Read exact stored references after owned processes have exited.

    This does not open an async saver, repair a graph, or write a checkpoint.
    Only the two actual graph Runs participate in this evidence projection.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from webagent.graph.models import GraphSnapshot
    fields = ('run_id', 'graph_version', 'state_schema_version', 'state_version',
              'business_event_id', 'progress_id', 'business_checkpoint_id', 'completed', 'verified_summary_refs')
    history = {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows = db.execute('''SELECT thread_id,checkpoint_ns,checkpoint_id,type,checkpoint FROM checkpoints
            WHERE thread_id IN (?,?) AND checkpoint_ns='' ORDER BY thread_id,checkpoint_id LIMIT 4097''', RUNS).fetchall()
        assert len(rows) <= 4096, 'Owned graph history exceeded the bounded acceptance projection'
        serializer = JsonPlusSerializer()
        for row in rows:
            assert len(row['checkpoint']) <= 2 * 1024 * 1024
            checkpoint = serializer.loads_typed((row['type'], row['checkpoint']))
            assert isinstance(checkpoint, dict) and checkpoint['id'] == row['checkpoint_id']
            UUID(row['checkpoint_id'])
            channels = checkpoint['channel_values']
            assert isinstance(channels, dict)
            state = {key: value for key, value in channels.items() if key in GraphSnapshot.model_fields}
            if not state:
                state = channels['__start__']
            state = GraphSnapshot.model_validate(state).state()
            assert state['run_id'] == row['thread_id']
            key = (row['thread_id'], row['checkpoint_id'])
            assert key not in history
            history[key] = {'thread_id': row['thread_id'], 'checkpoint_ns': row['checkpoint_ns'],
                            'checkpoint_id': row['checkpoint_id'], **{field: state[field] for field in fields}}
    assert all(any(key[0] == run_id for key in history) for run_id in RUNS)
    return history


def assert_logged_framework_refs(paths, history):
    records = [json.loads(line) for path in paths for line in path.read_bytes().splitlines()]
    checkpoints = [row for row in records if row['event'] == 'graph_checkpoint_saved']
    assert checkpoints
    for row in checkpoints:
        assert row['run_id'] in RUNS and row['thread_id'] == row['run_id']
        saved = history[(row['thread_id'], row['checkpoint_id'])]
        assert saved['thread_id'] == saved['run_id'] == row['run_id'] and saved['checkpoint_ns'] == ''
        assert saved['checkpoint_id'] == row['checkpoint_id']
        for logged, stored in (('graph_version', 'graph_version'), ('state_schema_version', 'state_schema_version'),
                ('state_version', 'state_version'), ('event_id', 'business_event_id'),
                ('progress_id', 'progress_id'), ('business_checkpoint_id', 'business_checkpoint_id')):
            assert row.get(logged) == saved[stored], 'Log reference does not match its exact stored checkpoint: ' + logged
    assert {row['run_id'] for row in checkpoints} == set(RUNS)
    return {'logged_checkpoints': len(checkpoints), 'stored_checkpoints': len(history),
            'scope': 'Exact stored checkpoint IDs and references for the two owned graph Runs; historical IDs are permitted'}


def response_ids(response, **bindings):
    value = {'request_id': response.headers['x-request-id'],
             'transport_request_id': response.headers['x-transport-request-id'],
             'http_status': response.status_code, **bindings}
    for field in ('request_id', 'transport_request_id'):
        identifier = UUID(value[field])
        assert identifier.version == 4 and str(identifier) == value[field]
    return value


def assert_http_log_bindings(paths, expected):
    records = [json.loads(line) for path in paths for line in path.read_bytes().splitlines()]
    requests = [row for row in records if row['event'] == 'api_request']
    assert requests
    attempts = [row['transport_request_id'] for row in requests]
    assert len(attempts) == len(set(attempts)), 'Each real HTTP attempt needs a unique transport ID'
    for row in requests:
        for field in ('request_id', 'transport_request_id'):
            assert UUID(row[field]).version == 4
    for item in expected:
        matches = [row for row in requests if row['transport_request_id'] == item['transport_request_id']]
        assert len(matches) == 1
        assert all(matches[0].get(key) == value for key, value in item.items())


def assert_invocation_outcomes(outcome):
    assert outcome['error'] is None and not outcome['cleanup_errors'] and not outcome['worker_failed']
    invocations = outcome['invocations']
    assert set(invocations) == set(RUNS)
    for run_id, state in (('obs-success', 'SUCCEEDED'), ('obs-failed', 'FAILED')):
        row = invocations[run_id]
        assert row['finally_exited'] and row['durable_state'] == state and row['queue_status'] == 'FINISHED'
        assert row['settlement_error'] is None
        checkpoint = row['framework_checkpoint']
        assert UUID(checkpoint['checkpoint_id']) and 0 <= checkpoint['state_version'] <= row['state_version']
        if run_id == 'obs-success' or row['returned']:
            assert row['returned'] and row['exception'] is None
        else:
            # The independent deadline may cancel a recovery-limit executor
            # after durable FAILED. Preserve that actual outcome as cancellation.
            assert row['blocked_reason'] == 'recovery_limit'
            error = row['exception']
            assert ((error['type'] == 'CancelledError' and error['cancelled'])
                    or error['status'] == 409 and error['code'] in ('BUDGET_EXCEEDED', 'RESOURCE_CONFLICT', 'STATE_CONFLICT'))
    assert set(outcome['returned']) == {key for key, row in invocations.items() if row['returned']}


def assert_framework_refs(invocations, originals):
    for run_id in RUNS:
        row = invocations[run_id]['framework_checkpoint']
        events = {event['event_id']: event for event in originals[run_id]['events']}
        progress = {item['progress_id']: item for item in originals[run_id]['progress']}
        assert events[row['business_event_id']]['state_version'] == row['state_version']
        assert progress[row['progress_id']]['state_version'] == row['state_version']
        assert progress[row['progress_id']]['business_event_id'] == row['business_event_id']
    failed = invocations['obs-failed']['framework_checkpoint']
    assert failed['state_version'] == 1 and not failed['completed'], 'Budget stop retains the real historical saver, not a fabricated terminal checkpoint'


def assert_network_audits(python_events, node_events, chromium, proxy_events):
    assert any(item['event'] == 'socket.connect' and item['loopback'] for item in python_events)
    assert not [item for item in python_events if not item['loopback']]
    assert any(item['event'] == 'audit_loaded' for item in node_events)
    for item in node_events:
        if item['event'] == 'net.connect' and item.get('transport') == 'tcp' or item['event'].startswith('dns.'):
            assert is_loopback(item['host'])
        if item['event'].startswith('dgram.'):
            raise AssertionError('Unexpected Node datagram activity requires attribution')
    assert chromium['known_loopback_connect_observed'] and not chromium['non_loopback']
    trace_names = ('langsmith', 'langchain.com', 'smith.langchain', 'api.smith')
    encoded = json.dumps([python_events, node_events, chromium, proxy_events]).lower()
    assert not any(name in encoded for name in trace_names), 'External framework tracing was attempted'
    assert any(item['outcome'] == 'allowed' for item in proxy_events), 'Actual source proxy traffic must be observed'
    return {'python_events': len(python_events), 'node_events': len(node_events),
            'chromium_events': chromium['event_count'], 'proxy_allowed': sum(item['outcome'] == 'allowed' for item in proxy_events),
            'proxy_denied': sum(item['outcome'] == 'denied' for item in proxy_events),
            'trace_connections': 0, 'scope': 'Owned Python socket APIs, Node net/dns/dgram APIs, Chromium netlog and managed proxy; not OS-wide packet capture'}


class OwnedProcess:
    def __init__(self, command, env, log_path, *, pass_fds=()):
        self.log = Path(log_path).open('wb')
        try:
            self.proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                         start_new_session=True, pass_fds=pass_fds)
        except BaseException:
            self.log.close()
            raise
        self.pgid = os.getpgid(self.proc.pid)
        assert self.pgid == self.proc.pid

    def alive(self):
        assert self.proc.poll() is None, 'Owned process exited before expected completion'

    def kill_owned_group(self):
        if self.proc.poll() is None:
            assert os.getpgid(self.proc.pid) == self.pgid == self.proc.pid
            os.killpg(self.pgid, signal.SIGKILL)

    async def close(self):
        try:
            if self.proc.poll() is None:
                assert os.getpgid(self.proc.pid) == self.pgid == self.proc.pid
                self.proc.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.proc.wait), 12)
                except TimeoutError:
                    self.kill_owned_group()
                    await asyncio.wait_for(asyncio.to_thread(self.proc.wait), 8)
            else:
                self.proc.wait()
        finally:
            self.log.close()


async def api_child(directory, port, fd):
    from webagent.observability.standard import install_safe_standard_logging
    install_safe_standard_logging(directory, 'api')
    import uvicorn
    from webagent.api import create_app
    from webagent.config import Settings
    server = uvicorn.Server(uvicorn.Config(create_app(Settings(directory), secret_store=object()),
        host='127.0.0.1', port=port, access_log=False, log_level='warning', log_config=None))
    listener = socket.socket(fileno=fd)
    await server.serve(sockets=[listener])


async def worker_child(directory, origin):
    # Imports below occur after the actual socket audit and before the graph is
    # constructed. Product tracing guards must override inherited true flags.
    from webagent.observability.standard import install_safe_standard_logging
    install_safe_standard_logging(directory, 'worker')
    # Exercise the installed library handler with a message that cannot be
    # logged verbatim. This emits only its trusted local error classification.
    import logging
    logging.getLogger('langgraph').warning(CANARIES['framework_payload'])
    from webagent.config import Settings, disable_external_tracing
    disable_external_tracing()
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from playwright.async_api import async_playwright
    from webagent.graph.executor import GraphExecutor
    from webagent.models.transport import DeepSeekTransport, ModelConfig
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.network.proxy import EgressProxy
    from webagent.observability.logging import SafeJSONLLogger
    from webagent.observability.logging import TrustedGraphDiagnostics
    from webagent.scheduler.store import SchedulerStore
    from webagent.scheduler.worker import QueueWorker
    from webagent.sessions.manager import ManagedBrowser
    settings, proxies = Settings(directory), []
    configs = {key: ModelConfig.model_validate(value) for key, value in json.loads((directory / 'owned-model-configs.json').read_text()).items()}
    stopped, returned, invocations = asyncio.Event(), set(), {}
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    real = async_playwright
    class AuditedPlaywright:
        async def start(self):
            return await self.__aenter__()
        async def __aenter__(self):
            self.context = real()
            self.playwright = await self.context.__aenter__()
            launch = self.playwright.chromium.launch
            async def audited_launch(**kwargs):
                assert '--proxy-bypass-list=<-loopback>' in kwargs.get('args', []) or kwargs.get('proxy')
                kwargs['args'] = [*kwargs.get('args', []), '--log-net-log=' + str(directory / 'chromium-netlog.json'),
                                  '--net-log-capture-mode=Default']
                return await launch(**kwargs)
            return SimpleNamespace(chromium=SimpleNamespace(launch=audited_launch), stop=self.playwright.stop)
        async def __aexit__(self, *args):
            return await self.context.__aexit__(*args)
    def proxy_factory(*args, **kwargs):
        proxy = EgressProxy(*args, **kwargs)
        proxies.append(proxy)
        return proxy
    manager = ManagedBrowser(settings, headless=True, playwright_factory=AuditedPlaywright, proxy_factory=proxy_factory,
        network_config=NetworkConfig(webarena_endpoints=(Endpoint('http', '127.0.0.1', urlsplit(origin).port),)))
    store, worker, pump, error, cleanup = SchedulerStore(settings.business_db, lease_seconds=180), None, None, None, []
    logger = SafeJSONLLogger(directory / 'logs/worker.jsonl')
    logger.emit('service_started', service='worker', pid=os.getpid())
    try:
        await manager.start()
        async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
            await saver.setup()
            await saver.conn.execute('PRAGMA synchronous=FULL')
            diagnostics = TrustedGraphDiagnostics(directory, logger, checkpointer=saver)
            def provider_factory(run_id):
                return DeepSeekTransport(configs[run_id], CANARIES['model_key'], allow_test_loopback=True)
            executor = GraphExecutor(settings, manager, checkpointer=saver, scheduler=store,
                secret_store=object(), provider_factory=provider_factory, diagnostics=diagnostics)
            async def monitored(token):
                result_returned, exception = False, None
                try:
                    result = await executor(token)
                    result_returned = True
                    returned.add(token.run_id)
                    write_json(directory / 'worker-returned.json', sorted(returned))
                    return result
                except BaseException as caught:
                    exception = {'type': type(caught).__name__, 'code': getattr(caught, 'code', None),
                                 'status': getattr(caught, 'status', None),
                                 'cancelled': isinstance(caught, asyncio.CancelledError)}
                    raise
                finally:
                    snapshot, framework_checkpoint, settlement_error = {}, None, None
                    try:
                        # Record only actual saver references. Production graph
                        # diagnostics owns log emission; no checkpoint is written
                        # or repaired by this acceptance wrapper.
                        saved = await saver.aget_tuple({'configurable': {'thread_id': token.run_id}})
                        assert saved is not None
                        channels = saved.checkpoint['channel_values']
                        framework_checkpoint = {'checkpoint_id': saved.checkpoint['id'],
                            **{key: channels[key] for key in ('state_version', 'business_event_id', 'progress_id', 'completed')}}
                        from webagent.db import connect
                        with connect(settings.business_db) as db:
                            db.execute('BEGIN')
                            snapshot = dict(db.execute('''SELECT r.state AS durable_state,r.state_version,
                                r.blocked_reason,q.status AS queue_status FROM runs r JOIN scheduler_queue q USING(run_id)
                                WHERE r.run_id=?''', (token.run_id,)).fetchone())
                    except BaseException as caught:
                        settlement_error = {'type': type(caught).__name__, 'code': getattr(caught, 'code', None)}
                    invocations[token.run_id] = {**snapshot, 'returned': result_returned, 'exception': exception,
                        'finally_exited': True, 'framework_checkpoint': framework_checkpoint, 'settlement_error': settlement_error}
                    write_json(directory / 'worker-invocations.json', invocations)
            worker = QueueWorker(store, manager.manager_id, executor=monitored, poll_seconds=.02,
                heartbeat_seconds=.2, deadline_seconds=.05, controls=executor.controls,
                control_settler=executor.settle_control)
            await worker.start()
            logger.emit('worker_ready', service='worker', pid=os.getpid(), worker_id=manager.manager_id,
                        worker_generation=worker.generation)
            write_json(directory / 'worker-ready.json', {'pid': os.getpid(), 'worker_id': manager.manager_id,
                'generation': worker.generation, 'tracing': {flag: os.environ.get(flag) for flag in TRACE_FLAGS}})
            # A fake framework tuple is presented to the trusted adapter only;
            # it never replaces the real saver or mutates a business event.
            pump = asyncio.create_task(worker.run(stopped))
            async with asyncio.timeout(120):
                while not stopped.is_set():
                    if pump.done():
                        await pump
                        raise AssertionError('Worker pump stopped unexpectedly')
                    if set(invocations) == set(RUNS) and not (directory / 'framework-canary-checked.json').exists():
                        assert_invocation_outcomes({'invocations': invocations, 'returned': sorted(returned),
                            'error': None, 'cleanup_errors': [], 'worker_failed': worker._failed})
                        await framework_canary(directory, diagnostics, saver, logger)
                    await asyncio.sleep(.05)
            stopped.set()
            await pump
    except Exception as caught:
        error = error_details(caught)
    finally:
        stopped.set()
        for method in ([lambda: worker.aclose()] if worker is not None else []) + [manager.aclose]:
            try:
                await method()
            except Exception as caught:
                cleanup.append(error_details(caught))
        if pump is not None and not pump.done():
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        write_json(directory / 'proxy-events.json', [item for proxy in proxies for item in proxy.events])
        write_json(directory / 'worker-outcome.json', {'error': error, 'cleanup_errors': cleanup,
            'returned': sorted(returned), 'invocations': invocations, 'worker_failed': bool(worker and worker._failed)})
        logger.emit('worker_stopped', service='worker', pid=os.getpid())
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    if error is not None:
        raise RuntimeError('Owned Worker failed; see worker-outcome.json')


async def framework_canary(directory, diagnostics, saver, logger):
    from webagent.observability.logging import TrustedGraphDiagnostics
    saved = await saver.aget_tuple({'configurable': {'thread_id': 'obs-success'}})
    assert saved is not None
    original = ledger(directory, 'obs-success')['events']
    class BadSaver:
        async def aget_tuple(self, config):
            return saved._replace(metadata={**saved.metadata, 'raw_framework_payload': CANARIES['framework_payload']})
    rejected = await TrustedGraphDiagnostics(directory, logger, checkpointer=BadSaver()).checkpoint('obs-success')
    assert rejected is False
    assert ledger(directory, 'obs-success')['events'] == original
    write_json(directory / 'framework-canary-checked.json', {'rejected': True, 'business_events_unchanged': True})


async def read_pages(client, run_id, headers):
    events, models, after, model_after, latest = [], [], 0, 0, None
    for _ in range(100):
        response = await client.get('/v1/diagnostics/runs/' + run_id,
            params={'after': after, 'model_after': model_after, 'limit': 5}, headers=headers)
        assert response.status_code == 200
        latest = response.json()
        assert not latest['graph_links_truncated'], 'Graph references must not be silently omitted'
        events.extend(latest['events'])
        models.extend(latest['model_attempts'])
        assert latest['next_event_cursor'] >= after and latest['next_model_cursor'] >= model_after
        if not latest['events'] and not latest['model_attempts']:
            break
        assert latest['next_event_cursor'] > after or latest['next_model_cursor'] > model_after
        after, model_after = latest['next_event_cursor'], latest['next_model_cursor']
    else:
        raise AssertionError('Diagnostic pagination failed to terminate')
    return {**latest, 'events': events, 'model_attempts': models}


def metrics_fixture(directory):
    """Explicit ledger-only data for positive counts; no remote effect exists."""
    from webagent.db import connect, transaction
    from webagent.db.repository import create_run, utc_text
    from webagent.graph.models import GRAPH_VERSION, STATE_SCHEMA_VERSION
    from webagent.graph.recovery import RecoveryStore
    from webagent.scheduler.models import Resource
    from webagent.scheduler.store import SchedulerStore
    run_id, worker_id = 'obs-metrics-fixture', 'obs-metrics-bookkeeper'
    path, now = directory / 'business.sqlite3', utc_text()
    store = SchedulerStore(path, lease_seconds=180)
    with connect(path) as db, transaction(db):
        source = db.execute('SELECT * FROM runs WHERE run_id="obs-failed"').fetchone()
        create_run(db, run_id=run_id, task_id=source['task_id'], contract_version=source['contract_version'],
            graph_version=GRAPH_VERSION, graph_state_schema_version=STATE_SCHEMA_VERSION,
            model_config_sha256=source['model_config_sha256'], runtime_config_sha256=source['runtime_config_sha256'],
            parent_run_id=source['run_id'])
    store.enqueue(run_id, [Resource.site_identity('owned-http', realm='webarena'), Resource.browser_context(run_id)],
                  expected_state_version=0, queue_class='webarena')
    generation = store.start_worker(worker_id)
    try:
        token = store.claim(worker_id, generation)
        assert token is not None and token.run_id == run_id
        store.abandon(token)
        token = store.claim(worker_id, generation)
        assert token is not None and store.validate(token, allow_reconciling=True)['state'] == 'RECONCILING'
        recovery = RecoveryStore(directory)
        assert recovery.begin(token, None)['allowed']
        recovery.blocked(token, 'session_unavailable')
        store.abandon(token)
        # This immutable diagnostic fixture is explicitly not a browser write.
        with connect(path) as db, transaction(db):
            db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
                target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'UNKNOWN',?,?)''', ('obs-unknown-ledger-fixture', 'obs-unknown-key',
                source['task_id'], run_id, 'repository_write:fixture/observability', 'commit',
                'synthetic-metrics-account', 'synthetic-before', now, now))
            leases = db.execute('SELECT resource_key FROM resource_leases WHERE holder_run_id=? AND resource_type="site_identity"', (run_id,)).fetchall()
            assert leases
            db.executemany('INSERT INTO resource_quarantines VALUES(?,?,?)',
                          [(row['resource_key'], 'obs-unknown-ledger-fixture', now) for row in leases])
    finally:
        store.stop_worker(worker_id, generation)
    return run_id


def assert_metrics_projection(metrics, directory):
    from webagent.db import connect
    with connect(directory / 'business.sqlite3') as db:
        db.execute('BEGIN')
        for section, key, table, column in (('queue', 'status_counts', 'scheduler_queue', 'status'),
                ('recovery', 'phase_counts', 'graph_recoveries', 'phase'), ('writes', 'status_counts', 'write_intents', 'status')):
            expected = {row[0]: row[1] for row in db.execute('SELECT ' + column + ',count(*) FROM ' + table + ' GROUP BY ' + column)}
            assert metrics[section][key] == expected
        assert metrics['writes']['quarantines'] == db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0]
        assert metrics['recovery']['budget_attempt_count'] == db.execute("SELECT count(*) FROM budget_attempts WHERE kind='recovery'").fetchone()[0]
        counters = [value for row in db.execute('SELECT recovery_counts_json FROM run_budgets')
                    for value in json.loads(row[0]).values()]
        assert metrics['recovery']['counter_groups'] == len(counters)
        assert metrics['recovery']['unknown_counter_groups'] == 0
        assert metrics['recovery']['budget_counter_total'] == metrics['recovery']['known_budget_counter_total'] == sum(counters)
        assert metrics['recovery']['phase_counts_are_receipts'] is True
        assert metrics['budget']['content_pages_used'] == db.execute('SELECT sum(content_pages_used) FROM run_budgets').fetchone()[0]
        assert_queue_timings(metrics['queue'], db, metrics['as_of'])
    assert metrics['queue']['status_counts']['RECOVERY'] == 1
    assert metrics['recovery']['phase_counts']['BEGIN'] == 1 and metrics['recovery']['phase_counts']['BLOCKED'] >= 1
    assert metrics['writes']['status_counts']['UNKNOWN'] == 1 and metrics['writes']['quarantines'] >= 1


def assert_queue_timings(queue, db, as_of):
    """Compare one stable ledger to the API's own wall-clock snapshot time."""
    def time(value):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    def elapsed(start, end):
        delta = time(end) - time(start)
        assert delta.total_seconds() >= 0
        return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
    def bucket(values):
        return {'count': len(values), 'known_count': len(values), 'unknown_count': 0,
                'known_total_ms': sum(values), 'max_ms': max(values) if values else None}
    assert queue['timing'] == 'wall_clock_snapshot'
    assert queue['current_wait_basis'] == 'scheduler_queue_updated_at'
    assert queue['completed_wait_source'] == 'scheduler_events_first_enqueued_to_first_claimed'
    rows = list(db.execute('SELECT q.status,q.updated_at,r.state FROM scheduler_queue q JOIN runs r USING(run_id)'))
    waiting = ('WAITING_CI', 'WAITING_SITE', 'WAITING_HANDOFF', 'PAUSED')
    for state in ('QUEUED', 'RECOVERY'):
        values = [elapsed(row['updated_at'], as_of) for row in rows if row['status'] == state and row['state'] not in waiting]
        assert queue['current_pending_wait_age_ms'][state] == bucket(values)
    for state in waiting:
        values = [elapsed(row['updated_at'], as_of) for row in rows if row['state'] == state]
        assert queue['wait_state_revision_age_ms'][state] == bucket(values)
    completed = db.execute("""SELECT min(CASE WHEN event_type='enqueued' THEN occurred_at END) AS enqueued,
        min(CASE WHEN event_type='claimed' THEN occurred_at END) AS claimed FROM scheduler_events
        WHERE event_type IN ('enqueued','claimed') GROUP BY run_id HAVING claimed IS NOT NULL""")
    assert queue['completed_enqueue_to_first_claim_ms'] == bucket([elapsed(row['enqueued'], row['claimed']) for row in completed])


async def verify(output, report):
    import httpx
    from verify_m1_01 import chromium_audit
    fixture = await OwnedFixture().start()
    directory = output / 'owned-data'
    processes, cleanup, primary, http_bindings = [], [], None, []
    listener = socket.socket()
    def check(name, condition):
        assert condition, name
        report['checks'][name] = True
    try:
        seed(directory, fixture.origin)
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend'), 'WEBAGENT_API_PORT': str(port),
            'WEBAGENT_DATA_DIR': str(directory), **{flag: 'true' for flag in TRACE_FLAGS},
            'LANGSMITH_API_KEY': CANARIES['model_key'], 'LANGCHAIN_API_KEY': CANARIES['model_key']}
        command = [sys.executable, str(Path(__file__).resolve()), '--directory', str(directory), '--origin', fixture.origin]
        api = OwnedProcess([*command, '--child', 'api', '--port', str(port), '--fd', str(listener.fileno())],
            env, output / 'api.stdout.log', pass_fds=(listener.fileno(),))
        processes.append(api)
        headers = {'Authorization': 'Bearer ' + CANARIES['api_token']}
        async with httpx.AsyncClient(base_url='http://127.0.0.1:' + str(port), timeout=5, trust_env=False) as client:
            async with asyncio.timeout(20):
                while True:
                    api.alive()
                    try:
                        health = await client.get('/health', headers=headers)
                        if health.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)
            check('api_health_is_independent_before_worker', health.status_code == 200 and health.json()['tasks_success_implied'] is False)
            absent = await client.get('/v1/health/worker', headers=headers)
            check('unstarted_worker_is_unhealthy', absent.status_code == 503 and not absent.json()['ready'])
            unauthorized = await client.get('/v1/metrics')
            check('diagnostics_retain_api_auth_boundary', unauthorized.status_code == 401)
            http_bindings.append(response_ids(unauthorized, method='GET'))
            assert unauthorized.json()['request_id'] == http_bindings[-1]['request_id']
            queued = (await client.get('/v1/metrics', headers=headers)).json()
            check('queue_metrics_read_real_enqueued_runs', queued['queue']['status_counts']['QUEUED'] == 2
                  and queued['run_state_counts']['QUEUED'] == 2)
            worker = OwnedProcess([*command, '--child', 'worker'], env, output / 'worker.stdout.log')
            processes.append(worker)
            async with asyncio.timeout(30):
                await fixture.provider_entered.wait()
            healthy = await client.get('/v1/health/worker', headers=headers)
            active = await read_pages(client, 'obs-success', headers)
            check('healthy_services_do_not_imply_task_success', healthy.status_code == 200 and healthy.json()['ready']
                  and healthy.json()['tasks_success_implied'] is False
                  and active['run']['state'] not in ('SUCCEEDED', 'PARTIAL'))
            fixture.provider_release.set()
            async with asyncio.timeout(90):
                while True:
                    api.alive()
                    worker.alive()
                    if (directory / 'framework-canary-checked.json').exists():
                        break
                    await asyncio.sleep(.05)
            before = {run_id: ledger(directory, run_id) for run_id in RUNS}
            diagnostic = {run_id: await read_pages(client, run_id, headers) for run_id in RUNS}
            for run_id in RUNS:
                assert_diagnostics(diagnostic[run_id], before[run_id])
            check('real_worker_graph_has_success_and_failure', diagnostic['obs-success']['run']['state'] == 'SUCCEEDED'
                  and diagnostic['obs-failed']['run']['state'] == 'FAILED')
            check('persistent_event_step_node_checkpoint_correlations_match',
                  any(event['step_id'] for event in diagnostic['obs-success']['events'])
                  and any(link['node'] == 'verify' for event in diagnostic['obs-success']['events'] for link in event['graph'])
                  and any(link['checkpoint_id'] for event in diagnostic['obs-success']['events'] for link in event['graph']))
            assert_costs(diagnostic['obs-success'], diagnostic['obs-failed'])
            check('versioned_synthetic_prices_and_unknown_usage_are_distinct', True)
            check('raw_framework_payload_cannot_replace_business_events',
                  json.loads((directory / 'framework-canary-checked.json').read_text())['business_events_unchanged'])
            metrics = (await client.get('/v1/metrics', headers=headers)).json()
            check('terminal_and_budget_metrics_match_actual_ledgers', metrics['run_state_counts'] == {'FAILED': 1, 'SUCCEEDED': 1}
                  and metrics['budget']['actions_used'] == sum(value['budget']['actions_used'] for value in before.values())
                  and metrics['budget']['model_calls_used'] == sum(value['budget']['model_calls_used'] for value in before.values()))
            check('unimplemented_quality_metrics_remain_unavailable', bool(metrics['unavailable_metrics'])
                  and all(value['reason'] == 'not_implemented' for value in metrics['unavailable_metrics']))
            write_json(output / 'api-diagnostics.json', diagnostic)
            write_json(output / 'metrics.json', metrics)
            await worker.close()
            check('owned_worker_shutdown_completed', worker.proc.returncode == 0)
            worker_outcome = json.loads((directory / 'worker-outcome.json').read_text())
            assert_invocation_outcomes(worker_outcome)
            assert_framework_refs(worker_outcome['invocations'], before)
            check('actual_executor_return_or_budget_cancellation_is_recorded_and_settled', True)
            check('failed_framework_checkpoint_retains_verified_historical_refs', True)
            write_json(output / 'invocation-outcomes.json', worker_outcome['invocations'])
            health = await client.get('/health', headers=headers)
            stopped = await client.get('/v1/health/worker', headers=headers)
            check('stopped_worker_unhealthy_while_api_stays_healthy', health.status_code == 200
                  and stopped.status_code == 503 and stopped.json()['state'] == 'STOPPED'
                  and stopped.json()['tasks_success_implied'] is False)
            # The process has actually exited and been reaped before the final
            # ledger baseline is compared, including any shutdown bookkeeping.
            after = {run_id: ledger(directory, run_id) for run_id in RUNS}
            check('health_reads_and_shutdown_do_not_rewrite_business_events',
                  all(before[key]['events'] == after[key]['events'] and before[key]['steps'] == after[key]['steps'] for key in RUNS))
            write_json(output / 'health.json', {'api': health.json(), 'worker': stopped.json()})
            fixture_run = metrics_fixture(directory)
            from webagent.graph.store import GraphStore
            current = GraphStore(directory / 'business.sqlite3').load_run(fixture_run)
            control_headers = {**headers, 'Idempotency-Key': 'owned-metrics-cancel'}
            control_body = {'expected_state_version': current['state_version'], 'contract_version': 1, 'settings_version': 0,
                            'idempotency_key': 'owned-metrics-cancel'}
            accepted = await client.post('/v1/runs/' + fixture_run + '/cancel', headers=control_headers, json=control_body)
            assert accepted.status_code == 202
            operation = accepted.json()['operation']
            replay = await client.post('/v1/runs/' + fixture_run + '/cancel', headers=control_headers, json=control_body)
            assert replay.status_code == 202 and replay.json() == accepted.json()
            control_ids = [response_ids(response, method='POST', task_id=operation['task_id'], run_id=fixture_run,
                operation_id=operation['operation_id'], event_id=operation['requested_event_id']) for response in (accepted, replay)]
            assert control_ids[0]['transport_request_id'] != control_ids[1]['transport_request_id']
            http_bindings.extend(control_ids)
            check('accepted_operation_is_correlated_without_fabricated_completion', operation['status'] == 'PENDING')
            fixture_diagnostic = await read_pages(client, fixture_run, headers)
            assert_diagnostics(fixture_diagnostic, ledger(directory, fixture_run))
            check('operation_id_points_to_its_persisted_request_event',
                sum(item['event_id'] == operation['requested_event_id'] and item['operation_id'] == operation['operation_id']
                    for item in fixture_diagnostic['events']) == 1)
            # Fixture preparation exercises a persisted business HTTP reply;
            # it never starts another Run or invokes a provider/browser.
            preparation_headers = {**headers, 'Idempotency-Key': 'owned-http-preparation', 'X-Request-ID': CANARIES['framework_payload']}
            preparation_body = {'instruction': 'Prepare owned HTTP audit. ' + CANARIES['prompt'], 'compiler_mode': 'fixture'}
            first = await client.post('/v1/tasks', headers=preparation_headers, json=preparation_body)
            again = await client.post('/v1/tasks', headers=preparation_headers, json=preparation_body)
            assert first.status_code == again.status_code == 201 and first.content == again.content
            task = first.json()['task']
            assert task['current_run_id'] is None and first.json()['current_run'] is None
            task_ids = [response_ids(response, method='POST', task_id=task['task_id']) for response in (first, again)]
            assert task_ids[0]['request_id'] == task_ids[1]['request_id'] == first.json()['request_id']
            assert task_ids[0]['transport_request_id'] != task_ids[1]['transport_request_id']
            http_bindings.extend(task_ids)
            check('business_reply_id_is_stable_with_distinct_transport_attempt_ids', True)
            metrics = (await client.get('/v1/metrics', headers=headers)).json()
            assert_metrics_projection(metrics, directory)
            check('queue_recovery_unknown_and_quarantine_metrics_match_sql', True)
            check('queue_wall_clock_timings_and_recovery_budget_counters_match_sql', True)
            write_json(output / 'ledger-only-metrics-fixture.json', {'scope': 'Synthetic ledger-only metrics fixture; no physical write, no model/browser execution, not a main business outcome',
                'run_id': fixture_run, 'operation': operation, 'diagnostics': fixture_diagnostic, 'metrics': metrics})
        await api.close()
        log_paths = [output / 'api.stdout.log', output / 'worker.stdout.log', *(directory / 'logs').glob('*.jsonl*')]
        report['log_scan'] = scan_logs(log_paths)
        graph_log_paths = list((directory / 'logs').glob('worker.jsonl*'))
        assert_log_correlations(graph_log_paths, before)
        history = read_framework_history(directory / 'graph.sqlite3')
        write_json(output / 'framework-history.json', {'checkpoints': list(history.values())})
        report['framework_log_refs'] = assert_logged_framework_refs(graph_log_paths, history)
        assert_http_log_bindings(list((directory / 'logs').glob('api.jsonl*')), http_bindings)
        write_json(output / 'http-correlation.json', {'responses': http_bindings,
            'control_replay': 'same persisted operation/requested event; unique transport attempts',
            'task_preparation_replay': 'same persisted business request ID; unique transport attempts; no additional Run'})
        check('application_logs_contain_no_canary_originals', True)
        check('safe_graph_logs_bind_real_event_node_step_checkpoint_ids', True)
        check('logged_framework_checkpoint_ids_match_exact_stored_history', True)
        check('api_logs_bind_real_http_ids_and_committed_control_ids', True)
        py = [json.loads(line) for path in directory.glob('*-python-network.jsonl') for line in path.read_text().splitlines()]
        node = [json.loads(line) for line in (directory / 'node-network.jsonl').read_text().splitlines()]
        chromium = chromium_audit(directory / 'chromium-netlog.json')
        proxy = json.loads((directory / 'proxy-events.json').read_text())
        report['network_audit'] = assert_network_audits(py, node, chromium, proxy)
        write_json(output / 'network-summary.json', {'summary': report['network_audit'], 'chromium': chromium, 'proxy': proxy})
        check('actual_outbound_audit_has_no_external_framework_trace', True)
        model = [request for request in fixture.requests if request['kind'] == 'model']
        check('owned_provider_remains_allowed_and_sensitive_inputs_exercised', bool(model)
              and any(item['prompt_canary_seen'] for item in model) and any(item['page_canary_seen'] for item in model)
              and any(item.get('cookie_canary_received') for item in fixture.requests))
        check('owned_fixture_has_no_unexpected_errors', not fixture.errors)
        report['scope_counts'] = {'real_runs': 2, 'real_child_processes': 2,
            'owned_model_http_requests': len(model), 'synthetic_origins': 1, 'ledger_only_metric_runs': 1,
            'additional_preparation_only_tasks': 1}
    except BaseException as error:
        primary = error
        report['error'] = error_details(error)
    finally:
        for process in reversed(processes):
            try:
                await process.close()
            except BaseException as error:
                cleanup.append(error_details(error))
        for close in (listener.close,):
            try:
                close()
            except BaseException as error:
                cleanup.append(error_details(error))
        try:
            await fixture.close()
        except BaseException as error:
            cleanup.append(error_details(error))
        try:
            write_json(output / 'owned-http-records.json', {'requests': fixture.requests, 'errors': fixture.errors})
        except BaseException as error:
            cleanup.append(error_details(error))
        report['cleanup_errors'] = cleanup
    if primary is not None:
        raise primary
    assert not cleanup, 'Owned cleanup failed independently of the primary result'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', '--output-dir', dest='output', type=Path)
    parser.add_argument('--child', choices=('api', 'worker'))
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--origin')
    parser.add_argument('--port', type=int)
    parser.add_argument('--fd', type=int)
    args = parser.parse_args()
    if args.child:
        directory = args.directory.resolve()
        install_audits(directory, args.child)
        # Do not import a framework before its product tracing guard is called.
        from webagent.config import disable_external_tracing
        disable_external_tracing()
        asyncio.run(api_child(directory, args.port, args.fd) if args.child == 'api' else worker_child(directory, args.origin))
        return 0
    if args.output is None:
        parser.error('--output is required')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'checks': {}, 'scope': 'M1-19 owned process/HTTP/model/Chromium/local diagnostics; no real accounts or paid services'}
    try:
        asyncio.run(verify(output, report))
        report['passed'] = True
    except BaseException as error:
        report.setdefault('error', error_details(error))
    finally:
        # Child processes and fixture connections have exited before hashes are
        # taken, including SQLite files and late network-audit flushes.
        report['artifact_sha256'] = artifact_hashes(output)
        report['private_input_scope'] = {'categories': sorted(CANARIES), 'manifest_excluded_directories': ['.security', '.private'],
                                         'scope': 'Generated canaries only; private inputs are not copied into clean-install evidence'}
        write_json(output / 'report.json', report)
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(output / 'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
