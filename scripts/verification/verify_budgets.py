#!/usr/bin/env python3
"""M1-11 independent durable-budget, crash and suspended-awaitable probe.

All databases, Runs, identities and dispatched intents are synthetic and live
in a newly created temporary directory. No provider, browser or public website
is contacted. The only killed process is this probe's own Python child.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

from webagent.budgets.clock import SystemClock
from webagent.budgets.deadline import BudgetDeadlineExceeded, DeadlineController
from webagent.budgets.store import BudgetStore
from webagent.config import disable_external_tracing
from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.evidence.service import EvidenceService
from webagent.models.adapter import ModelAdapter
from webagent.models.journal import list_attempts
from webagent.models.schema import ModelInput
from webagent.models.transport import ModelConfig
from webagent.scheduler.models import ExecutionToken, Resource
from webagent.scheduler.store import SchedulerStore
from webagent.tasks.compiler import compile_draft


class VirtualClock:
    domain = 'budget-probe-virtual-boot'

    def __init__(self, wall=None):
        self.wall = wall or datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.ns = 0

    def utcnow(self):
        return self.wall

    def monotonic_ns(self):
        return self.ns

    def advance(self, seconds, *, wall_seconds=None):
        self.ns += round(seconds * 1_000_000_000)
        self.wall += timedelta(seconds=seconds if wall_seconds is None else wall_seconds)


def prepared(directory, *, clock=None):
    directory.mkdir()
    path = directory / 'business.sqlite3'
    migrate(path)
    if clock is None:
        return path, SchedulerStore(path)
    return path, SchedulerStore(path, clock=clock.utcnow, budgets=BudgetStore(path, clock=clock))


def queue_run(path, store, name, *, limits=None, source_kind=None, queue_class='ordinary', repository=False):
    now = utc_text(store.budgets.clock.utcnow())
    parameters = ({'source_id': 'local-fixture', 'source_kind': source_kind,
                   'baseline': False, 'scheduled_at': now, 'confirmed_boundary': None}
                  if source_kind else {'queries': ['fixture'], 'topic_criteria': ['fixture evidence'],
                                      'cutoff_at': now, 'max_items': 3})
    contract = compile_draft(
        {'instruction': 'Synthetic independent budget acceptance',
         'scenario': 'monitoring' if source_kind else 'research',
         'source_ids': ['local-fixture'], 'parameters': parameters},
        task_id='task-' + name, version=1, created_at=now,
        provenance=[{'origin': 'api', 'reference': 'budget-verification-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    contract['budget_profile'].update(limits or {})
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'],
                    requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=name, task_id=contract['task_id'], contract_version=1,
                   graph_version='budget-probe-v1', graph_state_schema_version='fixture-v1',
                   model_config_sha256=ModelConfig().config_sha256, runtime_config_sha256='b' * 64)
    resources = [Resource.site_identity('local-fixture', name), Resource.browser_context(name)]
    if repository:
        resources.append(Resource.repository_write('Fixture/budget-safety'))
    store.enqueue(name, resources, expected_state_version=0, queue_class=queue_class)
    return contract


def debit_rows(path):
    with connect(path) as db:
        return [dict(row) for row in db.execute('SELECT run_id,quota_date,debit_kind FROM quota_debits ORDER BY run_id')]


def state(path, run_id):
    with connect(path) as db:
        return db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()[0]


def rejected(call):
    try:
        call()
    except BusinessError as error:
        assert error.status == 409
        return error
    raise AssertionError('Expected a durable authority or budget rejection')


def advance_wait(clock, store, worker, generation, seconds):
    while seconds > 0:
        interval = min(20, seconds)
        clock.advance(interval)
        store.heartbeat_worker(worker, generation)
        seconds -= interval


async def crash_child(directory):
    """Commit one heartbeat, then leave the final interval open for SIGKILL."""
    disable_external_tracing()
    path = directory / 'business.sqlite3'
    store = SchedulerStore(path)
    generation = store.start_worker('owned-crash-child')
    token = store.claim('owned-crash-child', generation)
    assert token is not None
    await asyncio.sleep(.025)
    token = store.heartbeat(token)
    with connect(path) as db:
        row = db.execute('''SELECT b.active_ms,t.anchor_utc,t.anchor_mono_ns,t.clock_domain
            FROM run_budgets b JOIN budget_timers t USING(run_id) WHERE run_id=?''', (token.run_id,)).fetchone()
    print(json.dumps({'event': 'heartbeat_committed', 'token': token.as_dict(), **dict(row)}), flush=True)
    # A next heartbeat would be due after five seconds in the normal Worker.
    # The parent kills this owned child after eighty milliseconds instead.
    await asyncio.Event().wait()


async def crash_probe(base, report, observed, check):
    path, store = prepared(base / 'crash')
    queue_run(path, store, 'crash-run')
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), '--crash-child',
            '--data-dir', str(path.parent), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend')},
        )
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, 'Owned crash child stopped before committing its heartbeat'
        committed = json.loads(line)
        assert committed['event'] == 'heartbeat_committed'
        old_token = ExecutionToken.from_dict(committed['token'])
        check('owned_child_commits_claim_and_heartbeat', committed['active_ms'] >= 10)
        await asyncio.sleep(.08)
        clock = store.budgets.clock
        same_domain = committed['clock_domain'] == clock.domain
        if clock.domain.startswith('boot:'):
            check('native_same_boot_clock_between_processes', same_domain)
        else:
            check('unavailable_boot_identity_uses_conservative_process_domain',
                  clock.domain.startswith('process:') and not same_domain)
        killed_at, killed_ns = clock.utcnow(), clock.monotonic_ns()
        if same_domain:
            minimum_open_ms = (killed_ns - committed['anchor_mono_ns']) // 1_000_000 - 2
        else:
            anchor = datetime.fromisoformat(committed['anchor_utc'].replace('Z', '+00:00'))
            minimum_open_ms = int((killed_at - anchor).total_seconds() * 1000) - 2
        assert minimum_open_ms >= 50
        process.kill()
        await asyncio.wait_for(process.wait(), 10)
        check('owned_child_sigkill_confirmed', process.returncode < 0)
        generation = store.start_worker('crash-replacement')
        recovered = store.budgets.status('crash-run')
        check('recovery_charges_last_open_interval',
              recovered['active_ms'] >= committed['active_ms'] + minimum_open_ms)
        rejected(lambda: store.validate(old_token, allow_reconciling=True))
        check('crashed_epoch_cannot_dispatch')
        resumed = store.claim('crash-replacement', generation)
        assert resumed is not None and resumed.run_id == old_token.run_id
        check('same_run_resume_keeps_single_original_debit', len(debit_rows(path)) == 1)
        check('resume_never_reduces_persisted_active_usage',
              store.budgets.status('crash-run')['active_ms'] >= recovered['active_ms'])
        rejected(lambda: store.validate(resumed))
        check('crash_resume_requires_external_reconciliation', state(path, 'crash-run') == 'RECONCILING')
        observed['crash'] = {'native_same_boot_domain': same_domain and clock.domain.startswith('boot:'),
                             'committed_active_ms': committed['active_ms'],
                             'minimum_unclosed_interval_ms': minimum_open_ms,
                             'recovered_active_ms': recovered['active_ms'], 'quota_debits': 1}
        store.stop_worker('crash-replacement', generation)
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 10)


class HangingProvider:
    config = ModelConfig()

    def __init__(self, entered, cancelled):
        self.entered, self.cancelled = entered, cancelled
        self.calls = 0

    async def complete(self, model_input, schema, *, images=(), repair_errors=None):
        self.calls += 1
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled()


def model_input(path, contract, token):
    with connect(path) as db:
        budget_id = db.execute('SELECT budget_record_id FROM run_budgets WHERE run_id=?', (token.run_id,)).fetchone()[0]
    now = utc_text()
    content = {
        'run_id': token.run_id, 'contract': contract,
        'observation': {'snapshot_id': 'fixture-snapshot', 'run_id': token.run_id, 'captured_at': now,
                        'source_url': contract['start_urls'][0], 'title': 'Synthetic budget page',
                        'tab_id': 'fixture-tab', 'frame_id': 'fixture-frame', 'page_version': 'v1',
                        'width': 100, 'height': 100, 'visible_excerpt': 'Synthetic fixture',
                        'evidence_ids': [], 'redaction_status': 'FILTERED'},
        'verified_checkpoint': {'checkpoint_id': 'fixture-checkpoint', 'task_id': contract['task_id'],
                                'run_id': token.run_id, 'contract_version': 1,
                                'current_subgoal': 'inspect', 'verified_item_ids': [],
                                'pending_item_ids': ['fixture-item'], 'current_object_id': 'fixture-object',
                                'current_object_version': None, 'current_snapshot_id': 'fixture-snapshot',
                                'flow_version': None, 'action_sequence': 0, 'business_event_id': 0,
                                'budget_record_ref': budget_id, 'identity_ref': None,
                                'pending_operation_ids': [], 'epoch': token.epoch,
                                'evidence_ids': [], 'saved_at': now},
        'image_evidence_ids': [], 'allowed_action_schema_ref': 'urn:webagent:m0-contract-v1:Action',
        'selected_flow_versions': [],
    }
    content['observation'] = EvidenceService(path.parent).publish_observation(
        content['observation'], {'title': 'Synthetic budget page', 'text': 'Synthetic fixture'},
        execution_token=token)
    return ModelInput.model_validate_json(canonical_json(content))


async def deadline_probe(base, observed, check, *, component):
    path, store = prepared(base / component)
    contract = queue_run(path, store, component, limits={'max_active_seconds': 1},
                         repository=component == 'attachment')
    generation = store.start_worker('deadline-' + component)
    token = store.claim('deadline-' + component, generation)
    entered = asyncio.Event()
    cancellation_states = []
    cancellation_authority = []

    def cancelled():
        cancellation_states.append(state(path, component))
        cancellation_authority.append(rejected(lambda: store.validate(token)).status)

    if component == 'provider':
        provider = HangingProvider(entered, cancelled)
        async def execute():
            return await ModelAdapter(path, provider).generate(model_input(path, contract, token), execution_token=token)
    else:
        repo = Resource.repository_write('Fixture/budget-safety').resource_key
        with connect(path) as db, transaction(db):
            now = utc_text()
            db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
                target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
                VALUES('synthetic-intent','synthetic-intent',?, ?,?,'fixture change','fixture','v1','INTENT',?,?)''',
                       (contract['task_id'], component, repo, now, now))
        async def execute():
            entered.set()
            try:
                await asyncio.Event().wait()  # An attachment/graph awaitable that never returns.
            finally:
                cancelled()

    async def status_check():
        return await asyncio.to_thread(store.budgets.flush, token)

    async def expire(reason):
        await asyncio.to_thread(store.expire_budget, token.run_id, reason)

    guard = DeadlineController(status_check, expire, poll_seconds=.01, cancel_seconds=.5)
    operation = asyncio.create_task(guard.run(execute))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if component == 'provider':
            # ModelAdapter also owns a local HTTP timeout. Advance the durable
            # elapsed checkpoint while that request is suspended so this probe
            # specifically observes the independent watchdog winning first.
            # The attachment case below reaches one real monotonic second.
            with connect(path) as db, transaction(db):
                db.execute('UPDATE run_budgets SET active_ms=1000,state_version=state_version+1 WHERE run_id=?',
                           (component,))
        try:
            await asyncio.wait_for(operation, 3)
        except BudgetDeadlineExceeded as error:
            check(component + '_hanging_awaitable_has_independent_deadline',
                  error.reason == 'active_time' and error.cancellation_completed)
        else:
            raise AssertionError('Suspended fixture completed without an independent deadline')
        check(component + '_durable_failed_before_cancellation', cancellation_states == ['FAILED'])
        check(component + '_old_authority_revoked_before_cancellation', cancellation_authority == [409])
        check(component + '_deadline_releases_active_slot',
              not any(row['resource_type'] == 'active_slot' for row in store.snapshot()['leases']))
        if component == 'provider':
            attempts = list_attempts(path, token.run_id)
            check('model_request_cancelled_and_charged_once', provider.calls == 1
                  and len(attempts) == 1 and attempts[0]['status'] == 'CANCELLED'
                  and store.budgets.status(component)['model_calls_used'] == 1)
        else:
            with connect(path) as db:
                intent = db.execute("SELECT status FROM write_intents WHERE operation_id='synthetic-intent'").fetchone()[0]
                quarantines = db.execute("SELECT count(*) FROM resource_quarantines WHERE operation_id='synthetic-intent'").fetchone()[0]
                retained = db.execute('SELECT count(*) FROM resource_leases WHERE holder_run_id=? AND resource_type IN (\'site_identity\',\'repository_write\')', (component,)).fetchone()[0]
            check('deadline_marks_dispatched_intent_unknown', intent == 'UNKNOWN')
            check('unknown_write_retains_repository_and_account_quarantine', quarantines == 2 and retained == 2)
            queue_run(path, store, 'quarantine-contender', repository=True)
            replacement = store.start_worker('quarantine-replacement')
            check('worker_restart_does_not_clear_unknown_repository_lock', store.claim('quarantine-replacement', replacement) is None)
            store.stop_worker('quarantine-replacement', replacement)
        observed[component] = {'state': state(path, component),
                               'active_ms': store.budgets.status(component)['active_ms'],
                               'quota_debits': len(debit_rows(path)),
                               'deadline_trigger': 'persisted_elapsed_checkpoint' if component == 'provider' else 'real_monotonic_elapsed',
                               'cancellation_observed_failed': cancellation_states == ['FAILED']}
    finally:
        if not operation.done():
            store.stop_worker('deadline-' + component, generation)
            operation.cancel()
            try:
                await asyncio.wait_for(operation, 1)
            except (asyncio.CancelledError, BusinessError):
                pass


def virtual_probes(base, observed, check):
    clock = VirtualClock()
    path, store = prepared(base / 'active', clock=clock)
    queue_run(path, store, 'active')
    generation = store.start_worker('active-worker')
    token = store.claim('active-worker', generation)
    for _ in range(59):
        clock.advance(20)
        token = store.heartbeat(token)
    clock.advance(19.999)
    token = store.heartbeat(token)
    before = store.budgets.status('active')
    check('virtual_active_time_below_twenty_minutes_remains_valid', not before['exhausted'] and before['active_ms'] == 1_199_999)
    clock.advance(.001)
    store.heartbeat_worker('active-worker', generation)
    store.sweep_budget_due()
    active = store.budgets.status('active')
    check('virtual_twenty_minute_active_budget_finishes_failed', state(path, 'active') == 'FAILED'
          and active['active_ms'] == 1_200_000 and active['reason'] == 'active_time')
    observed['active_boundary'] = {'before_ms': before['active_ms'], 'after_ms': active['active_ms']}

    clock = VirtualClock()
    path, store = prepared(base / 'ci', clock=clock)
    queue_run(path, store, 'ci')
    generation = store.start_worker('ci-worker')
    token = store.claim('ci-worker', generation)
    for iteration in range(2):
        waiting = store.defer(token, 'WAITING_CI')
        advance_wait(clock, store, 'ci-worker', generation, 600)
        if iteration == 0:
            store.resume('ci', waiting['run_state_version'])
            token = store.claim('ci-worker', generation)
            token = store.reconcile('ci', token.state_version)
    store.sweep_budget_due()
    ci = store.budgets.status('ci')
    check('two_ci_waits_accumulate_six_hundred_plus_six_hundred_seconds',
          ci['ci_wait_ms'] == 1_200_000 and ci['reason'] == 'ci_wait' and state(path, 'ci') == 'FAILED')
    check('ci_wait_does_not_spend_active_time_or_redebit_resume', ci['active_ms'] == 0 and len(debit_rows(path)) == 1)
    observed['ci'] = {'ci_wait_ms': ci['ci_wait_ms'], 'active_ms': ci['active_ms'], 'quota_debits': 1}

    clock = VirtualClock()
    path, store = prepared(base / 'handoff', clock=clock)
    queue_run(path, store, 'handoff', limits={'max_handoff_seconds': 60})
    generation = store.start_worker('handoff-worker')
    token = store.claim('handoff-worker', generation)
    waiting = store.defer(token, 'WAITING_HANDOFF', control_owner='human')
    first = store.budgets.status('handoff')['handoff_deadline']
    clock.advance(5)
    store.resume('handoff', waiting['run_state_version'])
    token = store.claim('handoff-worker', generation)
    token = store.reconcile('handoff', token.state_version)
    store.defer(token, 'WAITING_HANDOFF', control_owner='human', handoff_deadline=clock.utcnow() + timedelta(hours=23))
    check('repeat_handoff_cannot_extend_first_deadline', store.budgets.status('handoff')['handoff_deadline'] == first)
    advance_wait(clock, store, 'handoff-worker', generation, 55)
    store.sweep_budget_due()
    check('first_handoff_window_expires_outside_executor', state(path, 'handoff') == 'FAILED'
          and store.budgets.status('handoff')['reason'] == 'handoff')
    check('expired_handoff_preserves_human_logical_hold',
          any(row['control_owner'] == 'human' and row['logical_hold'] for row in store.snapshot()['leases']))
    observed['handoff'] = {'initial_deadline': first, 'active_ms': store.budgets.status('handoff')['active_ms']}

    clock = VirtualClock()
    path, store = prepared(base / 'rollback', clock=clock)
    queue_run(path, store, 'rollback')
    generation = store.start_worker('rollback-worker')
    token = store.claim('rollback-worker', generation)
    clock.advance(10, wall_seconds=-300)
    store.budgets.flush(token)
    check('wall_clock_rollback_does_not_replace_monotonic_active_time',
          store.budgets.status('rollback')['active_ms'] == 10_000)
    clock.domain = 'changed-boot-domain'
    clock.ns = 0
    clock.wall -= timedelta(seconds=10)
    status = store.budgets.flush(token)
    check('unknown_monotonic_domain_and_wall_rollback_conservatively_exhaust_remaining',
          status['remaining_active_ms'] == 0 and status['reason'] == 'active_time')
    store.expire_budget('rollback', status['reason'])
    observed['clock_recovery'] = {'active_ms': status['active_ms'], 'remaining_active_ms': 0, 'state': state(path, 'rollback')}

    clock = VirtualClock()
    path, store = prepared(base / 'site', clock=clock)
    queue_run(path, store, 'site', limits={'max_active_seconds': 10})
    generation = store.start_worker('site-worker')
    token = store.claim('site-worker', generation)
    clock.advance(8)
    token = store.heartbeat(token)
    store.wait_site(token, 'local-fixture', 5)
    with connect(path) as db:
        gate = db.execute("SELECT state,next_eligible_at FROM site_gates WHERE site_id='public:local-fixture'").fetchone()
    check('site_wait_longer_than_remaining_budget_finishes_failed', state(path, 'site') == 'FAILED'
          and store.budgets.status('site')['reason'] == 'site_wait_exceeds_budget')
    check('budget_termination_preserves_shared_site_cooldown', gate['state'] == 'COOLDOWN'
          and gate['next_eligible_at'] == utc_text(clock.utcnow() + timedelta(seconds=5)))
    check('waiting_site_budget_termination_releases_active_slot',
          not any(row['resource_type'] == 'active_slot' for row in store.snapshot()['leases']))
    observed['site'] = {'state': state(path, 'site'), 'cooldown_until': gate['next_eligible_at'], 'active_ms': 8000}


def counter_and_quota_probes(base, observed, check):
    for kind, limit, reason in [('actions', 150, 'action_limit'), ('pages', 25, 'content_page_limit'),
                                ('recoveries', 3, 'recovery_limit')]:
        clock = VirtualClock()
        path, store = prepared(base / kind, clock=clock)
        queue_run(path, store, kind)
        generation = store.start_worker(kind + '-worker')
        token = store.claim(kind + '-worker', generation)
        for number in range(limit):
            kwargs = {'kind': 'recovery', 'site_id': 'local-fixture', 'subgoal': 'fixture-subgoal',
                      'obstacle_type': 'network_error'} if kind == 'recoveries' else {'kind': 'action'}
            if kind == 'pages':
                kwargs['content_page'] = True
            store.budgets.consume(token, attempt_id=f'fixture-{number}', **kwargs)
        check(kind + '_exact_product_limit_dispatches_are_counted',
              store.budgets.status(kind)['actions_used'] == limit)
        error = rejected(lambda: store.budgets.consume(token, attempt_id='over-limit', **kwargs))
        check(kind + '_next_dispatch_is_rejected_without_refund',
              error.code == 'BUDGET_EXCEEDED' and store.budgets.status(kind)['reason'] == reason)
        store.expire_budget(kind, reason)
        observed[kind] = {'accepted_attempts': limit, 'next_attempt_rejected': True}

    clock = VirtualClock()
    path, store = prepared(base / 'quota', clock=clock)
    generation = store.start_worker('quota-worker')
    for number in range(42):
        name = f'ordinary-{number:02}'
        queue_run(path, store, name)
        token = store.claim('quota-worker', generation)
        assert token is not None and token.run_id == name
        store.finish(token)
    queue_run(path, store, 'ordinary-43')
    check('ordinary_daily_run_43_is_queued_without_debit', store.claim('quota-worker', generation) is None
          and len(debit_rows(path)) == 42)
    for source in ('security_community', 'cisa_kev'):
        for number in range(4):
            name = f'{source}-{number}'
            queue_run(path, store, name, source_kind=source, queue_class='monitoring')
            token = store.claim('quota-worker', generation)
            assert token is not None and token.run_id == name
            store.finish(token)
    check('eight_reserved_monitoring_runs_fill_public_daily_fifty', len(debit_rows(path)) == 50)
    queue_run(path, store, 'public-51', source_kind='cisa_kev', queue_class='monitoring')
    check('public_daily_run_51_has_no_debit_or_execution_authority',
          store.claim('quota-worker', generation) is None and len(debit_rows(path)) == 50)
    observed['daily_quota'] = {'ordinary': 42, 'monitoring': 8, 'total': 50, 'over_limit_queued': 2}

    clock = VirtualClock(datetime(2026, 9, 29, 15, 59, 58, tzinfo=timezone.utc))
    path, store = prepared(base / 'midnight', clock=clock)
    queue_run(path, store, 'cross-midnight')
    generation = store.start_worker('midnight-worker')
    token = store.claim('midnight-worker', generation)
    clock.advance(2)
    waiting = store.defer(token, 'PAUSED')
    store.resume(token.run_id, waiting['run_state_version'])
    token = store.claim('midnight-worker', generation)
    token = store.reconcile(token.run_id, token.state_version)
    store.finish(token)
    check('shanghai_midnight_resume_keeps_original_day_single_debit',
          debit_rows(path) == [{'run_id': 'cross-midnight', 'quota_date': '2026-09-29', 'debit_kind': 'ordinary'}])
    queue_run(path, store, 'next-day-run')
    next_token = store.claim('midnight-worker', generation)
    check('new_run_uses_new_shanghai_day_while_utc_day_unchanged',
          next_token is not None and {row['quota_date'] for row in debit_rows(path)} == {'2026-09-29', '2026-09-30'}
          and clock.utcnow().date().isoformat() == '2026-09-29')
    store.finish(next_token)
    observed['shanghai_midnight'] = {'original_run_debits': 1, 'new_run_debits': 1, 'utc_date': '2026-09-29'}


async def verify(output, report):
    disable_external_tracing()
    observed = {}
    def check(name, condition=True):
        assert condition, name
        report['checks'][name] = True
    with tempfile.TemporaryDirectory(prefix='webpilot-budgets-') as directory:
        base = Path(directory).resolve()
        await crash_probe(base, report, observed, check)
        await deadline_probe(base, observed, check, component='provider')
        await deadline_probe(base, observed, check, component='attachment')
        virtual_probes(base, observed, check)
        counter_and_quota_probes(base, observed, check)
    (output / 'observations.json').write_text(json.dumps(observed, indent=2) + '\n')
    report['scope_counts'] = {'owned_sigkill_children': 1, 'real_hanging_components': 2,
                              'real_monotonic_wait_deadlines': 1, 'persisted_checkpoint_wait_deadlines': 1,
                              'virtual_elapsed_time_domains': 10, 'native_same_boot_clock':
                                  int(observed['crash']['native_same_boot_domain'])}
    if not observed['crash']['native_same_boot_domain']:
        report['clock_limitation'] = 'Kernel boot identity unavailable in this sandbox; conservative UTC recovery verified. Native same-boot verification requires normal local process permissions.'
    report['passed'] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--crash-child', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    if args.crash_child:
        if args.data_dir is None:
            parser.error('--data-dir is required for the owned child')
        asyncio.run(crash_child(args.data_dir))
        return 0
    if args.output_dir is None:
        parser.error('--output-dir is required')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-11', 'passed': False, 'checks': {},
              'created_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Isolated SQLite and owned Python SIGKILL; independent cancellation of a suspended ModelAdapter provider using a persisted elapsed checkpoint and of an attachment awaitable using real monotonic elapsed time; virtual clocks and dispatch/quota boundaries. No user data, real browser actions, attachment files, provider network, public task or execution graph.'}
    try:
        asyncio.run(verify(args.output_dir, report))
    except Exception as error:
        import traceback
        report['error_type'] = type(error).__name__
        report['error_locations'] = [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                     'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]
    report['artifact_sha256'] = {str(path.relative_to(args.output_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(args.output_dir.rglob('*')) if path.is_file()}
    path = args.output_dir / 'report.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(path)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
