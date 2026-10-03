"""A related Run may query a stopped writer, never bypass its unknown effects."""
from copy import deepcopy

import pytest

from webagent.controls.models import ControlRequest
from webagent.controls.store import ControlStore
from webagent.db import connect, transaction
from webagent.db.repository import add_contract, create_run, utc_text, canonical_json
from webagent.evidence.service import EvidenceService
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource
from webagent.scheduler.store import validate_in_transaction
from webagent.sessions.models import SessionOwner
from test_write_protocol import setup, register, facts
from test_gateway_store import observe, action
from test_run_controls import prepared, start, request, intent
from test_run_controls import Secrets


def stopped_writer(path, *, context='closed'):
    f = setup(path)
    register(f)
    f.protocol.mark_unknown(f.token, 'operation-1')
    f.scheduler.finish(f.token, 'CANCELLED')
    if context == 'closed':
        f.registry.closing_terminal(f.session.session_id, 'manager-gateway', f.session.owner)
        f.registry.closed(f.session.session_id, 'manager-gateway')
    elif context == 'lost':
        f.registry.lost(f.session.session_id, 'manager-gateway', 'browser_disconnected')
    return f


def new_run(f, *, identifier='run-check', version=1):
    with connect(f.path) as db, transaction(db):
        create_run(db, run_id=identifier, task_id=f.contract['task_id'], contract_version=version,
            parent_run_id=f.token.run_id, graph_version='graph-v1', graph_state_schema_version='state-v1',
            model_config_sha256='a'*64, runtime_config_sha256='b'*64)
    resources = [Resource.site_identity('local-fixture', f.contract['identity_ref']),
                 Resource.repository_write('fixture/project'), Resource.browser_context(identifier)]
    return identifier, resources


@pytest.mark.parametrize('context', ['closed', 'lost'])
def test_only_explicit_query_run_can_transfer_terminal_writer_scopes(database, context):
    f = stopped_writer(database, context=context)
    before = f.budgets.status(f.token.run_id)
    run_id, resources = new_run(f)
    f.scheduler.enqueue_write_reconciliation(run_id, 0, resources, source_run_id=f.token.run_id)
    token = f.scheduler.claim(f.token.worker_id, f.token.worker_generation)
    assert token is not None and token.run_id == run_id
    with connect(database) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] == 'RECONCILING'
        validate_in_transaction(db, token, now=f.clock.utcnow(), allow_reconciling=True)
        with pytest.raises(BusinessError):
            validate_in_transaction(db, token, now=f.clock.utcnow())
        assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2
        assert db.execute('SELECT status FROM write_intents').fetchone()[0] == 'UNKNOWN'
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (f.token.run_id,)).fetchone()[0] == 'CANCELLED'
    for name in ('actions_used','model_calls_used','observations_used','recovery_counts'):
        assert f.budgets.status(f.token.run_id)[name] == before[name]
    assert f.budgets.status(run_id)['actions_used'] == 0


def test_normal_new_run_stays_queued_while_task_write_is_unknown(database):
    f = stopped_writer(database)
    run_id, resources = new_run(f)
    f.scheduler.enqueue(run_id, resources, expected_state_version=0)
    assert f.scheduler.claim(f.token.worker_id, f.token.worker_generation) is None
    with connect(database) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] == 'QUEUED'
        assert db.execute('SELECT count(*) FROM quota_debits').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM resource_leases WHERE holder_run_id=? AND resource_type IN ('site_identity','repository_write')",
                          (f.token.run_id,)).fetchone()[0] == 2


