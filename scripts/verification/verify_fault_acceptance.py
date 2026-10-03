#!/usr/bin/env python3
"""M1-24 fail-fast integrated fault acceptance on fresh owned domains.

No historical report is accepted as an input. Fixed current-source probes are
executed again, and three additional *same Run* domains connect actual API /
Worker death, browser actions, durable ledgers and the workbench/results UI.
Private databases, logs and source artifacts stay in mode-0700 .private/.owned trees;
only bounded summaries and synthetic screenshots are public evidence. No paid
model, user account, default data domain, public site or physical repository is
used. This is engineering acceptance, not an M1-25/public benchmark result.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'backend'), str(SCRIPTS)]

TRACE_FLAGS = ('LANGSMITH_TRACING', 'LANGSMITH_TRACING_V2', 'LANGCHAIN_TRACING',
               'LANGCHAIN_TRACING_V2', 'LANGCHAIN_HANDLER')
for flag in TRACE_FLAGS:
    os.environ[flag] = 'false'
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

# Fixed selectors are a repeatable subset, never user-controlled code or a
# recursive call to scripts/check.sh. Writes retain all seven FR-03 outcomes.
RECOVERY_CASES = ('business_before', 'business_after', 'dispatch_return_before',
                  'saver_before', 'saver_after', 'saver-error', 'disk-error',
                  'graph-version', 'state-version', 'event-missing',
                  'evidence-missing', 'evidence-corrupt', 'budget-exhausted',
                  'unknown-write', 'queued-continuation')
GROUPS = (
    ('recovery', 'verify_recovery.py', tuple(arg for case in RECOVERY_CASES for arg in ('--case', case))),
    ('writes', 'verify_writes.py', ()),
    ('controls', 'verify_controls.py', ()),
    ('budgets', 'verify_budgets.py', ()),
    ('scheduler', 'verify_scheduler.py', ()),
    ('gateway', 'verify_gateway.py', ()),
    ('api-security', 'verify_api_security.py', ()),
    ('network-boundary', 'verify_network_boundary.py', ()),
    ('network-headless', 'verify_network_boundary.py', ('--headless',)),
    ('evidence', 'verify_evidence.py', ()),
    ('workbench', 'verify_workbench.py', ()),
    ('results', 'verify_results.py', ()),
)
API_POINTS = ('api-before-commit', 'api-after-commit')
CROSS_CASES = ('worker-pause-resume',) + API_POINTS
WRITE_CASES = ('intent-not-applied', 'physical-applied', 'physical-unknown',
               'object-changed', 'version-changed', 'during-check', 'cancelled-write')
SCOPE = {
    'FR-01': {'status': 'M1_covered', 'current_run_evidence': ['recovery', 'same-run-cross-layer'],
              'claims': ['owned HTTP model replacement', 'actual asynchronous StateGraph/AsyncSqliteSaver',
                         'new OS process recovery with frozen Run bindings'],
              'prerequisites': ['dependency/license and lock reproducibility remain separate check.sh groups',
                                'network probe records actual allowed/blocked requests; owned model only']},
    'FR-02': {'status': 'M1_covered', 'current_run_evidence': ['recovery', 'same-run-cross-layer'],
              'claims': ['business/node/saver before and after crash', 'version/event mismatch blocks recovery',
                         'original budget, failure and result events preserved',
                         'API control-intent COMMIT before/after death; same Run UI agrees with DB']},
    'FR-03': {'status': 'M1_covered', 'current_run_evidence': ['writes', 'recovery', 'results'],
              'claims': ['all seven current write fault cases rerun with real Chromium and owned external log',
                         'APPLIED reuse, NOT_APPLIED conditional retry, UNKNOWN no replay/success',
                         'node reentry/new Run and changed object/version'],
              'boundary': 'write probe uses persistent LangGraph write-node fixture, not public GitHub execution'},
    'FR-04': {'status': 'M1_partial', 'current_run_evidence': ['controls', 'same-run-cross-layer'],
              'implemented': ['pause/cancel in-flight drain', 'old epoch rejection', 'duplicate continue',
                              'persistent wait crash recovery and same thread serialization'],
              'pending': ['real human handoff/continue lifecycle M3-03/04',
                          '24-hour human handoff timeout and full mixed-control fault set M3-15'],
              'fixture_boundary': 'CI/site/handoff waiting rows in controls are explicitly seeded, not full flows'},
    'FR-05': {'status': 'M1_partial', 'current_run_evidence': ['budgets', 'scheduler', 'controls', 'recovery'],
              'implemented': ['persistent actions/pages/model/active/recovery budgets', 'daily debit and restart',
                              'independent deadline cancellation and action admission',
                              'M1 concurrency/resource admission and paused logical holds'],
              'pending': ['CI and cooldown full flows M2-08/M3-02', 'combined two-run/four-context waits M3-15',
                          'coordinated business/graph/artifact backup, migration and restore M3-16']},
    'TRD-12.3': {'M1_covered': ['state/event atomicity', 'stale executor', 'budget restart',
                              'unknown write', 'network scope', 'evidence failure', 'M1 gateway/schema injection subset'],
                 'pending_later_phases': ['CI SHA binding', 'flow invalidation/promotion',
                                         'monitor event/watermark crash', 'full product prompt-injection suite',
                                         'formal benchmark reset'],
                 'evidence_boundary': 'M1 originals/filtered copies, file corruption/missing and injected disk failure; '
                                      'unfinished HAR flush is not claimed as full operational acceptance'},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def public_files(output):
    return [path for path in sorted(output.rglob('*')) if path.is_file()
            and not any(part in ('.private', '.owned', '.security') for part in path.relative_to(output).parts)
            and path != output / 'report.json']


def validate_subreport(directory: Path, report: dict, *, summary_only=False):
    """Recheck newly produced artifacts after the child has actually exited."""
    assert report.get('passed') is True, 'current-source subprobe failed'
    checks = report.get('checks')
    assert isinstance(checks, (dict, list)) and checks, 'subprobe has no executed checks'
    if isinstance(checks, dict):
        assert all(value is True for value in checks.values()), 'failed child check'
    else:
        assert all(isinstance(value, dict) and value.get('passed') is True for value in checks), 'failed child check'
    hashes = report.get('artifact_sha256')
    assert isinstance(hashes, dict) and (hashes or summary_only), 'subprobe has no durable artifact hashes'
    for name, expected in hashes.items():
        assert isinstance(name, str) and isinstance(expected, str) and len(expected) == 64
        relative = Path(name)
        original = directory / relative
        assert not relative.is_absolute(), 'artifact escaped private domain'
        for parent in [original, *original.parents]:
            if parent == directory.parent:
                break
            assert not parent.is_symlink(), 'artifact uses a symlink'
        artifact = original.resolve()
        assert artifact.is_relative_to(directory.resolve()), 'artifact escaped private domain'
        assert artifact.is_file(), 'artifact missing'
        assert digest(artifact) == expected, 'post-exit artifact changed'
    return {'checks': len(checks), 'checked_files': len(hashes),
            'check_names': list(checks) if isinstance(checks, dict) else [item['name'] for item in checks]}


def validate_group_scope(name, directory, report):
    """Essential named behavior/case guards; counts alone grant no FR scope."""
    checks = report['checks']
    names = set(checks) if isinstance(checks, dict) else {item['name'] for item in checks}
    if name in ('recovery', 'writes'):
        matrix = json.loads((directory / 'matrix.json').read_text())
        cases = RECOVERY_CASES if name == 'recovery' else WRITE_CASES
        assert len(matrix) == len(cases) and {item['case'] for item in matrix} == set(cases)
        if name == 'recovery':
            required = {case + suffix for case in cases for suffix in (
                '_preserves_original_business_ledgers_and_budget', '_never_replays_last_click')}
        else:
            required = {case + suffix for case in cases for suffix in (
                '_has_one_committed_intent', '_actual_crash_window', '_different_process_preserves_ledgers',
                '_each_actual_dispatch_has_an_independent_budget_debit')}
            for case in cases:
                confirmed = case in ('intent-not-applied', 'physical-applied', 'cancelled-write')
                required.add(case + ('_confirmed_without_duplicate_post' if confirmed else '_unknown_never_replays_or_succeeds'))
            for case in ('intent-not-applied', 'physical-applied'):
                required |= {case + '_node_reentry_reuses_semantic_operation_without_write',
                             case + '_new_run_reuses_same_business_key_without_replay'}
            for item in matrix:
                assert item['operation_status'] == ('CONFIRMED' if item['case'] in (
                    'intent-not-applied', 'physical-applied', 'cancelled-write') else 'UNKNOWN')
        assert required <= names, 'required fault outcome was not executed'
    elif name == 'scheduler':
        assert {'two_active_third_queued', 'pause_releases_active_slot', 'stopped_generation_fenced',
                'crashed_worker_epoch_fenced', 'run_state_events_remain_paired',
                'four_real_contexts_share_materialized_reservation', 'fifth_real_context_rejected'} <= names
        assert report['scope_counts']['active_limit'] == 2 and report['scope_counts']['context_limit'] == 4
    elif name in ('network-boundary', 'network-headless'):
        assert {'unregistered_port_and_redirect_blocked_before_target_connection',
                'fetch_xhr_beacon_iframe_popup_script_image_worker_and_websocket_cannot_bypass',
                'all_unauthorized_targets_remain_uncontacted'} <= names
        assert report['browser']['headless'] is (name == 'network-headless')
        assert report['canary'] == {'unauthorized_tcp_connections': 0, 'unauthorized_udp_packets': 0}
    elif name in ('workbench', 'results'):
        scan = report.get('secret_scan', {})
        assert scan.get('passed') is True and scan.get('scanned_files', 0) > 0, 'UI export secret scan did not execute'
        assert report.get('artifact_sha256'), 'UI probe must preserve and hash its actual public artifacts'


def assert_state_events(value):
    """Every committed Run version has exactly one matching durable event."""
    run = value['run']
    changed = [item for item in value['task_events'] if item['event_type'] == 'state_changed']
    assert [item['state_version'] for item in changed] == list(range(1, run['state_version'] + 1))
    if changed:
        payload = json.loads(changed[-1]['payload_json'])
        assert payload['current_state'] == run['state'], 'state disagrees with latest committed event'
    result_events = [item for item in value['task_events'] if item['event_type'] == 'result_ready']
    assert len(result_events) == len(value['run_results']) <= 1
    assert value['integrity'] == 'ok' and not value['foreign_keys']


def evidence_hashes(directory: Path, run_id: str):
    from webagent.db import connect
    with connect(directory / 'business.sqlite3') as db:
        rows = db.execute('SELECT evidence_id,artifact_path,sha256 FROM evidence WHERE run_id=? ORDER BY evidence_id',
                          (run_id,)).fetchall()
    return {row['evidence_id']: {'recorded_sha256': row['sha256'],
                               'actual_sha256': digest(directory / row['artifact_path'])} for row in rows}


def assert_evidence_preserved(before, after):
    assert before, 'real browser produced no evidence'
    for evidence_id, item in before.items():
        assert item['recorded_sha256'] == item['actual_sha256']
        assert after.get(evidence_id) == item, 'original evidence changed during process recovery'


def assert_workspace_budget(workspace, durable, limits):
    budget = workspace['budget']
    assert budget['initialized'] is True
    for field in ('actions_used', 'content_pages_used', 'observations_used', 'screenshots_used',
                  'model_calls_used', 'active_ms', 'ci_wait_ms'):
        assert budget[field] == durable[field], 'workspace budget differs from stopped Run ledger'
    assert budget['limits'] == limits
    assert budget['remaining_actions'] == max(0, limits['max_actions'] - durable['actions_used'])
    assert budget['remaining_content_pages'] == max(0, limits['max_content_pages'] - durable['content_pages_used'])
    assert budget['remaining_active_ms'] == max(0, limits['max_active_seconds'] * 1000 - durable['active_ms'])
    assert budget['remaining_ci_wait_ms'] == max(0, limits['max_ci_wait_seconds'] * 1000 - durable['ci_wait_ms'])


def original_evidence(db, evidence_id, run_id):
    """Resolve only this Run's bounded derivative chain to its actual original."""
    chain = []
    while True:
        assert evidence_id not in chain and len(chain) < 16, 'cyclic or overlong evidence chain'
        row = db.execute('SELECT evidence_id,original_evidence_id,artifact_path,sha256 FROM evidence '
                         'WHERE evidence_id=? AND run_id=?', (evidence_id, run_id)).fetchone()
        assert row is not None, 'evidence chain missing or belongs to another Run'
        chain.append(evidence_id)
        if row['original_evidence_id'] is None:
            return dict(row), chain
        evidence_id = row['original_evidence_id']


