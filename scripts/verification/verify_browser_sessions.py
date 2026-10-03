#!/usr/bin/env python3
"""M1-08 real headed Chromium lifecycle probe, restricted to a local fixture.

Uses temporary profiles, synthetic authentication state and an in-memory key
store with the production AES vault. It never opens the user's Chrome profile,
contacts a real site, or launches a task Run. Each context uses the production
network proxy with one explicitly registered WebArena fixture origin.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')
for _name in ('LANGSMITH_TRACING', 'LANGSMITH_TRACING_V2', 'LANGCHAIN_TRACING',
              'LANGCHAIN_TRACING_V2', 'LANGCHAIN_HANDLER'):
    os.environ[_name] = 'false'

from webagent.config import Settings
from webagent.db import connect, migrate
from webagent.errors import BusinessError
from webagent.network.config import NetworkConfig
from webagent.network.policy import Endpoint
from webagent.sessions.auth import AuthStateStore
from webagent.sessions.manager import ManagedBrowser
from webagent.sessions.models import SessionOwner
from webagent.settings.secrets import CredentialError, validate_reference

SYNTHETIC_AUTH = 'SYNTHETIC_BROWSER_AUTH_NEVER_EXPORT_608'
SYNTHETIC_OTHER = 'SYNTHETIC_OTHER_BROWSER_ACCOUNT_723'
FIXTURE = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>M1-08 isolated session verification</title>
<style>body{font:18px system-ui;margin:64px;background:#f3f6fa;color:#172b42}
main{max-width:720px;padding:32px;background:white;border:1px solid #d6e0eb;border-radius:12px}
small{color:#51667b}</style><main><small>WebPilot / local synthetic fixture</small>
<h1>Isolated browser session</h1><p>This page contains no external resources.</p>
<p>Authentication test values are never displayed or included in screenshots.</p></main></html>'''

WRITE_STATE = '''async value => {
  document.cookie = `synthetic_auth=${value}; Path=/; SameSite=Lax`;
  localStorage.setItem('synthetic-local-auth', value);
  sessionStorage.setItem('synthetic-tab-auth', value);
  const db = await new Promise((resolve, reject) => {
    const req = indexedDB.open('synthetic-auth-database', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('values');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(new Error('synthetic database open failed'));
  });
  await new Promise((resolve, reject) => {
    const transaction = db.transaction('values', 'readwrite');
    transaction.objectStore('values').put(value, 'auth');
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(new Error('synthetic database write failed'));
  });
  db.close();
}'''
READ_STATE = '''async () => {
  const databases = await indexedDB.databases();
  let indexed = null;
  if (databases.some(value => value.name === 'synthetic-auth-database')) {
    const db = await new Promise((resolve, reject) => {
      const req = indexedDB.open('synthetic-auth-database', 1);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(new Error('synthetic database read failed'));
    });
    indexed = await new Promise((resolve, reject) => {
      const req = db.transaction('values').objectStore('values').get('auth');
      req.onsuccess = () => resolve(req.result ?? null);
      req.onerror = () => reject(new Error('synthetic database read failed'));
    });
    db.close();
  }
  return {cookie: document.cookie, local: localStorage.getItem('synthetic-local-auth'),
          session: sessionStorage.getItem('synthetic-tab-auth'), indexed};
}'''


def now():
    return datetime.now(timezone.utc).isoformat()


class SyntheticKeyStore:
    def __init__(self):
        self.values = {}

    def put(self, reference, secret):
        validate_reference(reference)
        if reference in self.values:
            raise CredentialError('already_exists')
        self.values[reference] = secret

    def get(self, reference):
        validate_reference(reference)
        if reference not in self.values:
            raise CredentialError('missing')
        return self.values[reference]

    def delete(self, reference):
        self.values.pop(reference, None)


class LocalFixture:
    """An HTTP origin with synthetic content and no external resources."""
    def __init__(self):
        self.requests = []
        self.active = set()
        self.port = None

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            method, target, _ = raw.split(b'\r\n', 1)[0].decode('ascii', 'replace').split(' ', 2)
            allowed = method == 'GET' and target in ('/fixture', '/favicon.ico')
            # Do not record Cookie, Authorization, request bodies or arbitrary headers.
            self.requests.append({'method': method, 'target': target, 'allowed': allowed,
                                  'upstream_forwarded': False})
            body = FIXTURE if allowed and target.endswith('/fixture') else b'ok' if allowed else b'access denied'
            status = b'200 OK' if allowed else b'403 Forbidden'
            writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Type: text/html; charset=utf-8\r\n'
                         b'Cache-Control: no-store\r\nConnection: close\r\nContent-Length: '
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
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*self.active, return_exceptions=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/fixture'


def network_config(port):
    return NetworkConfig(webarena_endpoints=(Endpoint('http', '127.0.0.1', port),))


def launch_options():
    # Needed only to verify the exact owned PID before the destructive synthetic
    # browser-crash test. Network switches are owned by production manager code.
    return {'args': ['--enable-automation']}


def owner(identifier, *, identity='synthetic-account-a'):
    return SessionOwner(kind='verification', owner_id=identifier, site_id='local-session-fixture',
                        identity_ref=identity, realm='webarena')


def session_record(settings, session_id):
    with connect(settings.business_db) as db:
        value = db.execute('SELECT * FROM browser_sessions WHERE session_id=?', (session_id,)).fetchone()
    assert value is not None
    return dict(value)


async def wait_state(manager, settings, session_id, expected):
    async with asyncio.timeout(8):
        while True:
            await manager.drain_events()
            value = session_record(settings, session_id)
            if value['state'] == expected:
                return value
            await asyncio.sleep(0.02)


async def page_for(manager, info, owned_by, url):
    context = await manager.context(info.session_id, owned_by)
    page = context.pages[0] if context.pages else await context.new_page()
    response = await page.goto(url, wait_until='load')
    assert response.status == 200
    return context, page


async def chromium_identity(browser):
    """Obtain only this Playwright launch's PID and unique temporary profile."""
    cdp = await browser.new_browser_cdp_session()
    try:
        processes = await cdp.send('SystemInfo.getProcessInfo')
        arguments = (await cdp.send('Browser.getBrowserCommandLine'))['arguments']
    finally:
        await cdp.detach()
    pids = [item['id'] for item in processes['processInfo'] if item['type'] == 'browser']
    profiles = [arg.removeprefix('--user-data-dir=') for arg in arguments if arg.startswith('--user-data-dir=')]
    assert len(pids) == len(profiles) == 1 and 'playwright' in profiles[0]
    assert int(pids[0]) != os.getpid()
    return {'pid': int(pids[0]), 'profile': profiles[0]}


