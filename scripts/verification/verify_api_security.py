#!/usr/bin/env python3
"""Actual loopback API/Vite/Chromium adversarial control-plane acceptance."""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import traceback

import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect

from verify_startup import ROOT, Process, TRACE_FLAGS, free_ports, request, wait_for, now
import sys
sys.path.insert(0, str(ROOT / 'backend'))
from webagent.api import create_app
from webagent.config import Settings
from webagent.db import connect
from webagent.security import LocalApiPolicy, load_or_create_token
from webagent.settings.secrets import CredentialError


class NoCredentials:
    def __init__(self):
        self.calls = 0

    def put(self, *_):
        self.calls += 1
        raise AssertionError('Credential writes are outside this probe')

    def get(self, *_):
        self.calls += 1
        raise CredentialError('missing')

    def delete(self, *_):
        self.calls += 1
        raise AssertionError('Credential deletion is outside this probe')


class Attacker(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<!doctype html><title>Isolated malicious origin</title><p>Synthetic adversarial page</p>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def verify(output: Path, report: dict):
    data = output / 'data'
    data.mkdir()
    token = load_or_create_token(data)
    api_headers = {'Authorization': 'Bearer ' + token}
    api_port, ui_port = free_ports()
    api_url, ui_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
    policy = LocalApiPolicy(token, frozenset({f'127.0.0.1:{api_port}'}), frozenset({api_url, ui_url}))
    store = NoCredentials()
    server = uvicorn.Server(uvicorn.Config(create_app(Settings(data), secret_store=store,
        local_api_policy=policy), host='127.0.0.1', port=api_port, log_level='error', access_log=False))
    api_thread = threading.Thread(target=server.run, daemon=True)
    attacker = ThreadingHTTPServer(('127.0.0.1', 0), Attacker)
    attacker_thread = threading.Thread(target=attacker.serve_forever, daemon=True)
    attacker_url = f'http://localhost:{attacker.server_port}'
    env = {**os.environ, 'WEBAGENT_DATA_DIR': str(data), 'WEBAGENT_API_PORT': str(api_port),
           'WEBAGENT_UI_PORT': str(ui_port), 'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1'}
    for name in ('NODE_OPTIONS', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    for flag in TRACE_FLAGS:
        env[flag] = 'false'
    frontend = None
    report.update(checks=[], browser_responses=[], processes=[],
                  configuration={'api_url': api_url, 'ui_url': ui_url, 'attacker_origin': attacker_url,
                                 'credentials': 'none', 'model_requests': False})

    def record(name, **details):
        report['checks'].append({'name': name, 'passed': True, **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    def unchanged():
        with connect(data / 'business.sqlite3') as db:
            assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM model_settings_versions').fetchone()[0] == 0
        assert store.calls == 0

    try:
        api_thread.start()
        attacker_thread.start()
        wait_for(lambda: server.started and request(api_url + '/health', api_headers)[0] == 200, 'API', [])
        frontend = Process('security-frontend', 'frontend', output, env)
        wait_for(lambda: request(ui_url)[0] == 200, 'UI', [frontend])
        with httpx.Client(trust_env=False, timeout=4) as client:
            paths = ['/health', '/v1/settings', '/v1/tasks', '/v1/tasks/fake', '/v1/events']
            for path in paths:
                for headers in ({}, {'Authorization': 'Bearer incorrect'}):
                    response = client.get(api_url + path, headers=headers)
                    assert response.status_code == 401 and response.headers['cache-control'] == 'no-store'
            record('all_reads_health_and_sse_require_bearer', paths=paths)
            for headers in [{'Host': 'rebind.attacker.example'}, {'Host': f'localhost:{api_port}'},
                            {'Host': f'127.0.0.1:{ui_port}'}, {'Origin': 'null'},
                            {'Origin': attacker_url}, {'Origin': ui_url + '/'},
                            {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}]:
                response = client.post(api_url + '/v1/tasks', json={'instruction': 'attack'},
                    headers={**api_headers, **headers, 'Idempotency-Key': 'must-not-write'})
                assert response.status_code == 403
            record('exact_host_origin_null_and_fetch_metadata_rejected_with_valid_bearer')
            for url in (api_url + '/v1/tasks', ui_url + '/api/v1/tasks'):
                response = client.options(url, headers={'Origin': attacker_url,
                    'Access-Control-Request-Method': 'POST',
                    'Access-Control-Request-Headers': 'authorization,x-webpilot-client'})
                assert response.status_code == 403 and 'access-control-allow-origin' not in response.headers
            record('cross_origin_preflight_never_grants_permission')
            for headers in ({}, {'X-WebPilot-Client': '1'}, {'Host': 'rebind.attacker.example'},
                            {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Dest': 'empty',
                             'X-WebPilot-Client': '1', 'Origin': attacker_url}):
                response = client.post(ui_url + '/api/v1/tasks', json={'instruction': 'attack'}, headers=headers)
                assert response.status_code == 403
            record('vite_proxy_requires_browser_same_origin_gate')
            raw_file = client.get(ui_url + '/@fs' + str(data / '.security/local-api-token'))
            assert raw_file.status_code in (403, 404) and token not in raw_file.text
            source = client.get(ui_url + '/src/api.ts')
            assert source.status_code == 200 and token not in source.text
            record('credential_not_exposed_by_source_or_vite_filesystem')
            unchanged()
            record('http_attacks_have_no_database_or_credential_effect')

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--disable-background-networking'])
                context = browser.new_context(service_workers='block')
                page = context.new_page()
                page.on('response', lambda response: report['browser_responses'].append({
                    'path': response.url.split('?', 1)[0].replace(api_url, 'API').replace(ui_url, 'UI').replace(attacker_url, 'ATTACKER'),
                    'status': response.status, 'method': response.request.method}))
                page.goto(attacker_url)
                results = page.evaluate('''async ({api, ui}) => {
                  const results = [];
                  for (const base of [api, ui + '/api']) {
                    for (const mode of ['cors', 'no-cors']) {
                      try {
                        const response = await fetch(base + '/v1/tasks', {method:'POST', mode,
                          headers: mode === 'cors' ? {'Content-Type':'application/json','X-WebPilot-Client':'1','Idempotency-Key':'browser-attack'} : {'Content-Type':'text/plain'},
                          body:JSON.stringify({instruction:'untrusted page'})});
                        results.push({mode, readable:response.type !== 'opaque', status:response.status});
                      } catch { results.push({mode, readable:false, blocked:true}); }
                    }
                    try { await fetch(base + '/v1/settings'); results.push({readable:true}); }
                    catch { results.push({readable:false, blocked:true}); }
                    const image = new Image(); image.src = base + '/v1/settings'; document.body.append(image);
                    const frame = document.createElement('iframe'); frame.src = base + '/v1/settings'; document.body.append(frame);
                    const target = document.createElement('iframe'); target.name='post-target-' + results.length; document.body.append(target);
                    const form = document.createElement('form'); form.action=base + '/v1/tasks'; form.method='POST';
                    form.target=target.name; form.enctype='text/plain';
                    const input=document.createElement('input'); input.name='instruction'; input.value='attack';
                    form.append(input); document.body.append(form); form.submit();
                    const events = new EventSource(base + '/v1/events'); setTimeout(()=>events.close(),300);
                  }
                  await new Promise(resolve=>setTimeout(resolve,600));
                  return results;
                }''', {'api': api_url, 'ui': ui_url})
                assert results and not any(item['readable'] for item in results)
                unchanged()
                record('real_malicious_page_fetch_nocors_form_frame_image_eventsource_blocked', attempts=len(results))
                # A sandboxed opaque origin cannot gain the proxy capability.
                opaque = page.evaluate('''async ({ui}) => {
                    return await new Promise(resolve => {
                      const frame=document.createElement('iframe'); frame.sandbox='allow-scripts';
                      window.addEventListener('message', event => {if(event.source===frame.contentWindow)resolve(event.data)}, {once:true});
                      frame.srcdoc='<script>fetch(' + JSON.stringify(ui + '/api/v1/settings') + ',{headers:{"X-WebPilot-Client":"1"}}).then(()=>parent.postMessage("readable","*")).catch(()=>parent.postMessage("blocked","*"))<\\/script>';
                      document.body.append(frame); setTimeout(()=>resolve('timeout'),1500);
                    });
                }''', {'ui': ui_url})
                assert opaque == 'blocked'
                unchanged()
                record('opaque_sandbox_origin_cannot_access_proxy')
                page.goto(ui_url, wait_until='networkidle')
                expect(page.get_by_role('heading', name='API 已连接', exact=True)).to_be_visible()
                page.locator('#model-data-consent').check()
                page.get_by_role('button', name='保存模型设置', exact=True).click()
                expect(page.get_by_text('配置版本 1', exact=True)).to_be_visible()
                response = client.get(api_url + '/v1/settings', headers=api_headers)
                assert response.json()['version'] == 1 and store.calls == 0
                record('normal_ui_health_and_model_save_use_guarded_proxy')
                browser_state = page.evaluate('''() => ({local:Object.keys(localStorage),session:Object.keys(sessionStorage),html:document.documentElement.outerHTML})''')
                assert browser_state['local'] == [] and browser_state['session'] == []
                assert token not in browser_state['html'] and not context.cookies()
                page.screenshot(path=str(output / 'normal-ui.png'), full_page=True)
                record('no_bearer_in_browser_storage_dom_or_cookies', screenshot='normal-ui.png')
                context.close()
                browser.close()
            with client.stream('GET', api_url + '/v1/events', headers=api_headers) as response:
                assert response.status_code == 200 and next(response.iter_lines()) == 'retry: 1000'
            record('authenticated_sse_remains_available')
        report['passed'] = True
    finally:
        if frontend is not None:
            report['processes'].append(frontend.stop())
        server.should_exit = True
        if api_thread.ident is not None:
            api_thread.join(12)
        attacker.shutdown(); attacker.server_close(); attacker_thread.join(3)
        assert not api_thread.is_alive() and not attacker_thread.is_alive()
        report['processes'].append({'name': 'isolated-api-and-attacker', 'stopped': True})
        # The private credential file itself is expected; every other artifact
        # must be free of its bytes, and .security is excluded from evidence.
        assert not [path for path in output.rglob('*') if path.is_file()
                    and '.security' not in path.parts and token.encode() in path.read_bytes()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('api-security-%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT / 'artifacts/verification/M1-12' / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-12', 'probe': 'local-api-browser-security', 'passed': False, 'started_at': now()}
    try:
        verify(output, report)
    except Exception as error:
        report['passed'] = False
        report['error'] = {'type': type(error).__name__, 'message': 'Verification failed; inspect source location',
            'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                       for frame in traceback.extract_tb(error.__traceback__)]}
    finally:
        report['finished_at'] = now()
        report['artifact_sha256'] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob('*')) if path.is_file() and '.security' not in path.parts and path.name != 'report.json'}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'report': str(output / 'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
