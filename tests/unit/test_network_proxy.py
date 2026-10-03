"""Strict HTTP boundary tests using owned in-memory streams and fake sockets."""
import asyncio
import base64
import json
import socket

import pytest

from webagent.network.policy import Endpoint, NetworkDenied, NetworkPolicy
from webagent.network.proxy import EgressProxy, ProxyRequestError, numeric_connection, parse_request


class Writer:
    def __init__(self, peer=('8.8.8.8', 80)):
        self.peer = peer
        self.sent = bytearray()
        self.closed = False

    def write(self, content): self.sent.extend(content)
    async def drain(self): pass
    def close(self): self.closed = True
    async def wait_closed(self): pass
    def get_extra_info(self, key): return self.peer if key == 'peername' else None


class Server:
    def __init__(self):
        self.running = True
        self.sockets = [self]
    def is_serving(self): return self.running
    def getsockname(self): return ('127.0.0.1', 19876)
    def close(self): self.running = False
    async def wait_closed(self): pass


def reader(content):
    value = asyncio.StreamReader()
    value.feed_data(content)
    value.feed_eof()
    return value


def request(proxy, *, method='GET', target='http://example.test/resource?private=secret',
            host='example.test', headers=b'', body=b'', authenticated=True):
    authorization = b'Proxy-Authorization: ' + proxy._authorization + b'\r\n' if authenticated else b''
    return method.encode() + b' ' + target.encode() + b' HTTP/1.1\r\nHost: ' + host.encode() + b'\r\n' + authorization + headers + b'\r\n' + body


