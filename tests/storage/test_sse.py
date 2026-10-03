"""Real loopback HTTP streams: disconnect, process restart and paginated replay."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from webagent.config import Settings
from webagent.security import load_or_create_token
from webagent.db import LATEST_VERSION, connect, transaction
from webagent.events import WaitingEvent, append_event
from webagent.state import transition, transition_in_transaction
from webagent import sse
from conftest import seed

ROOT = Path(__file__).resolve().parents[2]


class Server:
    def __init__(self, path):
        self.path = path
        self.headers = {"Authorization": "Bearer " + load_or_create_token(path.parent)}
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(128)
        self.url = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
        self.log = (path.parent / 'api-test.log').open('ab')
        self.process = None

    def start(self):
        self.process = subprocess.Popen([
            sys.executable, '-m', 'uvicorn', 'webagent.api:create_app', '--factory',
            '--fd', str(self.socket.fileno()), '--no-access-log', '--timeout-graceful-shutdown', '2',
        ], pass_fds=(self.socket.fileno(),), stdout=self.log, stderr=subprocess.STDOUT,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend'),
                 'WEBAGENT_DATA_DIR': str(self.path.parent), 'WEBAGENT_API_PORT': str(self.socket.getsockname()[1])})
        deadline = time.monotonic() + 10
        with httpx.Client(headers=self.headers, trust_env=False, timeout=.5) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError((self.path.parent / 'api-test.log').read_text())
                try:
                    if client.get(self.url + '/health').status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
        raise AssertionError('API startup timed out')

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(6)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
                raise AssertionError('SSE API failed to stop gracefully')

    def close(self):
        try:
            self.stop()
        finally:
            self.socket.close()
            self.log.close()


@pytest.fixture
def server(database):
    with connect(database) as db, transaction(db):
        seed(db)
    server = Server(database)
    try:
        server.start()
        yield server
    finally:
        server.close()


def next_event(lines):
    fields = {}
    for line in lines:
        if not line:
            if 'data' in fields:
                event = json.loads(fields['data'])
                assert int(fields['id']) == event['event_id']
                assert fields['event'] == event['event_type'] == event['payload']['event_type']
                assert set(event) == {'event_id', 'task_id', 'run_id', 'event_type', 'state_version', 'occurred_at', 'payload'}
                return event
            fields = {}
        elif not line.startswith(':'):
            key, value = line.split(':', 1)
            fields[key] = value.lstrip(' ')
    raise AssertionError('Stream ended before next event')


def move(server, target, version):
    return transition(server.path, run_id='run-1', expected_state_version=version, target=target)


def test_disconnect_and_restart_replay_then_follow_live_events(server):
    first = move(server, 'RUNNING', 0)
    second = move(server, 'VERIFYING', 1)
    with httpx.Client(base_url=server.url, headers=server.headers, trust_env=False, timeout=3) as client:
        with client.stream('GET', '/v1/events') as response:
            assert response.status_code == 200
            assert response.headers['content-type'].startswith('text/event-stream')
            assert response.headers['x-accel-buffering'] == 'no'
            assert response.headers['cache-control'] == 'no-store, no-transform'
            assert next_event(response.iter_lines()) == first
        # This intentionally disconnects before acknowledging the second event.
        server.stop()
        server.start()
        with client.stream('GET', '/v1/events', headers={'Last-Event-ID': str(first['event_id'])}) as response:
            lines = response.iter_lines()
            assert next_event(lines) == second
            third = move(server, 'RUNNING', 2)
            assert next_event(lines) == third
            fourth = move(server, 'CANCELLED', 3)
            assert next_event(lines) == fourth
        assert client.get('/health').json()['storage']['schema_version'] == LATEST_VERSION
        # Latest cursor does not replay an acknowledged event.
        with client.stream('GET', '/v1/events', headers={'Last-Event-ID': str(fourth['event_id'])}, timeout=.6) as response:
            with pytest.raises(httpx.ReadTimeout):
                next_event(response.iter_lines())
        assert client.get('/health').status_code == 200


def test_sse_never_reads_uncommitted_or_rolled_back_state(server):
    with httpx.Client(base_url=server.url, headers=server.headers, trust_env=False, timeout=.6) as client:
        with pytest.raises(RuntimeError):
            with connect(server.path) as db, transaction(db):
                transition_in_transaction(db, run_id='run-1', expected_state_version=0, target='RUNNING')
                with client.stream('GET', '/v1/events') as response:
                    with pytest.raises(httpx.ReadTimeout):
                        next_event(response.iter_lines())
                assert client.get('/health').status_code == 200
                raise RuntimeError('rollback writer')
        first = move(server, 'CANCELLED', 0)
        with client.stream('GET', '/v1/events') as response:
            assert next_event(response.iter_lines()) == first


def test_sse_replays_multiple_pages_with_global_cursor_and_filters(server):
    with connect(server.path) as db, transaction(db):
        seed(db, task_id='task-2', run_id='run-2')
        expected = []
        for index in range(205):
            event = append_event(db, run_id='run-1' if index % 2 == 0 else 'run-2',
                                 expected_state_version=0,
                                 payload=WaitingEvent(wait_id=f'w-{index}', reason='site'))
            if index % 2 == 0:
                expected.append(event)
    with httpx.Client(base_url=server.url, headers=server.headers, trust_env=False, timeout=3) as client:
        with client.stream('GET', '/v1/events?task_id=task-1&run_id=run-1') as response:
            lines = response.iter_lines()
            actual = [next_event(lines) for _ in expected]
            assert actual == expected  # 103 matching records crosses a page boundary.
        with client.stream('GET', '/v1/events?run_id=run-1', headers={'Last-Event-ID': '2'}) as response:
            assert next_event(response.iter_lines()) == expected[1]
        with client.stream('GET', '/v1/events') as left, client.stream('GET', '/v1/events') as right:
            assert next_event(left.iter_lines()) == next_event(right.iter_lines()) == expected[0]


def test_sse_heartbeat_has_no_id_and_generator_cancels_cleanly(database, monkeypatch):
    monkeypatch.setattr(sse, 'HEARTBEAT_SECONDS', 0)
    monkeypatch.setattr(sse, 'POLL_SECONDS', 0)

    async def verify():
        async def connected(): return False
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=Settings(database.parent))),
                                  is_disconnected=connected)
        response = await sse.events(request, last_event_id=None, task_id=None, run_id=None)
        stream = response.body_iterator
        assert await anext(stream) == 'retry: 1000\n\n'
        assert await anext(stream) == ': keep-alive\n\n'
        await stream.aclose()
        with connect(database) as db, transaction(db):
            seed(db)  # stream leaves no open read/write transaction
    asyncio.run(verify())


def test_sse_json_escapes_newlines():
    frame = sse.encode_event({'event_id': 1, 'event_type': 'state_changed', 'payload': {'blocked_reason': '中文\nid: 999'}})
    assert frame.count('\nid:') == 0
    assert frame.count('\ndata:') == 1
    assert '\\nid: 999' in frame
