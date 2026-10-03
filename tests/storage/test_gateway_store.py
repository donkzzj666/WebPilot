"""Gateway journal uses actual SQLite, scheduler leases and frozen contracts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import shutil
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from webagent.budgets.store import BudgetStore
from webagent.db import connect, migrate, transaction
from webagent.db import migrations
from webagent.db.repository import add_contract, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.gateway.store import GatewayStore
from webagent.identities.store import IdentityStore
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry
from webagent.tasks.compiler import compile_draft
from storage.conftest import seed


class Clock:
    domain = 'gateway-journal-fixture'

    def __init__(self):
        self.wall, self.ns = datetime.now(timezone.utc), 0

    def utcnow(self):
        return self.wall

    def monotonic_ns(self):
        return self.ns

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.ns += round(seconds * 1_000_000_000)


def verified_identity(path):
    store, registry = IdentityStore(path), SessionRegistry(path)
    login = store.create(site_id='local-fixture', realm='public', origin='http://127.0.0.1:8765',
                         expected_account='fixture-user')
    context = registry.reserve('identity-manager', SessionOwner('login', login.login_id, 'local-fixture'))
    registry.opened(context.session_id, 'identity-manager')
    login = store.attach_session(login.login_id, login.state_version, context.session_id, 'identity-manager')
    login = store.begin_confirm(login.login_id, login.state_version)
    login = store.identity_candidate(login.login_id, login.state_version, 'fixture-user')
    verified = store.finalize_verified(login.login_id, login.state_version,
        identity_ref=login.candidate_identity_ref, normalized_account='fixture-user', auth_ref=str(uuid4()),
        auth_sha256='a' * 64, verification_origin=login.origin, adapter_id='gateway-fixture',
        evidence_sha256='b' * 64)
    registry.closing(context.session_id, 'identity-manager')
    registry.closed(context.session_id, 'identity-manager')
    return verified.identity_ref


def fixture(path, *, write=False, limits=None, extra_source=False):
    clock = Clock()
    identity = verified_identity(path) if write else None
    policy = {'mode': 'repository_write', 'repository': 'fixture/project', 'base_branch': 'main',
              'branch': 'repair', 'base_sha': 'a' * 40, 'task_kind': 'ordinary_repair',
              'allowed_files': ['src/app.py'], 'workflow_exception_files': [],
              'protected_patterns': ['tests/*', '.github/*'], 'required_checks': ['fixture-check'],
              'independent_rules_ref': 'fixture-rules',
              'allowed_operations': ['edit_file', 'commit']}
    parameters = ({'operation_kind': 'code_repair', 'repository': 'fixture/project',
                   'base_sha': 'a' * 40, 'branch': 'repair', 'failure_run_id': 'fixture-ci',
                   'required_checks': ['fixture-check'], 'independent_rules_ref': 'fixture-rules'}
                  if write else {'queries': ['fixture'], 'topic_criteria': ['fixture'],
                                 'cutoff_at': utc_text(clock.utcnow()), 'max_items': 3})
    draft = {'instruction': 'Synthetic structured browser journal fixture',
             'scenario': 'operations' if write else 'research', 'source_ids': ['local-fixture'],
             'parameters': parameters, 'identity_ref': identity}
    if write:
        draft['action_policy'] = policy
    contract = compile_draft(draft, task_id='task-gateway', version=1, created_at=utc_text(clock.utcnow()),
        provenance=[{'origin': 'api', 'reference': 'gateway-test', 'content_sha256': 'c' * 64,
                     'authorizes_execution': True}]).contract
    contract['budget_profile'].update(limits or {})
    if extra_source:
        contract['sources'].append({'source_id': 'other-source', 'site_id': 'other-site',
                                    'origin': 'https://other.example', 'path_prefix': '/'})
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'],
                    requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id='run-gateway', task_id=contract['task_id'], contract_version=1,
                   graph_version='graph-v1', graph_state_schema_version='state-v1',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
    budgets = BudgetStore(path, clock=clock)
    scheduler = SchedulerStore(path, budgets=budgets, clock=clock.utcnow)
    generation = scheduler.start_worker('worker-gateway')
    resources = [Resource.site_identity('local-fixture', identity), Resource.browser_context('run-gateway')]
    if write:
        resources.append(Resource.repository_write('fixture/project'))
    scheduler.enqueue('run-gateway', resources, expected_state_version=0)
    token = scheduler.claim('worker-gateway', generation)
    registry = SessionRegistry(path)
    session = registry.reserve('manager-gateway', SessionOwner('run', token.run_id, 'local-fixture', identity),
                               execution_token=token)
    registry.opened(session.session_id, 'manager-gateway', execution_token=token)
    binding = {'session_id': session.session_id, 'manager_id': 'manager-gateway', 'session_generation': 1,
               'tab_id': 'tab-gateway', 'frame_id': 'frame-gateway', 'page_version': 'page-v1',
               'width': 800, 'height': 600}
    return SimpleNamespace(path=path, clock=clock, budgets=budgets, scheduler=scheduler,
        token=token, registry=registry, session=session, binding=binding,
        store=GatewayStore(path, budgets), contract=contract)


def observe(f, suffix='1', *, shot=False, attempt_id=None, binding=None):
    capture = {'snapshot_id': 'snapshot-' + suffix, 'source_url': f.contract['start_urls'][0],
               'title': 'SECRET raw title', 'visible_excerpt': 'SECRET raw page text'}
    if shot:
        capture.update(screenshot_sha256='d' * 64, screenshot_evidence_id='shot-' + suffix)
    return f.store.record_observation(f.token, binding or f.binding, capture,
                                      attempt_id=attempt_id or ('capture-' + suffix))


def action(f, *, step='step-1', snapshot='snapshot-1', kind='click', coordinate=False, write=False):
    scope = ({'repository': 'fixture/project', 'branch': 'repair', 'base_sha': 'a' * 40,
              'operation': 'edit_file', 'files': ['src/app.py'], 'operation_id': 'operation-' + step,
              'identity_ref': f.contract['identity_ref'], 'target_rechecked_at': utc_text(f.clock.utcnow())}
             if write else None)
    locator = ({'strategy': 'coordinate', 'screenshot_evidence_id': 'shot-' + snapshot.removeprefix('snapshot-'),
                'snapshot_id': snapshot, 'tab_id': f.binding['tab_id'], 'frame_id': f.binding['frame_id'],
                'width': 800, 'height': 600, 'x': 100, 'y': 120} if coordinate else
               {'strategy': 'semantic', 'role': 'button', 'accessible_name': 'Inspect fixture', 'label': None})
    args = ({'text': 'synthetic SECRET input'} if kind == 'input' else
            {'url': f.contract['start_urls'][0]} if kind == 'navigate' else
            {'key': 'Enter'} if kind == 'keypress' else
            {'option_label': 'Fixture'} if kind == 'select' else
            {'direction': 'down', 'pixels': 300} if kind == 'scroll' else
            {'tab_id': 'tab-second'} if kind == 'switch_tab' else
            {'attachment_url': 'http://127.0.0.1:8765/fixture.pdf', 'link_evidence_id': 'fixture-link'}
                if kind == 'download_attachment' else {})
    return {'run_id': f.token.run_id, 'step_id': step, 'epoch': f.token.epoch, 'snapshot_id': snapshot,
            'action_type': kind, 'expected_effect': 'write' if write else 'read',
            'target': {'page_url': f.contract['start_urls'][0], 'tab_id': f.binding['tab_id'],
                       'frame_id': f.binding['frame_id'], 'locator': locator, 'write_scope': scope}, 'args': args}


def counts(path):
    with connect(path) as db:
        return {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('observations', 'gateway_observations', 'gateway_attempts', 'steps',
                              'write_intents', 'budget_attempts', 'resource_quarantines')}


def test_observation_binds_generation_epoch_viewport_and_screenshot_without_raw_content(database):
    f = fixture(database)
    snapshot = observe(f, shot=True)
    assert all(snapshot[key] == f.binding[key] for key in f.binding)
    assert snapshot['run_id'] == f.token.run_id and snapshot['epoch'] == f.token.epoch
    assert snapshot['screenshot_sha256'] == 'd' * 64 and snapshot['screenshot_evidence_id'] == 'shot-1'
    assert snapshot['title'] == '[worker observation]' and snapshot['visible_excerpt'] == ''
    assert snapshot['redaction_status'] == 'BLOCKED'
    status = f.budgets.status(f.token.run_id)
    assert status['observations_used'] == status['screenshots_used'] == 1
    assert status['actions_used'] == 0
    with connect(database) as db:
        assert 'SECRET' not in '\n'.join(db.iterdump())


@pytest.mark.parametrize('kind', ['navigate', 'click', 'input', 'keypress', 'select', 'scroll',
                                 'switch_tab', 'read_visible', 'screenshot', 'download_attachment'])
def test_each_allowlisted_action_has_intent_before_dispatch_and_safe_terminal_audit(database, kind):
    f = fixture(database)
    observe(f)
    prepared = f.store.prepare(f.token, action(f, kind=kind), f.binding)
    assert prepared['dispatch_allowed'] and prepared['status'] == 'INTENT'
    assert counts(database)['gateway_attempts'] == counts(database)['steps'] == 1
    status = f.budgets.status(f.token.run_id)
    assert status['actions_used'] == int(kind not in ('read_visible', 'screenshot'))
    assert status['content_pages_used'] == int(kind in ('navigate', 'click'))
    completed = f.store.finish(f.token, 'step-1', result={'private': 'SECRET browser result'})
    assert completed['status'] == 'COMPLETED' and completed['result_accepted']
    assert completed['actual_result'] == {'result_sha256': hashlib.sha256(
        b'{"private":"SECRET browser result"}').hexdigest()}
    with connect(database) as db:
        dump = '\n'.join(db.iterdump())
        assert 'SECRET' not in dump
        event = db.execute("SELECT payload_json FROM task_events WHERE event_type='action_recorded' ORDER BY event_id DESC").fetchone()
        assert json.loads(event[0])['attempt_status'] == 'COMPLETED'


def test_duplicate_step_and_changed_payload_never_reserve_or_authorize_another_dispatch(database):
    f = fixture(database)
    observe(f)
    request = action(f)
    f.store.prepare(f.token, request, f.binding)
    before = counts(database)
    replay = f.store.prepare(f.token, request, f.binding)
    assert replay['duplicate'] and not replay['dispatch_allowed'] and counts(database) == before
    changed = action(f, kind='input')
    with pytest.raises(BusinessError, match='another atomic action'):
        f.store.prepare(f.token, changed, f.binding)
    assert counts(database) == before and f.budgets.status(f.token.run_id)['actions_used'] == 1


def test_concurrent_same_step_returns_exactly_one_dispatch_authorization(database):
    f = fixture(database)
    observe(f)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: f.store.prepare(f.token, action(f), f.binding), range(2)))
    assert sum(result['dispatch_allowed'] for result in results) == 1
    assert f.budgets.status(f.token.run_id)['actions_used'] == 1


@pytest.mark.parametrize('field,value', [('tab_id', 'other-tab'), ('frame_id', 'other-frame'),
                                      ('page_version', 'page-v2'), ('width', 801), ('height', 601),
                                      ('manager_id', 'other-manager'), ('session_generation', 2)])
def test_changed_browser_binding_rejects_old_observation_before_budget_and_intent(database, field, value):
    f = fixture(database)
    observe(f, shot=True)
    binding = f.binding | {field: value}
    request = action(f, coordinate=True)
    if field in ('tab_id', 'frame_id'):
        request['target'][field] = value
        request['target']['locator'][field] = value
    before = counts(database)
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, request, binding)
    assert counts(database) == before and f.budgets.status(f.token.run_id)['actions_used'] == 0


@pytest.mark.parametrize('boundary', ['epoch', 'worker', 'generation', 'expired', 'human', 'pause', 'lost'])
def test_stale_execution_control_or_session_cannot_create_action_intent(database, boundary):
    f = fixture(database)
    observe(f)
    token = f.token
    if boundary == 'epoch': token = replace(token, epoch=token.epoch + 1)
    elif boundary == 'worker': token = replace(token, worker_id='other-worker')
    elif boundary == 'generation': token = replace(token, worker_generation=token.worker_generation + 1)
    elif boundary == 'expired': f.clock.advance(31)
    elif boundary == 'pause': f.scheduler.defer(token, 'PAUSED')
    elif boundary == 'lost': f.registry.lost(f.session.session_id, 'manager-gateway', 'page_crashed')
    else:
        with connect(database) as db, transaction(db):
            db.execute("UPDATE resource_leases SET control_owner='human' WHERE holder_run_id=? AND resource_type='site_identity'",
                       (token.run_id,))
    before = counts(database)
    with pytest.raises(BusinessError):
        f.store.prepare(token, action(f), f.binding)
    assert counts(database) == before and f.budgets.status(f.token.run_id)['actions_used'] == 0


def test_current_head_and_consumed_snapshot_require_new_observation_for_every_atomic_action(database):
    f = fixture(database)
    observe(f)
    observe(f, '2')
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, action(f), f.binding)
    f.store.prepare(f.token, action(f, snapshot='snapshot-2'), f.binding)
    f.store.finish(f.token, 'step-1')
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, action(f, step='step-2', snapshot='snapshot-2'), f.binding)
    observe(f, '3')
    f.clock.advance(3)
    assert f.store.prepare(f.token, action(f, step='step-2', snapshot='snapshot-3'), f.binding)['dispatch_allowed']
    assert f.budgets.status(f.token.run_id)['actions_used'] == 2


@pytest.mark.parametrize('damage', ['missing_shot', 'wrong_shot', 'viewport', 'out_of_bounds'])
def test_coordinates_require_current_screenshot_and_exact_bounds(database, damage):
    f = fixture(database)
    observe(f, shot=damage != 'missing_shot')
    request = action(f, coordinate=True)
    locator = request['target']['locator']
    if damage == 'wrong_shot': locator['screenshot_evidence_id'] = 'unrelated-shot'
    elif damage == 'viewport': locator['width'] = 801
    elif damage == 'out_of_bounds': locator['x'] = 800
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, request, f.binding)
    assert counts(database)['steps'] == 0 and f.budgets.status(f.token.run_id)['actions_used'] == 0


def test_coordinate_dispatch_counts_final_pixel_check_and_readonly_queries_do_not_charge_again(database):
    f = fixture(database)
    observe(f, shot=True)
    request = action(f, coordinate=True)
    assert f.budgets.status(f.token.run_id)['screenshots_used'] == 1
    f.store.prepare(f.token, request, f.binding)
    status = f.budgets.status(f.token.run_id)
    assert status['screenshots_used'] == 2 and status['actions_used'] == 1
    for _ in range(3):
        f.store.get_observation(f.token.run_id, 'snapshot-1')
        f.store.get_attempt(f.token.run_id, 'step-1')
        f.store.list_attempts(f.token.run_id)
    duplicate = f.store.prepare(f.token, request, f.binding)
    assert duplicate['duplicate'] and not duplicate['dispatch_allowed']
    assert f.budgets.status(f.token.run_id)['screenshots_used'] == 2
    assert f.budgets.status(f.token.run_id)['actions_used'] == 1


def test_coordinate_intent_fault_rolls_back_action_and_final_screenshot_reservation_together(database):
    f = fixture(database)
    observe(f, shot=True)
    with connect(database) as db:
        db.execute("CREATE TRIGGER injected_coordinate_failure BEFORE INSERT ON gateway_attempts BEGIN SELECT RAISE(ABORT,'injected coordinate audit failure'); END")
    before = counts(database)
    with pytest.raises(sqlite3.IntegrityError, match='injected coordinate audit failure'):
        f.store.prepare(f.token, action(f, coordinate=True), f.binding)
    assert counts(database) == before
    status = f.budgets.status(f.token.run_id)
    assert status['actions_used'] == 0 and status['screenshots_used'] == 1
    with connect(database) as db:
        assert db.execute('SELECT valid FROM gateway_page_heads').fetchone()[0] == 1
        db.execute('DROP TRIGGER injected_coordinate_failure')
    assert f.store.prepare(f.token, action(f, coordinate=True), f.binding)['dispatch_allowed']
    assert f.budgets.status(f.token.run_id)['screenshots_used'] == 2


def test_coordinate_second_charge_time_stop_keeps_no_partial_dispatch_reservation(database, monkeypatch):
    f = fixture(database, limits={'max_active_seconds': 1})
    observe(f, shot=True)
    consume = f.budgets.consume_in_transaction

    def cross_deadline(db, token, **metadata):
        if metadata['attempt_id'].startswith('gateway-final-shot-'):
            f.clock.advance(1)
        return consume(db, token, **metadata)

    monkeypatch.setattr(f.budgets, 'consume_in_transaction', cross_deadline)
    before = counts(database)
    with pytest.raises(BusinessError) as denied:
        f.store.prepare(f.token, action(f, coordinate=True), f.binding)
    assert denied.value.code == 'BUDGET_EXCEEDED' and denied.value.field == 'active_time'
    assert counts(database) == before
    status = f.budgets.status(f.token.run_id)
    assert status['reason'] == 'active_time' and status['exhausted']
    assert status['active_ms'] == 1000
    assert status['actions_used'] == 0 and status['screenshots_used'] == 1


def test_intent_insertion_failure_rolls_back_budget_attempt_counter_and_snapshot_consumption(database):
    f = fixture(database)
    observe(f)
    with connect(database) as db:
        db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON gateway_attempts BEGIN SELECT RAISE(ABORT,'injected audit failure'); END")
    before = counts(database)
    with pytest.raises(sqlite3.IntegrityError, match='injected audit failure'):
        f.store.prepare(f.token, action(f), f.binding)
    assert counts(database) == before and f.budgets.status(f.token.run_id)['actions_used'] == 0
    with connect(database) as db:
        assert db.execute('SELECT valid FROM gateway_page_heads').fetchone()[0] == 1
        db.execute('DROP TRIGGER injected_failure')
    assert f.store.prepare(f.token, action(f), f.binding)['dispatch_allowed']


def test_budget_limit_commits_stop_without_partial_intent_or_attempt(database):
    f = fixture(database, limits={'max_actions': 1})
    observe(f)
    f.store.prepare(f.token, action(f), f.binding)
    f.store.finish(f.token, 'step-1')
    observe(f, '2')
    before = counts(database)
    with pytest.raises(BusinessError) as rejected:
        f.store.prepare(f.token, action(f, step='step-2', snapshot='snapshot-2'), f.binding)
    assert rejected.value.code == 'BUDGET_EXCEEDED' and rejected.value.field == 'action_limit'
    assert counts(database) == before
    assert f.budgets.status(f.token.run_id)['reason'] == 'action_limit'


def test_ten_fresh_atomic_steps_count_ten_without_input_or_result_plaintext(database):
    f = fixture(database)
    for index in range(10):
        observe(f, str(index))
        step = 'step-' + str(index)
        f.store.prepare(f.token, action(f, step=step, snapshot='snapshot-' + str(index), kind='input'), f.binding)
        f.store.finish(f.token, step)
    assert f.budgets.status(f.token.run_id)['actions_used'] == 10
    attempts = f.store.list_attempts(f.token.run_id)
    assert [row['sequence'] for row in attempts] == list(range(1, 11))
    assert len(attempts) == 10


@pytest.mark.parametrize('outcome', ['COMPLETED', 'FAILED', 'UNKNOWN'])
def test_external_write_never_claims_business_success_and_preserves_quarantine(database, outcome):
    f = fixture(database, write=True)
    observe(f)
    prepared = f.store.prepare(f.token, action(f, kind='input', write=True), f.binding, external_write=True)
    assert prepared['status'] == 'INTENT' and prepared['operation_id'] == 'operation-step-1'
    with connect(database) as db:
        assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'INTENT'
    completed = f.store.finish(f.token, 'step-1', outcome=outcome, result={'receipt': 'SECRET fake receipt'})
    assert completed['status'] == 'UNKNOWN' and not completed['result_accepted']
    assert completed['actual_result'] is None
    with connect(database) as db:
        intent = db.execute('SELECT * FROM write_intents').fetchone()
        assert intent['status'] == 'UNKNOWN' and intent['receipt'] is None
        quarantined = {row[0] for row in db.execute('SELECT resource_key FROM resource_quarantines')}
        assert Resource.repository_write('fixture/project').resource_key in quarantined
        assert Resource.site_identity('local-fixture', f.contract['identity_ref']).resource_key in quarantined
        assert 'SECRET' not in '\n'.join(db.iterdump())
    replay = f.store.prepare(f.token, action(f, kind='input', write=True), f.binding, external_write=True)
    assert replay['duplicate'] and not replay['dispatch_allowed']
    observe(f, '2')  # Recovery observation is allowed without releasing UNKNOWN.
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, action(f, step='step-2', snapshot='snapshot-2'), f.binding)
    assert f.budgets.status(f.token.run_id)['actions_used'] == 1


def own_dispatch(f, *, token=None, step_id='step-1', operation_id='operation-step-1', session_id=None, owner=None):
    return f.registry.validate_gateway_dispatch(session_id or f.session.session_id, owner or f.session.owner,
        execution_token=token or f.token, step_id=step_id, operation_id=operation_id)


def write_intent(f):
    observe(f)
    return f.store.prepare(f.token, action(f, kind='input', write=True), f.binding, external_write=True)


def test_only_current_journaled_write_intent_can_pass_the_private_dispatch_validator(database):
    f = fixture(database, write=True)
    write_intent(f)
    before, budget = counts(database), f.budgets.status(f.token.run_id)
    with pytest.raises(BusinessError):
        f.registry.validate_execution(f.session.owner, f.token)
    assert own_dispatch(f) == f.token
    assert own_dispatch(f) == f.token  # Validation itself creates no reusable permission.
    assert counts(database) == before and f.budgets.status(f.token.run_id) == budget


@pytest.mark.parametrize('boundary', ['epoch', 'worker', 'generation', 'state_version', 'run', 'resources'])
def test_current_intent_exception_does_not_refresh_or_accept_old_execution_bindings(database, boundary):
    f = fixture(database, write=True)
    write_intent(f)
    changes = {'epoch': {'epoch': f.token.epoch + 1}, 'worker': {'worker_id': 'foreign-worker'},
               'generation': {'worker_generation': f.token.worker_generation + 1},
               'state_version': {'state_version': f.token.state_version + 1},
               'run': {'run_id': 'foreign-run'}, 'resources': {'resources': f.token.resources[:-1]}}
    before = counts(database)
    with pytest.raises(BusinessError):
        own_dispatch(f, token=replace(f.token, **changes[boundary]))
    assert counts(database) == before


@pytest.mark.parametrize('boundary', ['unknown', 'other_intent', 'other_unknown', 'pause', 'human',
                                      'reconciling', 'quarantine', 'budget_stop', 'completed', 'session_lost',
                                      'identity_expired', 'other_pending_step', 'reservation_missing',
                                      'reservation_session', 'reservation_epoch', 'reservation_worker',
                                      'reservation_generation'])
def test_current_intent_exception_rejects_unresolved_other_work_or_revoked_control(database, boundary):
    f = fixture(database, write=True)
    write_intent(f)
    token = f.token
    if boundary == 'unknown':
        f.store.finish(token, 'step-1', outcome='UNKNOWN')
    elif boundary in ('other_intent', 'other_unknown'):
        with connect(database) as db, transaction(db):
            now = utc_text(f.clock.utcnow())
            db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
                target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
                VALUES('other-operation','other-business',?,?,?,?,?,?,?,?,?)''',
                (f.contract['task_id'], token.run_id, Resource.repository_write('fixture/project').resource_key,
                 'commit', f.contract['identity_ref'], 'page-v1',
                 'UNKNOWN' if boundary == 'other_unknown' else 'INTENT', now, now))
    elif boundary == 'pause':
        f.scheduler.defer(token, 'PAUSED')
    elif boundary == 'human':
        with connect(database) as db, transaction(db):
            db.execute("UPDATE resource_leases SET control_owner='human' WHERE holder_run_id=? AND resource_type='site_identity'",
                       (token.run_id,))
    elif boundary == 'reconciling':
        from webagent.state import transition_in_transaction
        with connect(database) as db, transaction(db):
            transition_in_transaction(db, run_id=token.run_id, expected_state_version=token.state_version,
                                      target='RECONCILING')
            row = f.scheduler._row(db, token.run_id)
            f.scheduler._change(db, row, utc_text(f.clock.utcnow()), 'reconciled',
                                run_state_version=row['run_state_version'])
        token = f.scheduler.refresh_qualification(token)
    elif boundary == 'quarantine':
        with connect(database) as db, transaction(db):
            db.execute('INSERT INTO resource_quarantines VALUES(?,?,?)',
                (Resource.repository_write('fixture/project').resource_key, 'operation-step-1', utc_text(f.clock.utcnow())))
    elif boundary == 'budget_stop':
        with connect(database) as db, transaction(db):
            f.budgets.stop_in_transaction(db, token.run_id, 'active_time')
    elif boundary == 'completed':
        # A step that has a terminal outcome cannot use this INTENT exception,
        # even if a separately persisted operation still remains INTENT.
        with connect(database) as db, transaction(db):
            db.execute("UPDATE steps SET status='FAILED',ended_at=? WHERE step_id='step-1'",
                       (utc_text(f.clock.utcnow()),))
    elif boundary == 'identity_expired':
        with connect(database) as db, transaction(db):
            db.execute("UPDATE identities SET state='NEEDS_LOGIN',state_version=state_version+1,updated_at=? WHERE identity_ref=?",
                       (utc_text(f.clock.utcnow()), f.contract['identity_ref']))
    elif boundary == 'other_pending_step':
        with connect(database) as db, transaction(db):
            db.execute('''INSERT INTO steps(step_id,run_id,sequence,step_kind,epoch,input_snapshot_id,
                action_json,started_at) VALUES('other-step',?,2,'atomic_action',?,'snapshot-1',
                '{"action_type":"scroll"}',?)''', (token.run_id, token.epoch, utc_text(f.clock.utcnow())))
            db.execute('''INSERT INTO gateway_attempts SELECT 'other-step',run_id,snapshot_id,'scroll',
                request_sha256,session_id,manager_id,session_generation,worker_id,worker_generation,
                epoch,state_version,0,NULL FROM gateway_attempts WHERE step_id='step-1' ''')
    elif boundary.startswith('reservation_'):
        with connect(database) as db, transaction(db):
            if boundary == 'reservation_missing':
                db.execute('DELETE FROM scheduler_context_reservations WHERE run_id=?', (token.run_id,))
            elif boundary == 'reservation_session':
                prior = dict(db.execute('SELECT * FROM scheduler_context_reservations WHERE run_id=?',
                                        (token.run_id,)).fetchone())
                db.execute('DELETE FROM scheduler_context_reservations WHERE run_id=?', (token.run_id,))
                db.execute('INSERT INTO scheduler_context_reservations VALUES(?,?,?,?,?,NULL,?)',
                           tuple(prior[key] for key in ('run_id', 'context_ordinal', 'worker_id',
                                                       'worker_generation', 'epoch', 'created_at')))
            else:
                field, value = {'reservation_epoch': ('epoch', token.epoch + 1),
                                'reservation_worker': ('worker_id', 'foreign-worker'),
                                'reservation_generation': ('worker_generation', token.worker_generation + 1)}[boundary]
                db.execute('UPDATE scheduler_context_reservations SET ' + field + '=? WHERE run_id=?',
                           (value, token.run_id))
    else:
        f.registry.lost(f.session.session_id, 'manager-gateway', 'page_crashed')
    before = counts(database)
    with pytest.raises(BusinessError):
        own_dispatch(f, token=token)
    assert counts(database) == before


@pytest.mark.parametrize('boundary', ['wrong_step', 'wrong_operation', 'wrong_owner', 'other_session_manager', 'read_step'])
def test_current_intent_exception_is_bound_to_exact_step_operation_and_managed_session(database, boundary):
    f = fixture(database, write=boundary != 'read_step')
    if boundary == 'read_step':
        observe(f)
        f.store.prepare(f.token, action(f, kind='scroll'), f.binding)
    else:
        write_intent(f)
    kwargs = {}
    if boundary == 'wrong_step': kwargs['step_id'] = 'other-step'
    elif boundary == 'wrong_operation': kwargs['operation_id'] = 'other-operation'
    elif boundary == 'wrong_owner': kwargs['owner'] = SessionOwner('run', f.token.run_id, 'foreign-site')
    elif boundary == 'other_session_manager':
        # Another live session cannot borrow a prior manager's action intent.
        with connect(database) as db, transaction(db):
            db.execute('''INSERT INTO browser_sessions(session_id,owner_kind,owner_id,run_id,site_id,identity_ref,
                realm,manager_id,state,generation,state_version,created_at)
                VALUES('foreign-context','run',?,?,?,?,?,'foreign-manager','OPENING',1,0,?)''',
                (f.token.run_id, f.token.run_id, f.session.owner.site_id, f.session.owner.identity_ref,
                 f.session.owner.realm, utc_text(f.clock.utcnow())))
        f.registry.opened('foreign-context', 'foreign-manager', execution_token=f.token)
        kwargs['session_id'] = 'foreign-context'
    before = counts(database)
    with pytest.raises(BusinessError):
        own_dispatch(f, **kwargs)
    assert counts(database) == before


@pytest.mark.parametrize('field,value', [('repository', 'other/project'), ('branch', 'main'),
                                      ('base_sha', 'b' * 40), ('operation', 'create_pr'),
                                      ('files', ['tests/acceptance.py']), ('identity_ref', 'other-identity'),
                                      ('target_rechecked_at', '2000-01-01T00:00:00.000000Z')])
def test_write_scope_must_match_frozen_repository_file_operation_and_fresh_identity(database, field, value):
    f = fixture(database, write=True)
    observe(f)
    request = action(f, write=True)
    request['target']['write_scope'][field] = value
    before = counts(database)
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, request, f.binding, external_write=True)
    assert counts(database) == before and f.budgets.status(f.token.run_id)['actions_used'] == 0


def test_read_only_policy_and_write_classification_cannot_be_bypassed(database):
    f = fixture(database)
    observe(f)
    request = action(f)
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, request, f.binding, external_write=True)
    assert counts(database)['steps'] == 0


@pytest.mark.parametrize('kind', ['navigate', 'download_attachment'])
def test_out_of_contract_target_denied_before_count_or_intent(database, kind):
    f = fixture(database)
    observe(f)
    request = action(f, kind=kind)
    request['args']['url' if kind == 'navigate' else 'attachment_url'] = 'https://outside.example/object'
    with pytest.raises(BusinessError) as rejected:
        f.store.prepare(f.token, request, f.binding)
    assert rejected.value.code == 'FORBIDDEN'
    assert f.budgets.status(f.token.run_id)['actions_used'] == 0


@pytest.mark.parametrize('boundary', ['pause', 'expired', 'session_lost', 'budget'])
def test_late_completion_discards_result_after_control_or_budget_loss(database, boundary):
    f = fixture(database, limits={'max_active_seconds': 10})
    observe(f)
    f.store.prepare(f.token, action(f), f.binding)
    if boundary == 'pause': f.scheduler.defer(f.token, 'PAUSED')
    elif boundary == 'expired': f.clock.advance(31)
    elif boundary == 'session_lost': f.registry.lost(f.session.session_id, 'manager-gateway', 'page_crashed')
    else: f.clock.advance(11)
    result = f.store.finish(f.token, 'step-1', result={'late': 'SECRET cannot deliver'})
    assert result['status'] == 'UNKNOWN' and not result['result_accepted'] and result['actual_result'] is None
    assert f.budgets.status(f.token.run_id)['actions_used'] == 1


def test_attempt_completion_is_terminal_and_wrong_cleanup_token_cannot_edit_history(database):
    f = fixture(database)
    observe(f)
    f.store.prepare(f.token, action(f), f.binding)
    with pytest.raises(BusinessError):
        f.store.finish(replace(f.token, epoch=f.token.epoch + 1), 'step-1')
    completed = f.store.finish(f.token, 'step-1', outcome='FAILED', error_code='ACTION_TIMEOUT')
    duplicate = f.store.finish(f.token, 'step-1', result={'changed': 'SECRET'})
    assert duplicate['status'] == completed['status'] == 'FAILED' and duplicate['duplicate']
    assert f.store.get_attempt(f.token.run_id, 'step-1')['actual_result'] is None


@pytest.mark.parametrize('table,statement', [
    ('gateway_observations', "UPDATE gateway_observations SET width=801"),
    ('gateway_observations', "DELETE FROM gateway_observations"),
    ('observations', "UPDATE observations SET title='changed'"),
    ('observations', "DELETE FROM observations"),
    ('gateway_attempts', "UPDATE gateway_attempts SET request_sha256='" + 'a' * 64 + "'"),
    ('gateway_attempts', "DELETE FROM gateway_attempts"),
    ('steps', "UPDATE steps SET epoch=2"),
    ('steps', "DELETE FROM steps"),
])
def test_gateway_bindings_and_completed_audit_are_immutable_in_sql(database, table, statement):
    f = fixture(database)
    observe(f)
    f.store.prepare(f.token, action(f), f.binding)
    f.store.finish(f.token, 'step-1')
    with connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)


def test_capture_attempt_identifier_cannot_record_two_snapshots_for_one_charge(database):
    f = fixture(database)
    observe(f, attempt_id='capture-one')
    with pytest.raises(BusinessError):
        observe(f, '2', attempt_id='capture-one')
    assert counts(database)['observations'] == 1
    assert f.budgets.status(f.token.run_id)['observations_used'] == 1


def test_blank_bootstrap_only_allows_explicit_contract_navigation(database):
    f = fixture(database)
    capture = {'snapshot_id': 'snapshot-1', 'source_url': 'about:blank'}
    f.store.record_observation(f.token, f.binding, capture, attempt_id='initial-capture')
    with pytest.raises(BusinessError):
        f.store.prepare(f.token, action(f), f.binding)
    request = action(f, kind='navigate')
    assert f.store.prepare(f.token, request, f.binding)['dispatch_allowed']
    assert f.budgets.status(f.token.run_id)['content_pages_used'] == 1


def test_source_permission_does_not_replace_missing_logical_site_lease(database):
    f = fixture(database, extra_source=True)
    observe(f)
    request = action(f, kind='navigate')
    request['args']['url'] = 'https://other.example/publication'
    with pytest.raises(BusinessError) as rejected:
        f.store.prepare(f.token, request, f.binding)
    assert rejected.value.code == 'FORBIDDEN'
    assert f.budgets.status(f.token.run_id)['actions_used'] == 0


def test_read_link_clicks_cannot_bypass_page_or_site_pacing_budget(database):
    f = fixture(database, limits={'max_content_pages': 1})
    observe(f)
    f.store.prepare(f.token, action(f), f.binding)
    f.store.finish(f.token, 'step-1')
    observe(f, '2')
    f.clock.advance(3)
    with pytest.raises(BusinessError) as rejected:
        f.store.prepare(f.token, action(f, step='step-2', snapshot='snapshot-2'), f.binding)
    assert rejected.value.code == 'BUDGET_EXCEEDED' and rejected.value.field == 'content_page_limit'
    assert counts(database)['steps'] == 1


def test_read_link_click_enforces_persistent_three_second_site_pacing(database):
    f = fixture(database)
    observe(f)
    f.store.prepare(f.token, action(f), f.binding)
    f.store.finish(f.token, 'step-1')
    observe(f, '2')
    request = action(f, step='step-2', snapshot='snapshot-2')
    with pytest.raises(BusinessError) as paced:
        f.store.prepare(f.token, request, f.binding)
    assert paced.value.code == 'SITE_THROTTLED'
    assert f.budgets.status(f.token.run_id)['actions_used'] == 1
    f.clock.advance(3)
    assert f.store.prepare(f.token, request, f.binding)['dispatch_allowed']


def test_identity_expired_after_observation_is_rechecked_in_intent_transaction(database):
    f = fixture(database, write=True)
    observe(f)
    with connect(database) as db, transaction(db):
        db.execute("UPDATE identities SET state='NEEDS_LOGIN',state_version=state_version+1,updated_at=? WHERE identity_ref=?",
                   (utc_text(f.clock.utcnow()), f.contract['identity_ref']))
    before = counts(database)
    with pytest.raises(BusinessError) as rejected:
        f.store.prepare(f.token, action(f, write=True), f.binding, external_write=True)
    assert rejected.value.code == 'FORBIDDEN' and counts(database) == before
    assert f.budgets.status(f.token.run_id)['actions_used'] == 0


@pytest.mark.parametrize('command', ['javascript', 'shell', 'http_request', 'sql', 'evaluate', 'refresh'])
def test_unstructured_commands_are_rejected_by_the_journal_allowlist(database, command):
    f = fixture(database)
    observe(f)
    request = action(f)
    request['action_type'] = command
    request['args'] = {'command': 'SECRET arbitrary command'}
    with pytest.raises(BusinessError) as rejected:
        f.store.prepare(f.token, request, f.binding)
    assert rejected.value.code == 'INVALID_PARAMETER'
    assert counts(database)['steps'] == 0 and f.budgets.status(f.token.run_id)['actions_used'] == 0


def test_readonly_inspection_cannot_mutate_head_counter_or_create_dispatch(database):
    f = fixture(database)
    observe(f)
    f.store.prepare(f.token, action(f), f.binding)
    f.store.finish(f.token, 'step-1')
    before, budget = counts(database), f.budgets.status(f.token.run_id)
    for _ in range(3):
        assert f.store.get_observation(f.token.run_id, 'snapshot-1')['redaction_status'] == 'BLOCKED'
        assert f.store.get_attempt(f.token.run_id, 'step-1')['status'] == 'COMPLETED'
        assert len(f.store.list_attempts(f.token.run_id)) == 1
    assert counts(database) == before and f.budgets.status(f.token.run_id) == budget
    with pytest.raises(BusinessError) as missing:
        f.store.get_attempt('other-run', 'step-1')
    assert missing.value.code == 'NOT_FOUND'


def test_v11_upgrade_preserves_history_and_failed_v12_upgrade_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=11)
    with connect(path) as db, transaction(db):
        seed(db)
        prior = [tuple(row) for row in db.execute('SELECT * FROM schema_migrations ORDER BY version')]
        prior_runs = [tuple(row) for row in db.execute('SELECT * FROM runs')]
        prior_contracts = [tuple(row) for row in db.execute('SELECT * FROM contracts')]
    sql = tmp_path / 'sql'
    shutil.copytree(migrations.SQL_DIR, sql)
    with (sql / '0012_gateway.sql').open('a') as stream:
        stream.write('INVALID GATEWAY SQL;\n')
    monkeypatch.setattr(migrations, 'SQL_DIR', sql)
    with pytest.raises(sqlite3.OperationalError):
        migrate(path)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 11
        assert [tuple(row) for row in db.execute('SELECT * FROM schema_migrations ORDER BY version')] == prior
        assert [tuple(row) for row in db.execute('SELECT * FROM runs')] == prior_runs
        assert [tuple(row) for row in db.execute('SELECT * FROM contracts')] == prior_contracts
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'gateway_%'").fetchone()
    monkeypatch.undo()
    assert migrate(path, target=12) == {'previous_version': 11, 'schema_version': 12, 'applied': 1}
    with connect(path) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM schema_migrations WHERE version<=11 ORDER BY version')] == prior
        assert [tuple(row) for row in db.execute('SELECT * FROM runs')] == prior_runs
        assert [tuple(row) for row in db.execute('SELECT * FROM contracts')] == prior_contracts
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
