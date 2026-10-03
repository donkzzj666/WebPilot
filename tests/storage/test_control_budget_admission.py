"""A committed user request stops new admissions, preserving already spent attempts."""
import pytest

from webagent.controls.models import ControlPending
from webagent.db import connect, transaction
from webagent.db.repository import canonical_json, utc_text
from webagent.events import OperationRequestedEvent, append_event
from storage.test_budgets import setup


def request_pause(path, token):
    """Seed a valid durable request without unrelated configuration/provider I/O."""
    with connect(path) as db, transaction(db):
        run = db.execute('SELECT task_id,contract_version FROM runs WHERE run_id=?',
                         (token.run_id,)).fetchone()
        event = append_event(db, run_id=token.run_id, expected_state_version=token.state_version,
                             payload=OperationRequestedEvent(operation_id='control-pause', action='pause'))
        db.execute('''INSERT INTO run_controls(operation_id,task_id,run_id,action,
            requested_state_version,accepted_run_state_version,contract_version,settings_version,
            request_scope,idempotency_key,request_sha256,accepted_json,requested_event_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('control-pause', run['task_id'], token.run_id, 'pause', token.state_version,
             token.state_version, run['contract_version'], 0, 'pause-test', 'pause-key', 'a'*64,
             canonical_json({'operation_id':'control-pause','status':'PENDING'}), event['event_id'], utc_text()))


@pytest.mark.parametrize('kind',['action','observation','screenshot','ci_poll','recovery'])
def test_pending_control_stops_new_admission_without_charging(database,kind):
    _, budgets, scheduler, _, token = setup(database)
    request_pause(database,token)
    before = budgets.status(token.run_id)
    extra = {'site_id':'local-fixture','subgoal':'read','obstacle_type':'locator_changed'} if kind=='recovery' else {}
    with pytest.raises(ControlPending) as caught:
        budgets.consume(token,kind=kind,attempt_id='after-request',**extra)
    assert caught.value.operation_id == 'control-pause'
    assert caught.value.action == 'pause'
    # Request acceptance does not fence the already-running owner; only a
    # boundary completion may revoke it after recording the in-flight result.
    assert scheduler.validate(token)['run_state_version'] == token.state_version
    after = budgets.status(token.run_id)
    for field in ('actions_used','observations_used','screenshots_used','content_pages_used','recovery_counts'):
        assert after[field] == before[field]
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM budget_attempts').fetchone()[0] == 0


@pytest.mark.parametrize('kind',['action','observation','screenshot','ci_poll','recovery'])
def test_existing_admission_remains_charged_and_cannot_redispatch(database,kind):
    _, budgets, _, _, token = setup(database)
    extra = {'site_id':'local-fixture','subgoal':'read','obstacle_type':'locator_changed'} if kind=='recovery' else {}
    original = budgets.consume(token,kind=kind,attempt_id='already-admitted',**extra)
    request_pause(database,token)
    duplicate = budgets.consume(token,kind=kind,attempt_id='already-admitted',**extra)
    assert original['charged'] and original['dispatch_allowed']
    assert duplicate['duplicate'] and not duplicate['charged'] and not duplicate['dispatch_allowed']
    for field in ('actions_used','observations_used','screenshots_used','content_pages_used','recovery_counts'):
        assert duplicate[field] == original[field]
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM budget_attempts').fetchone()[0] == 1
