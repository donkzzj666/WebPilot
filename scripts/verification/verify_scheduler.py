#!/usr/bin/env python3
"""M1-10 real Worker competition/crash and transactional scheduling probe.

Uses only new temporary SQLite databases and registered synthetic loopback pages.
The child owns its browser/process lock; no user task, account or model is used.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')
from webagent.config import Settings, disable_external_tracing
from webagent.db import connect, migrate, transaction
from webagent.db.repository import create_task, add_contract, create_run
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource, ExecutionToken
from webagent.scheduler.store import SchedulerStore
from webagent.scheduler.worker import QueueWorker
from webagent.sessions.manager import ManagedBrowser
from webagent.sessions.models import SessionOwner
from verify_browser_sessions import LocalFixture, network_config, chromium_identity, launch_options, kill_owned_chromium


def seed(path, names):
    with connect(path) as db, transaction(db):
        for name in names:
            create_task(db, task_id=name, instruction='Synthetic scheduler acceptance', requested_fields=['contract'])
            add_contract(db, {'schema_version': 'm0-contract-v1', 'task_id': name, 'contract_version': 1,
                'scenario': 'research', 'objective': 'Synthetic scheduling only',
                'parameters': {'query': 'fixture'}, 'sources': ['local-fixture'], 'action_policy': {'mode': 'read_only'}})
            create_run(db, run_id=name, task_id=name, contract_version=1,
                graph_version='scheduler-fixture-v1', graph_state_schema_version='fixture-v1',
                model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)


async def child(args):
    disable_external_tracing()
    settings = Settings(args.data_dir.resolve())
    manager = ManagedBrowser(settings, network_config=network_config(args.fixture_port), launch_options=launch_options())
    pump = None
    stopped = asyncio.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(number, stopped.set)
    try:
        await manager.start()
        store = SchedulerStore(settings.business_db)
        async def executor(token):
            owner = SessionOwner('run', token.run_id, 'local-scheduler-fixture', realm='webarena')
            session = await manager.create(owner, execution_token=token)
            context = await manager.context(session.session_id, owner, execution_token=token)
            response = await context.pages[0].goto(f'http://127.0.0.1:{args.fixture_port}/fixture')
            assert response.status == 200
            browser = await chromium_identity(context.browser)
            print(json.dumps({'event': 'fixture_claimed', 'token': token.as_dict(),
                              'session_id': session.session_id, 'chromium': browser}), flush=True)
            await stopped.wait()
        pump = QueueWorker(store, manager.manager_id, executor=executor, heartbeat_seconds=.2, poll_seconds=.05)
        await pump.start()
        print(json.dumps({'event': 'fixture_worker_ready', 'worker_id': manager.manager_id,
                          'generation': pump.generation}), flush=True)
        await pump.run(stopped)
    finally:
        if pump is not None:
            await pump.aclose()
        await manager.aclose()


async def line_until(reader, event):
    async with asyncio.timeout(15):
        while True:
            line = await reader.readline()
            if not line:
                raise RuntimeError('Synthetic Worker stopped before readiness')
            value = json.loads(line)
            if value.get('event') == event:
                return value


def alive_owned_browser(identity):
    result = subprocess.run(['ps', '-p', str(identity['pid']), '-o', 'command='], capture_output=True, text=True)
    return result.returncode == 0 and '--user-data-dir=' + identity['profile'] in result.stdout


async def verify(output, report):
    fixture = await LocalFixture().start()
    process = None
    chromium = None
    manager = None
    temp = tempfile.TemporaryDirectory(prefix='webpilot-scheduler-')
    settings = Settings(Path(temp.name).resolve())
    try:
        migrate(settings.business_db)
        seed(settings.business_db, ['run-one', 'run-two', 'run-three', 'same-account', 'same-repository', 'environment-other'])
        store = SchedulerStore(settings.business_db)
        site_a = Resource.site_identity('fixture-a', 'identity-one')
        site_b = Resource.site_identity('fixture-b', 'identity-two')
        repo = Resource.repository_write('Fixture/Shared')
        for name, resources in [('run-one', [site_a, repo]), ('run-two', [site_b]),
                                ('run-three', [Resource.site_identity('fixture-c')]),
                                ('same-account', [site_a]), ('same-repository', [Resource.site_identity('fixture-d', 'other'), repo]),
                                ('environment-other', [Resource.webarena_environment()])]:
            store.enqueue(name, [*resources, Resource.browser_context(name)], expected_state_version=0)
        def check(name, condition=True):
            assert condition, name
            report['checks'][name] = True
        generation = store.start_worker('matrix-worker')
        one = store.claim('matrix-worker', generation)
        two = store.claim('matrix-worker', generation)
        check('two_active_third_queued', one is not None and two is not None and store.claim('matrix-worker', generation) is None)
        store.defer(two, 'PAUSED')
        three = store.claim('matrix-worker', generation)
        check('pause_releases_active_slot', three is not None and three.run_id == 'run-three')
        store.finish(three, target='CANCELLED')
        # Account/repository blockers must be skipped without partially acquiring
        # their earlier resources; the independent environment item remains due.
        independent = store.claim('matrix-worker', generation)
        check('blocked_account_and_repository_do_not_block_independent', independent is not None and independent.run_id == 'environment-other')
        store.finish(independent, target='CANCELLED')
        check('same_account_and_cross_account_repository_serialized', store.claim('matrix-worker', generation) is None)
        store.stop_worker('matrix-worker', generation)
        try:
            store.validate(one)
        except BusinessError as error:
            check('stopped_generation_fenced', error.status == 409)
        else:
            raise AssertionError('old execution remained authorized')

        # A separate data domain gives the process fixture an empty queue and
        # does not interact with the user's normal running Worker.
        child_settings = Settings(settings.data_dir / 'process-domain')
        child_settings.data_dir.mkdir()
        migrate(child_settings.business_db)
        seed(child_settings.business_db, ['process-run'])
        child_store = SchedulerStore(child_settings.business_db)
        child_store.enqueue('process-run', [Resource.site_identity('local-scheduler-fixture', realm='webarena'),
                            Resource.browser_context('process-run')], expected_state_version=0)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'backend'))
        process = await asyncio.create_subprocess_exec(str(ROOT / '.venv/bin/python'), str(Path(__file__).resolve()),
            '--worker-child', '--data-dir', str(child_settings.data_dir), '--fixture-port', str(fixture.port),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        await line_until(process.stdout, 'fixture_worker_ready')
        claimed = await line_until(process.stdout, 'fixture_claimed')
        chromium = claimed['chromium']
        old_token = ExecutionToken.from_dict(claimed['token'])
        check('real_child_worker_claims_and_opens_reserved_browser', alive_owned_browser(chromium))
        competing = await asyncio.create_subprocess_exec(str(ROOT / 'scripts/dev.sh'), 'worker',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**env, 'WEBAGENT_DATA_DIR': str(child_settings.data_dir)})
        stdout, stderr = await asyncio.wait_for(competing.communicate(), 10)
        # Error strings are checked locally, never retained as unrestricted logs.
        check('competing_worker_rejected', competing.returncode != 0 and b'worker_ready' not in stdout)
        child_store.validate(old_token)
        check('competition_does_not_revoke_current_owner')
        process.kill()
        await asyncio.wait_for(process.wait(), 10)
        async with asyncio.timeout(10):
            while alive_owned_browser(chromium):
                await asyncio.sleep(.05)
        check('owned_chromium_exits_after_worker_crash')
        manager = ManagedBrowser(child_settings, network_config=network_config(fixture.port))
        await manager.start()
        recovered_generation = child_store.start_worker(manager.manager_id)
        try:
            child_store.validate(old_token)
        except BusinessError as error:
            check('crashed_worker_epoch_fenced', error.status == 409)
        else:
            raise AssertionError('crashed Worker retained authority')
        recovery = child_store.claim(manager.manager_id, recovered_generation)
        check('recovery_qualification_available', recovery is not None)
        try:
            child_store.validate(recovery)
        except BusinessError as error:
            check('recovery_is_not_automatic_replay', error.status == 409)
        else:
            raise AssertionError('normal execution allowed before reconciliation')
        with connect(child_settings.business_db) as db:
            state = db.execute("SELECT state FROM runs WHERE run_id='process-run'").fetchone()[0]
            browser_state = db.execute('SELECT state FROM browser_sessions WHERE session_id=?', (claimed['session_id'],)).fetchone()[0]
            retained = db.execute("SELECT count(*) FROM resource_leases WHERE holder_run_id='process-run' AND resource_type='site_identity'").fetchone()[0]
            event_count = db.execute("SELECT count(*) FROM task_events WHERE run_id='process-run' AND event_type='state_changed'").fetchone()[0]
        check('crash_preserves_recovery_account_hold', state == 'RECONCILING' and retained == 1)
        check('crash_context_marked_lost', browser_state == 'LOST')
        check('run_state_events_remain_paired', event_count == 2)
        # Materialize the recovery reservation, then share the remaining places
        # with real isolated verification contexts. Reconciliation authority
        # permits rebuilding a context but never ordinary browser actions.
        await manager.create(SessionOwner('run', recovery.run_id, 'local-scheduler-fixture', realm='webarena'),
                             execution_token=recovery)
        for index in range(3):
            await manager.create(SessionOwner('verification', f'capacity-{index}',
                                             'local-scheduler-fixture', realm='webarena'))
        with connect(child_settings.business_db) as db:
            live = db.execute("SELECT count(*) FROM browser_sessions WHERE state IN ('OPENING','OPEN','CLOSING')").fetchone()[0]
            pending = db.execute('SELECT count(*) FROM scheduler_context_reservations WHERE session_id IS NULL').fetchone()[0]
        check('four_real_contexts_share_materialized_reservation', live == 4 and pending == 0)
        try:
            await manager.create(SessionOwner('verification', 'capacity-fifth',
                                             'local-scheduler-fixture', realm='webarena'))
        except BusinessError as error:
            check('fifth_real_context_rejected', error.status == 409)
        else:
            raise AssertionError('fifth browser context was allowed')
        child_store.stop_worker(manager.manager_id, recovered_generation)
        report['scope_counts'] = {'active_limit': 2, 'context_limit': 4, 'fixture_worker_processes': 2}
        report['passed'] = True
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
        if chromium is not None and alive_owned_browser(chromium):
            kill_owned_chromium(chromium)
        if manager is not None:
            await manager.aclose()
        await fixture.close()
        temp.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--worker-child', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--fixture-port', type=int)
    args = parser.parse_args()
    if args.worker_child:
        asyncio.run(child(args))
        return 0
    if args.output_dir is None:
        parser.error('--output-dir is required')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-10', 'passed': False, 'checks': {},
        'created_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Real SQLite leases and queue; competing independent Worker and SIGKILL of an owned synthetic-browser Worker; no product graph/model/task execution or user data.'}
    try:
        asyncio.run(verify(args.output_dir, report))
    except Exception as error:
        import traceback
        report['error_type'] = type(error).__name__
        report['error_locations'] = [{'file': Path(frame.filename).name, 'line': frame.lineno,
            'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]
    report['artifact_sha256'] = {}
    path = args.output_dir / 'report.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(path)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