def kill_owned_chromium(identity):
    """No name-wide process lookup: recheck this PID's unique launch profile."""
    process = subprocess.run(['ps', '-p', str(identity['pid']), '-o', 'command='],
                             capture_output=True, text=True, check=False)
    if process.returncode:
        return False
    if '--user-data-dir=' + identity['profile'] not in process.stdout:
        raise AssertionError('Refused to signal a PID whose owned Chromium profile changed')
    try:
        os.kill(identity['pid'], signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


async def crash_child(args):
    settings = Settings(args.crash_child)
    migrate(settings.business_db)
    vault = AuthStateStore(settings.data_dir / 'auth', key_store=SyntheticKeyStore())
    manager = ManagedBrowser(settings, auth_store=vault, launch_options=launch_options(),
                             network_config=network_config(args.fixture_port))
    await manager.start()
    owned_by = owner('abrupt-manager-child')
    info = await manager.create(owned_by)
    context, _ = await page_for(manager, info, owned_by, f'http://127.0.0.1:{args.fixture_port}/fixture')
    identity = await chromium_identity(context.browser)
    print(json.dumps({'session_id': info.session_id, 'chromium': identity}), flush=True)
    await asyncio.Event().wait()


async def verify(output, report):
    def record(name, **details):
        report['checks'].append({'name': name, 'passed': True, 'at': now(), **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    fixture = await LocalFixture().start()
    managers = []
    chromium_processes = []
    child = None
    temporary = tempfile.TemporaryDirectory(prefix='browser-session-data-', dir=output)
    report['test_network_policy'] = {'mode': 'production authenticated per-context egress proxy',
        'allowed_url': fixture.url, 'realm': 'webarena', 'default_background_policy': 'deny_all',
        'scope': 'one explicit synthetic fixture origin; no public or unregistered private destinations'}
    try:
        with nullcontext(temporary.name) as temporary_path:
            settings = Settings(Path(temporary_path))
            migrate(settings.business_db)
            key_store = SyntheticKeyStore()
            vault = AuthStateStore(settings.data_dir / 'auth', key_store=key_store)
            manager = ManagedBrowser(settings, auth_store=vault, launch_options=launch_options(),
                                     network_config=network_config(fixture.port))
            managers.append(manager)
            await manager.start()
            a, b = owner('isolated-a'), owner('isolated-b', identity='synthetic-account-b')
            first, second = await manager.create(a), await manager.create(b)
            context_a, page_a = await page_for(manager, first, a, fixture.url)
            context_b, page_b = await page_for(manager, second, b, fixture.url)
            chromium_processes.append(await chromium_identity(context_a.browser))
            report['browser'] = {'version': context_a.browser.version, 'headless': False,
                                 'temporary_profile': True, 'user_profile_accessed': False}
            await page_a.evaluate(WRITE_STATE, SYNTHETIC_AUTH)
            empty = await page_b.evaluate(READ_STATE)
            assert empty == {'cookie': '', 'local': None, 'session': None, 'indexed': None}
            await page_b.evaluate(WRITE_STATE, SYNTHETIC_OTHER)
            value_a, value_b = await page_a.evaluate(READ_STATE), await page_b.evaluate(READ_STATE)
            assert value_a['cookie'] == 'synthetic_auth=' + SYNTHETIC_AUTH
            assert value_b['cookie'] == 'synthetic_auth=' + SYNTHETIC_OTHER
            assert all(value_a[name] == SYNTHETIC_AUTH for name in ('local', 'session', 'indexed'))
            assert all(value_b[name] == SYNTHETIC_OTHER for name in ('local', 'session', 'indexed'))
            await page_a.screenshot(path=str(output / 'isolated-local-fixture.png'))
            record('two_real_contexts_isolate_cookie_local_session_and_indexed_storage')

            try:
                await manager.context(first.session_id, b)
            except BusinessError as error:
                assert error.status in (403, 409)
            else:
                raise AssertionError('A different owner obtained another context')
            record('context_lookup_requires_exact_owner')

            snapshot = await manager.save_auth(first.session_id, a)
            encrypted = settings.data_dir / 'auth' / (snapshot.ref + '.auth')
            ciphertext = encrypted.read_bytes()
            assert SYNTHETIC_AUTH.encode() not in ciphertext and SYNTHETIC_OTHER.encode() not in ciphertext
            assert stat.S_IMODE(encrypted.stat().st_mode) == 0o600
            assert stat.S_IMODE(encrypted.parent.stat().st_mode) == 0o700
            with connect(settings.business_db) as db:
                assert SYNTHETIC_AUTH not in '\n'.join(db.iterdump())
            record('explicit_auth_save_uses_encrypted_private_file', file_mode='0600', directory_mode='0700',
                   ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest())
            closed = await manager.close(first.session_id, a)
            assert closed.state == 'CLOSED'
            assert session_record(settings, first.session_id)['state'] == 'CLOSED'
            record('normal_context_close_is_durably_closed')

            restored = await manager.create(a, auth_ref=snapshot.ref, replaces=first.session_id)
            _, page_restored = await page_for(manager, restored, a, fixture.url)
            restored_values = await page_restored.evaluate(READ_STATE)
            assert restored_values['cookie'] == 'synthetic_auth=' + SYNTHETIC_AUTH
            assert restored_values['local'] == restored_values['indexed'] == SYNTHETIC_AUTH
            assert restored_values['session'] is None
            restored_record = session_record(settings, restored.session_id)
            assert restored_record['requires_identity_check'] and restored_record['requires_business_check']
            record('auth_restore_preserves_cookie_local_and_indexed_but_not_session_storage',
                   requires_identity_recheck=True, requires_business_recheck=True,
                   full_browser_memory_restored=False)

            extra_c, extra_d = owner('slot-c'), owner('slot-d')
            third, fourth = await manager.create(extra_c), await manager.create(extra_d)
            try:
                await manager.create(owner('slot-overflow'))
            except BusinessError as error:
                assert error.status in (409, 429)
            else:
                raise AssertionError('A fifth live context exceeded the capacity')
            assert len(context_a.browser.contexts) == 4
            assert (await page_b.evaluate(READ_STATE))['session'] == SYNTHETIC_OTHER
            record('four_context_capacity_does_not_evict_existing_contexts')
            await manager.close(third.session_id, extra_c)
            await manager.close(fourth.session_id, extra_d)

            await page_restored.close()
            lost_window = await wait_state(manager, settings, restored.session_id, 'LOST')
            assert lost_window['requires_identity_check'] and lost_window['requires_business_check']
            record('closing_last_page_marks_session_lost_with_required_rechecks')
            assert kill_owned_chromium(chromium_processes[-1])
            lost_browser = await wait_state(manager, settings, second.session_id, 'LOST')
            assert lost_browser['requires_identity_check'] and lost_browser['requires_business_check']
            record('killing_only_this_launched_chromium_marks_owned_contexts_lost')
            await manager.aclose()
            managers.remove(manager)

            environment = {**os.environ, 'PYTHONUNBUFFERED': '1'}
            for name in ('NODE_OPTIONS', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                         'http_proxy', 'https_proxy', 'all_proxy'):
                environment.pop(name, None)
            child = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
                '--crash-child', str(settings.data_dir), '--fixture-port', str(fixture.port),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment)
            line = await asyncio.wait_for(child.stdout.readline(), 25)
            if not line:
                raise AssertionError('Abrupt-manager fixture did not become ready')
            child_info = json.loads(line)
            chromium_processes.append(child_info['chromium'])
            assert session_record(settings, child_info['session_id'])['state'] == 'OPEN'
            child.kill()
            await asyncio.wait_for(child.wait(), 8)
            child = None
            kill_owned_chromium(child_info['chromium'])
            restarted = ManagedBrowser(settings, auth_store=vault, launch_options=launch_options(),
                                       network_config=network_config(fixture.port))
            managers.append(restarted)
            await restarted.start()
            orphan = session_record(settings, child_info['session_id'])
            assert orphan['state'] == 'LOST'
            assert orphan['requires_identity_check'] and orphan['requires_business_check']
            record('abrupt_manager_restart_marks_orphaned_context_lost')
            await restarted.aclose()
            managers.remove(restarted)
            with connect(settings.business_db) as db:
                sessions = [dict(row) for row in db.execute('SELECT * FROM browser_sessions ORDER BY created_at,session_id')]
                assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
            assert all(value['state'] in ('CLOSED', 'LOST') for value in sessions)
            serial = json.dumps(sessions, ensure_ascii=False)
            assert SYNTHETIC_AUTH not in serial and SYNTHETIC_OTHER not in serial
            (output / 'session-registry.json').write_text(json.dumps(sessions, ensure_ascii=False, indent=2) + '\n')
            record('all_sessions_terminal_and_no_task_execution_run_created', session_count=len(sessions))
    finally:
        cleanup_errors = []
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
        for manager in reversed(managers):
            try:
                await manager.aclose()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        for identity in chromium_processes:
            try:
                kill_owned_chromium(identity)
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        await fixture.close()
        temporary.cleanup()
        serialized = json.dumps(fixture.requests, ensure_ascii=False, indent=2)
        assert SYNTHETIC_AUTH not in serialized and SYNTHETIC_OTHER not in serialized
        (output / 'fixture-request-summary.json').write_text(serialized + '\n')
        report['cleanup_errors'] = cleanup_errors
        if cleanup_errors:
            raise AssertionError('Verification cleanup did not complete: ' + ','.join(cleanup_errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--crash-child', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--fixture-port', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_child:
        asyncio.run(crash_child(args))
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT / 'artifacts/verification/M1-08' / ('browser-' + stamp)).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-08', 'probe': 'headed-browser-sessions', 'passed': False,
              'started_at': now(), 'checks': [], 'scope':
              'Real headed bundled Chromium, isolated temporary contexts, production encrypted auth files with synthetic keys. '
              'Only a loopback fixture; no user profile, real account, external website or execution Run.'}
    try:
        asyncio.run(verify(output, report))
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    report['finished_at'] = now()
    report['artifact_sha256'] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(output.rglob('*')) if path.is_file()}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'report': str(output / 'report.json'),
                      'error': report.get('error')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