async def exchange(build, *, peer=('8.8.8.8', 80), answers=None, policy=None):
    calls, resolutions = [], []
    upstream = Writer(peer)
    async def resolver(host, port):
        resolutions.append((host, port))
        return ['8.8.8.8'] if answers is None else answers
    async def connector(ip, port):
        calls.append((ip, port))
        return reader(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok'), upstream
    proxy = EgressProxy(policy or NetworkPolicy(resolver=resolver), connector=connector)
    proxy._server = Server()
    client = Writer()
    await proxy._accept(reader(build(proxy)), client)
    return proxy, client, upstream, calls, resolutions


def test_authenticated_http_connects_numeric_ip_and_strips_proxy_credentials():
    proxy, client, upstream, calls, resolutions = asyncio.run(exchange(lambda proxy: request(proxy,
        method='POST', headers=b'Content-Length: 4\r\nCookie: synthetic-browser-cookie\r\n', body=b'data')))
    assert bytes(client.sent).endswith(b'ok')
    assert calls == [('8.8.8.8', 80)] and resolutions == [('example.test', 80)]
    assert bytes(upstream.sent).startswith(b'POST /resource?private=secret HTTP/1.1\r\nHost: example.test:80\r\n')
    assert b'proxy-authorization' not in bytes(upstream.sent).lower()
    assert b'Cookie: synthetic-browser-cookie'.lower() in bytes(upstream.sent)
    assert bytes(upstream.sent).endswith(b'\r\n\r\ndata')
    metadata = json.dumps(proxy.events)
    assert 'private' not in metadata and 'secret' not in metadata and 'cookie' not in metadata
    assert proxy._password not in metadata and proxy._username not in metadata


@pytest.mark.parametrize('peer', [('127.0.0.1', 80), ('1.1.1.1', 80), ('8.8.8.8', 19001), None])
def test_peer_mismatch_sends_zero_http_bytes_even_after_a_socket_was_opened(peer):
    proxy, client, upstream, calls, _ = asyncio.run(exchange(lambda proxy: request(proxy,
        method='POST', headers=b'Content-Length: 18\r\n', body=b'private-http-body!'), peer=peer))
    assert calls == [('8.8.8.8', 80)]
    assert upstream.sent == b'' and upstream.closed
    assert bytes(client.sent).startswith(b'HTTP/1.1 403')
    assert proxy.events[-1]['outcome'] == 'denied'


@pytest.mark.parametrize('authorized', [False, 'wrong'])
def test_missing_or_wrong_context_credentials_do_not_resolve_or_connect(authorized):
    def build(proxy):
        raw = request(proxy, authenticated=bool(authorized))
        return raw.replace(proxy._authorization, b'Basic c29tZW9uZTplbHNl') if authorized else raw
    proxy, client, upstream, calls, resolutions = asyncio.run(exchange(build))
    assert bytes(client.sent).startswith(b'HTTP/1.1 407')
    assert calls == resolutions == [] and upstream.sent == b''


@pytest.mark.parametrize('extra', [
    b'Content-Length: 1\r\nTransfer-Encoding: chunked\r\n',
    b'Transfer-Encoding: chunked\r\n', b'Content-Length: 0\r\nContent-Length: 0\r\n',
    b'Content-Length : 0\r\n', b'Content-Length: +0\r\n', b'Content-Length: 00\r\n',
    b'Content-Length: 0,0\r\n', b'Content-Length: 9000000\r\n',
    b'Upgrade: websocket\r\n', b'Connection: upgrade\r\n',
    b'Connection: content-length\r\n', b'Expect: 100-continue\r\n',
    b' bad-fold: value\r\n', b'Bad\x00Name: content\r\n', b'Bad: content\nInjected: x\r\n',
])
def test_ambiguous_framing_and_unsupported_channels_are_rejected_before_network(extra):
    proxy, client, upstream, calls, resolutions = asyncio.run(exchange(lambda proxy: request(proxy, headers=extra)))
    assert bytes(client.sent).startswith(b'HTTP/1.1 400')
    assert calls == resolutions == [] and upstream.sent == b''


@pytest.mark.parametrize('target,host', [
    ('http://example.test/', '127.0.0.1'), ('http://example.test/', 'example.test:81'),
    ('http://user:pass@example.test/', 'example.test'), ('http://example.test:0/', 'example.test'),
    ('http://example.test/', 'example.test/path'), ('https://example.test/', 'example.test'),
])
def test_http_authority_mismatch_and_non_absolute_http_fail_before_network(target, host):
    _, client, _, calls, resolutions = asyncio.run(exchange(lambda proxy: request(proxy, target=target, host=host)))
    assert bytes(client.sent).startswith(b'HTTP/1.1 400') and calls == resolutions == []


def test_pipelined_second_request_is_never_forwarded_to_any_destination():
    _, _, upstream, calls, _ = asyncio.run(exchange(lambda proxy: request(proxy) +
        request(proxy, target='http://127.0.0.1:8000/internal', host='127.0.0.1:8000')))
    assert len(calls) == 1 and b'127.0.0.1' not in upstream.sent and b'/internal' not in upstream.sent


def test_connect_validates_fixed_ip_and_peer_before_returning_tunnel_success():
    proxy, client, upstream, calls, _ = asyncio.run(exchange(lambda proxy: request(proxy,
        method='CONNECT', target='example.test:443', host='example.test:443'), peer=('8.8.8.8', 443)))
    assert bytes(client.sent).startswith(b'HTTP/1.1 200 Connection Established')
    assert calls == [('8.8.8.8', 443)]
    assert b'CONNECT' not in upstream.sent and proxy.events[-1]['outcome'] == 'allowed'
    _, failed_client, failed_upstream, _, _ = asyncio.run(exchange(lambda proxy: request(proxy,
        method='CONNECT', target='example.test:443', host='example.test:443'), peer=('127.0.0.1', 443)))
    assert bytes(failed_client.sent).startswith(b'HTTP/1.1 403') and failed_upstream.sent == b''


@pytest.mark.parametrize('target', ['example.test', 'example.test:0', 'example.test:25',
                                    'example.test:443/path', 'user:pass@example.test:443'])
def test_connect_never_opens_an_arbitrary_or_ambiguous_tunnel(target):
    _, client, _, calls, _ = asyncio.run(exchange(lambda proxy: request(proxy,
        method='CONNECT', target=target, host=target)))
    assert not bytes(client.sent).startswith(b'HTTP/1.1 200') and calls == []


def test_proxy_cannot_connect_back_to_its_own_listener_even_if_misregistered():
    policy = NetworkPolicy('webarena', (Endpoint('http', '127.0.0.1', 19876),))
    _, client, _, calls, _ = asyncio.run(exchange(lambda proxy: request(proxy,
        target='http://127.0.0.1:19876/', host='127.0.0.1:19876'), policy=policy))
    assert bytes(client.sent).startswith(b'HTTP/1.1 403') and calls == []


def test_numeric_connection_uses_address_family_and_numeric_resolution_flags(monkeypatch):
    captured = []
    async def open_connection(host, port, **kwargs):
        captured.append((host, port, kwargs))
        return None, None
    monkeypatch.setattr(asyncio, 'open_connection', open_connection)
    asyncio.run(numeric_connection('8.8.8.8', 443))
    assert captured == [('8.8.8.8', 443, {'family': socket.AF_INET,
                                        'flags': socket.AI_NUMERICHOST | socket.AI_NUMERICSERV})]


def test_proxy_credentials_are_unique_and_close_revokes_listener_and_streams():
    async def exercise():
        first, second = EgressProxy(NetworkPolicy()), EgressProxy(NetworkPolicy())
        first._server, second._server = Server(), Server()
        assert first.playwright_proxy['password'] != second.playwright_proxy['password']
        assert first.playwright_proxy['username'] != second.playwright_proxy['username']
        assert first._password not in repr(first)
        opened = Writer()
        first._writers.add(opened)
        await first.aclose()
        assert not first.active and opened.closed and first._authorization == b''
        with pytest.raises(NetworkDenied):
            _ = first.playwright_proxy
        await second.aclose()
    asyncio.run(exercise())


def test_concurrent_start_and_close_cannot_orphan_a_second_listener(monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        servers = []
        async def start_server(*args, **kwargs):
            entered.set()
            await release.wait()
            result = Server()
            servers.append(result)
            return result
        monkeypatch.setattr(asyncio, 'start_server', start_server)
        proxy = EgressProxy(NetworkPolicy())
        first = asyncio.create_task(proxy.start())
        await entered.wait()
        second = asyncio.create_task(proxy.start())
        closing = asyncio.create_task(proxy.aclose())
        release.set()
        await asyncio.gather(first, second, closing)
        assert len(servers) == 1 and not servers[0].running and not proxy.active
        with pytest.raises(NetworkDenied):
            await proxy.start()
    asyncio.run(exercise())
