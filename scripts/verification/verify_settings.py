#!/usr/bin/env python3
"""Verify M1-07 through real Chromium, Vite proxy, FastAPI and isolated SQLite.

Only the OS credential boundary is a synthetic in-memory fixture. This probe
never reads user credentials or contacts a model provider. Native Keychain
acceptance is a separate probe. No request bodies, HAR or traces are recorded.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import traceback
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

from verify_startup import Process, TRACE_FLAGS, free_ports, now, request, wait_for
from webagent.api import create_app
from webagent.security import LocalApiPolicy, load_or_create_token
from webagent.config import Settings
from webagent.db import connect
from webagent.settings.secrets import CredentialError, validate_reference

import httpx
from playwright.sync_api import expect, sync_playwright
from pydantic import SecretStr
import uvicorn


class SyntheticStore:
    """Thread-safe test double; secret values exist only for this probe process."""
    def __init__(self):
        self._values: dict[str, SecretStr] = {}
        self._fault: str | None = None
        self._lock = threading.Lock()
        self.put_count = 0

    def put(self, reference: str, secret: SecretStr) -> None:
        validate_reference(reference)
        if not isinstance(secret, SecretStr):
            raise CredentialError('invalid_secret')
        with self._lock:
            if reference in self._values:
                raise CredentialError('already_exists')
            self._values[reference] = secret
            self.put_count += 1

    def get(self, reference: str) -> SecretStr:
        with self._lock:
            if self._fault:
                raise CredentialError(self._fault)
            if reference not in self._values:
                raise CredentialError('missing')
            return self._values[reference]

    def delete(self, reference: str) -> None:
        with self._lock:
            self._values.pop(reference, None)

    def fault(self, reason: str | None) -> None:
        with self._lock:
            self._fault = reason

    def contains_only(self, value: str) -> bool:
        with self._lock:
            return len(self._values) == 1 and next(iter(self._values.values())).get_secret_value() == value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


def verify(output: Path, report: dict, *, headed: bool) -> None:
    data = output / 'data'
    data.mkdir()
    api_port, ui_port = free_ports()
    api_url, ui_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
    store = SyntheticStore()
    synthetic_key = 'SYNTHETIC_M107_UI_' + uuid4().hex
    token = load_or_create_token(data)
    api_headers = {'Authorization': 'Bearer ' + token}
    policy = LocalApiPolicy(token, frozenset({f'127.0.0.1:{api_port}'}), frozenset({api_url, ui_url}))
    app = create_app(Settings(data), secret_store=store, local_api_policy=policy)
    api_requests: list[dict] = []

    @app.middleware('http')
    async def summarize_request(request, call_next):
        response = await call_next(request)
        # Only route names and status are retained, never headers or bodies.
        api_requests.append({'method': request.method, 'path': request.url.path,
                             'status': response.status_code})
        return response

    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=api_port,
                                         log_level='warning', access_log=False))
    api_thread = threading.Thread(target=server.run, daemon=True, name='m107-settings-api')
    env = {**os.environ, 'WEBAGENT_DATA_DIR': str(data), 'WEBAGENT_API_PORT': str(api_port),
           'WEBAGENT_UI_PORT': str(ui_port), 'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1'}
    for name in ('NODE_OPTIONS', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    for flag in TRACE_FLAGS:
        env[flag] = 'false'
    frontend = None
    browser_requests: list[dict] = []
    page_errors: list[dict] = []
    report.update(checks=[], processes=[], configuration={
        'api_url': api_url, 'ui_url': ui_url, 'data_dir': str(data),
        'credential_store': 'isolated-synthetic-memory', 'real_provider_requests': False,
        'headed': headed,
    })

    def record(name: str, **details) -> None:
        report['checks'].append({'name': name, 'passed': True, 'at': now(), **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    def stop_api() -> None:
        server.should_exit = True
        api_thread.join(timeout=12)
        assert not api_thread.is_alive(), 'Owned API thread failed to stop'

    try:
        api_thread.start()
        wait_for(lambda: server.started and request(api_url + '/health', api_headers)[0] == 200,
                 'isolated settings API', [])
        frontend = Process('settings-frontend', 'frontend', output, env)
        wait_for(lambda: request(ui_url)[0] == 200, 'settings frontend', [frontend])

        with httpx.Client(base_url=api_url, headers=api_headers, trust_env=False, timeout=5) as client:
            initial = client.get('/v1/settings')
            assert initial.status_code == 200 and initial.headers['cache-control'] == 'no-store'
            assert initial.json()['version'] == 0
            assert initial.json()['readiness']['credential_status'] == 'not_configured'
            assert initial.json()['task_execution_enabled'] is False
            record('initial_settings_missing_and_no_store')

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not headed, args=[
                    '--disable-background-networking', '--disable-component-update',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                ])
                context = browser.new_context(viewport={'width': 1280, 'height': 1000},
                                              service_workers='block')

                def restrict_network(route) -> None:
                    parsed = urlsplit(route.request.url)
                    allowed = parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port in (api_port, ui_port)
                    if not allowed:
                        browser_requests.append({'method': route.request.method, 'allowed': False})
                        route.abort()
                        return
                    route.continue_()

                def summarize_browser_request(request) -> None:
                    parsed = urlsplit(request.url)
                    if parsed.path == '/api/v1/settings/model' and request.method == 'PUT':
                        body = request.post_data_json
                        browser_requests.append({
                            'method': 'PUT', 'path': parsed.path, 'allowed': True,
                            'expected_version': body.get('expected_version'),
                            'api_key_supplied': 'api_key' in body,
                            'data_sharing_accepted': body.get('accept_data_sharing') is True,
                        })

                context.route('**/*', restrict_network)
                page = context.new_page()
                page.on('request', summarize_browser_request)
                # Error text could contain input; count only, never persist it.
                page.on('pageerror', lambda _: page_errors.append({'kind': 'pageerror'}))
                page.goto(ui_url, wait_until='networkidle')
                expect(page.get_by_text('本机模型配置尚未就绪', exact=True)).to_be_visible()
                save = page.get_by_role('button', name='保存模型设置', exact=True)
                reload = page.get_by_role('button', name='重新载入配置', exact=True)
                key = page.locator('#model-api-key')
                consent = page.locator('#model-data-consent')
                expect(save).to_be_disabled()
                assert page.locator('#model-data-disclosure').bounding_box()['y'] < key.bounding_box()['y']
                for phrase in ('任务正文', '过滤后的网页内容', '选中的截图'):
                    assert phrase in page.locator('#model-data-disclosure').inner_text()
                record('data_disclosure_precedes_key_and_requires_consent')

                key.fill(synthetic_key)
                consent.check()
                with page.expect_response(lambda response: response.url.endswith('/api/v1/settings/model')) as saved:
                    save.click()
                assert saved.value.status == 200
                expect(page.get_by_text('已保存配置版本 1。供应商连接及凭据有效性尚未验证。', exact=True)).to_be_visible()
                expect(key).to_have_value('')
                assert store.contains_only(synthetic_key) and store.put_count == 1
                assert synthetic_key not in page.locator('body').inner_text()
                assert page.evaluate('localStorage.length + sessionStorage.length') == 0
                first = client.get('/v1/settings')
                assert first.headers['cache-control'] == 'no-store'
                assert synthetic_key not in first.text
                assert first.json()['readiness'] == {
                    'ready': True, 'credential_status': 'available', 'provider_verified': False, 'reasons': [],
                }
                page.screenshot(path=str(output / '01-configured.png'), full_page=True)
                record('secret_saved_once_cleared_and_never_echoed', version=1, screenshot='01-configured.png')
                record('local_readiness_does_not_claim_provider_or_execution',
                       provider_verified=first.json()['readiness']['provider_verified'],
                       task_execution_enabled=first.json()['task_execution_enabled'])

                page.locator('#model-max-tokens').fill('2048')
                with page.expect_response(lambda response: response.url.endswith('/api/v1/settings/model')) as changed:
                    save.click()
                assert changed.value.status == 200
                expect(page.get_by_text('已保存配置版本 2。供应商连接及凭据有效性尚未验证。', exact=True)).to_be_visible()
                expect(key).to_have_value('')
                assert store.put_count == 1 and store.contains_only(synthetic_key)
                assert browser_requests[-1]['api_key_supplied'] is False
                current = client.get('/v1/settings').json()
                assert current['version'] == 2 and current['model']['max_tokens'] == 2048
                record('empty_key_omitted_and_existing_credential_preserved', version=2)

                # Simulate another local page publishing while this UI holds v2.
                external_body = {'expected_version': 2, 'model': {**current['model'], 'max_tokens': 3072},
                                 'accept_data_sharing': True}
                external = client.put('/v1/settings/model', json=external_body)
                assert external.status_code == 200 and external.json()['version'] == 3
                with page.expect_response(lambda response: response.url.endswith('/api/v1/settings/model')) as conflict:
                    save.click()
                assert conflict.value.status == 409
                expect(save).to_be_disabled()
                conflict_reload = page.get_by_role('button', name='重新载入最新配置', exact=True)
                expect(conflict_reload).to_be_visible()
                expect(key).to_have_value('')
                assert len([item for item in browser_requests if item['method'] == 'PUT']) == 3
                assert client.get('/v1/settings').json()['model']['max_tokens'] == 3072
                record('stale_save_returns_409_without_overwrite_or_automatic_retry')

                conflict_reload.click()
                expect(page.get_by_text('配置版本 3', exact=True)).to_be_visible()
                expect(page.locator('#model-max-tokens')).to_have_value('3072')
                expect(consent).not_to_be_checked()
                expect(key).to_have_value('')
                record('explicit_reload_reads_current_version_and_resets_unsaved_input')

                store.fault('locked')
                reload.click()
                expect(page.get_by_text('系统凭据库已锁定，请解锁后重试。', exact=True)).to_be_visible()
                expect(page.get_by_text('本机模型配置尚未就绪', exact=True)).to_be_visible()
                locked = client.get('/v1/settings').json()
                assert not locked['readiness']['ready'] and locked['readiness']['credential_status'] == 'locked'
                record('locked_credential_status_is_shown_without_false_readiness')

                page.set_viewport_size({'width': 375, 'height': 900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                expect(key).to_have_value('')
                page.screenshot(path=str(output / '02-mobile-locked.png'), full_page=True)
                record('mobile_layout_has_no_horizontal_overflow', viewport_width=375,
                       screenshot='02-mobile-locked.png')

                with connect(data / 'business.sqlite3') as db:
                    assert db.execute('SELECT COUNT(*) FROM model_settings_versions').fetchone()[0] == 3
                    assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
                record('configuration_does_not_create_or_start_runs', configuration_versions=3, runs=0)

                stop_api()
                reload.click()
                expect(page.get_by_text('暂时无法读取模型配置，请确认 API 已启动后重新载入。', exact=True)).to_be_visible()
                expect(save).to_be_disabled()
                expect(key).to_have_value('')
                record('api_unavailable_disables_form_and_keeps_reload_available')
                assert not page_errors
                assert not [item for item in browser_requests if not item['allowed']]
                assert len([item for item in browser_requests if item['method'] == 'PUT']) == 3
                record('no_browser_errors_external_requests_or_hidden_save_retries')
                context.close()
                browser.close()
        report['passed'] = True
    finally:
        cleanup_errors = []
        if api_thread.ident is not None and api_thread.is_alive():
            try:
                stop_api()
            except Exception:
                cleanup_errors.append('owned_api_thread_did_not_stop')
        report['processes'].append({'name': 'settings-api', 'stopped': not api_thread.is_alive()})
        if frontend is not None:
            try:
                result = frontend.stop()
                report['processes'].append(result)
                if result['forced_kill']:
                    cleanup_errors.append('frontend_required_forced_kill')
            except Exception:
                cleanup_errors.append('owned_frontend_process_did_not_stop')
        store.clear()
        report['http_requests'] = api_requests
        report['browser_requests'] = browser_requests
        report['browser_page_errors'] = page_errors
        report['cleanup_errors'] = cleanup_errors
        if cleanup_errors:
            report['passed'] = False
        # Check raw database/log/artifact bytes after every owned writer exits.
        leaked = [str(path.relative_to(output)) for path in output.rglob('*')
                  if path.is_file() and synthetic_key.encode() in path.read_bytes()]
        report['secret_scan'] = {'passed': not leaked, 'affected_artifacts': leaked}
        if leaked:
            report['passed'] = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='New evidence directory; existing directories are refused')
    parser.add_argument('--headed', action='store_true', help='Show the isolated bundled Chromium window')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('settings-%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT / 'artifacts' / 'verification' / 'M1-07' / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-07', 'probe': 'settings-ui-http', 'started_at': now(), 'passed': False,
              'scope': 'Real Chromium, Vite proxy, FastAPI and SQLite with a synthetic in-memory credential store. No user Keychain or model provider access.'}
    try:
        verify(output, report, headed=args.headed)
    except Exception as error:
        report['passed'] = False
        # Exception text/locals may contain synthetic input. Keep locations only.
        report['error'] = {'type': type(error).__name__, 'message': 'Verification failed; inspect completed checks and source location.',
                           'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                                      for frame in traceback.extract_tb(error.__traceback__)]}
    finally:
        report['finished_at'] = now()
        report['artifact_sha256'] = {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob('*')) if path.is_file() and '.security' not in path.parts and path.name != 'report.json'
        }
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'report': str(output / 'report.json')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
