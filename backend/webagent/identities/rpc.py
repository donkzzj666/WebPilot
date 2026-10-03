"""Private authenticated IPC: the API never owns the user's browser context.

Only bounded metadata commands cross this socket; no passwords, verification
codes, browser handles or authentication JSON are accepted or returned.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from ..errors import BusinessError
from ..security.token import LocalTokenError, load_or_create_token
from .api_models import LoginRequest, LoginCommand

MAX_FRAME = 16384
RPC_TIMEOUT = 75
MAX_CLIENTS = 16
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
ERRORS = {
    'INVALID_PARAMETER': (422, '登录请求参数无效。'),
    'NOT_FOUND': (404, '登录准备或身份记录不存在。'),
    'FORBIDDEN': (403, '登录身份或站点范围不匹配。'),
    'STATE_CONFLICT': (409, '登录状态已变化，请重新查询后继续。'),
    'RESOURCE_CONFLICT': (409, '登录资源正在使用中。'),
    'SERVICE_UNAVAILABLE': (503, '登录服务暂不可用，请确认 Worker 已启动。'),
}


def unavailable():
    return BusinessError('SERVICE_UNAVAILABLE', '登录服务暂不可用，请确认 Worker 已启动。', status=503, field='login_session')


def socket_path(settings) -> Path:
    # A fixed short private parent avoids macOS's Unix path-length limit even
    # when the project/data path is long. Never expose this through the API.
    parent = Path('/tmp').resolve() / f'webpilot-login-{os.getuid()}'
    digest = hashlib.sha256(str(settings.data_dir.resolve()).encode()).hexdigest()[:32]
    return parent / f'{digest}.sock'


def private_parent(path):
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError('unsafe IPC directory')
    except OSError:
        raise unavailable() from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key')
        result[key] = value
    return result


def decode(raw):
    if len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
        raise ValueError('Invalid IPC frame')
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(value) is not dict:
        raise ValueError('Invalid IPC frame')
    return value


def encode(value):
    if is_dataclass(value):
        value = asdict(value)
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(raw) > MAX_FRAME:
        raise ValueError('IPC response too large')
    return raw


def command_payload(command, payload):
    if type(payload) is not dict:
        raise ValueError('Invalid command')
    if command == 'create':
        return LoginRequest.model_validate(payload).model_dump()
    if command in ('list_identities', 'list_sites'):
        if payload:
            raise ValueError('Unexpected metadata')
        return {}
    if command not in ('get', 'confirm', 'close'):
        raise ValueError('Unknown command')
    identifier = payload.get('login_session_id')
    if type(identifier) is not str or not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Invalid login session')
    remaining = {k: v for k, v in payload.items() if k != 'login_session_id'}
    if command == 'get':
        if remaining:
            raise ValueError('Unexpected metadata')
        return {'login_session_id': identifier}
    return {'login_session_id': identifier, **LoginCommand.model_validate(remaining).model_dump()}


class LoginClient:
    def __init__(self, settings):
        self.settings = settings

    async def call(self, command, payload=None):
        try:
            payload = command_payload(command, {} if payload is None else payload)
        except (ValueError, TypeError):
            raise BusinessError('INVALID_PARAMETER', '登录请求参数无效。', field='login_session') from None
        path = socket_path(self.settings)
        writer = None
        try:
            async with asyncio.timeout(RPC_TIMEOUT + 5):
                private_parent(path)
                info = path.lstat()
                if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise OSError('unsafe IPC socket')
                token = await asyncio.to_thread(load_or_create_token, self.settings.data_dir)
                request_id = uuid4().hex
                reader, writer = await asyncio.open_unix_connection(path, limit=MAX_FRAME)
                writer.write(encode({'id': request_id, 'token': token, 'command': command, 'payload': payload}))
                await writer.drain()
                result = decode(await reader.readuntil(b'\n'))
                if result.get('id') != request_id:
                    raise ValueError('Mismatched response')
                if result.get('ok') is True:
                    if set(result) != {'id', 'ok', 'result'} or type(result['result']) not in (dict, list):
                        raise ValueError('Invalid result response')
                    return result['result']
                if result.get('ok') is not False or set(result) != {'id', 'ok', 'error'}:
                    raise ValueError('Invalid error response')
                error = result['error']
                if (type(error) is not dict or not {'code', 'message', 'status'} <= set(error)
                        or set(error) - {'code', 'message', 'status', 'field', 'current_state_version'}
                        or type(error['code']) is not str or error['code'] not in ERRORS):
                    raise ValueError('Invalid error response')
                status, message = ERRORS[error['code']]
                version = error.get('current_state_version')
                if (type(error['status']) is not int or error['status'] != status
                        or type(error['message']) is not str
                        or (version is not None and (type(version) is not int or version < 0))
                        or (error.get('field') is not None and type(error['field']) is not str)):
                    raise ValueError('Invalid error metadata')
                raise BusinessError(error['code'], message, status=status, field='login_session',
                                    current_state_version=version)
        except BusinessError:
            raise
        except (OSError, LocalTokenError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, KeyError):
            raise unavailable() from None
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1)
                except (OSError, TimeoutError):
                    pass


class LoginServer:
    """Start only after ManagedBrowser.start() holds the data-directory lock."""
    def __init__(self, settings, service):
        self.settings, self.service = settings, service
        self.path = socket_path(settings)
        self._server = None
        self._token = None
        self._handlers = set()
        self._writers = set()
        self._inode = None
        self._closed = False
        self._gate = asyncio.Lock()

    async def start(self):
        async with self._gate:
            return await self._start()

    async def _start(self):
        if self._closed:
            raise unavailable()
        if self._server is not None:
            return self
        private_parent(self.path)
        self._token = await asyncio.to_thread(load_or_create_token, self.settings.data_dir)
        try:
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError('unsafe stale socket')
                try:
                    _, probe = await asyncio.wait_for(asyncio.open_unix_connection(self.path), 1)
                except ConnectionRefusedError:
                    pass
                else:
                    probe.close()
                    await probe.wait_closed()
                    raise BusinessError('RESOURCE_CONFLICT', '另一个登录服务正在使用此数据目录。', status=409)
                # Manager's process lock proves that the previous owner exited.
                self.path.unlink()
            self._server = await asyncio.start_unix_server(self._handle, self.path, limit=MAX_FRAME)
            self.path.chmod(0o600)
            self._inode = self.path.lstat().st_ino
            return self
        except OSError:
            raise unavailable() from None

    async def _handle(self, reader, writer):
        if self._closed or len(self._handlers) >= MAX_CLIENTS:
            writer.close()
            return
        task = asyncio.current_task()
        self._handlers.add(task)
        self._writers.add(writer)
        request_id = None
        try:
            async with asyncio.timeout(3):
                value = decode(await reader.readuntil(b'\n'))
            if set(value) != {'id', 'token', 'command', 'payload'}:
                raise ValueError('Invalid frame')
            if type(value['id']) is not str or not re.fullmatch('[0-9a-f]{32}', value['id']):
                raise ValueError('Invalid request id')
            request_id = value['id']
            if type(value['token']) is not str or not hmac.compare_digest(value['token'].encode(), self._token.encode()):
                raise ValueError('Invalid credential')
            payload = command_payload(value['command'], value['payload'])
            if 'login_session_id' in payload:
                payload['login_id'] = payload.pop('login_session_id')
            async with asyncio.timeout(RPC_TIMEOUT):
                operation = getattr(self.service, value['command'])(**payload)
                result = await operation if inspect.isawaitable(operation) else operation
            if hasattr(result, 'as_dict'):
                result = result.as_dict()
            elif is_dataclass(result):
                result = asdict(result)
            response = {'id': request_id, 'ok': True, 'result': result}
        except asyncio.CancelledError:
            raise
        except BusinessError as error:
            # Only service-defined metadata errors, never raw browser errors.
            response = {'id': request_id, 'ok': False, 'error': {
                'code': error.code, 'message': str(error), 'status': error.status,
                'field': error.field, 'current_state_version': error.current_state_version}}
        except Exception:
            response = {'id': request_id, 'ok': False, 'error': {
                'code': 'SERVICE_UNAVAILABLE', 'message': '登录服务请求未完成。', 'status': 503}}
        finally:
            # Sending a result is below; cancellation must still close streams.
            if task.cancelling():
                writer.close()
                self._writers.discard(writer)
                self._handlers.discard(task)
        try:
            writer.write(encode(response))
            await asyncio.wait_for(writer.drain(), 2)
        except (ValueError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass
            self._writers.discard(writer)
            self._handlers.discard(task)

    async def aclose(self):
        async with self._gate:
            await self._close()

    async def _close(self):
        self._closed = True
        if self._server is not None:
            self._server.close()
        # Python 3.12's server.wait_closed() also waits for accepted transports.
        # Close/cancel them first so a pending confirmation cannot hold Worker
        # shutdown until its 75-second request deadline.
        for writer in tuple(self._writers):
            writer.close()
        pending = tuple(self._handlers)
        for task in pending:
            task.cancel()
        if pending:
            _, unsettled = await asyncio.wait(pending, timeout=10)
            if unsettled:
                raise unavailable()
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except TimeoutError:
                raise unavailable() from None
        if self._inode is not None:
            try:
                if self.path.lstat().st_ino == self._inode:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        self._token = None