async def reject_old_token(directory, fixture, run_id, case, old_token):
    """Production admission over the actual old token; browser is a spy seam."""
    from types import SimpleNamespace
    from verify_controls import facts
    from webagent.errors import BusinessError
    from webagent.gateway.service import BrowserGateway
    from webagent.sessions.models import SessionOwner
    backend = SimpleNamespace(owner=SessionOwner('run', run_id, 'owned-http', realm='webarena'))
    gateway = BrowserGateway(directory / 'business.sqlite3', backend)
    action = {'run_id': run_id, 'step_id': 'm124-old-epoch-' + case, 'epoch': old_token.epoch,
              'snapshot_id': 'old-snapshot', 'action_type': 'scroll', 'expected_effect': 'read',
              'target': {'page_url': fixture.origin + '/start/' + case, 'tab_id': 'old-tab',
                         'frame_id': 'old-frame', 'locator': None, 'write_scope': None},
              'args': {'direction': 'down', 'pixels': 1}}
    before = facts(directory, run_id)
    try:
        await gateway.dispatch(old_token, action)
        raise AssertionError('Old Worker qualification dispatched after restart')
    except BusinessError as error:
        assert error.code == 'RESOURCE_CONFLICT'
    after = facts(directory, run_id)
    # The real live Worker's watchdog can persist elapsed time concurrently;
    # a rejection must not debit new work, but real elapsed time still counts.
    prior, current = before['run_budgets'][0], after['run_budgets'][0]
    for field in ('budget_record_id', 'actions_used', 'content_pages_used', 'observations_used',
                  'screenshots_used', 'model_calls_used', 'recovery_counts_json'):
        assert current[field] == prior[field]
    assert current['active_ms'] >= prior['active_ms'] and current['ci_wait_ms'] >= prior['ci_wait_ms']
    assert after['budget_attempts'] == before['budget_attempts'] and after['steps'] == before['steps']
    assert fixture.clicks[run_id] == 1
    return after


