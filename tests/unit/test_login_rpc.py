"""Private login RPC over owned short Unix sockets, without a browser or user key."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import stat
import tempfile

import pytest

from webagent.config import Settings
from webagent.errors import BusinessError
from webagent.identities import rpc
from webagent.security import load_or_create_token

SECRET = 'SYNTHETIC_PASSWORD_AND_OTP_DO_NOT_ECHO'


class Service:
    def __init__(self):
        self.calls = []
        self.block = False
        self.started = asyncio.Event()

    async def create(self, **payload):
        self.calls.append(('create', payload))
        self.started.set()
        if self.block:
            await asyncio.Future()
        return {'login_id': 'login-fixture', 'state': 'AWAITING_USER', 'state_version': 2,
                'expected_account': payload['expected_account']}

    async def get(self, login_id):
        self.calls.append(('get', {'login_id': login_id}))
        return {'login_id': login_id, 'state_version': 2}

    async def confirm(self, login_id, expected_version):
        self.calls.append(('confirm', {'login_id': login_id, 'expected_version': expected_version}))
        if expected_version != 2:
            raise BusinessError('STATE_CONFLICT', 'Login version changed', status=409,
                                field='expected_version', current_state_version=2)
        return {'login_id': login_id, 'state_version': 3, 'state': 'VERIFIED'}

    async def close(self, login_id, expected_version):
        self.calls.append(('close', {'login_id': login_id, 'expected_version': expected_version}))
        return {'login_id': login_id, 'state_version': expected_version + 1, 'state': 'CLOSED'}

    async def list_sites(self):
        self.calls.append(('list_sites', {}))
        return [{'site_id': 'github'}]

    async def list_identities(self):
        self.calls.append(('list_identities', {}))
        return []


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    # macOS sockaddr_un is short; every test owns and deletes only this path.
    with tempfile.TemporaryDirectory(prefix='webpilot-rpc-test-', dir='/private/tmp') as temporary:
        path = Path(temporary) / 'ipc' / 'login.sock'
        monkeypatch.setattr(rpc, 'socket_path', lambda _: path)
        yield Settings(tmp_path), path


@asynccontextmanager
async def running(settings, service=None):
    service = service or Service()
    server = rpc.LoginServer(settings, service)
    try:
        await server.start()
        yield server, service, rpc.LoginClient(settings)
    finally:
        await server.aclose()


def frame(settings, **updates):
    return {'id': 'a' * 32, 'token': load_or_create_token(settings.data_dir),
            'command': 'list_sites', 'payload': {}, **updates}


async def raw_exchange(path, raw, *, eof=False):
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(raw)
        await writer.drain()
        if eof:
            writer.write_eof()
        return await asyncio.wait_for(reader.read(), 4)
    finally:
        writer.close()
        await writer.wait_closed()


def test_real_rpc_authentication_metadata_dispatch_and_cleanup(isolated):
    settings, path = isolated
    async def scenario():
        async with running(settings) as (server, service, client):
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert await client.call('list_sites') == [{'site_id': 'github'}]
            assert await client.call('list_identities') == []
            created = await client.call('create', {'site_id': 'github', 'expected_account': 'alice'})
            assert created['state_version'] == 2
            assert service.calls[-1] == ('create', {'site_id': 'github', 'expected_account': 'alice',
                                                    'expected_identity_ref': None})
            assert (await client.call('get', {'login_session_id': 'login-fixture'}))['state_version'] == 2
            assert (await client.call('confirm', {'login_session_id': 'login-fixture', 'expected_version': 2}))['state'] == 'VERIFIED'
            assert (await client.call('close', {'login_session_id': 'login-fixture', 'expected_version': 3}))['state'] == 'CLOSED'
        assert not path.exists() and not server._handlers and not server._writers
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['wrong-token', 'missing-token', 'duplicate-token', 'duplicate-payload-key',
    'unknown-field', 'unknown-command', 'password', 'otp', 'invalid-id', 'non-dict', 'nan',
    'malformed-json', 'oversized', 'unterminated'])
def test_bad_frames_never_call_service_or_echo_credentials(isolated, mutation):
    settings, path = isolated
    async def scenario():
        async with running(settings) as (_, service, _):
            value = frame(settings)
            if mutation == 'wrong-token': value['token'] = SECRET
            elif mutation == 'missing-token': value.pop('token')
            elif mutation == 'unknown-field': value['password'] = SECRET
            elif mutation == 'unknown-command': value['command'] = '__dict__'
            elif mutation in ('password', 'otp'):
                value['command'] = 'create'; value['payload'] = {'site_id': 'github', 'expected_account': 'alice', mutation: SECRET}
            elif mutation == 'invalid-id': value['id'] = SECRET
            raw = rpc.encode(value)
            if mutation == 'duplicate-token': raw = raw[:-2] + b',"token":"' + SECRET.encode() + b'"}\n'
            elif mutation == 'duplicate-payload-key':
                raw = raw.replace(b'"payload":{}', b'"payload":{"site_id":"github","site_id":"' + SECRET.encode() + b'"}')
            elif mutation == 'non-dict': raw = b'[]\n'
            elif mutation == 'nan': raw = raw.replace(b'"payload":{}', b'"payload":{"x":NaN}')
            elif mutation == 'malformed-json': raw = b'{"password":"' + SECRET.encode() + b'",\n'
            elif mutation == 'oversized': raw = b'x' * (rpc.MAX_FRAME + 1) + b'\n'
            elif mutation == 'unterminated': raw = raw.rstrip(b'\n')
            result = await raw_exchange(path, raw, eof=mutation == 'unterminated')
            assert service.calls == [] and SECRET.encode() not in result
            assert value.get('token', 'absent').encode() not in result
            assert rpc.decode(result)['ok'] is False
    asyncio.run(scenario())


def test_second_frame_does_not_dispatch_another_command(isolated):
    settings, path = isolated
    async def scenario():
        async with running(settings) as (_, service, _):
            result = await raw_exchange(path, rpc.encode(frame(settings)) * 2)
            assert rpc.decode(result)['ok'] is True
            assert service.calls == [('list_sites', {})]
    asyncio.run(scenario())


@pytest.mark.parametrize('payload', [[], '', 0, False, 'metadata'])
def test_explicit_bad_payload_is_not_silently_defaulted(isolated, payload):
    settings, _ = isolated
    async def scenario():
        async with running(settings) as (_, service, client):
            with pytest.raises(BusinessError) as caught:
                await client.call('list_sites', payload)
            assert caught.value.code == 'INVALID_PARAMETER' and service.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize('command,payload', [('unknown', {}), ('get', {'login_session_id': '../secret'}),
    ('get', {'login_session_id': 'x', 'password': SECRET}), ('confirm', {'login_session_id': 'x'}),
    ('confirm', {'login_session_id': 'x', 'expected_version': True}),
    ('close', {'login_session_id': 'x', 'expected_version': -1}),
    ('create', {'site_id': 'github', 'expected_account': 'alice', 'otp': SECRET})])
def test_client_rejects_bad_command_metadata_before_socket_connection(isolated, command, payload):
    settings, path = isolated
    async def scenario():
        with pytest.raises(BusinessError) as caught:
            await rpc.LoginClient(settings).call(command, payload)
        assert caught.value.code == 'INVALID_PARAMETER'
        assert SECRET not in str(caught.value) and not path.exists()
    asyncio.run(scenario())


def test_version_conflict_preserves_safe_metadata(isolated):
    settings, _ = isolated
    async def scenario():
        async with running(settings) as (_, _, client):
            with pytest.raises(BusinessError) as caught:
                await client.call('confirm', {'login_session_id': 'login-fixture', 'expected_version': 1})
            assert caught.value.status == 409 and caught.value.code == 'STATE_CONFLICT'
            assert caught.value.current_state_version == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', [0o644, 0o666, 0o700])
def test_client_rejects_nonprivate_socket_permissions(isolated, mode):
    settings, path = isolated
    async def scenario():
        async with running(settings) as (_, service, client):
            path.chmod(mode)
            with pytest.raises(BusinessError) as caught:
                await client.call('list_sites')
            assert caught.value.status == 503 and service.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['directory-public', 'directory-symlink', 'socket-regular', 'socket-symlink'])
def test_unsafe_filesystem_nodes_are_not_followed_or_deleted(isolated, kind):
    settings, path = isolated
    path.parent.mkdir(mode=0o700)
    if kind == 'directory-public': path.parent.chmod(0o755)
    elif kind == 'directory-symlink':
        other = path.parent.with_name('other'); path.parent.rename(other); path.parent.symlink_to(other)
    elif kind == 'socket-regular': path.write_text(SECRET)
    else:
        other = path.parent / 'other'; other.write_text(SECRET); path.symlink_to(other)
    async def scenario():
        server = rpc.LoginServer(settings, Service())
        with pytest.raises(BusinessError) as caught:
            await server.start()
        assert caught.value.status == 503
        with pytest.raises(BusinessError) as caught:
            await rpc.LoginClient(settings).call('list_sites')
        assert caught.value.status == 503
        await server.aclose()
    asyncio.run(scenario())
    if kind.startswith('socket-'):
        assert path.read_text() == SECRET


def test_missing_worker_returns_503_without_any_browser_creation(isolated):
    settings, path = isolated
    async def scenario():
        with pytest.raises(BusinessError) as caught:
            await rpc.LoginClient(settings).call('list_sites')
        assert caught.value.status == 503 and not path.exists()
    asyncio.run(scenario())


def test_stale_owned_socket_is_replaced_and_unavailable_client_does_not_delete_it(isolated):
    settings, path = isolated
    path.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX); stale.bind(str(path)); path.chmod(0o600); stale.close()
    async def scenario():
        with pytest.raises(BusinessError):
            await rpc.LoginClient(settings).call('list_sites')
        assert path.exists()
        async with running(settings) as (_, _, client):
            assert await client.call('list_sites') == [{'site_id': 'github'}]
        assert not path.exists()
    asyncio.run(scenario())


def test_live_socket_conflict_does_not_replace_or_close_first_owner(isolated):
    settings, path = isolated
    async def scenario():
        async with running(settings) as (_, _, client):
            inode = path.stat().st_ino
            second = rpc.LoginServer(settings, Service())
            with pytest.raises(BusinessError) as caught:
                await second.start()
            assert caught.value.status == 409 and caught.value.code == 'RESOURCE_CONFLICT'
            await second.aclose()
            assert path.stat().st_ino == inode
            assert await client.call('list_sites') == [{'site_id': 'github'}]
    asyncio.run(scenario())


def test_close_never_unlinks_a_replacement_socket_path(isolated):
    settings, path = isolated
    async def scenario():
        server = await rpc.LoginServer(settings, Service()).start()
        original = path.with_name('original.sock'); path.rename(original)
        path.write_text('replacement')
        await server.aclose()
        assert path.read_text() == 'replacement'
        original.unlink()
    asyncio.run(scenario())


def test_service_timeout_cancels_operation_and_returns_static_503(isolated, monkeypatch):
    settings, _ = isolated
    monkeypatch.setattr(rpc, 'RPC_TIMEOUT', .03)
    async def scenario():
        service = Service(); service.block = True
        async with running(settings, service) as (_, _, client):
            with pytest.raises(BusinessError) as caught:
                await client.call('create', {'site_id': 'github', 'expected_account': 'alice'})
            assert caught.value.status == 503 and SECRET not in str(caught.value)
    asyncio.run(scenario())


def test_shutdown_cancels_hanging_handlers_and_releases_socket(isolated):
    settings, path = isolated
    async def scenario():
        service = Service(); service.block = True
        server = await rpc.LoginServer(settings, service).start()
        task = asyncio.create_task(rpc.LoginClient(settings).call('create', {'site_id': 'github', 'expected_account': 'alice'}))
        try:
            await asyncio.wait_for(service.started.wait(), 1)
            await asyncio.wait_for(server.aclose(), 1)
            with pytest.raises(BusinessError) as caught:
                await task
            assert caught.value.status == 503 and not path.exists()
            assert not server._handlers and not server._writers
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True); await server.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['wrong-id', 'missing-result', 'extra-password', 'invalid-status', 'wrong-code-status', 'duplicate-key'])
def test_client_fails_closed_on_malformed_worker_response(isolated, change):
    settings, path = isolated
    async def handler(reader, writer):
        value = rpc.decode(await reader.readuntil(b'\n'))
        result = {'id': value['id'], 'ok': True, 'result': []}
        if change == 'wrong-id': result['id'] = 'b' * 32
        elif change == 'missing-result': result.pop('result')
        elif change == 'extra-password': result['password'] = SECRET
        elif change in ('invalid-status', 'wrong-code-status'):
            result = {'id': value['id'], 'ok': False, 'error': {'code': 'STATE_CONFLICT',
                'message': SECRET, 'status': 'bad' if change == 'invalid-status' else 200}}
        raw = rpc.encode(result)
        if change == 'duplicate-key': raw = raw[:-2] + b',"ok":true}\n'
        writer.write(raw); await writer.drain(); writer.close(); await writer.wait_closed()
    async def scenario():
        rpc.private_parent(path)
        worker = await asyncio.start_unix_server(handler, path); path.chmod(0o600)
        try:
            with pytest.raises(BusinessError) as caught:
                await rpc.LoginClient(settings).call('list_sites')
            assert caught.value.status == 503 and SECRET not in str(caught.value)
        finally:
            worker.close(); await worker.wait_closed(); path.unlink(missing_ok=True)
    asyncio.run(scenario())


def test_valid_worker_error_cannot_inject_raw_secret_message(isolated):
    settings, path = isolated
    async def handler(reader, writer):
        value = rpc.decode(await reader.readuntil(b'\n'))
        writer.write(rpc.encode({'id': value['id'], 'ok': False, 'error': {
            'code': 'SERVICE_UNAVAILABLE', 'status': 503, 'message': SECRET,
            'field': 'login_session', 'current_state_version': None}}))
        await writer.drain(); writer.close(); await writer.wait_closed()
    async def scenario():
        rpc.private_parent(path)
        worker = await asyncio.start_unix_server(handler, path); path.chmod(0o600)
        try:
            with pytest.raises(BusinessError) as caught:
                await rpc.LoginClient(settings).call('list_sites')
            assert caught.value.status == 503 and SECRET not in str(caught.value)
        finally:
            worker.close(); await worker.wait_closed(); path.unlink(missing_ok=True)
    asyncio.run(scenario())
