#!/usr/bin/env python3
"""Real HTTP → private RPC → Chromium → SQLite login acceptance.

Only a registered local fixture and synthetic credentials are used. The real
AES vault uses an in-memory key store; native Keychain is verified separately.
Never capture a login screenshot, DOM dump, request body or authentication data.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

import httpx
import uvicorn
from webagent.api import create_app
from webagent.config import Settings
from webagent.db import connect, migrate
from webagent.errors import BusinessError
from webagent.identities.rpc import LoginServer, socket_path
from webagent.identities.service import LoginService
from webagent.identities.sites import FixtureSiteAdapter, SiteCatalog
from webagent.security.local_api import LocalApiPolicy
from webagent.security.token import load_or_create_token
from webagent.sessions.auth import AuthStateStore
from webagent.sessions.manager import ManagedBrowser
from verify_browser_sessions import SyntheticKeyStore, network_config

PASSWORD = 'SYNTHETIC_PASSWORD_PRIVATE_M109_982'
OTP = 'SYNTHETIC_OTP_PRIVATE_M109_463'
COOKIE = 'SYNTHETIC_COOKIE_PRIVATE_M109_871'
FORM = b'''<!doctype html><html><head><meta name="user-login" content=""></head>
<body class="logged-out"><h1>WebPilot synthetic login fixture</h1>
<form method="post" action="/login"><label>Account<input name="account"></label>
<label>Password<input name="password" type="password"></label>
<label>One-time code<input name="otp" type="password"></label>
<button>Sign in</button></form></body></html>'''


class LoginFixture:
    def __init__(self):
        self.account = None
        self.expired = False
        self.active = set()

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            lines = raw.decode('latin-1').split('\r\n')
            method, target, _ = lines[0].split(' ', 2)
            headers = dict(line.split(': ', 1) for line in lines[1:] if ': ' in line)
            headers = {k.lower(): v for k, v in headers.items()}
            extra = b''
            status = b'200 OK'
            if method == 'POST' and target == '/login':
                body = await reader.readexactly(int(headers.get('content-length', '0')))
                fields = parse_qs(body.decode())
                assert fields.get('password') == [PASSWORD] and fields.get('otp') == [OTP]
                account = fields.get('account', [''])[0]
                assert account in ('alice', 'bob')
                self.account, self.expired = account, False
                extra = ('Set-Cookie: synthetic_login=' + COOKIE + '; Path=/; HttpOnly; SameSite=Lax\r\n'
                         'Location: /identity\r\n').encode()
                status, body = b'303 See Other', b''
            elif target == '/identity' and self.account and not self.expired and (
                    'synthetic_login=' + COOKIE) in headers.get('cookie', ''):
                body = ('<!doctype html><html><head><meta name="user-login" content="' + self.account
                        + '"></head><body class="logged-in"><h1>Signed in</h1></body></html>').encode()
            else:
                body = FORM
            writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Type: text/html; charset=utf-8\r\n'
                b'Cache-Control: no-store\r\nConnection: close\r\n' + extra + b'Content-Length: '
                + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
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
        self.origin = f'http://127.0.0.1:{self.port}'
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.active):
            task.cancel()
        await asyncio.gather(*self.active, return_exceptions=True)


async def exercise(report):
    fixture = await LoginFixture().start()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(64)
    listener.setblocking(False)
    authority = f'127.0.0.1:{listener.getsockname()[1]}'
    manager = rpc = server = serving = None
    temporary = tempfile.TemporaryDirectory(prefix="webpilot-login-acceptance-")
    try:
        folder = temporary.name
        settings = Settings(Path(folder).resolve())
        migrate(settings.business_db)
        key_store = SyntheticKeyStore()
        auth = AuthStateStore(settings.data_dir / 'auth', key_store=key_store)
        manager = ManagedBrowser(settings, auth_store=auth, network_config=network_config(fixture.port))
        catalog = SiteCatalog(adapters=(FixtureSiteAdapter(site_id='fixture-github', origin=fixture.origin),))
        service = LoginService(settings, manager, catalog=catalog)
        await service.start()
        rpc = await LoginServer(settings, service).start()
        token = load_or_create_token(settings.data_dir)
        policy = LocalApiPolicy(token, frozenset({authority}), frozenset({'http://' + authority}))
        app = create_app(settings, local_api_policy=policy)
        server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.02)
        responses = []
        def check(name, condition):
            assert condition, name
            report['checks'][name] = True

        async with httpx.AsyncClient(base_url='http://' + authority,
                headers={'Authorization': 'Bearer ' + token}, timeout=80) as client:
            async def request(method, path, payload=None, status=200):
                response = await client.request(method, '/v1/identities' + path, json=payload)
                assert response.status_code == status, (path, response.status_code)
                value = response.json()
                responses.append(value)
                return value

            async def create(**extra):
                return await request('POST', '/login-sessions', {
                    'site_id': 'fixture-github', 'expected_account': 'alice', **extra}, status=201)

            async def command(login, action):
                return await request('POST', '/login-sessions/' + login['login_id'] + '/' + action,
                                     {'expected_version': login['state_version']})

            async def context(login):
                info = service.store.get(login['login_id'])
                return await manager.login_context(login['session_id'], service._owner(info))

            async def sign_in(login, account):
                page = (await context(login)).pages[0]
                await page.goto(fixture.origin + '/login')
                await page.locator('[name=account]').fill(account)
                await page.locator('[name=password]').fill(PASSWORD)
                await page.locator('[name=otp]').fill(OTP)
                async with page.expect_navigation():
                    await page.get_by_role('button', name='Sign in').click()

            sites = await request('GET', '/sites')
            check('trusted_site_catalog', sites[0]['site_id'] == 'fixture-github')
            login = await create()
            check('anonymous_preparation', login['state'] == 'AWAITING_USER' and login['identity_ref'] is None)
            owned_by = service._owner(service.store.get(login['login_id']))
            for name, action in [('model_capture_blocked', manager.context), ('generic_auth_export_blocked', manager.save_auth)]:
                try:
                    await action(login['session_id'], owned_by)
                except BusinessError as error:
                    check(name, error.status == 403)
                else:
                    raise AssertionError(name)
            login = await command(login, 'confirm')
            check('not_logged_in_rejected', login['state'] == 'NEEDS_LOGIN' and login['identity_ref'] is None)
            await sign_in(login, 'bob')
            login = await command(login, 'confirm')
            check('wrong_account_rejected', login['state'] == 'NEEDS_LOGIN' and login['reason'] == 'account_mismatch')
            check('no_identity_before_proof', await request('GET', '') == [])
            await sign_in(login, 'alice')
            old_version = login['state_version']
            login = await command(login, 'confirm')
            identity_ref = login['identity_ref']
            check('correct_account_verified', login['state'] == 'VERIFIED' and bool(identity_ref))
            await request('POST', '/login-sessions/' + login['login_id'] + '/confirm',
                          {'expected_version': old_version}, status=409)
            check('stale_confirmation_conflicts', True)
            check('verified_capture_still_blocked', login['capture_blocked'] is True)
            login = await command(login, 'close')
            check('explicit_close', login['state'] == 'CLOSED')
            restored = await create(expected_identity_ref=identity_ref)
            restored = await command(restored, 'confirm')
            check('encrypted_restore_reverified', restored['state'] == 'VERIFIED' and restored['identity_ref'] == identity_ref)
            await command(restored, 'close')
            fixture.expired = True
            expired = await create(expected_identity_ref=identity_ref)
            expired = await command(expired, 'confirm')
            check('expired_restore_requires_login', expired['state'] == 'NEEDS_LOGIN' and expired['identity_ref'] is None)
            await request('POST', '/login-sessions', {'site_id': 'fixture-github', 'expected_account': 'bob',
                          'expected_identity_ref': identity_ref}, status=403)
            check('identity_scope_enforced', True)
            await request('POST', '/login-sessions', {'site_id': 'fixture-github', 'expected_account': 'alice',
                          'password': PASSWORD, 'otp': OTP, 'url': 'http://127.0.0.1'}, status=422)
            check('credential_and_url_payload_rejected', True)
            page = (await context(expired)).pages[0]
            await page.close()
            async with asyncio.timeout(8):
                while True:
                    lost = await request('GET', '/login-sessions/' + expired['login_id'])
                    if lost['state'] == 'LOST':
                        break
                    await asyncio.sleep(.02)
            check('user_closed_window_detected', lost['identity_ref'] is None)
            pending = await create()
            await rpc.aclose()
            rpc = None
            await request('GET', '/login-sessions/' + pending['login_id'], status=503)
            check('worker_unavailable_fails_closed', True)
            await manager.aclose()
            manager = ManagedBrowser(settings, auth_store=auth, network_config=network_config(fixture.port))
            service = LoginService(settings, manager, catalog=catalog)
            await service.start()
            rpc = await LoginServer(settings, service).start()
            recovered = await request('GET', '/login-sessions/' + pending['login_id'])
            check('worker_restart_invalidates_preparation', recovered['state'] in ('LOST', 'CLOSED'))
            with connect(settings.business_db) as db:
                rows = list(db.execute('SELECT * FROM identities'))
                proofs = list(db.execute('SELECT * FROM identity_verifications'))
                dump = '\n'.join(db.iterdump())
            check('atomic_identity_and_proof_publication', len(rows) == 1 and len(proofs) == 2)
            serialized = json.dumps(responses) + dump
            files = [path for path in settings.data_dir.rglob('*') if path.is_file() and '.security' not in path.parts]
            check('credentials_absent_from_api_database_and_disk', all(secret not in serialized and
                all(secret.encode() not in path.read_bytes() for path in files) for secret in (PASSWORD, OTP, COOKIE)))
            check('rpc_socket_private', socket_path(settings).stat().st_mode & 0o777 == 0o600)
            report['database'] = {'identity_count': len(rows), 'verification_count': len(proofs)}
            report['passed'] = True
    finally:
        if serving is not None:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
        if rpc is not None:
            await rpc.aclose()
        if manager is not None:
            await manager.aclose()
        listener.close()
        await fixture.close()
        temporary.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-09', 'passed': False, 'checks': {},
              'created_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Real loopback HTTP, authenticated Unix RPC, headed Chromium, production proxy, SQLite and AES; synthetic site/credentials and memory key store. No real GitHub account verified.'}
    try:
        asyncio.run(exercise(report))
    except Exception as error:
        # Avoid exception values/locals, which may contain form or auth data.
        import traceback
        report['error_type'] = type(error).__name__
        report['error_locations'] = [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                     'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]
    report['artifact_sha256'] = {}
    path = args.output_dir / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(path)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