class ApiCommitFault:
    """Wrap one real ControlStore transaction in this trusted child only."""
    def __init__(self, directory, point, key):
        if point not in API_POINTS:
            raise ValueError('Unknown fixed API commit fault')
        self.directory, self.point, self.key, self.hit = Path(directory), point, key, False

    def stop(self, stage):
        if self.hit or stage != self.point:
            return
        self.hit = True
        write_json(self.directory / 'api-boundary.json', {'stage': stage, 'pid': os.getpid()})
        os.kill(os.getpid(), signal.SIGSTOP)

    def wrap(self, real_transaction):
        @contextmanager
        def faulted(db):
            relevant = False
            with real_transaction(db):
                yield db
                relevant = db.execute('SELECT 1 FROM run_controls WHERE idempotency_key=?', (self.key,)).fetchone() is not None
                if relevant:
                    self.stop('api-before-commit')
            if relevant:
                self.stop('api-after-commit')
        return faulted


async def api_child(directory, fd, origin, api_port, ui_port, point=None, key=None):
    import uvicorn
    from verify_controls import OwnedSecrets, owned_resources
    from webagent.api import create_app
    from webagent.config import Settings, disable_external_tracing
    import webagent.controls.store as controls_store
    from webagent.security import LocalApiPolicy, load_or_create_token
    disable_external_tracing()
    settings = Settings(directory)
    api_url, ui_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
    policy = LocalApiPolicy(load_or_create_token(directory), frozenset({f'127.0.0.1:{api_port}'}),
                            frozenset({api_url, ui_url}))
    app = create_app(settings, secret_store=OwnedSecrets(), local_api_policy=policy)
    app.state.control_store = controls_store.ControlStore(settings.business_db, secret_store=OwnedSecrets(),
        resource_factory=lambda contract, run_id: owned_resources(origin, contract, run_id))
    if point is not None:
        controls_store.transaction = ApiCommitFault(directory, point, key).wrap(controls_store.transaction)
    await uvicorn.Server(uvicorn.Config(app, fd=fd, log_config=None, access_log=False, log_level='warning')).serve()


async def start_api(domain, ui_port, api_handles, *, point=None, key=None):
    import httpx
    from webagent.security import load_or_create_token
    if domain.listener is None:
        domain.listener = socket.socket()
        domain.listener.bind(('127.0.0.1', 0))
        domain.listener.listen(128)
    port = domain.listener.getsockname()[1]
    log = (domain.directory / f'api-{len(api_handles)}.log').open('wb')
    domain.logs.append(log)
    command = [sys.executable, str(Path(__file__).resolve()), '--api-dir', str(domain.directory),
               '--api-fd', str(domain.listener.fileno()), '--origin', domain.fixture.origin,
               '--api-port', str(port), '--ui-port', str(ui_port)]
    if point:
        command += ['--api-point', point, '--fault-key', key]
    domain.api = await asyncio.create_subprocess_exec(*command, cwd=ROOT, pass_fds=(domain.listener.fileno(),),
        stdout=log, stderr=asyncio.subprocess.STDOUT, env={**os.environ, 'PYTHONUTF8': '1'})
    api_handles.append(domain.api)
    if domain.client is None:
        domain.client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=8,
            headers={'Authorization': 'Bearer ' + load_or_create_token(domain.directory)})
    async with asyncio.timeout(20):
        while True:
            assert domain.api.returncode is None, 'owned API exited before ready'
            try:
                if (await domain.client.get('/health')).status_code == 200:
                    return port
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.03)


