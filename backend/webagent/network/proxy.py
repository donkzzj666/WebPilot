"""Authenticated per-context HTTP/1.1 proxy with fixed-IP CONNECT tunnels.

The HTTP side accepts one strictly framed request per connection and never
forwards hop-by-hop proxy credentials. Unsupported transfer codings, upgrades,
ambiguous headers and request reuse fail closed. CONNECT forwards opaque TLS
only after destination/peer checks; browser channel restrictions remain a
separate required defense in sessions.manager.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import hmac
import ipaddress
import re
import secrets
import socket

from .policy import Endpoint, NetworkDenied, NetworkPolicy, parse_url

MAX_HEADER_BYTES = 32768
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_CONNECTIONS = 64
HEADER_SECONDS = 10
CONNECT_SECONDS = 10
IDLE_SECONDS = 60
_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'})
_DROP = frozenset({'proxy-authorization', 'proxy-authenticate', 'proxy-connection', 'connection',
                  'keep-alive', 'upgrade', 'te', 'trailer', 'transfer-encoding'})


class ProxyRequestError(Exception):
    def __init__(self, reason='invalid_request', status=400):
        self.reason = reason
        self.status = status
        super().__init__('Proxy request rejected')


def parse_request(raw: bytes):
    if not raw.endswith(b'\r\n\r\n') or len(raw) > MAX_HEADER_BYTES:
        raise ProxyRequestError()
    lines = raw[:-4].split(b'\r\n')
    parts = lines[0].split(b' ')
    if len(parts) != 3 or parts[2] != b'HTTP/1.1':
        raise ProxyRequestError()
    try:
        method, target = parts[0].decode('ascii'), parts[1].decode('ascii')
    except UnicodeError:
        raise ProxyRequestError() from None
    if method not in _METHODS | {'CONNECT'} or not target:
        raise ProxyRequestError()
    headers = {}
    for line in lines[1:]:
        if b':' not in line:
            raise ProxyRequestError()
        key, value = line.split(b':', 1)
        if not _TOKEN.fullmatch(key) or any(c < 32 and c != 9 or c == 127 for c in value):
            raise ProxyRequestError()
        name = key.decode('ascii').lower()
        if name in headers:
            raise ProxyRequestError('duplicate_header')
        headers[name] = value.strip(b' \t')
    if 'host' not in headers or 'transfer-encoding' in headers:
        raise ProxyRequestError('invalid_framing')
    if any(name in headers for name in ('upgrade', 'expect', 'te', 'trailer')):
        raise ProxyRequestError('unsupported_channel')
    for name in ('connection', 'proxy-connection'):
        if name in headers and any(value.strip().lower() not in (b'close', b'keep-alive')
                                   for value in headers[name].split(b',')):
            raise ProxyRequestError('unsupported_channel')
    length = headers.get('content-length', b'0')
    if not re.fullmatch(rb'0|[1-9][0-9]{0,8}', length):
        raise ProxyRequestError('invalid_framing')
    body_length = int(length)
    if body_length > MAX_BODY_BYTES or method == 'CONNECT' and body_length:
        raise ProxyRequestError('invalid_framing')
    try:
        if method == 'CONNECT':
            # An authority has an explicit port and no path/query/userinfo.
            if any(char in target for char in '/?#@'):
                raise ValueError
            endpoint, parsed = parse_url('https://' + target)
            if parsed.port is None:
                raise ValueError
        else:
            endpoint, parsed = parse_url(target)
            if endpoint.scheme != 'http':
                raise ValueError
        host_value = headers['host'].decode('ascii')
        host_endpoint, host_parsed = parse_url(endpoint.scheme + '://' + host_value)
        if host_parsed.path or host_parsed.query or host_endpoint != endpoint:
            raise ValueError
    except (ValueError, UnicodeError, NetworkDenied):
        raise ProxyRequestError('authority_mismatch') from None
    return method, target, endpoint, parsed, headers, body_length


async def numeric_connection(ip: str, port: int):
    address = ipaddress.ip_address(ip)
    return await asyncio.open_connection(str(address), port,
        family=socket.AF_INET6 if address.version == 6 else socket.AF_INET,
        flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV)


class EgressProxy:
    def __init__(self, policy: NetworkPolicy, *, connector=None):
        if not isinstance(policy, NetworkPolicy):
            raise NetworkDenied('invalid_policy')
        # Capture an immutable policy, including independently copied tuples.
        self.policy = NetworkPolicy(policy.realm, policy.webarena_endpoints, policy.control_ports,
                                    policy.denied_endpoints, policy.resolver)
        self._connector = connector or numeric_connection
        self._username = secrets.token_urlsafe(24)
        self._password = secrets.token_urlsafe(32)
        self._authorization = b'Basic ' + base64.b64encode((self._username + ':' + self._password).encode())
        self._server = None
        self._closed = False
        self._lifecycle = asyncio.Lock()
        self._handlers = set()
        self._writers = set()
        self._events = deque(maxlen=1000)

    @property
    def active(self):
        return self._server is not None and self._server.is_serving() and not self._closed

    @property
    def port(self):
        if not self.active:
            raise NetworkDenied('proxy_loop')
        return self._server.sockets[0].getsockname()[1]

    @property
    def playwright_proxy(self):
        return {'server': 'http://127.0.0.1:' + str(self.port),
                'username': self._username, 'password': self._password, 'bypass': '<-loopback>'}

    @property
    def events(self):
        return [dict(value) for value in self._events]

    async def start(self):
        async with self._lifecycle:
            if self._closed:
                raise NetworkDenied('proxy_loop')
            if self._server is None:
                self._server = await asyncio.start_server(self._accept, '127.0.0.1', 0, limit=MAX_HEADER_BYTES)
            return self

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        async with self._lifecycle:
            self._closed = True
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
            for writer in tuple(self._writers):
                writer.close()
            pending = tuple(task for task in self._handlers if task is not asyncio.current_task())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._authorization = b''

    def _record(self, endpoint, outcome, reason):
        self._events.append({'scheme': endpoint.scheme if endpoint else None,
            'host': endpoint.host if endpoint else None, 'port': endpoint.port if endpoint else None,
            'outcome': outcome, 'reason': reason})

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        if not self.active or len(self._handlers) >= MAX_CONNECTIONS:
            writer.close()
            return
        self._handlers.add(task)
        self._writers.add(writer)
        endpoint = None
        response_started = False
        upstream = None
        try:
            async with asyncio.timeout(HEADER_SECONDS):
                raw = await reader.readuntil(b'\r\n\r\n')
                method, target, endpoint, parsed, headers, body_length = parse_request(raw)
                authorization = headers.get('proxy-authorization', b'')
                if not hmac.compare_digest(authorization, self._authorization):
                    raise ProxyRequestError('authentication_required', 407)
                body = await reader.readexactly(body_length)
            destination = await self.policy.resolve('https://' + target if method == 'CONNECT' else target)
            # Never permit a registered fixture to loop back into this proxy.
            if destination.port == self.port and any(ipaddress.ip_address(ip).is_loopback for ip in destination.addresses):
                raise NetworkDenied('proxy_loop')
            selected = destination.addresses[0]
            async with asyncio.timeout(CONNECT_SECONDS):
                upstream_reader, upstream = await self._connector(selected, destination.port)
            self._writers.add(upstream)
            self.policy.validate_peer(destination, selected, upstream.get_extra_info('peername'))
            self._record(endpoint, 'allowed', 'destination_validated')
            if method == 'CONNECT':
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
                response_started = True
                await self._tunnel(reader, writer, upstream_reader, upstream)
            else:
                path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
                # A single origin-form request and exact body are forwarded.
                # Any pipelined client bytes are discarded when this closes.
                outgoing = method.encode() + b' ' + path.encode('ascii') + b' HTTP/1.1\r\n'
                outgoing += b'Host: ' + endpoint.authority.encode('ascii') + b'\r\n'
                outgoing += b''.join(name.encode('ascii') + b': ' + value + b'\r\n'
                    for name, value in headers.items() if name not in _DROP | {'host'})
                outgoing += b'Connection: close\r\n\r\n'
                async with asyncio.timeout(IDLE_SECONDS):
                    upstream.write(outgoing + body)
                    await upstream.drain()
                response_started = True
                await self._copy(upstream_reader, writer)
        except asyncio.CancelledError:
            raise
        except ProxyRequestError as error:
            self._record(endpoint, 'denied', error.reason)
            if not response_started:
                await self._error(writer, error.status)
        except NetworkDenied as error:
            self._record(endpoint, 'denied', error.reason)
            if not response_started:
                await self._error(writer, 403)
        except (TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            self._record(endpoint, 'denied', 'connection_failed')
            if not response_started:
                await self._error(writer, 502)
        except Exception:
            self._record(endpoint, 'denied', 'proxy_error')
            if not response_started:
                await self._error(writer, 502)
        finally:
            for stream in (upstream, writer):
                if stream is not None:
                    self._writers.discard(stream)
                    stream.close()
                    try:
                        async with asyncio.timeout(1):
                            await stream.wait_closed()
                    except (TimeoutError, ConnectionError, OSError):
                        pass
            self._handlers.discard(task)

    @staticmethod
    async def _error(writer, status):
        phrase = {400: b'Bad Request', 403: b'Forbidden', 407: b'Proxy Authentication Required',
                  502: b'Bad Gateway'}[status]
        challenge = b'Proxy-Authenticate: Basic realm="managed-browser"\r\n' if status == 407 else b''
        try:
            writer.write(b'HTTP/1.1 ' + str(status).encode() + b' ' + phrase + b'\r\n'
                         + challenge + b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    @staticmethod
    async def _copy(reader, writer):
        while True:
            async with asyncio.timeout(IDLE_SECONDS):
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()

    async def _tunnel(self, client_reader, client_writer, upstream_reader, upstream_writer):
        tasks = {asyncio.create_task(self._copy(client_reader, upstream_writer)),
                 asyncio.create_task(self._copy(upstream_reader, client_writer))}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