@pytest.mark.parametrize('boundary', ['live_context', 'human_control'])
def test_query_run_does_not_steal_live_browser_or_human_control(database, boundary):
    f = stopped_writer(database, context='open' if boundary == 'live_context' else 'closed')
    if boundary == 'human_control':
        with connect(database) as db, transaction(db):
            db.execute("UPDATE resource_leases SET control_owner='human',state_version=state_version+1 "
                       "WHERE holder_run_id=? AND resource_type='site_identity'", (f.token.run_id,))
    run_id, resources = new_run(f)
    f.scheduler.enqueue_write_reconciliation(run_id, 0, resources, source_run_id=f.token.run_id)
    assert f.scheduler.claim(f.token.worker_id, f.token.worker_generation) is None
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM resource_leases WHERE holder_run_id=? AND resource_type IN ('site_identity','repository_write')",
                          (f.token.run_id,)).fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM resource_leases WHERE holder_run_id=?', (run_id,)).fetchone()[0] == 0


def test_different_contract_cannot_opt_into_old_task_write_scope(database):
    f = stopped_writer(database)
    changed = deepcopy(f.contract)
    changed['contract_version'] = 2
    changed['objective'] += ' altered objective'
    with connect(database) as db, transaction(db):
        add_contract(db, changed)
    run_id, resources = new_run(f, version=2)
    with pytest.raises(BusinessError):
        f.scheduler.enqueue_write_reconciliation(run_id, 0, resources, source_run_id=f.token.run_id)
    with connect(database) as db:
        assert not db.execute('SELECT 1 FROM write_reconciliation_runs WHERE run_id=?', (run_id,)).fetchone()
    assert f.scheduler.claim(f.token.worker_id, f.token.worker_generation) is None


def test_raw_queued_run_cannot_gain_reconciling_state_without_query_relation(database):
    f = stopped_writer(database)
    run_id, _ = new_run(f)
    from webagent.state import transition_in_transaction
    with connect(database) as db, transaction(db), pytest.raises(Exception):
        transition_in_transaction(db, run_id=run_id, expected_state_version=0, target='RECONCILING')


def test_legacy_unknown_effect_cannot_claim_protocol_query_mode(tmp_path):
    case = prepared(tmp_path)
    token, _ = start(case)
    intent(case[0], token.run_id, status='UNKNOWN')
    request(case, token.run_id, 'cancel')
    case[4].apply_at_boundary(token)
    with connect(case[0]) as db:
        version = db.execute('SELECT state_version FROM runs WHERE run_id=?', (token.run_id,)).fetchone()[0]
    operation = case[4].request(case[1]['task_id'], 'retry', ControlRequest(expected_state_version=version,
        contract_version=1, settings_version=1), 'query-retry')['operation']
    assert case[4].apply_idle(operation['run_id'])['status'] == 'APPLIED'
    query = case[3].claim(token.worker_id, token.worker_generation)
    assert query is None
    with connect(case[0]) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (operation['run_id'],)).fetchone()[0] == 'QUEUED'
        assert db.execute('SELECT originating_run_id,recorded_status FROM run_retry_operations').fetchone()[:] == (token.run_id, 'UNKNOWN')


def test_control_retry_of_claimed_unknown_effect_is_only_query_mode(database):
    from pydantic import SecretStr
    from webagent.settings.models import ModelConnection, ModelSettingsRequest
    from webagent.settings.service import update_model
    f = stopped_writer(database)
    secret = Secrets()
    update_model(database, secret, ModelSettingsRequest(expected_version=0, model=ModelConnection(),
        accept_data_sharing=True, api_key=SecretStr('SYNTHETIC_CONTROL_KEY')))
    controls = ControlStore(database, secret_store=secret, scheduler=f.scheduler)
    with connect(database) as db, transaction(db):
        db.execute("UPDATE tasks SET preparation_status='READY',current_contract_version=1,"
                   "requested_fields_json='[]',current_run_id=? WHERE task_id=?",
                   (f.token.run_id, f.contract['task_id']))
    with connect(database) as db:
        version = db.execute('SELECT state_version FROM runs WHERE run_id=?', (f.token.run_id,)).fetchone()[0]
    operation = controls.request(f.contract['task_id'], 'retry', ControlRequest(expected_state_version=version,
        contract_version=1, settings_version=1), 'query-retry')['operation']
    assert controls.apply_idle(operation['run_id'])['status'] == 'APPLIED'
    f.clock.advance(1)  # Control acceptance uses real UTC; the scheduler fixture clock is frozen.
    query = f.scheduler.claim(f.token.worker_id, f.token.worker_generation)
    assert query is not None and query.run_id == operation['run_id']
    with connect(database) as db:
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (query.run_id,)).fetchone()[0] == 'RECONCILING'
        assert db.execute('SELECT originating_run_id,recorded_status FROM run_retry_operations').fetchone()[:] == (f.token.run_id, 'UNKNOWN')
        with pytest.raises(BusinessError):
            validate_in_transaction(db, query, now=f.clock.utcnow())