async def stop_api(domain, *, killed=False):
    child = domain.api
    assert child is not None and child.returncode is None
    child.kill() if killed else child.terminate()
    await asyncio.wait_for(child.wait(), 10)
    # Uvicorn may restore and re-raise the terminating signal after its
    # shutdown handlers finish. Both codes denote the awaited normal stop.
    assert child.returncode == -signal.SIGKILL if killed else child.returncode in (0, -signal.SIGTERM)


async def cross_layer(output: Path, private: Path, report: dict, *, headed=False):
    """All observable layers are read from the exact crashed/resumed Run."""
    import httpx
    from playwright.async_api import async_playwright, expect
    from verify_controls import (ControlsFixture, Domain, assert_completed_event,
                                 assert_resume_preserved, assert_source_result, command_body, facts)
    from verify_results import ledger_digest
    from verify_startup import Process, free_ports
    from verify_task_entry import wait_until
    from webagent.db import connect
    from webagent.security import load_or_create_token
    fixture = ControlsFixture(private)
    private.mkdir(parents=True, exist_ok=False)
    domains, worker_handles, api_handles, frontends, contexts = [], [], [], [], []
    browser = playwright = None
    tokens, outcomes, errors = {}, [], []
    primary = None
    work_completed = False
    def record(name, **details):
        report['checks'].append({'name': name, 'passed': True, **details})
        print(json.dumps({'group': 'same-run-cross-layer', 'check': name, 'passed': True}), flush=True)
    try:
        await fixture.start()
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=not headed, args=[
            '--disable-background-networking', '--disable-component-update',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
        for case in CROSS_CASES:
            directory = private / case
            domain = Domain(directory, fixture, case)
            domains.append(domain)
            _, ui_port = free_ports()
            api_port = await start_api(domain, ui_port, api_handles)
            tokens[case] = load_or_create_token(directory)
            status, started = await domain.post('start', body=command_body(directory, task_id=domain.task_id), key='m124-start-' + case)
            assert status == 202 and started['operation']['status'] == 'PENDING'
            domain.run_id = started['operation']['run_id']
            fixture.bind(case, domain.run_id)
            fixture.block_click.add(domain.run_id)
            await domain.start_worker(point='control_applied', kill=True)
            worker_handles.append(domain.worker)
            async with asyncio.timeout(35):
                while not fixture.click_entered.setdefault(domain.run_id, asyncio.Event()).is_set():
                    await domain.ensure_alive()
                    await asyncio.sleep(.03)
            from verify_recovery import current_token
            old_token = current_token(directory, domain.run_id)
            status, paused = await domain.post('pause')
            assert status == 202 and paused['operation']['status'] == 'PENDING'
            fixture.click_release[domain.run_id].set()
            boundary, before = await domain.crash_snapshot()
            assert before['run']['state'] == 'PAUSED' and fixture.clicks[domain.run_id] == 1
            assert_completed_event(before, await domain.operation(paused['operation']['operation_id']))
            assert_state_events(before)
            originals = evidence_hashes(directory, domain.run_id)
            record(case + '_real_worker_dies_after_atomic_pause_commit', run_id=domain.run_id,
                   worker_pid=boundary['pid'], clicks=1, evidence_count=len(originals))
            await stop_api(domain)
            if case in API_POINTS:
                key = 'm124-cancel-' + case
                await start_api(domain, ui_port, api_handles, point=case, key=key)
                cancel = asyncio.create_task(domain.post('cancel', key=key))
                marker = directory / 'api-boundary.json'
                try:
                    async with asyncio.timeout(20):
                        while not marker.exists():
                            assert not cancel.done(), 'cancel returned without hitting fixed COMMIT boundary'
                            assert domain.api.returncode is None
                            await asyncio.sleep(.02)
                    api_boundary = json.loads(marker.read_text())
                    assert api_boundary == {'stage': case, 'pid': domain.api.pid}
                    await stop_api(domain, killed=True)
                    try:
                        await cancel
                    except httpx.HTTPError:
                        pass  # Real death before a receipt can reach the client.
                finally:
                    if not cancel.done():
                        cancel.cancel()
                        await asyncio.gather(cancel, return_exceptions=True)
                at_crash = facts(directory, domain.run_id)
                cancel_rows = [item for item in at_crash['run_controls'] if item['action'] == 'cancel']
                cancel_events = [item for item in at_crash['task_events'] if item['event_type'] == 'operation_requested'
                                 and json.loads(item['payload_json']).get('action') == 'cancel']
                expected = 0 if case == 'api-before-commit' else 1
                assert len(cancel_rows) == len(cancel_events) == expected
                assert at_crash['run']['state'] == 'PAUSED'
                assert_resume_preserved(before, at_crash)
                assert_evidence_preserved(originals, evidence_hashes(directory, domain.run_id))
                record(case + '_control_and_event_both_rollback_or_commit', surviving_cancel_intents=expected,
                       api_pid=api_boundary['pid'])
                await start_api(domain, ui_port, api_handles)
                status, continued = await domain.post('cancel', key=key)
                assert status == 202
                if expected:
                    assert continued['operation']['operation_id'] == cancel_rows[0]['operation_id']
                expected_state = 'CANCELLED'
            else:
                await start_api(domain, ui_port, api_handles)
                fixture.phase[domain.run_id] = 'B'
                status, continued = await domain.post('resume')
                assert status == 202 and continued['operation']['run_id'] == domain.run_id
                expected_state = 'SUCCEEDED'
            await domain.start_worker(point='model_before' if expected_state == 'SUCCEEDED' else None)
            worker_handles.append(domain.worker)
            if expected_state == 'SUCCEEDED':
                await domain.boundary()
                current = facts(directory, domain.run_id)
                assert current['run']['state'] in ('RUNNING', 'RECONCILING', 'VERIFYING')
                assert current['queue']['worker_generation'] != old_token.worker_generation
                assert current['queue']['epoch'] != old_token.epoch
                await reject_old_token(directory, fixture, domain.run_id, case, old_token)
                record('live_restarted_worker_rejects_original_token_before_browser_or_budget',
                       run_id=domain.run_id, old_epoch=old_token.epoch, current_epoch=current['queue']['epoch'],
                       old_generation=old_token.worker_generation, current_generation=current['queue']['worker_generation'],
                       backend_boundary='rejection-only spy; actual production gateway admission at live model boundary')
                (directory / 'release-boundary').touch()
            operation = await domain.await_operation(continued['operation']['operation_id'])
            after = await domain.await_facts(lambda value: value['run']['state'] == expected_state)
            await domain.await_metrics(lambda value: value['active'].get(domain.run_id, 0) == 0)
            assert_completed_event(after, operation)
            assert_state_events(after)
            assert_resume_preserved(before, after)
            assert_evidence_preserved(originals, evidence_hashes(directory, domain.run_id))
            assert fixture.clicks[domain.run_id] == 1 and len(set(domain.worker_pids)) == 2
            if expected_state == 'SUCCEEDED':
                assert_source_result(after, fixture, domain.run_id)
            else:
                assert not after['run_results']
                assert len([item for item in after['run_controls'] if item['action'] == 'cancel']) == 1
            await domain.stop_worker()
            after = facts(directory, domain.run_id)
            assert_resume_preserved(before, after)
            record(case + '_new_worker_preserves_budget_actions_and_original_artifacts', run_id=domain.run_id,
                   state=expected_state, old_worker_pid=domain.worker_pids[0], new_worker_pid=domain.worker_pids[1])
            env = {**os.environ, 'WEBAGENT_DATA_DIR': str(directory), 'WEBAGENT_API_PORT': str(api_port),
                   'WEBAGENT_UI_PORT': str(ui_port), 'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1'}
            for flag in ('NODE_OPTIONS', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
                env.pop(flag, None)
            frontend = Process(case + '-frontend', 'frontend', directory, env)
            frontends.append(frontend)
            ui_url = f'http://127.0.0.1:{ui_port}'
            async def ui_ready():
                try:
                    return (await domain.client.get(ui_url)).status_code == 200
                except httpx.HTTPError:
                    return False
            await wait_until(ui_ready, 'same-Run frontend', frontend=frontend)
            context = await browser.new_context(viewport={'width': 1280, 'height': 960}, service_workers='block')
            contexts.append(context)
            async def restrict(route):
                parsed = urlsplit(route.request.url)
                if parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port in (api_port, ui_port):
                    await route.continue_()
                else:
                    await route.abort()
            await context.route('**/*', restrict)
            page = await context.new_page()
            before_read = ledger_digest(directory / 'business.sqlite3')
            response = await domain.client.get('/v1/tasks/' + domain.task_id + '/workspace')
            assert response.status_code == 200
            workspace = response.json()
            assert workspace['run']['run_id'] == domain.run_id and workspace['run']['state'] == expected_state
            assert workspace['run']['state_version'] == after['run']['state_version']
            from webagent.budgets.store import LIMIT_FIELDS
            with connect(directory / 'business.sqlite3') as db:
                limits_row = db.execute('SELECT * FROM budget_limits WHERE run_id=?', (domain.run_id,)).fetchone()
            limits = {field: limits_row[field] for field in LIMIT_FIELDS}
            assert_workspace_budget(workspace, after['run_budgets'][0], limits)
            for field in ('status', 'epoch', 'revision'):
                assert workspace['queue'][field] == after['queue'][field]
            persisted_op = next(item for item in after['run_controls'] if item['operation_id'] == operation['operation_id'])
            projected_op = next(item for item in workspace['controls'] if item['operation_id'] == operation['operation_id'])
            assert persisted_op['status'] == projected_op['status'] == 'APPLIED'
            response = await domain.client.get('/v1/tasks/' + domain.task_id + '/results')
            assert response.status_code == 200
            result = response.json()
            assert result['selected_run']['run_id'] == domain.run_id and result['selected_run']['state'] == expected_state
            assert result['display_complete_success'] is (expected_state == 'SUCCEEDED')
            await page.goto(ui_url + '/?task=' + domain.task_id, wait_until='domcontentloaded')
            await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', expected_state)
            await expect(page.get_by_test_id('results-selected-run')).to_contain_text(domain.run_id)
            await expect(page.get_by_test_id('results-outcome')).to_contain_text(expected_state)
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success',
                'true' if expected_state == 'SUCCEEDED' else 'false')
            await expect(page.get_by_test_id('workbench-queue')).to_contain_text('队列已结束')
            await expect(page.get_by_test_id('workbench-operation')).to_have_attribute('data-status', 'APPLIED')
            await expect(page.get_by_test_id('workbench-operation')).to_contain_text(operation['operation_id'])
            ui_budget = page.get_by_test_id('workbench-budget')
            for label, value in (('剩余动作', workspace['budget']['remaining_actions']),
                                 ('剩余内容页', workspace['budget']['remaining_content_pages']),
                                 ('模型调用', workspace['budget']['model_calls_used']),
                                 ('已用动作', workspace['budget']['actions_used'])):
                await expect(ui_budget.locator('dt').filter(has_text=label).locator('xpath=following-sibling::dd[1]')).to_have_text(str(value))
            for label, field in (('剩余执行时间', 'remaining_active_ms'), ('剩余检查等待', 'remaining_ci_wait_ms')):
                expected_time = await page.evaluate('ms => `${(ms / 1000).toFixed(1)} 秒`', workspace['budget'][field])
                await expect(ui_budget.locator('dt').filter(has_text=label).locator('xpath=following-sibling::dd[1]')).to_have_text(
                    expected_time)
            event_ids = {str(item['event_id']) for item in workspace['events']}
            shown = set(await page.get_by_test_id('workbench-event').evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('data-event-id'))"))
            assert shown == event_ids and event_ids
            assert ledger_digest(directory / 'business.sqlite3') == before_read
            await page.locator('#execution-workbench').screenshot(path=str(output / (case + '-workbench.png')))
            await page.get_by_test_id('results-page').screenshot(path=str(output / (case + '-results.png')))
            record(case + '_same_run_workbench_results_api_ui_match_db_and_are_read_only',
                   run_id=domain.run_id, state=expected_state, event_count=len(shown))
            if expected_state == 'SUCCEEDED':
                result_values = result['result']['items']['values']
                for index, value in enumerate(result_values):
                    field = page.get_by_test_id('results-fields').locator(f'[data-result-path="/values/{index}/normalized_value"]')
                    await expect(field.get_by_test_id('result-field-value')).to_have_text(value['normalized_value'])
                assert any(item['displayable'] for item in result['evidence'])
                # Corrupt an original actually used by this recovered Run. The
                # immutable historical success remains, current display fails.
                with connect(directory / 'business.sqlite3') as db:
                    row, derivative_chain = original_evidence(db, result_values[0]['evidence_ids'][0], domain.run_id)
                assert row['original_evidence_id'] is None
                original_path = directory / row['artifact_path']
                assert digest(original_path) == row['sha256']
                original_path.write_bytes(b'Owned M1-24 deliberate evidence corruption\n')
                failed_projection = (await domain.client.get('/v1/tasks/' + domain.task_id + '/results')).json()
                assert failed_projection['selected_run']['state'] == 'SUCCEEDED'
                assert not failed_projection['display_complete_success'] and failed_projection['display_blockers']
                await page.get_by_role('button', name='刷新结果', exact=True).click()
                await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
                await expect(page.get_by_test_id('results-blockers')).to_be_visible()
                await expect(page.get_by_test_id('workbench-state')).to_have_attribute('data-state', 'SUCCEEDED')
                assert ledger_digest(directory / 'business.sqlite3') == before_read
                await page.get_by_test_id('results-page').screenshot(path=str(output / 'same-run-evidence-failure.png'))
                record('recovered_run_corrupt_original_blocks_current_complete_success_without_rewriting_history',
                       run_id=domain.run_id, original_evidence_id=row['evidence_id'],
                       proposal_derivative_chain=derivative_chain,
                       recorded_sha256=row['sha256'], corrupt_sha256=digest(original_path))
            outcomes.append({'case': case, 'task_id': domain.task_id, 'run_id': domain.run_id,
                             'state': expected_state, 'worker_pids': domain.worker_pids,
                             'original_evidence': originals, 'actions_before': before['run_budgets'][0]['actions_used'],
                             'actions_after': after['run_budgets'][0]['actions_used'], 'clicks': fixture.clicks[domain.run_id],
                             'budget_record_id_before': before['run_budgets'][0]['budget_record_id'],
                             'budget_record_id_after': after['run_budgets'][0]['budget_record_id'],
                             'original_attempt_ids': [item['attempt_id'] for item in before['budget_attempts']],
                             'original_quota_debit_ids': [item['debit_id'] for item in before['quota_debits']],
                             'state_versions': {'paused': before['run']['state_version'], 'final': after['run']['state_version']}})
            await context.close()
            closed = frontend.stop()
            assert not closed['forced_kill']
            await domain.close()
        assert not fixture.errors
        work_completed = True
    except BaseException as error:
        primary = error
        report['primary_failure'] = bounded_error(error)
    finally:
        for context in contexts:
            try:
                await context.close()
            except Exception as error:
                errors.append(type(error).__name__)
        if browser is not None:
            try:
                await browser.close()
            except Exception as error:
                errors.append(type(error).__name__)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as error:
                errors.append(type(error).__name__)
        for frontend in frontends:
            if frontend.alive():
                try:
                    frontend.stop()
                except Exception as error:
                    errors.append(type(error).__name__)
        for domain in domains:
            try:
                await domain.close()
            except Exception as error:
                errors.append(type(error).__name__)
        try:
            await fixture.close()
        except Exception as error:
            errors.append(type(error).__name__)
        lifecycle = {'worker_processes': [{'pid': child.pid, 'exit_code': child.returncode} for child in worker_handles],
                     'api_processes': [{'pid': child.pid, 'exit_code': child.returncode} for child in api_handles],
                     'all_workers_exited': all(child.returncode is not None for child in worker_handles),
                     'all_apis_exited': all(child.returncode is not None for child in api_handles),
                     'all_frontends_exited': all(not frontend.alive() for frontend in frontends),
                     'browser_disconnected': browser is None or not browser.is_connected(),
                     'fixture_drained': not fixture.active, 'cleanup_errors': errors}
        report['lifecycle'] = lifecycle
        exited = all(lifecycle[key] for key in ('all_workers_exited', 'all_apis_exited', 'all_frontends_exited',
                                               'browser_disconnected', 'fixture_drained'))
        if not exited:
            errors.append('OwnedServicesStillActive')
        storage = []
        for domain in domains if exited else []:
            item = {'case': domain.alias}
            for kind in ('business', 'graph'):
                path = domain.directory / (kind + '.sqlite3')
                try:
                    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                        item[kind] = {'integrity': db.execute('PRAGMA integrity_check').fetchone()[0],
                                      'foreign_key_errors': len(db.execute('PRAGMA foreign_key_check').fetchall())}
                        if kind == 'business':
                            item[kind].update(active_workers=db.execute("SELECT count(*) FROM scheduler_workers WHERE state='ACTIVE'").fetchone()[0],
                                active_browser_sessions=db.execute("SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING')").fetchone()[0])
                    assert item[kind]['integrity'] == 'ok' and item[kind]['foreign_key_errors'] == 0
                except Exception as error:
                    item[kind] = {'failure': bounded_error(error)}
                    errors.append({'stage': kind + '_post_exit_integrity', **bounded_error(error)})
            if not item.get('business', {}).get('failure') and (
                    item['business']['active_workers'] != 0 or item['business']['active_browser_sessions'] != 0):
                errors.append('ActiveDurableWorkerOrBrowserSession')
            storage.append(item)
        for path, value in ((output / 'same-run-matrix.json', outcomes),
                            (output / 'owned-lifecycle.json', lifecycle),
                            (private / 'http-records.json', {'requests': fixture.requests,
                               'provider_requests': fixture.provider_requests, 'errors': fixture.errors}),
                            (output / 'post-exit-storage.json', storage)):
            try:
                write_json(path, value)
            except Exception as error:
                errors.append({'stage': 'post_exit_record', **bounded_error(error)})
        try:
            public_scan = {name: all(secret.encode() not in path.read_bytes() for path in public_files(output))
                           for name, secret in {**tokens, 'provider': 'owned-controls-synthetic-provider-key'}.items()}
            assert all(public_scan.values())
            report['public_secret_scan'] = public_scan
        except Exception as error:
            errors.append({'stage': 'post_exit_public_scan', **bounded_error(error)})
        report['storage'] = storage
        report['secondary_failures'] = errors
        report['passed'] = primary is None and work_completed and exited and not errors
        if primary is not None:
            raise primary
        if not report['passed']:
            raise AssertionError('Owned post-exit checks failed; secondary failures are preserved')


def bounded_error(error):
    import traceback
    return {'type': type(error).__name__, 'locations': [
        {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
        for frame in traceback.extract_tb(error.__traceback__)]}


def process_inventory():
    """PID, parent and birth identity only; no arguments/environment exported."""
    import subprocess
    rows = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,stat=,lstart='], text=True)
    result = {}
    for line in rows.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4:
            result[int(fields[0])] = {'parent': int(fields[1]), 'state': fields[2], 'birth': fields[3]}
    return result


def descendants(inventory, root_pid):
    owned = {root_pid}
    while True:
        expanded = owned | {pid for pid, row in inventory.items() if row['parent'] in owned}
        if expanded == owned:
            return {pid: inventory[pid]['birth'] for pid in owned if pid in inventory}
        owned = expanded


def live_owned(inventory, witnessed):
    return [pid for pid, birth in witnessed.items() if pid in inventory
            and inventory[pid]['birth'] == birth and not inventory[pid]['state'].startswith('Z')]


async def run_group(output, private, group, *, headed=False):
    name, script, arguments = group
    # Existing probe helpers exclude .private in every absolute ancestor. A
    # separate private .owned parent lets their own public export/secret scans
    # execute normally; this outer report still excludes the whole subtree.
    directory = output / '.owned' / name
    log_path = private / (name + '-runner.log')
    command = [sys.executable, str(SCRIPTS / script), '--output-dir', str(directory), *arguments]
    if headed and name in ('workbench', 'results'):
        command.append('--headed')
    started = time.monotonic()
    witnessed = {}
    leaked = []
    lifecycle_path = output / (name + '-lifecycle.json')
    write_json(lifecycle_path, {'group': name, 'all_observed_services_exited': False, 'state': 'starting'})
    with log_path.open('wb') as log:
        child = await asyncio.create_subprocess_exec(*command, cwd=ROOT,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend'), 'PYTHONUTF8': '1'},
            stdout=log, stderr=asyncio.subprocess.STDOUT)
        waiter = asyncio.create_task(child.wait())
        try:
            while child.returncode is None:
                witnessed.update(descendants(process_inventory(), child.pid))
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), .2)
                except TimeoutError:
                    pass
            code = child.returncode
        finally:
            exit_confirmed = False
            try:
                if child.returncode is None:
                    child.terminate()
                    try:
                        await asyncio.wait_for(asyncio.shield(waiter), 10)
                    except TimeoutError:
                        child.kill()
                        await waiter
                leaked = live_owned(process_inventory(), witnessed)
                # Identity is checked again just before every signal. These
                # are witnessed live descendants, never historical PIDs.
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    for pid in live_owned(process_inventory(), witnessed):
                        try:
                            os.kill(pid, sig)
                        except ProcessLookupError:
                            pass
                    if live_owned(process_inventory(), witnessed):
                        await asyncio.sleep(.5)
                exit_confirmed = child.returncode is not None and not live_owned(process_inventory(), witnessed)
                assert exit_confirmed, 'owned subprobe service could not be stopped'
            finally:
                # Persist this even when final cleanup itself fails, so an
                # orphan reparented to PID 1 cannot regain hash eligibility.
                write_json(lifecycle_path, {'group': name, 'process_pid': child.pid,
                    'exit_code': child.returncode, 'observed_processes': len(witnessed),
                    'all_observed_services_exited': exit_confirmed})
    report_path = directory / 'report.json'
    assert report_path.is_file(), name + ' did not preserve its current failure report'
    value = json.loads(report_path.read_text())
    summary = {'group': name, 'passed': False, 'exit_code': code, 'process_pid': child.pid,
               'process_exited_before_hash_verification': child.returncode is not None,
               'observed_processes': len(witnessed), 'all_observed_services_exited': True,
               'elapsed_seconds': round(time.monotonic() - started, 3),
               'private_report': str(report_path.relative_to(output)), 'report_sha256': digest(report_path)}
    try:
        assert code == 0, name + ' current-source subprocess failed'
        assert not leaked, name + ' left an owned service alive'
        summary.update(validate_subreport(directory, value, summary_only=name in (
            'scheduler', 'network-boundary', 'network-headless')))
        validate_group_scope(name, directory, value)
        summary['passed'] = True
    except Exception as error:
        summary['failure'] = bounded_error(error)
        if value.get('error_type'):
            summary['child_error_type'] = value['error_type']
        write_json(output / (name + '-summary.json'), summary)
        raise
    write_json(output / (name + '-summary.json'), summary)
    return summary


