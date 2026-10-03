#!/usr/bin/env python3
"""Exercise production managed-browser egress against isolated TCP/UDP canaries.

No external website, existing user profile, credentials or business data is used.
The fixture endpoint is registered only in this process's benchmark policy.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import traceback
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from webagent.config import Settings
from webagent.sessions import SessionOwner
from webagent.sessions.manager import ManagedBrowser
from webagent.network.policy import Endpoint, NetworkPolicy
from webagent.network.proxy import EgressProxy
from webagent.network.config import NetworkConfig


def make_manager(settings, port, headless=False):
    return ManagedBrowser(settings, headless=headless, network_config=NetworkConfig(
        webarena_endpoints=(Endpoint('http', '127.0.0.1', port),)))


async def proxy_request(proxy, request):
    address = urlsplit(proxy.playwright_proxy['server'])
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 4)
    finally:
        writer.close()
        await writer.wait_closed()


async def verify_proxy(directory, tls_fixture, tls_client, forbidden, check):
    policy = NetworkPolicy(realm='webarena', webarena_endpoints=(
        Endpoint('https', '127.0.0.1', tls_fixture.port),))
    proxy = await EgressProxy(policy).start()
    try:
        info = proxy.playwright_proxy
        auth = base64.b64encode(f"{info['username']}:{info['password']}".encode()).decode()
        authorization = f'Proxy-Authorization: Basic {auth}\r\n'
        target = f'127.0.0.1:{tls_fixture.port}'
        address = urlsplit(info['server'])
        reader, writer = await asyncio.open_connection(address.hostname, address.port)
        try:
            writer.write(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{authorization}\r\n'.encode())
            await writer.drain()
            connected = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 4)
            assert connected.startswith(b'HTTP/1.1 200')
            await writer.start_tls(tls_client, server_hostname='127.0.0.1', ssl_handshake_timeout=4)
            writer.write(f'GET /ok HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n'.encode())
            await writer.drain()
            result = await asyncio.wait_for(reader.read(), 4)
            assert b'Authorized fixture' in result
        finally:
            writer.close()
            await writer.wait_closed()
        check('authenticated_https_connect_reaches_only_registered_peer_with_verified_tls')

        missing = await proxy_request(proxy, f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode())
        assert missing.startswith(b'HTTP/1.1 407')
        check('proxy_requires_separate_unpublished_credentials')

        attacks = [
            f'GET {forbidden.origin}/ HTTP/1.1\r\nHost: 127.0.0.1:{forbidden.port}\r\n{authorization}\r\n',
            f'CONNECT 127.0.0.1:{forbidden.port} HTTP/1.1\r\nHost: 127.0.0.1:{forbidden.port}\r\n{authorization}\r\n',
            f'GET http://127.0.0.1:{tls_fixture.port}/ HTTP/1.1\r\nHost: wrong.invalid\r\n{authorization}\r\n',
            f'POST http://127.0.0.1:{tls_fixture.port}/ HTTP/1.1\r\nHost: {target}\r\n{authorization}Content-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n',
            f'GET http://127.0.0.1:{tls_fixture.port}/ HTTP/1.1\r\nHost: {target}\r\n{authorization}Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n',
        ]
        for attack in attacks:
            result = await proxy_request(proxy, attack.encode())
            assert result.startswith((b'HTTP/1.1 400', b'HTTP/1.1 403'))
        assert forbidden.connections == 0
        check('direct_proxy_denies_unregistered_connect_host_mismatch_smuggling_and_upgrade')

        answers = [['93.184.216.34'], ['127.0.0.1']]
        resolved = []

        async def changing_resolver(host, port):
            resolved.append((host, port))
            return answers.pop(0)

        changing = NetworkPolicy(realm='public', resolver=changing_resolver)
        await changing.resolve('https://rebind.invalid/')
        rejected = False
        try:
            await changing.resolve('https://rebind.invalid/')
        except Exception:
            rejected = True
        assert rejected and len(resolved) == 2
        check('dns_change_from_public_to_loopback_is_revalidated_and_rejected')
    finally:
        await proxy.aclose()


class Fixture:
    def __init__(self):
        self.hits = []
        self.connections = 0
        self.active = set()
        self.server = None
        self.port = None
        self.forbidden = ''

    async def start(self, tls=None):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, ssl=tls)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.port}'

    async def handle(self, reader, writer):
        self.connections += 1
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            method, target, _ = raw.split(b'\r\n', 1)[0].decode('ascii').split(' ', 2)
            # Record only fixed fixture paths, never request headers or values.
            path = target.split('?', 1)[0]
            self.hits.append(path if path in ('/', '/ok', '/frame', '/worker.js', '/sw.js', '/native-sw.js', '/redirect', '/file-redirect') else 'other')
            status, content_type, extra = '200 OK', 'text/html; charset=utf-8', ''
            if path == '/redirect':
                status, extra, body = '302 Found', f'Location: {self.forbidden}/redirected\r\n', b''
            elif path == '/file-redirect':
                status, extra, body = '302 Found', f'Location: {self.file_url}\r\n', b''
            elif path == '/worker.js':
                content_type = 'application/javascript'
                body = ('fetch(' + json.dumps(self.forbidden + '/worker') + ', {mode:"no-cors"})'
                        '.then(()=>postMessage("proxy_response"),()=>postMessage("blocked"));').encode()
            elif path == '/sw.js':
                content_type, body = 'application/javascript', b'self.addEventListener("fetch",()=>{});'
            elif path == '/native-sw.js':
                content_type = 'application/javascript'
                body = ('self.addEventListener("activate", e=>e.waitUntil(fetch('
                        + json.dumps(self.forbidden + '/native-sw')
                        + ',{mode:"no-cors"}).catch(()=>{})));').encode()
            else:
                body = b'<!doctype html><meta charset="utf-8"><title>Network boundary fixture</title><h1 id="ready">Authorized fixture</h1>'
            writer.write((f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n{extra}'
                          f'Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n').encode() + body)
            await writer.drain()
        except (TimeoutError, ConnectionError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.active.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*tuple(self.active), return_exceptions=True)


class DatagramCanary(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = 0

    def datagram_received(self, data, address):
        self.received += 1


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'isolated-network-fixture')])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / 'fixture.pem', directory / 'fixture.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=cert_path)
    return server, client


async def rejected_navigation(context, url):
    page = await context.new_page()
    try:
        try:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=4000)
        except Exception:
            return
        assert response is not None and response.status >= 400, 'Forbidden navigation returned a successful response'
    finally:
        await page.close()


async def run(output, headless=False):
    report = {'task': 'M1-12', 'probe': 'managed-browser-network-boundary', 'passed': False,
              'started_at': datetime.now(UTC).isoformat(), 'checks': [],
              'scope': 'Real bundled Chromium (headed by default; explicit headless-shell option), production egress proxy, isolated loopback HTTP/TLS/TCP/UDP fixtures. No external site or user credentials.'}

    def check(name, **data):
        report['checks'].append({'name': name, 'passed': True, **data})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    try:
        with tempfile.TemporaryDirectory(prefix='webpilot-network-') as temporary:
            directory = Path(temporary)
            permitted, forbidden, tls_fixture = Fixture(), Fixture(), Fixture()
            udp_transport = None
            manager = None
            try:
                await permitted.start()
                await forbidden.start()
                udp = DatagramCanary()
                udp_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: udp, local_addr=('127.0.0.1', forbidden.port))
                tls_server, tls_client = certificate(directory)
                await tls_fixture.start(tls_server)
                permitted.forbidden = forbidden.origin
                local_file = directory / 'private-fixture.txt'
                local_file.write_text('SYNTHETIC_PRIVATE_FILE_MUST_NOT_BE_READ')
                permitted.file_url = local_file.as_uri()

                # Assigned below once the production configuration interface is loaded.
                manager = make_manager(Settings(directory / 'data'), permitted.port, headless=headless)
                await manager.start()
                owner = SessionOwner('verification', 'network-benchmark', 'fixture', realm='webarena')
                session = await manager.create(owner)
                context = await manager.context(session.session_id, owner)
                page = context.pages[0]
                response = await page.goto(permitted.origin, wait_until='load')
                assert response.status == 200
                assert await page.locator('#ready').inner_text() == 'Authorized fixture'
                assert await page.evaluate("fetch('/ok').then(r=>r.status)") == 200
                await page.evaluate("() => { const frame=document.createElement('iframe'); frame.src='/frame'; document.body.append(frame); }")
                async with context.expect_page() as popup_event:
                    await page.evaluate("window.open('/frame')")
                popup = await popup_event.value
                await popup.wait_for_load_state()
                assert await popup.locator('#ready').inner_text() == 'Authorized fixture'
                await popup.close()
                await page.screenshot(path=str(output / 'authorized-fixture.png'))
                check('registered_business_page_fetch_iframe_and_popup_remain_usable')

                await rejected_navigation(context, forbidden.origin + '/direct')
                await rejected_navigation(context, permitted.origin + '/redirect')
                check('unregistered_port_and_redirect_blocked_before_target_connection')

                results = await page.evaluate('''async forbidden => {
                    const settled = promise => Promise.race([promise, new Promise(r=>setTimeout(()=>r('timeout'),1200))]);
                    const fetchResult = await settled(fetch(forbidden+'/fetch', {mode:'no-cors'}).then(()=> 'proxy_response',()=> 'blocked'));
                    const xhrResult = await settled(new Promise(resolve=>{const x=new XMLHttpRequest();x.open('GET',forbidden+'/xhr');x.onload=()=>resolve(false);x.onerror=()=>resolve(true);x.send();}));
                    navigator.sendBeacon(forbidden+'/beacon', 'synthetic');
                    for (const tag of ['img','iframe','script']) { const e=document.createElement(tag);e.src=forbidden+'/'+tag;document.body.append(e); }
                    window.open(forbidden+'/popup');
                    const worker = await settled(new Promise(resolve=>{const w=new Worker('/worker.js');w.onmessage=e=>{resolve(e.data);w.terminate();};w.onerror=()=>resolve('blocked');}));
                    const sockets = await Promise.all(['ws:','wss:'].map(scheme=>settled(new Promise(resolve=>{
                        const w=new WebSocket(forbidden.replace('http:',scheme)+'/socket');w.onopen=()=>{w.close();resolve(false)};w.onerror=()=>resolve(true);w.onclose=()=>resolve(true);
                    }))));
                    return {fetchResult,xhrResult,worker,sockets};
                }''', forbidden.origin)
                report['channel_results'] = results
                # no-cors deliberately hides the proxy's HTTP 403 response;
                # target socket counters and proxy denials prove interception.
                assert results['fetchResult'] in ('proxy_response', 'blocked') and results['xhrResult'] is True
                assert results['worker'] in ('proxy_response', 'blocked') and all(value is True for value in results['sockets'])
                await asyncio.sleep(.25)
                assert forbidden.connections == 0
                denials = [event for event in manager._contexts[session.session_id].proxy.events
                           if event['port'] == forbidden.port and event['outcome'] == 'denied']
                assert len(denials) >= 6
                check('fetch_xhr_beacon_iframe_popup_script_image_worker_and_websocket_cannot_bypass', target_connections=0)

                sw = await page.evaluate('''async () => {
                    if (!('serviceWorker' in navigator)) return 'unavailable';
                    return Promise.race([navigator.serviceWorker.register('/sw.js').then(value=> value ? 'registered' : 'blocked',()=> 'blocked'),new Promise(r=>setTimeout(()=>r('blocked'),700))]);
                }''')
                assert sw != 'registered' and not context.service_workers and '/sw.js' not in permitted.hits
                check('service_worker_disabled_before_page_execution')

                for url in (local_file.as_uri(), permitted.origin + '/file-redirect'):
                    await rejected_navigation(context, url)
                await page.evaluate('''file => {
                    const f=document.createElement('iframe');f.src=file;document.body.append(f);window.open(file);
                }''', local_file.as_uri())
                await asyncio.sleep(.15)
                for candidate in context.pages:
                    if not candidate.is_closed():
                        for frame in candidate.frames:
                            # A blocked iframe may never get a JS execution
                            # context; Frame.content() would wait indefinitely.
                            assert not frame.url.startswith('file:')
                check('file_navigation_redirect_iframe_and_popup_cannot_read_local_file')

                await page.evaluate('''async port => {
                    if (typeof RTCPeerConnection !== 'undefined') {
                        let pc;
                        try { pc=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:'+port},{urls:'turn:127.0.0.1:'+port+'?transport=tcp',username:'synthetic',credential:'synthetic'}]});pc.createDataChannel('test');await pc.setLocalDescription(await pc.createOffer());await new Promise(r=>setTimeout(r,600)); } catch {} finally { if(pc)pc.close(); }
                    }
                    if (typeof WebTransport !== 'undefined') {
                        try {const w=new WebTransport('https://127.0.0.1:'+port+'/transport');await Promise.race([w.ready.catch(()=>{}),new Promise(r=>setTimeout(r,600))]);w.close();}catch{}
                    }
                }''', forbidden.port)
                assert forbidden.connections == 0 and udp.received == 0
                check('webrtc_stun_turn_and_webtransport_do_not_reach_tcp_or_udp_canary', tcp_connections=0, udp_packets=0)

                # Test the native switches independently of the supplemental
                # page init script, using a fresh isolated JS world.
                cdp = await context.new_cdp_session(page)
                try:
                    tree = await cdp.send('Page.getFrameTree')
                    world = await cdp.send('Page.createIsolatedWorld', {
                        'frameId': tree['frameTree']['frame']['id'], 'worldName': 'native-network-verification'})
                    expression = '''(async () => {
                        let pc;
                        try {pc=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:PORT'},
                          {urls:'turn:127.0.0.1:PORT?transport=tcp',username:'synthetic',credential:'synthetic'}]});
                          pc.createDataChannel('test');await pc.setLocalDescription(await pc.createOffer());
                          await new Promise(r=>setTimeout(r,1200));}catch{}finally{if(pc)pc.close();}
                        try {const w=new WebTransport('https://127.0.0.1:PORT/transport');
                          await Promise.race([w.ready.catch(()=>{}),new Promise(r=>setTimeout(r,600))]);w.close();}catch{}
                        return true;
                    })()'''.replace('PORT', str(forbidden.port))
                    result = await cdp.send('Runtime.evaluate', {'expression': expression,
                        'contextId': world['executionContextId'], 'awaitPromise': True, 'returnByValue': True})
                    assert result.get('result', {}).get('value') is True
                    report['native_canary'] = {'tcp_connections': forbidden.connections, 'udp_packets': udp.received}
                    assert forbidden.connections == 0 and udp.received == 0
                    check('native_network_restrictions_hold_without_page_javascript_wrappers', tcp_connections=0, udp_packets=0)

                    before_events = len(manager._contexts[session.session_id].proxy.events)
                    native_sw = await cdp.send('Runtime.evaluate', {
                        'expression': '''(async()=>{
                          const r=await Promise.race([navigator.serviceWorker.register('/native-sw.js').catch(()=>null),new Promise(r=>setTimeout(()=>r(null),2000))]);
                          if(!r)return 'unavailable';
                          for(let i=0;i<40;i++){if(r.active && r.active.state==='activated'){await r.unregister();return true;}await new Promise(r=>setTimeout(r,50));}
                          await r.unregister();return 'unavailable';
                        })()''', 'contextId': world['executionContextId'], 'awaitPromise': True, 'returnByValue': True})
                    sw_result = native_sw.get('result', {}).get('value')
                    assert sw_result in (True, 'unavailable')
                    new_events = manager._contexts[session.session_id].proxy.events[before_events:]
                    if sw_result is True:
                        assert any(event['port'] == forbidden.port and event['outcome'] == 'denied' for event in new_events)
                    assert forbidden.connections == 0
                    check('native_service_worker_attempt_is_blocked_or_uses_enforced_proxy',
                          registration='blocked' if sw_result == 'unavailable' else 'registered_with_egress_denied')
                finally:
                    await cdp.detach()
                assert forbidden.connections == 0 and udp.received == 0

                public_owner = SessionOwner('verification', 'network-public', 'fixture', realm='public')
                public = await manager.create(public_owner)
                public_context = await manager.context(public.session_id, public_owner)
                before = permitted.connections
                for host in ('127.0.0.1', 'localhost', '2130706433', '0x7f000001', '[::1]'):
                    await rejected_navigation(public_context, f'http://{host}:{permitted.port}/public-denied')
                assert permitted.connections == before
                check('public_realm_rejects_loopback_and_alternate_ip_spellings')

                await verify_proxy(directory, tls_fixture, tls_client, forbidden, check)

                # A disappeared proxy must not become a DIRECT connection.
                await manager._contexts[session.session_id].proxy.aclose()
                before = permitted.connections
                await rejected_navigation(context, permitted.origin + '/proxy-stopped')
                assert permitted.connections == before
                check('closed_proxy_cannot_fall_back_to_direct_business_connection')

                assert forbidden.connections == 0 and udp.received == 0
                report['canary'] = {'unauthorized_tcp_connections': forbidden.connections, 'unauthorized_udp_packets': udp.received}
                report['browser'] = {'version': manager._browser.version, 'headless': headless}
                check('all_unauthorized_targets_remain_uncontacted')
                report['passed'] = True
            finally:
                if manager is not None:
                    await manager.aclose()
                if udp_transport:
                    udp_transport.close()
                await asyncio.gather(permitted.close(), forbidden.close(), tls_fixture.close())
    except Exception as error:
        # Fixed fixture failures only: no browser exception messages or headers.
        report['error'] = {'type': type(error).__name__, 'last_completed_check': report['checks'][-1]['name'] if report['checks'] else None,
                           'locations': [{'file': Path(f.filename).name, 'line': f.lineno} for f in traceback.extract_tb(error.__traceback__)]}
    report['finished_at'] = datetime.now(UTC).isoformat()
    report['artifact_sha256'] = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(output.rglob('*')) if p.is_file()}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(output / 'report.json'), 'error': report.get('error')}))
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--headless', action='store_true', help='Also verify the separately launched Chromium headless-shell')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    return asyncio.run(run(args.output_dir.resolve(), headless=args.headless))


if __name__ == '__main__':
    raise SystemExit(main())