def retried_dispatch(f):
    original_run = f.token.run_id
    run_id, resources = new_run(f)
    f.scheduler.enqueue_write_reconciliation(run_id, 0, resources, source_run_id=original_run)
    f.token = f.scheduler.claim(f.token.worker_id, f.token.worker_generation)
    owner = SessionOwner('run', run_id, 'local-fixture', f.contract['identity_ref'])
    f.session = f.registry.reserve('manager-gateway', owner, execution_token=f.token)
    f.registry.opened(f.session.session_id, 'manager-gateway', execution_token=f.token)
    f.binding['session_id'] = f.session.session_id
    snapshot = observe(f, 'check')
    EvidenceService(f.path.parent).publish_observation(snapshot, {'title': 'Fixture', 'text': 'Definitely absent'},
                                                      execution_token=f.token)
    payload = facts(f, 'NOT_APPLIED', snapshot_id='snapshot-check')
    evidence = f.protocol.evidence.publish(run_id, canonical_json(payload).encode('utf-8'),
        source_url=f.contract['start_urls'][0], captured_at=f.clock.utcnow(), object_id='operation-1',
        query_scope='write state', locator_or_page='fixture-state', snapshot_id='snapshot-check',
        execution_token=f.token, retain=True)
    assert f.protocol.record_check(f.token, 'operation-1', 'retry-check', payload,
                                  [evidence['evidence_id']])['status'] == 'NOT_APPLIED'
    f.token = f.scheduler.reconcile(run_id, f.token.state_version)
    observe(f, 'retry')
    dispatched = f.store.prepare(f.token, action(f, step='retry-write', snapshot='snapshot-retry', kind='input',
        write=True), f.binding, external_write=True, write_claim=f.claim)
    assert dispatched['dispatch_allowed'] and dispatched['operation_id'] == 'operation-1'
    return original_run


@pytest.mark.parametrize('boundary', ['cancel', 'budget'])
def test_new_run_inflight_attempt_keeps_original_operation_unknown_on_revocation(database, boundary):
    f = stopped_writer(database)
    original_run = retried_dispatch(f)
    if boundary == 'cancel':
        controls = ControlStore(database, scheduler=f.scheduler)
        controls.request(f.token.run_id, 'cancel', ControlRequest(expected_state_version=f.token.state_version,
            contract_version=1, settings_version=0), 'cancel-retry')
        assert controls.apply_at_boundary(f.token)['status'] == 'APPLIED'
    else:
        with connect(database) as db, transaction(db):
            f.budgets.stop_in_transaction(db, f.token.run_id, 'active_time')
        f.scheduler.expire_budget(f.token.run_id, 'active_time')
    with connect(database) as db:
        assert db.execute('SELECT originating_run_id,status FROM write_intents').fetchone()[:] == (original_run, 'UNKNOWN')
        assert db.execute('SELECT status FROM steps WHERE step_id="retry-write"').fetchone()[0] == 'UNKNOWN'
        assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2
        assert db.execute('SELECT state FROM runs WHERE run_id=?', (original_run,)).fetchone()[0] == 'CANCELLED'
        assert db.execute('SELECT count(*) FROM write_protocol_dispatches').fetchone()[0] == 1
    assert f.budgets.status(original_run)['actions_used'] == 0
    assert f.budgets.status(f.token.run_id)['actions_used'] == 1