async def run_suite(output, report, *, headed=False, runner=run_group):
    private = output / '.private'
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    owned = output / '.owned'
    owned.mkdir(mode=0o700)
    os.chmod(owned, 0o700)
    cross = {'passed': False, 'checks': []}
    report['groups'] = []
    try:
        print(json.dumps({'group': 'same-run-cross-layer', 'phase': 'started'}), flush=True)
        await cross_layer(output, private / 'cross-layer', cross, headed=headed)
    except BaseException as error:
        cross['passed'] = False
        cross['failure'] = bounded_error(error)
        raise
    finally:
        lifecycle = cross.get('lifecycle', {})
        report['all_owned_services_exited'] = all(lifecycle.get(key) is True for key in (
            'all_workers_exited', 'all_apis_exited', 'all_frontends_exited', 'browser_disconnected', 'fixture_drained'))
        write_json(output / 'same-run-summary.json', cross)
    assert cross['passed'] is True
    report['groups'].append({'group': 'same-run-cross-layer', 'passed': True, 'checks': len(cross['checks'])})
    for group in GROUPS:
        print(json.dumps({'group': group[0], 'phase': 'started'}), flush=True)
        try:
            summary = await runner(output, private, group, headed=headed)
        except BaseException:
            lifecycle_path = output / (group[0] + '-lifecycle.json')
            exit_record = json.loads(lifecycle_path.read_text()) if lifecycle_path.is_file() else {}
            report['all_owned_services_exited'] &= exit_record.get('all_observed_services_exited') is True
            raise
        report['groups'].append(summary)
        print(json.dumps({'group': group[0], 'passed': True, 'checks': summary['checks'],
                          'elapsed_seconds': summary['elapsed_seconds']}), flush=True)
    assert [item['group'] for item in report['groups']] == ['same-run-cross-layer', *[item[0] for item in GROUPS]]
    report['passed'] = True
    report['scope_counts'] = {'current_source_groups': len(report['groups']),
                             'same_run_cross_layer_domains': len(CROSS_CASES),
                             'checks': sum(item['checks'] for item in report['groups']),
                             'paid_model_calls': 0, 'real_accounts': 0, 'physical_repository_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--headed', action='store_true')
    # Trusted child interface: no production endpoint can select this seam.
    parser.add_argument('--api-dir', type=Path)
    parser.add_argument('--api-fd', type=int)
    parser.add_argument('--api-port', type=int)
    parser.add_argument('--ui-port', type=int)
    parser.add_argument('--origin')
    parser.add_argument('--api-point', choices=API_POINTS)
    parser.add_argument('--fault-key')
    args = parser.parse_args()
    if args.api_dir:
        asyncio.run(api_child(args.api_dir.resolve(), args.api_fd, args.origin, args.api_port, args.ui_port,
                              args.api_point, args.fault_key))
        return 0
    if args.output_dir is None:
        parser.error('--output-dir is required')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-24', 'probe': 'integrated-foundation-faults', 'passed': False,
              'started_at': now(), 'fail_fast': True, 'scope': SCOPE,
              'configuration': {'private_domain': '.private (cross-layer data/logs) and .owned (subprobe outputs), '
                                                   'both 0700 and retained for local audit',
                                'fresh_execution_only': True, 'recursive_check_invocation': False,
                                'injectable_boundaries': 'owned constructor/transaction wrappers in probe processes only'}}
    try:
        asyncio.run(run_suite(output, report, headed=args.headed))
    except BaseException as error:
        report['passed'] = False
        report['failure'] = bounded_error(error)
        report['stopped_at_first_failure'] = True
    finally:
        report['finished_at'] = now()
        # Each subordinate has exited and verified its artifacts before it is
        # returned. The cross-layer finally independently awaits every handle.
        try:
            if report.get('all_owned_services_exited') is True:
                first = process_inventory()
                owned = descendants(first, os.getpid())
                owned.pop(os.getpid(), None)
                report['all_owned_services_exited'] = not live_owned(process_inventory(), owned)
            if report.get('all_owned_services_exited') is True:
                hashes = {str(path.relative_to(output)): digest(path)
                          for tree in (output / '.private', output / '.owned') for path in sorted(tree.rglob('*'))
                          if path.is_file() and not path.is_symlink() and '.security' not in path.relative_to(tree).parts}
                write_json(output / 'private-artifact-manifest.json', {'scope': 'private local audit only; hashes contain no file bytes',
                            'owned_services_exited': True, 'artifact_sha256': hashes})
                report['private_artifact_files'] = len(hashes)
                report['artifact_sha256'] = {str(path.relative_to(output)): digest(path) for path in public_files(output)}
            else:
                report['passed'] = False
                report['artifact_sha256'] = {}
                report['hashing_withheld_for_unverified_owned_service_exit'] = True
        except BaseException as error:
            report['passed'] = False
            report['artifact_sha256'] = {}
            report['finalization_failure'] = bounded_error(error)
            report['hashing_withheld_for_failed_finalization'] = True
        finally:
            write_json(output / 'report.json', report)
    print(json.dumps({'passed': report['passed'], 'groups': len(report.get('groups', [])),
                      'report': str(output / 'report.json')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
