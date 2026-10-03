"""M1-04 real HTTP acceptance using an isolated API and synthetic declared inputs."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from webagent.db import connect, transaction
from webagent.db.repository import create_run
from webagent.state import transition
from webagent.security import load_or_create_token

FINANCE = {
    'instruction': '读取本地财报夹具', 'source_ids': ['local-fixture'], 'scenario': 'finance',
    'parameters': {'entity_id': 'fixture-company', 'report_version': '2025',
                   'period_type': 'annual', 'metrics': ['revenue'], 'currency': 'USD'},
}


def verify(output: Path, report: dict):
    data = output / 'data'
    data.mkdir()
    api_headers = {"Authorization": "Bearer " + load_or_create_token(data)}
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    process = None
    log = (output / 'api.log').open('w')
    exchanges = []

    def start():
        nonlocal process
        process = subprocess.Popen([
            sys.executable, '-m', 'uvicorn', 'webagent.api:create_app', '--factory',
            '--fd', str(listener.fileno()), '--no-access-log',
        ], cwd=ROOT, pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend'), 'WEBAGENT_DATA_DIR': str(data), 'WEBAGENT_API_PORT': str(listener.getsockname()[1])})
        deadline = time.monotonic() + 15
        with httpx.Client(headers=api_headers, trust_env=False, timeout=.5) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('API exited during startup')
                try:
                    health = client.get(url + '/health')
                    if health.status_code == 200:
                        assert health.json()['task_execution_enabled'] is True
                        assert health.json()['task_compiler'] == 'fixture'
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
        raise TimeoutError('API startup')

    def stop():
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(8)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(5)
                raise RuntimeError('API did not stop cleanly')

    def record(name, **values):
        report['checks'].append({'name': name, 'passed': True, **values})

    try:
        start()
        with httpx.Client(base_url=url, headers=api_headers, trust_env=False, timeout=5) as client:
            def post(path, body, key):
                response = client.post(path, json=body, headers={'Idempotency-Key': key})
                exchanges.append({'path': path, 'status': response.status_code, 'response': response.json()})
                return response

            first = post('/v1/tasks', FINANCE, 'create-ready')
            assert first.status_code == 201
            task_id = first.json()['task']['task_id']
            location = '/v1/tasks/' + task_id
            assert first.headers['location'] == location
            assert first.json()['task']['preparation_status'] == 'READY'
            assert first.json()['current_run'] is None
            record('create_complete_contract_without_execution', task_id=task_id)
            replay = post('/v1/tasks', dict(reversed(list(FINANCE.items()))), 'create-ready')
            assert replay.content == first.content and replay.status_code == first.status_code
            record('same_key_normalized_body_exact_replay')
            conflict = post('/v1/tasks', {**FINANCE, 'instruction': '不同目标'}, 'create-ready')
            assert conflict.status_code == 409 and conflict.json()['code'] == 'IDEMPOTENCY_CONFLICT'
            record('same_key_different_body_conflict')
            invalid = post('/v1/tasks', {**FINANCE, 'force_execute': True}, 'invalid')
            assert invalid.status_code == 422 and invalid.json()['code'] == 'INVALID_PARAMETER'
            record('unknown_control_field_rejected')
            partial = post('/v1/tasks', {'instruction': FINANCE['instruction']}, 'partial')
            assert partial.status_code == 201 and partial.json()['contract'] is None
            partial_id = partial.json()['task']['task_id']
            clarify_path = f'/v1/tasks/{partial_id}/clarifications'
            selected = post(clarify_path, {'contract_version': 1,
                'values': {'scenario': 'finance', 'source_ids': ['local-fixture']}}, 'select')
            assert selected.status_code == 200 and selected.json()['contract_version'] == 2
            values = {'parameters.'+key: value for key,value in FINANCE['parameters'].items()}
            completed = post(clarify_path, {'contract_version': 2, 'values': values}, 'complete')
            assert completed.status_code == 200 and completed.json()['task']['preparation_status'] == 'READY'
            assert completed.json()['contract']['contract_version'] == 3
            record('progressive_clarification_creates_immutable_contract')
            stale = post(clarify_path, {'contract_version': 1, 'values': {'scenario': 'finance'}}, 'stale')
            assert stale.status_code == 409 and stale.json()['code'] == 'CONTRACT_VERSION_CONFLICT'
            record('stale_clarification_rejected')
            db_path = data / 'business.sqlite3'
            with connect(db_path) as db, transaction(db):
                create_run(db, task_id=task_id, run_id='fixture-failed-run', contract_version=1,
                           graph_version='fixture-v1', graph_state_schema_version='fixture-v1',
                           model_config_sha256='a'*64, runtime_config_sha256='b'*64)
                db.execute('UPDATE tasks SET current_run_id=? WHERE task_id=?', ('fixture-failed-run',task_id))
            changed = {**FINANCE, 'instruction': '新的显式目标', 'contract_version': 1}
            blocked = post(location+'/revisions', changed, 'revise')
            assert blocked.status_code == 409 and blocked.json()['code'] == 'STATE_CONFLICT'
            record('active_run_blocks_contract_replacement')
            transition(db_path, run_id='fixture-failed-run', expected_state_version=0, target='RUNNING')
            transition(db_path, run_id='fixture-failed-run', expected_state_version=1, target='FAILED')
            revised = post(location+'/revisions', changed, 'revise')
            assert revised.status_code == 200
            assert revised.json()['contract']['contract_version'] == 2
            assert revised.json()['contract_history'][0] == first.json()['contract']
            history = client.get(location+'/runs').json()['runs']
            assert len(history) == 1 and history[0]['state'] == 'FAILED' and history[0]['contract_version'] == 1
            detail = client.get(location).json()
            assert detail['contract']['original_instruction'] == FINANCE['instruction']
            record('new_contract_preserves_failed_run_and_original_contract')
            stop()
            start()
            after_restart = post('/v1/tasks', FINANCE, 'create-ready')
            assert after_restart.status_code == 201 and after_restart.content == first.content
            assert client.get(location).json()['contract']['contract_version'] == 2
            record('restart_replays_original_response_while_get_shows_latest')
            with connect(db_path) as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
                report['row_counts'] = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('tasks','contracts','runs','task_revisions','api_idempotency')}
            assert not (data/'graph.sqlite3').exists()
            record('business_storage_integrity_and_graph_isolation')
    finally:
        try:
            stop()
        finally:
            listener.close()
            log.close()
            (output/'http-exchanges.json').write_text(json.dumps(exchanges,ensure_ascii=False,indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT/'artifacts/verification/M1-04'/('http-'+stamp)).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-04', 'passed': False, 'checks': [],
              'scope': 'Real loopback HTTP and isolated synthetic records; no model, production execution or real website.'}
    try:
        verify(output, report)
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    report['artifact_sha256'] = {str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(output.rglob('*')) if p.is_file() and '.security' not in p.parts}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed': report['passed'], 'report': str(output/'report.json'), 'error': report.get('error')},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
