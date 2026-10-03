#!/usr/bin/env python3
"""M1-25 production UI/API/Worker loop over one fixed public arXiv paper.

This runner deliberately has no fixture provider, ledger seeding, adapter
injection or automatic success repair. The caller prepares a fresh isolated
business database and its real DeepSeek configuration through production
settings first. Real provider calls may incur a charge when this runner is
explicitly executed. Human source review is always a separate pending step.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'backend'), str(SCRIPTS)]
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')
from verify_startup import Process, TRACE_FLAGS, free_ports, now
from verify_task_entry import safe_error
from verify_fault_acceptance import process_inventory, descendants, live_owned

SOURCE_URL = 'https://arxiv.org/abs/2307.13854v1'
SOURCE_PATH = '/abs/2307.13854v1'
SOURCE_ID = 'm125-arxiv-fixed-paper'
QUERY = 'arxiv:2307.13854v1'
CUTOFF = '2026-10-03T00:00:00Z'
TOPIC = '只核对该固定论文版本的公开元数据'
METADATA_FIELDS = ('canonical_id', 'version', 'title', 'authors',
                   'first_published_at', 'revised_at', 'source_url')
INSTRUCTION = (
    '只读读取 https://arxiv.org/abs/2307.13854v1 这一篇固定版本论文的公开元数据。'
    '这是固定论文详情页核对，不进行关键词发现、引用关系查找或全文主张分析。'
    'research 参数：queries=["arxiv:2307.13854v1"]，'
    'topic_criteria=["只核对该固定论文版本的公开元数据"]，'
    'cutoff_at="2026-10-03T00:00:00Z"，max_items=1。'
    '返回 canonical_id、version、title、authors、first_published_at、revised_at、source_url；'
    '没有该版本修订时 revised_at 为 null。claims 和 relations 均为空数组，'
    '它们表示本次未提取，不表示论文不存在主张或关联。只使用已授权的这一来源。'
)
CLARIFICATIONS = {'scenario': 'research', 'start_urls': SOURCE_URL,
    'parameters.queries': QUERY, 'parameters.topic_criteria': TOPIC,
    'parameters.cutoff_at': CUTOFF, 'parameters.max_items': '1'}
TERMINAL = {'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'}
WAITING = {'PAUSED', 'WAITING_CI', 'WAITING_SITE', 'WAITING_HANDOFF'}
PRIVATE_PARTS = {'.private', '.owned', '.security'}


class ProbeFailure(AssertionError):
    """A fixed public reason, excluding provider/page/credential bytes."""


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def preserve_failure(report, error, phase):
    value = safe_error(error) | {'phase': phase}
    if isinstance(error, ProbeFailure):
        value['reason'] = str(error)
    if 'error' not in report:
        report['error'] = value
    else:
        report.setdefault('secondary_errors', []).append(value)


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ro_database(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def no_symlink(path):
    return not any(parent.is_symlink() for parent in (path, *path.parents))


def validate_paths(data: Path, output: Path):
    require(data.is_absolute() and output.is_absolute(), 'absolute_owned_paths_required')
    require(no_symlink(data) and no_symlink(output), 'symlink_domain_rejected')
    data, output = data.resolve(), output.resolve()
    default = ROOT / 'data'
    require(data != default and not data.is_relative_to(default), 'default_data_domain_rejected')
    require(data != ROOT and data.is_dir(), 'prepared_isolated_data_required')
    require(not output.exists(), 'new_output_directory_required')
    require(not data.is_relative_to(output) and not output.is_relative_to(data), 'overlapping_domains_rejected')
    require((data / 'business.sqlite3').is_file(), 'prepared_business_database_required')
    require(no_symlink(data / 'business.sqlite3'), 'symlink_database_rejected')
    existing = default / 'business.sqlite3'
    if existing.exists():
        require(not os.path.samefile(data / 'business.sqlite3', existing), 'default_database_alias_rejected')
    return data, output


def preflight(data):
    with closing(ro_database(data / 'business.sqlite3')) as db:
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'prepared_database_integrity_failed')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'prepared_database_foreign_keys_failed')
        for table in ('tasks', 'contracts', 'runs', 'task_compilations', 'model_generations', 'model_attempts'):
            require(db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0, 'fresh_task_domain_required')
        require(db.execute("SELECT count(*) FROM scheduler_workers WHERE state='ACTIVE'").fetchone()[0] == 0,
                'preexisting_active_worker_rejected')
        row = db.execute('SELECT * FROM model_settings_versions ORDER BY version DESC LIMIT 1').fetchone()
        require(row is not None, 'production_model_configuration_required')
        model = json.loads(row['model_json'])
        require(model.get('provider') == 'deepseek' and model.get('model_id') == 'deepseek-flash'
                and model.get('base_url') in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'),
                'real_deepseek_configuration_required')
        require(hashlib.sha256(row['model_json'].encode()).hexdigest() == row['model_config_sha256']
                and hashlib.sha256(row['runtime_json'].encode()).hexdigest() == row['runtime_config_sha256'],
                'prepared_configuration_integrity_failed')
        require(row['credential_ref'] is not None and row['disclosure_version'] == 'model-data-v1',
                'confirmed_production_credential_required')
        return {'settings_version': row['version'], 'model': model,
                'model_config_sha256': row['model_config_sha256'],
                'runtime_config_sha256': row['runtime_config_sha256'],
                'credential_ref': row['credential_ref']}


def assert_contract(contract):
    require(contract.get('scenario') == 'research', 'research_contract_required')
    require(contract.get('action_policy') == {'mode': 'read_only'} and contract.get('identity_ref') is None,
            'public_readonly_contract_required')
    require(contract.get('memory_mode') == 'disabled' and contract.get('snapshot_id') is None,
            'evaluation_memory_rejected')
    require(contract.get('start_urls') == [SOURCE_URL], 'fixed_paper_start_url_required')
    sources = contract.get('sources', [])
    require(len(sources) == 1 and sources[0].get('source_id') == SOURCE_ID
            and sources[0].get('origin') == 'https://arxiv.org'
            and sources[0].get('path_prefix') == SOURCE_PATH, 'single_public_source_scope_required')
    parameters = contract.get('parameters', {})
    require(parameters.get('queries') == [QUERY] and parameters.get('max_items') == 1,
            'fixed_paper_query_and_limit_required')
    require(parameters.get('topic_criteria') == [TOPIC], 'fixed_metadata_topic_required')
    require(datetime.fromisoformat(parameters.get('cutoff_at', '').replace('Z', '+00:00'))
            == datetime.fromisoformat(CUTOFF.replace('Z', '+00:00')), 'fixed_cutoff_required')
    require(contract.get('original_instruction') == INSTRUCTION, 'ui_instruction_integrity_failed')
    require(all(item.get('origin') not in ('evaluation_manifest', 'explicit_test_configuration')
                for item in contract.get('provenance', [])), 'synthetic_or_evaluator_contract_rejected')


def assert_result(result, run_id):
    run, final = result.get('selected_run') or {}, result.get('result') or {}
    require(run.get('run_id') == run_id and run.get('state') == 'SUCCEEDED', 'same_run_success_required')
    require(result.get('result_status') == 'AVAILABLE' and result.get('display_complete_success') is True
            and not result.get('display_blockers'), 'currently_readable_success_required')
    require(final.get('outcome') == 'SUCCEEDED' and final.get('coverage', {}).get('complete') is True
            and not final.get('unresolved'), 'aggregated_complete_success_required')
    require(not result.get('write_intents') and result.get('pending_write_count') == 0
            and not final.get('side_effects'), 'readonly_side_effects_required')
    items = final.get('items', {})
    publications = items.get('publications', [])
    require(items.get('scenario') == 'research' and len(publications) == 1, 'one_publication_required')
    paper = publications[0]
    require(all(field in paper for field in METADATA_FIELDS), 'all_metadata_fields_required')
    require(paper['source_url'] == SOURCE_URL and paper['version'] == 'v1', 'requested_version_required')
    require(paper.get('claims') == [] and paper.get('relations') == [], 'metadata_only_result_required')
    checks = result.get('field_checks', [])
    require(checks and all(item.get('verdict') == 'PASS' and item.get('evidence_ids') for item in checks),
            'all_field_bindings_must_pass')
    paths = {item['result_path'] for item in checks}
    require(all('/publications/0/' + field in paths for field in METADATA_FIELDS if field != 'authors')
            and any(path.startswith('/publications/0/authors/') for path in paths),
            'metadata_field_bindings_required')
    require(final.get('checks') and all(item.get('verdict') == 'PASS' for item in final['checks']),
            'all_acceptance_criteria_must_pass')
    return paper


def accounting(compilations, attempts):
    """Unknown usage and unpriced calls stay unknown, never become zero cost."""
    usage = []
    estimates = []
    unknown_usage = 0
    for item in compilations:
        value = json.loads(item['usage_json']) if item.get('usage_json') else None
        usage.append({'scope': 'task_compilation', 'call_id': item['call_id'], 'status': item['status'],
                      'usage': value, 'duration_ms': item.get('duration_ms'),
                      'estimated_cost': None, 'cost_status': 'unknown_preparation_not_priced'})
        unknown_usage += value is None
    for item in attempts:
        record = json.loads(item['record_json']) if item.get('record_json') else {}
        value, estimate = record.get('usage'), record.get('estimated_cost')
        usage.append({'scope': 'run_model_attempt', 'call_id': item['call_id'],
            'request_id': item['request_id'], 'status': item['status'],
            'attempt_number': item['attempt_number'], 'diagnostic_subtype': item.get('diagnostic_subtype'),
            'usage': value, 'duration_ms': record.get('duration_ms'),
            'estimated_cost': estimate, 'cost_currency': record.get('cost_currency'),
            'price_version': record.get('price_version'),
            'cost_status': 'estimated' if estimate is not None else 'unknown_unpriced_or_unfinished'})
        unknown_usage += value is None
        if estimate is not None:
            estimates.append((record.get('cost_currency'), Decimal(estimate)))
    unknown_cost = len(usage) - len(estimates)
    sums = {currency: str(sum((value for unit, value in estimates if unit == currency), Decimal(0)))
            for currency in sorted({unit for unit, _ in estimates if unit is not None})}
    return {'provider_requests_reserved': len(usage), 'compilation_calls': len(compilations),
            'run_model_attempts': len(attempts), 'unknown_usage_records': unknown_usage,
            'unknown_cost_records': unknown_cost, 'total_cost_known': unknown_cost == 0,
            'estimated_subtotals': sums, 'records': usage,
            'note': 'Provider billing was not queried. Missing usage and unpriced calls are unknown, not free.'}


def durable_summary(data, task_id=None, run_id=None):
    with closing(ro_database(data / 'business.sqlite3')) as db:
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'postrun_database_integrity_failed')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'postrun_database_foreign_keys_failed')
        compilations = [dict(row) for row in db.execute('SELECT * FROM task_compilations ORDER BY created_at')]
        attempts = [dict(row) for row in db.execute('SELECT * FROM model_attempts ORDER BY started_at,request_id')]
        result = {'accounting': accounting(compilations, attempts), 'integrity': 'ok',
                  'task_count': db.execute('SELECT count(*) FROM tasks').fetchone()[0],
                  'run_count': db.execute('SELECT count(*) FROM runs').fetchone()[0],
                  'write_intent_count': db.execute('SELECT count(*) FROM write_intents').fetchone()[0]}
        if task_id:
            result['task'] = dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
        if run_id:
            result['run'] = dict(db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone())
            result['budget'] = dict(db.execute('SELECT * FROM run_budgets WHERE run_id=?', (run_id,)).fetchone())
            result['state_events'] = [dict(row) for row in db.execute(
                "SELECT event_id,event_type,state_version,occurred_at,payload_json FROM task_events "
                "WHERE run_id=? AND event_type IN ('state_changed','result_ready') ORDER BY event_id", (run_id,))]
            rows = db.execute('SELECT * FROM evidence WHERE run_id=? ORDER BY captured_at,evidence_id', (run_id,))
            result['evidence'] = [dict(row) for row in rows]
            result['browser_actions'] = [dict(row) for row in db.execute(
                'SELECT s.step_id,s.sequence,s.status,s.error_code,s.started_at,s.ended_at,g.action_kind,g.external_write '
                'FROM gateway_attempts g JOIN steps s ON s.step_id=g.step_id AND s.run_id=g.run_id '
                'WHERE g.run_id=? ORDER BY s.sequence', (run_id,))]
            result['browser_sessions'] = [dict(row) for row in db.execute(
                'SELECT session_id,state,generation,loss_reason FROM browser_sessions WHERE run_id=? ORDER BY created_at',
                (run_id,))]
        return result


def evidence_manifest(data, rows):
    result = []
    fields = ('evidence_id', 'run_id', 'source_url', 'captured_at', 'object_id', 'query_scope',
              'locator_or_page', 'artifact_kind', 'sensitivity', 'original_evidence_id',
              'redaction_status', 'policy_version', 'artifact_path', 'sha256', 'snapshot_id', 'step_id')
    for row in rows:
        relative = Path(row['artifact_path'])
        path = data / relative
        require(not relative.is_absolute() and path.resolve().is_relative_to(data.resolve())
                and no_symlink(path) and path.is_file(), 'original_evidence_path_unavailable')
        actual = digest(path)
        require(actual == row['sha256'], 'original_evidence_digest_mismatch')
        result.append({key: row.get(key) for key in fields} | {'actual_sha256': actual,
                       'bytes': path.stat().st_size, 'retained_in_isolated_data': True})
    return result


def public_files(output):
    return [path for path in sorted(output.rglob('*')) if path.is_file()
            and not PRIVATE_PARTS.intersection(path.relative_to(output).parts)
            and path != output / 'report.json']


def scan_exports(output, canaries, *, report=None):
    files = public_files(output)
    matches = []
    for path in files:
        raw = path.read_bytes()
        names = [name for name, secret in canaries.items() if secret and secret.encode() in raw]
        if names:
            matches.append({'artifact': str(path.relative_to(output)), 'secret_names': sorted(names)})
    if report is not None:
        raw = json.dumps(report, ensure_ascii=False).encode()
        names = [name for name, secret in canaries.items() if secret and secret.encode() in raw]
        if names:
            matches.append({'artifact': 'report.json', 'secret_names': sorted(names)})
    return {'passed': not matches, 'scanned_files': len(files) + (report is not None),
            'secret_canaries': len(canaries), 'matches': matches}


def child_environment(data, api_port, ui_port):
    env = {**os.environ, 'WEBAGENT_DATA_DIR': str(data), 'WEBAGENT_API_PORT': str(api_port),
           'WEBAGENT_UI_PORT': str(ui_port), 'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1'}
    for name in ('NODE_OPTIONS', 'PYTHONSTARTUP', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    for flag in TRACE_FLAGS:
        env[flag] = 'false'
    return env


def sandbox_profile():
    """OS-enforced exclusion of preparation material and historical evidence.

    The runner does not parse any file in these trees. Runtime processes use
    production code, the explicitly entered UI fields and their own new data.
    """
    excluded = [ROOT / 'docs/m0', ROOT / '.git']
    history = ROOT / 'artifacts/verification'
    for path in sorted(history.glob('M1-*')):
        suffix = path.name.removeprefix('M1-')
        if suffix.isdigit() and int(suffix) < 25:
            excluded.append(path)
    # A path literal is SBPL syntax, not a shell command. JSON string escaping
    # covers quotes/backslashes and cannot perform command substitution.
    rules = ['(version 1)', '(allow default)']
    rules.extend('(deny file-read* (subpath ' + json.dumps(str(path)) + '))' for path in excluded)
    return '\n'.join(rules) + '\n', excluded


def sandbox_read_guard(profile, env):
    require(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(), 'macos_read_sandbox_required')
    denied = [ROOT / 'docs/m0/evaluation/task-cards.json', ROOT / 'docs/m0/evaluation/evaluator-only/README.md']
    require(all(path.is_file() for path in denied), 'read_guard_targets_required')
    _, excluded = sandbox_profile()
    denied_directories = [path for path in excluded if path != ROOT / 'docs/m0']
    require(ROOT / '.git' in denied_directories and any(path.name == 'M1-24' for path in denied_directories),
            'git_and_historical_read_guard_targets_required')
    require(all(path.is_dir() for path in denied_directories), 'read_guard_directories_required')
    control = ROOT / 'backend/webagent/config.py'
    # Deliberately never emit source contents, even on a failed sandbox guard.
    code = '''import errno,json,os,sys
rows=[]
for index,path in enumerate(sys.argv[1:-1]):
    try:
        if index>=2:
            os.listdir(path)
        else:
            with open(path, 'rb') as handle: handle.read(1)
    except PermissionError as error:
        rows.append(error.errno in (errno.EACCES, errno.EPERM))
    except OSError:
        rows.append(False)
    else:
        rows.append(False)
with open(sys.argv[-1], 'rb') as handle: control=bool(handle.read(1))
print(json.dumps({'denied':rows,'runtime_source_readable':control}))
'''
    completed = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(ROOT / '.venv/bin/python'),
        '-c', code, *(str(path) for path in (*denied, *denied_directories)), str(control)], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15, check=False)
    require(completed.returncode == 0, 'read_sandbox_probe_failed')
    try:
        value = json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise ProbeFailure('read_sandbox_probe_invalid') from None
    require(value == {'denied': [True] * (len(denied) + len(denied_directories)), 'runtime_source_readable': True},
            'read_sandbox_not_enforced')
    return {'task_cards_read_denied': True, 'evaluator_read_denied': True, 'runtime_source_readable': True,
            'git_directory_read_denied': True,
            'historical_acceptance_directories_read_denied': [str(path.relative_to(ROOT)) for path in denied_directories
                                                             if path != ROOT / '.git'],
            'profile_sha256': digest(profile), 'denied_read_was_tested': True}


class SandboxedProcess(Process):
    """Use the existing scoped process lifecycle with an enforced read profile."""
    def __init__(self, name, component, output, env, profile):
        self.name = name
        self.log_path = output / (name + '.log')
        self.log = self.log_path.open('w', encoding='utf-8', buffering=1)
        self.started_at, self.forced_kill = now(), False
        self.proc = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(profile),
            str(ROOT / 'scripts/dev.sh'), component], cwd=ROOT, env=env,
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)


async def settle_witnessed_children(witnessed):
    """Only actual descendant PID/birth witnesses can receive a signal."""
    before = live_owned(process_inventory(), witnessed)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in live_owned(process_inventory(), witnessed):
            # Recheck after the inventory and immediately before the signal.
            if pid in live_owned(process_inventory(), witnessed):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 3
        while live_owned(process_inventory(), witnessed) and time.monotonic() < deadline:
            await asyncio.sleep(.1)
    remaining = live_owned(process_inventory(), witnessed)
    require(not remaining, 'owned_descendant_process_still_live')
    return {'observed_descendants': len(witnessed), 'left_alive_after_normal_cleanup': len(before),
            'all_observed_descendants_exited': True}


async def wait_until(check, processes, timeout=30):
    async with asyncio.timeout(timeout):
        while True:
            for process in processes:
                process.assert_alive()
            if await check():
                return
            await asyncio.sleep(.15)


async def verify(data, output, report, *, headed, timeout_seconds):
    import httpx
    from playwright.async_api import async_playwright, expect
    from webagent.security import load_or_create_token
    from webagent.settings.secrets import MacOSKeychainSecretStore

    configured = preflight(data)
    token = load_or_create_token(data)
    # Exact-match export canaries remain process memory only.
    secret = await asyncio.to_thread(MacOSKeychainSecretStore().get, configured['credential_ref'])
    canaries = {'local_api_token': token, 'provider_key': secret.get_secret_value()}
    private = output / '.private'
    private.mkdir(mode=0o700)
    api_port, ui_port = free_ports()
    api_url, ui_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
    env = child_environment(data, api_port, ui_port)
    profile, excluded = sandbox_profile()
    profile_path = private / 'runtime-read-boundary.sb'
    profile_path.write_text(profile, encoding='utf-8')
    report['read_boundary'] = await asyncio.to_thread(sandbox_read_guard, profile_path, env)
    report['read_boundary']['excluded_trees'] = [str(path.relative_to(ROOT)) for path in excluded]
    report['configuration'] = {key: value for key, value in configured.items() if key != 'credential_ref'} | {
        'data_dir': str(data), 'api_url': api_url, 'ui_url': ui_url, 'headed': headed,
        'real_provider_requests': True, 'fixture_provider': False, 'seeded_task_or_run': False,
        'process_entrypoint': 'scripts/dev.sh', 'browser_network': 'production_authenticated_egress_proxy',
        'tracing': {flag: env[flag] for flag in TRACE_FLAGS}}
    active, requests, page_errors, witnessed = [], [], [], {}
    task_id = run_id = None
    browser = context = playwright = page = None
    first_error = None
    report['processes'], report['checks'], report['secondary_errors'] = [], [], []
    stop_witnessing = asyncio.Event()

    async def witness_children():
        while not stop_witnessing.is_set():
            inventory = await asyncio.to_thread(process_inventory)
            current = descendants(inventory, os.getpid())
            current.pop(os.getpid(), None)
            witnessed.update(current)
            await asyncio.sleep(.15)

    monitor = asyncio.create_task(witness_children())

    def record(name, **details):
        report['checks'].append({'name': name, 'passed': True, 'at': now(), **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    async with httpx.AsyncClient(base_url=api_url, headers={'Authorization': 'Bearer ' + token},
                                 trust_env=False, follow_redirects=False, timeout=5) as client:
        async def get(path):
            response = await client.get(path)
            require(response.status_code == 200, 'production_read_api_failed')
            return response.json()

        async def healthy():
            try:
                response = await client.get('/health')
                value = response.json()
                return response.status_code == 200 and value.get('status') == 'ok' and value.get('task_execution_enabled') is True
            except (httpx.HTTPError, ValueError):
                return False

        try:
            api = SandboxedProcess('api', 'api', private, env, profile_path); active.append(api)
            await wait_until(healthy, active)
            frontend = SandboxedProcess('frontend', 'frontend', private, env, profile_path); active.append(frontend)
            async def frontend_ready():
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=2) as check:
                        return (await check.get(ui_url)).status_code == 200
                except httpx.HTTPError:
                    return False
            await wait_until(frontend_ready, active)
            worker = SandboxedProcess('worker', 'worker', private, env, profile_path); active.append(worker)
            async def worker_ready():
                return worker.contains('"event": "worker_ready"') and worker.contains('"model_ready": true')
            await wait_until(worker_ready, active, 45)
            ready = await get('/v1/settings')
            require(ready.get('readiness', {}).get('ready') is True
                    and ready.get('version') == configured['settings_version'], 'production_model_readiness_failed')
            record('independent_production_api_frontend_worker_ready')

            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=not headed, args=[
                '--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-first-run',
                '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication',
                '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost'])
            context = await browser.new_context(viewport={'width': 1440, 'height': 1100}, service_workers='block')
            async def local_only(route):
                parts = urlsplit(route.request.url)
                permitted = parts.scheme in ('http', 'ws') and parts.hostname == '127.0.0.1' and parts.port in (api_port, ui_port)
                if permitted:
                    await route.continue_()
                else:
                    requests.append({'kind': 'blocked_nonlocal_workstation_ui_request'})
                    await route.abort()
            await context.route('**/*', local_only)
            page = await context.new_page()
            page.on('pageerror', lambda _: page_errors.append('ui_page_error'))
            def response_record(response):
                parts = urlsplit(response.url)
                if parts.path.startswith('/api/v1/'):
                    requests.append({'method': response.request.method, 'path': parts.path, 'status': response.status})
            page.on('response', response_record)
            await page.goto(ui_url, wait_until='networkidle')
            await expect(page.get_by_role('button', name='提交任务', exact=True)).to_be_enabled()
            await page.locator('#task-instruction').fill(INSTRUCTION)
            await page.locator('#task-scenario').select_option('research')
            for selector, value in {'#source-id': SOURCE_ID, '#site-id': 'arxiv', '#source-origin': 'https://arxiv.org',
                                    '#source-path': SOURCE_PATH, '#start-url': SOURCE_URL}.items():
                await page.locator(selector).fill(value)
            await page.locator('#source-authorization').check()
            await expect(page.locator('#task-permission')).to_have_value('read_only')
            await page.locator('.task-compose').screenshot(path=str(output / '01-ui-input.png'))
            write_json(output / 'ui-input.json', {'instruction': INSTRUCTION, 'scenario': 'research',
                'source_url': SOURCE_URL, 'source_id': SOURCE_ID, 'source_path': SOURCE_PATH,
                'permission': 'read_only', 'identity_ref': None})
            async with page.expect_response(lambda item: urlsplit(item.url).path == '/api/v1/tasks'
                    and item.request.method == 'POST', timeout=125000) as submitted:
                await page.get_by_role('button', name='提交任务', exact=True).click()
            created = await submitted.value
            require(created.status in (200, 201), 'ui_natural_compilation_failed')
            detail = await created.json()
            task_id = detail['task']['task_id']; report['task_id'] = task_id
            record('task_created_by_ui_natural_compiler', task_id=task_id,
                   preparation_status=detail['task']['preparation_status'])

            if detail.get('missing_fields'):
                fields = detail['missing_fields']
                require(all(field in CLARIFICATIONS for field in fields), 'unsupported_required_clarification')
                await expect(page.get_by_test_id('task-clarifications')).to_be_visible()
                for field in fields:
                    control = page.locator('#clarify-' + field.replace('.', '-'))
                    if field == 'scenario':
                        await control.select_option(CLARIFICATIONS[field])
                    else:
                        await control.fill(CLARIFICATIONS[field])
                await page.locator('#clarify-confirmation').check()
                async with page.expect_response(lambda item: urlsplit(item.url).path == '/api/v1/tasks/' + task_id + '/clarifications'
                        and item.request.method == 'POST') as clarified:
                    await page.get_by_role('button', name='确认补充信息', exact=True).click()
                response = await clarified.value
                require(response.status in (200, 201), 'ui_clarification_failed')
                detail = await response.json()
                record('requested_missing_fields_completed_in_ui', fields=fields)
            require(detail['task']['preparation_status'] == 'READY' and not detail.get('missing_fields'),
                    'ui_task_contract_not_ready')
            assert_contract(detail['contract'])
            write_json(output / 'contract.json', detail['contract'])
            await expect(page.get_by_test_id('task-status')).to_contain_text('契约已就绪')
            await page.get_by_test_id('task-detail').screenshot(path=str(output / '02-ui-contract.png'))
            await expect(page.get_by_test_id('workbench-connection')).to_have_text('已同步', timeout=15000)
            await page.get_by_test_id('workbench-confirm').check()
            await expect(page.get_by_test_id('workbench-start')).to_be_enabled()
            async with page.expect_response(lambda item: urlsplit(item.url).path == '/api/v1/tasks/' + task_id + '/start'
                    and item.request.method == 'POST') as started:
                await page.get_by_test_id('workbench-start').click()
            response = await started.value
            require(response.status in (200, 201, 202), 'ui_start_failed')
            workspace = await get('/v1/tasks/' + task_id + '/workspace')
            run_id = workspace['run']['run_id']; report['run_id'] = run_id
            record('same_run_started_only_through_production_ui', run_id=run_id)
            loop_started = time.monotonic()
            deadline = loop_started + timeout_seconds
            last_state = None
            while True:
                for process in active:
                    process.assert_alive()
                workspace = await get('/v1/tasks/' + task_id + '/workspace')
                run = workspace['run']
                require(run['run_id'] == run_id, 'run_identity_changed_during_execution')
                if run['state'] != last_state:
                    print(json.dumps({'run_state': run['state'], 'state_version': run['state_version']}), flush=True)
                    last_state = run['state']
                if run['state'] in TERMINAL:
                    break
                require(run['state'] not in WAITING, 'real_loop_requires_unresolved_prerequisite')
                require(time.monotonic() < deadline, 'real_loop_timeout')
                await asyncio.sleep(.5)
            report['execution_wall_seconds'] = round(time.monotonic() - loop_started, 3)
            write_json(output / 'workspace-terminal.json', workspace)
            result = await get('/v1/tasks/' + task_id + '/results?run_id=' + run_id)
            write_json(output / 'result.json', result)
            paper = assert_result(result, run_id)
            record('runtime_aggregator_and_read_api_report_current_success', run_id=run_id)
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'SUCCEEDED', timeout=15000)
            await page.get_by_role('button', name='刷新结果', exact=True).click()
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'true', timeout=15000)
            await expect(page.get_by_test_id('results-selected-run')).to_contain_text(run_id)
            await page.locator('#execution-workbench').screenshot(path=str(output / '03-ui-workbench-success.png'))
            await page.get_by_test_id('results-page').screenshot(path=str(output / '04-ui-results-success.png'))
            for evidence in result['evidence']:
                if evidence.get('displayable') and evidence.get('artifact_kind') == 'text':
                    await page.get_by_role('button', name='查看证据 ' + evidence['evidence_id'], exact=True).first.click()
                    await expect(page.get_by_test_id('result-evidence-content').locator('pre')).to_be_visible()
                    await page.get_by_test_id('result-evidence-content').screenshot(path=str(output / '05-ui-evidence-reader.png'))
                    record('controlled_evidence_text_opened_in_result_ui', evidence_id=evidence['evidence_id'])
                    break
            else:
                raise ProbeFailure('readable_text_evidence_required')
            require(not page_errors and not any(item.get('kind') for item in requests), 'workstation_ui_errors_or_egress')
            record('same_run_workbench_results_and_controlled_evidence_ui_agree')
            report['manual_review'] = {'status': 'PENDING', 'passed': None,
                'required_fields': list(METADATA_FIELDS), 'source_url': SOURCE_URL,
                'date_mapping': {'first_submitted_at': 'first_published_at',
                    'requested_version_submitted_at': 'first_published_at for requested v1',
                    'revised_at': 'null because requested v1 is the original submission, not a revision'},
                'result_item': paper, 'note': 'Automation does not mark independent human source comparison as passed.'}
        except BaseException as error:
            first_error = error
            # An explicitly owned failed run is cancelled through the real API
            # before shutdown. This is cleanup, never a hidden resume/retry.
            if task_id and run_id:
                try:
                    workspace = await get('/v1/tasks/' + task_id + '/workspace')
                    run = workspace.get('run') or {}
                    if run.get('run_id') == run_id and run.get('state') not in TERMINAL:
                        cancelled = await client.post('/v1/runs/' + run_id + '/cancel', json={
                            'expected_state_version': run['state_version'], 'contract_version': run['contract_version'],
                            'settings_version': run['settings_version']}, headers={'Idempotency-Key': 'm125-cleanup-' + uuid4().hex})
                        report['failure_cleanup_cancel'] = {'submitted': True, 'status': cancelled.status_code}
                        require(cancelled.status_code in (200, 201, 202), 'owned_cancel_cleanup_rejected')
                        end = time.monotonic() + 15
                        while time.monotonic() < end:
                            latest = await get('/v1/tasks/' + task_id + '/workspace')
                            if latest['run']['state'] in TERMINAL:
                                report['failure_cleanup_cancel']['terminal_state'] = latest['run']['state']
                                break
                            await asyncio.sleep(.25)
                except BaseException as cleanup_error:
                    report['secondary_errors'].append(safe_error(cleanup_error))
            preserve_failure(report, error, 'production-ui-loop')
        finally:
            for owner in (context, browser, playwright):
                if owner is not None:
                    try:
                        await (owner.stop() if owner is playwright else owner.close())
                    except BaseException as cleanup_error:
                        report['secondary_errors'].append(safe_error(cleanup_error))
            for process in reversed(active):
                try:
                    if process.alive():
                        stopped = await asyncio.to_thread(process.stop)
                    else:
                        process.proc.wait(); process.log.close()
                        stopped = {'name': process.name, 'pid': process.proc.pid, 'exit_code': process.proc.returncode,
                                   'forced_kill': False, 'exited_early': True}
                    report['processes'].append(stopped)
                    require(not process.alive(), 'owned_process_still_live')
                    require(not stopped['forced_kill'] and stopped['exit_code'] in (0, -signal.SIGTERM, 128 + signal.SIGTERM),
                            'owned_service_shutdown_failed')
                except BaseException as cleanup_error:
                    report['secondary_errors'].append(safe_error(cleanup_error))
            stop_witnessing.set()
            try:
                await monitor
                report['descendant_lifecycle'] = await settle_witnessed_children(witnessed)
                if report['descendant_lifecycle']['left_alive_after_normal_cleanup']:
                    preserve_failure(report, ProbeFailure('owned_descendant_cleanup_leak'), 'descendant-cleanup')
            except BaseException as cleanup_error:
                preserve_failure(report, cleanup_error, 'descendant-cleanup')
                report['descendant_lifecycle'] = {'all_observed_descendants_exited': False,
                                                 'observed_descendants': len(witnessed)}

    closed = all(not process.alive() for process in active) and report.get('descendant_lifecycle', {}).get(
        'all_observed_descendants_exited') is True
    report['all_owned_processes_exited'] = closed
    if closed:
        try:
            summary = durable_summary(data, task_id, run_id)
            manifest = evidence_manifest(data, summary.pop('evidence', []))
            write_json(output / 'durable-summary.json', summary)
            write_json(output / 'evidence-manifest.json', manifest)
            if first_error is None:
                require(summary['task_count'] == summary['run_count'] == 1 and summary['write_intent_count'] == 0,
                        'one_natural_task_and_no_write_intents_required')
                require(summary['accounting']['compilation_calls'] == 1 and summary['accounting']['run_model_attempts'] > 0,
                        'real_compilation_and_runtime_model_calls_required')
                require(summary['budget']['model_calls_used'] == summary['accounting']['run_model_attempts'],
                        'model_budget_ledger_mismatch')
                require(summary['browser_actions'] and any(item['action_kind'] == 'navigate'
                        and item['status'] == 'COMPLETED' for item in summary['browser_actions'])
                        and all(item['external_write'] == 0 for item in summary['browser_actions']),
                        'actual_readonly_browser_navigation_required')
                require(summary['browser_sessions'] and all(item['state'] in ('CLOSED', 'LOST')
                        for item in summary['browser_sessions']), 'owned_browser_sessions_still_open')
                require(manifest and any(item['source_url'] == SOURCE_URL and item['artifact_kind'] == 'text'
                        and item['original_evidence_id'] is None for item in manifest), 'public_page_original_required')
                record('postexit_original_artifacts_integrity_and_real_model_budget_verified',
                       evidence_artifacts=len(manifest), model_attempts=summary['accounting']['run_model_attempts'])
            private_hashes = {str(path.relative_to(data)): digest(path) for path in sorted(data.rglob('*'))
                              if path.is_file() and not PRIVATE_PARTS.intersection(path.relative_to(data).parts)
                              and no_symlink(path)}
            write_json(private / 'postexit-data-manifest.json', {'data_dir': str(data), 'artifact_sha256': private_hashes})
            report['retained_private_data_manifest_sha256'] = digest(private / 'postexit-data-manifest.json')
            report['accounting'] = summary['accounting']
            report['budget'] = summary.get('budget')
        except BaseException as final_error:
            report['secondary_errors'].append(safe_error(final_error))
            if first_error is None:
                preserve_failure(report, final_error, 'postexit-ledger-and-evidence')
                first_error = final_error
    write_json(output / 'ui-requests.json', {'requests': requests, 'page_errors': page_errors,
        'bodies_headers_queries_and_model_raw_excluded': True})
    report['secret_scan'] = scan_exports(output, canaries, report=report)
    if not report['secret_scan']['passed']:
        # Retain offending bytes privately; never publish a failed secret scan's
        # original artifact or a report containing a live credential.
        for match in report['secret_scan']['matches']:
            if match['artifact'] != 'report.json':
                path = output / match['artifact']
                target = private / 'rejected-exports' / match['artifact']
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
        raw = json.dumps(report, ensure_ascii=False)
        for secret in canaries.values():
            if secret:
                raw = raw.replace(secret, '[REDACTED]')
        filtered = json.loads(raw)
        report.clear(); report.update(filtered)
        preserve_failure(report, ProbeFailure('public_export_secret_scan_failed'), 'secret-scan')
        first_error = first_error or ProbeFailure('public_export_secret_scan_failed')
    report['passed'] = first_error is None and closed and not report['secondary_errors'] and 'error' not in report
    report['acceptance_status'] = 'AUTOMATED_PASS_MANUAL_REVIEW_PENDING' if report['passed'] else 'FAILED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True, help='Fresh production-configured isolated data directory')
    parser.add_argument('--output', type=Path, required=True, help='A new public acceptance artifact directory')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--timeout-seconds', type=int, default=450)
    args = parser.parse_args()
    require(30 <= args.timeout_seconds <= 600, 'bounded_timeout_required')
    data, output = validate_paths(args.data_dir, args.output)
    output.mkdir(parents=True)
    report = {'task': 'M1-25', 'probe': 'production-public-fixed-paper-readonly-loop', 'started_at': now(),
        'passed': False, 'm125_completion': False, 'manual_review': {'status': 'PENDING', 'passed': None},
        'scope': {'development_task': 'C1-01 narrow fixed-paper metadata lookup', 'formal_benchmark': False,
                  'full_C1_01_scenario': False, 'grader_answers_read': False, 'paid_provider': 'real configured DeepSeek'},
        'source_sha256': digest(Path(__file__))}
    try:
        asyncio.run(verify(data, output, report, headed=args.headed, timeout_seconds=args.timeout_seconds))
    except BaseException as error:
        report['passed'] = False
        preserve_failure(report, error, 'runner-finalization')
    report['finished_at'] = now()
    report['source_unchanged_during_execution'] = report['source_sha256'] == digest(Path(__file__))
    if not report['source_unchanged_during_execution']:
        report['passed'] = False
        report['acceptance_status'] = 'SOURCE_CHANGED_FAILED'
    report['artifact_sha256'] = {str(path.relative_to(output)): digest(path) for path in public_files(output)}
    write_json(output / 'report.json', report)
    print(json.dumps({'passed': report['passed'], 'output': str(output),
                      'manual_review': report['manual_review']['status']}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
