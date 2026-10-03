"""Result-page reads preserve business history and never manufacture success."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from api_support import AuthenticatedTestClient, create_test_app
from webagent.config import Settings
from webagent.db import connect, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.evidence.service import POLICY_VERSION
from webagent.models.schema import ProposeResult
from webagent.results.store import results
from webagent.scheduler.models import Resource
from webagent.state import transition
from test_verification_service import prepared, verify, finalize
from unit.test_verification_rules import setup


def display_copy(case, *, data=b'Approved display copy', **override):
    original = case[4]
    kwargs = {key: original[key] for key in
              ('source_url', 'captured_at', 'object_id', 'locator_or_page', 'query_scope', 'artifact_kind', 'snapshot_id')}
    kwargs.update(sensitivity='redacted', original_evidence_id='e1', redaction_status='FILTERED',
                  policy_version=POLICY_VERSION, retain=True)
    kwargs.update(override)
    return case[0].evidence.publish('run-1', data, **kwargs)


def completed(tmp_path, *, assistance=0, display=True):
    case = prepared(tmp_path)
    if assistance:
        with connect(case[0].database) as db:
            db.execute('UPDATE runs SET assistance_count=? WHERE run_id=?', (assistance, 'run-1'))
    derivative = display_copy(case) if display else None
    original_result = finalize(case, verify(case)).model_dump(mode='json')
    return case, derivative, original_result


def ledger(path):
    with connect(path) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '"')]
                for name in tables}


def intent(path, *, operation='pending-operation', status='UNKNOWN', originating='run-1',
           identity='local-identity', task_id='task-1'):
    with connect(path) as db, transaction(db):
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,receipt,created_at,updated_at)
            VALUES(?,?,?,?,?,'create_pr',?,'before',?,?,?,?)''',
            (operation, operation, task_id, originating, Resource.repository_write('fixture/demo').resource_key,
             identity, status, 'receipt' if status == 'CONFIRMED' else None,
             utc_text(), utc_text()))


def another_run(path, identifier, *, parent='run-1', contract_version=1, task_id='task-1'):
    with connect(path) as db, transaction(db):
        create_run(db, run_id=identifier, task_id=task_id, contract_version=contract_version, parent_run_id=parent,
                   graph_version='v1', graph_state_schema_version='v1', model_config_sha256='a'*64,
                   runtime_config_sha256='b'*64)


def revised_code_run(path, *, identity='new-identity'):
    with connect(path) as db, transaction(db):
        contract = setup('code')[0].model_dump(mode='json')
        contract.update(contract_version=2, identity_ref=identity)
        add_contract(db, contract)
    another_run(path, 'rerun-1', contract_version=2)


@pytest.mark.parametrize('assistance', [0, 2])
def test_real_aggregated_result_and_fields_are_bound_with_present_readable_evidence(tmp_path, assistance):
    case, derivative, original = completed(tmp_path, assistance=assistance)
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1')
    assert view['result_status'] == 'AVAILABLE'
    assert view['result'] == original
    assert view['display_complete_success'] and view['display_blockers'] == []
    assert view['assistance'] == ('assisted' if assistance else 'autonomous')
    assert view['selected_run']['run_id'] == 'run-1'
    assert view['selected_run']['assistance_count'] == assistance
    assert view['field_checks'] == case[0].read('run-1')['field_checks']
    evidence = view['evidence'][0]
    assert evidence['evidence_id'] == 'e1' and evidence['run_id'] == 'run-1'
    assert evidence['display_evidence_id'] == derivative['evidence_id']
    assert evidence['display_sha256'] == hashlib.sha256(b'Approved display copy').hexdigest()
    assert evidence['display_size_bytes'] == len(b'Approved display copy')
    assert not any(key in json.dumps(view) for key in ('artifact_path', 'facts_json', 'credential_ref'))
    assert ledger(case[0].database) == before


@pytest.mark.parametrize('mutation,expected', [('missing', 'MISSING'), ('corrupt', 'CORRUPT'),
                                             ('expired', 'EXPIRED'), ('symlink', 'CORRUPT')])
@pytest.mark.parametrize('target', ['original', 'display'])
def test_current_artifact_failure_blocks_success_without_rewriting_historical_outcome(tmp_path, mutation, expected, target):
    case, derivative, original = completed(tmp_path)
    item = case[4] if target == 'original' else derivative
    path = tmp_path / item['artifact_path']
    if mutation == 'missing':
        path.unlink()
    elif mutation == 'corrupt':
        path.write_bytes(b'Changed after finalization')
    elif mutation == 'expired':
        case[0].evidence.expire(item['evidence_id'])
    else:
        path.unlink()
        path.symlink_to(tmp_path / 'business.sqlite3')
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1')
    assert view['result'] == original and view['result']['outcome'] == 'SUCCEEDED'
    assert not view['display_complete_success']
    assert view['evidence'][0]['display_status'] == expected
    assert not view['evidence'][0]['displayable']
    assert view['evidence'][0]['display_evidence_id'] is None
    assert ledger(case[0].database) == before


@pytest.mark.parametrize('kind', ['none', 'unsafe', 'foreign-object', 'foreign-source', 'future-policy'])
def test_only_an_approved_copy_bound_to_the_exact_capture_is_exposed(tmp_path, kind):
    case = prepared(tmp_path)
    if kind == 'unsafe': display_copy(case, data=b'Authorization: Bearer PRIVATE_CANARY_1234')
    if kind == 'foreign-object': display_copy(case, object_id='unrelated-object')
    if kind == 'foreign-source': display_copy(case, source_url='https://unrelated.example/report')
    if kind == 'future-policy': display_copy(case, policy_version='unsupported-policy')
    finalize(case, verify(case))
    view = results(tmp_path, 'task-1')
    assert view['result_status'] == 'AVAILABLE' and view['result']['outcome'] == 'SUCCEEDED'
    assert not view['display_complete_success'] and not view['evidence'][0]['displayable']
    assert view['evidence'][0]['display_status'] == 'BLOCKED'
    assert 'PRIVATE_CANARY' not in json.dumps(view)


def test_physical_read_budget_is_shared_and_never_assumes_unread_evidence_passes(tmp_path, monkeypatch):
    completed(tmp_path)
    monkeypatch.setattr('webagent.results.store.MAX_READ_BYTES', 1)
    view = results(tmp_path, 'task-1')
    assert view['result_status'] == 'AVAILABLE'
    assert not view['display_complete_success'] and view['evidence'][0]['availability'] == 'UNAVAILABLE'


def test_artifacts_larger_than_browser_read_limit_are_not_advertised_as_displayable(tmp_path, monkeypatch):
    completed(tmp_path)
    monkeypatch.setattr('webagent.results.store.MAX_DISPLAY_BYTES', 2)
    view = results(tmp_path, 'task-1')
    assert view['result_status'] == 'AVAILABLE' and view['evidence'][0]['availability'] == 'AVAILABLE'
    assert view['evidence'][0]['display_status'] == 'BLOCKED' and not view['display_complete_success']


def test_missing_storage_control_is_not_recreated_and_fault_latch_is_not_modified(tmp_path):
    case, _, _ = completed(tmp_path)
    marker = tmp_path / 'evidence/control/fault.marker'
    marker.unlink()
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1')
    assert not marker.exists() and view['evidence'][0]['availability'] == 'UNAVAILABLE'
    assert not view['display_complete_success'] and ledger(case[0].database) == before


def test_projection_opens_every_artifact_and_control_descriptor_without_write_flags(tmp_path, monkeypatch):
    case, _, _ = completed(tmp_path)
    original_open, opened = os.open, []
    def readonly_open(path, flags, *args, **kwargs):
        opened.append((path, flags))
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        return original_open(path, flags, *args, **kwargs)
    before = ledger(case[0].database)
    monkeypatch.setattr(os, 'open', readonly_open)
    assert results(tmp_path, 'task-1')['display_complete_success']
    assert any(path == 'fault.marker' for path, _ in opened)
    assert any(str(path).endswith('.blob') for path, _ in opened)
    assert ledger(case[0].database) == before


def test_latest_run_selection_and_history_pagination_preserve_all_attempts(tmp_path):
    case, _, _ = completed(tmp_path)
    for n in range(5): another_run(case[0].database, 'rerun-' + str(n))
    first = results(tmp_path, 'task-1', limit=2)
    assert first['selected_run']['run_id'] == 'rerun-4'
    assert first['result_status'] == 'NOT_READY' and not first['display_complete_success']
    assert [r['run_id'] for r in first['runs']] == ['rerun-4', 'rerun-3']
    assert type(first['next_cursor']) is str
    another_run(case[0].database, 'newer')
    cursor, old = first['next_cursor'], []
    while cursor:
        page = results(tmp_path, 'task-1', before=int(cursor), limit=2, run_id='run-1')
        assert page['selected_run']['run_id'] == 'run-1' and page['display_complete_success']
        old.extend(r['run_id'] for r in page['runs'])
        cursor = page['next_cursor']
    assert old == ['rerun-2', 'rerun-1', 'rerun-0', 'run-1']
    page = results(tmp_path, 'task-1', before=int(first['next_cursor']), limit=2)
    assert page['selected_run']['run_id'] == 'newer'


@pytest.mark.parametrize('status', ['INTENT', 'UNKNOWN'])
def test_task_wide_unresolved_write_cannot_hide_behind_other_run_selection(tmp_path, status):
    case, _, original = completed(tmp_path)
    another_run(case[0].database, 'rerun-1')
    intent(case[0].database, status=status, originating='rerun-1')
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1', run_id='run-1')
    assert view['result'] == original and view['result']['side_effects'] == []
    assert view['pending_write_count'] == 1 and not view['display_complete_success']
    assert view['write_intents'][0]['originating_run_id'] == 'rerun-1'
    assert view['write_intents'][0]['status'] == status
    assert not view['write_intents'][0]['recorded_in_selected_result']
    assert 'task_writes_pending' in view['display_blockers']
    assert ledger(case[0].database) == before


def test_truncated_write_history_is_explicit_and_pending_count_is_not_truncated(tmp_path):
    case, _, _ = completed(tmp_path)
    for i in range(102): intent(case[0].database, operation='operation-' + str(i))
    view = results(tmp_path, 'task-1')
    assert len(view['write_intents']) == 100 and view['pending_write_count'] == 102
    assert view['write_intents_truncated'] and not view['display_complete_success']


def test_cancelled_run_without_aggregation_is_not_a_success_or_fake_result(tmp_path):
    case, _, _ = completed(tmp_path)
    another_run(case[0].database, 'cancelled')
    transition(case[0].database, run_id='cancelled', expected_state_version=0, target='CANCELLED')
    view = results(tmp_path, 'task-1', run_id='cancelled')
    assert view['selected_run']['state'] == 'CANCELLED'
    assert view['result_status'] == 'NOT_READY' and view['result'] is None
    assert not view['display_complete_success'] and view['evidence'] == []


@pytest.mark.parametrize('unavailable_policy', [False, True])
def test_no_result_does_not_fabricate_a_critical_violation_for_an_authorized_write(tmp_path, unavailable_policy):
    case = prepared(tmp_path, scenario='code')
    with connect(case[0].database) as db, transaction(db):
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('authorized-operation','authorized-key','task-1','run-1',?,'create_pr',
            'test-identity','before','UNKNOWN',?,?)''',
            (Resource.repository_write('fixture/demo').resource_key, utc_text(), utc_text()))
    transition(case[0].database, run_id='run-1', expected_state_version=2, target='CANCELLED')
    if unavailable_policy:
        # Simulate damaged historical content after the real cancellation.
        with connect(case[0].database) as db:
            db.execute('DROP TRIGGER contracts_immutable')
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('UPDATE contracts SET contract_sha256=?', ('f' * 64,))
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1')
    assert view['selected_run']['state'] == 'CANCELLED'
    assert view['result_status'] == ('UNAVAILABLE' if unavailable_policy else 'NOT_READY')
    assert view['pending_write_count'] == 1 and view['result'] is None
    assert not view['write_intents'][0]['critical_violation']
    assert 'critical_side_effect' not in view['display_blockers']
    assert ('write_policy_unavailable' in view['display_blockers']) == unavailable_policy
    assert not view['display_complete_success'] and ledger(case[0].database) == before


@pytest.mark.parametrize('status', ['CONFIRMED', 'NOT_APPLIED', 'UNKNOWN'])
@pytest.mark.parametrize('identity,critical', [('test-identity', False), ('new-identity', True)])
def test_shared_write_ledger_uses_origin_contract_across_identity_revisions(tmp_path, status, identity, critical):
    case = prepared(tmp_path, scenario='code')
    revised_code_run(case[0].database)
    intent(case[0].database, status=status, identity=identity)
    before = ledger(case[0].database)
    for selected in ('run-1', 'rerun-1'):
        view = results(tmp_path, 'task-1', run_id=selected)
        assert view['result_status'] == 'NOT_READY' and view['result'] is None
        write = view['write_intents'][0]
        assert write['originating_run_id'] == 'run-1' and write['status'] == status
        assert write['critical_violation'] is critical
        assert ('critical_side_effect' in view['display_blockers']) is critical
        assert 'write_policy_unavailable' not in view['display_blockers']
        assert not view['display_complete_success']
        assert view['pending_write_count'] == int(status == 'UNKNOWN')
        assert ('task_writes_pending' in view['display_blockers']) == (status == 'UNKNOWN')
    assert ledger(case[0].database) == before


def test_selected_contract_unavailable_does_not_discard_valid_origin_authority(tmp_path):
    case = prepared(tmp_path, scenario='code')
    revised_code_run(case[0].database)
    intent(case[0].database, status='NOT_APPLIED', identity='test-identity')
    with connect(case[0].database) as db:
        db.execute('DROP TRIGGER contracts_immutable')
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('UPDATE contracts SET contract_sha256=? WHERE contract_version=2', ('f' * 64,))
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1', run_id='rerun-1')
    assert view['result_status'] == 'UNAVAILABLE' and view['result'] is None
    assert not view['write_intents'][0]['critical_violation']
    assert 'result_integrity_unavailable' in view['display_blockers']
    assert 'write_policy_unavailable' not in view['display_blockers']
    assert 'critical_side_effect' not in view['display_blockers']
    assert not view['display_complete_success'] and ledger(case[0].database) == before


@pytest.mark.parametrize('damage', ['missing-contract', 'contract-hash', 'run-hash',
                                  'contract-task', 'malformed-contract', 'missing-run', 'foreign-run'])
def test_unavailable_origin_contract_is_unknown_and_blocks_a_readable_selected_result(tmp_path, damage):
    case, _, original = completed(tmp_path)
    revised_code_run(case[0].database)
    intent(case[0].database, status='NOT_APPLIED', originating='rerun-1', identity='new-identity')
    if damage == 'foreign-run':
        with connect(case[0].database) as db, transaction(db):
            create_task(db, task_id='foreign-task', instruction='Foreign task', requested_fields=['contract'])
            contract = setup('code')[0].model_dump(mode='json')
            contract.update(task_id='foreign-task', identity_ref='new-identity')
            add_contract(db, contract)
        another_run(case[0].database, 'foreign-run', parent=None, task_id='foreign-task')
    # These fixtures deliberately bypass immutability/FK guards to model a
    # damaged historical binding, never a permitted runtime mutation.
    with connect(case[0].database) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        if damage == 'missing-contract':
            db.execute('DROP TRIGGER contracts_no_delete')
            db.execute('DELETE FROM contracts WHERE task_id=? AND contract_version=2', ('task-1',))
        elif damage == 'run-hash':
            db.execute('DROP TRIGGER runs_fixed_binding')
            db.execute('UPDATE runs SET contract_sha256=? WHERE run_id=?', ('f' * 64, 'rerun-1'))
        elif damage in ('missing-run', 'foreign-run'):
            db.execute('DROP TRIGGER write_intents_fixed_binding')
            db.execute('UPDATE write_intents SET originating_run_id=?', (damage,))
        else:
            db.execute('DROP TRIGGER contracts_immutable')
            if damage == 'contract-hash':
                db.execute('UPDATE contracts SET contract_sha256=? WHERE contract_version=2', ('f' * 64,))
            else:
                contract = json.loads(db.execute('SELECT content_json FROM contracts WHERE contract_version=2').fetchone()[0])
                if damage == 'contract-task':
                    db.execute('PRAGMA ignore_check_constraints=ON')
                    contract['task_id'] = 'foreign-task'
                else: contract['action_policy'] = {'mode': 'unsupported'}
                text = canonical_json(contract)
                digest = hashlib.sha256(text.encode()).hexdigest()
                db.execute('UPDATE contracts SET content_json=?,contract_sha256=? WHERE contract_version=2', (text, digest))
                db.execute('DROP TRIGGER runs_fixed_binding')
                db.execute('UPDATE runs SET contract_sha256=? WHERE run_id=?', (digest, 'rerun-1'))
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1', run_id='run-1')
    assert view['result_status'] == 'AVAILABLE' and view['result'] == original
    assert not view['write_intents'][0]['critical_violation']
    assert 'write_policy_unavailable' in view['display_blockers']
    assert 'critical_side_effect' not in view['display_blockers']
    assert not view['display_complete_success'] and ledger(case[0].database) == before


@pytest.mark.parametrize('source_unavailable', [False, True])
def test_recorded_selected_critical_side_effect_survives_origin_authority_projection(tmp_path, source_unavailable):
    case = prepared(tmp_path, scenario='code')
    revised_code_run(case[0].database)
    # Authorized by the new origin, but the selected old Run recorded that it
    # could not reuse this operation under its own identity. Both facts remain.
    intent(case[0].database, status='NOT_APPLIED', originating='rerun-1', identity='new-identity')
    original = finalize(case, verify(case)).model_dump(mode='json')
    assert original['side_effects'][0]['critical_violation']
    if source_unavailable:
        with connect(case[0].database) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('DROP TRIGGER contracts_immutable')
            db.execute('UPDATE contracts SET contract_sha256=? WHERE contract_version=2', ('f' * 64,))
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1', run_id='run-1')
    assert view['result_status'] == 'AVAILABLE' and view['result'] == original
    assert view['write_intents'][0]['recorded_in_selected_result']
    assert view['write_intents'][0]['critical_violation']
    assert 'critical_side_effect' in view['display_blockers']
    assert 'historical_side_effect_unresolved' in view['display_blockers']
    assert ('write_policy_unavailable' in view['display_blockers']) is source_unavailable
    assert not view['display_complete_success'] and ledger(case[0].database) == before


def test_missing_selected_run_still_projects_unavailable_task_write_authority(tmp_path):
    case = prepared(tmp_path, scenario='code')
    with connect(case[0].database) as db, transaction(db):
        create_task(db, task_id='empty-task', instruction='Empty task', requested_fields=['contract'])
    # A dangling task-wide ledger row must not borrow another task's policy.
    with connect(case[0].database) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,created_at,updated_at)
            VALUES('foreign-operation','foreign-key','empty-task','run-1',?,'create_pr',
            'test-identity','before','NOT_APPLIED',?,?)''',
            (Resource.repository_write('fixture/demo').resource_key, utc_text(), utc_text()))
    before = ledger(case[0].database)
    view = results(tmp_path, 'empty-task')
    assert view['selected_run'] is None and view['result_status'] == 'NOT_READY'
    assert not view['write_intents'][0]['critical_violation']
    assert 'write_policy_unavailable' in view['display_blockers']
    assert 'critical_side_effect' not in view['display_blockers']
    assert not view['display_complete_success'] and ledger(case[0].database) == before


def test_four_state_checks_are_not_overwritten_by_display_projection(tmp_path):
    case = prepared(tmp_path)
    display_copy(case)
    body = case[1].model_dump(mode='json')
    body['items']['values'][0]['raw_value'] = '9'
    proposal = ProposeResult.model_validate_json(canonical_json(body))
    case = (case[0], proposal, case[2], case[3], case[4])
    original = finalize(case, verify(case)).model_dump(mode='json')
    view = results(tmp_path, 'task-1')
    assert view['result'] == original and original['outcome'] != 'SUCCEEDED'
    assert any(f['verdict'] == 'FAIL' for f in view['field_checks'])
    assert not view['display_complete_success']


def test_dynamic_result_maps_do_not_create_fictional_evidence_references(tmp_path):
    case = prepared(tmp_path, scenario='grafana')
    display_copy(case)
    body = case[1].model_dump(mode='json')
    body['items']['variables']['evidence_ids'] = 'aaa'
    proposal = ProposeResult.model_validate_json(canonical_json(body))
    case = (case[0], proposal, case[2], case[3], case[4])
    original = finalize(case, verify(case)).model_dump(mode='json')
    view = results(tmp_path, 'task-1')
    assert view['result_status'] == 'AVAILABLE' and view['result'] == original
    assert [e['evidence_id'] for e in view['evidence']] == ['e1']


@pytest.mark.parametrize('target', ['result-hash', 'verification-hash', 'contract-hash',
                                   'result-task', 'field-duplicate', 'sensitive-actual',
                                   'field-password-object', 'field-cookie-object', 'check-password-object',
                                   'field-pass-without-evidence'])
def test_corrupted_historical_payloads_are_unavailable_without_leaking_or_mutating(tmp_path, target):
    case, _, _ = completed(tmp_path)
    # Deliberately bypass immutable-history triggers to model disk/operator
    # corruption. Production reader has neither this connection nor writes.
    with connect(case[0].database) as db:
        for name in ('run_results_no_update', 'run_verifications_no_update', 'contracts_immutable'):
            db.execute('DROP TRIGGER IF EXISTS ' + name)
        if target == 'result-hash': db.execute("UPDATE run_results SET result_sha256=?", ('f' * 64,))
        elif target == 'verification-hash': db.execute("UPDATE run_verifications SET content_sha256=?", ('f' * 64,))
        elif target == 'contract-hash':
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("UPDATE contracts SET contract_sha256=?", ('f' * 64,))
        elif target == 'result-task':
            row = db.execute('SELECT result_json FROM run_results').fetchone()
            body = json.loads(row[0]); body['task_id'] = 'foreign-task'
            text = canonical_json(body)
            db.execute('UPDATE run_results SET result_json=?,result_sha256=?', (text, hashlib.sha256(text.encode()).hexdigest()))
        else:
            row = db.execute('SELECT content_json FROM run_verifications').fetchone()
            body = json.loads(row[0])
            if target == 'field-duplicate': body['evaluation']['fields'].append(deepcopy(body['evaluation']['fields'][0]))
            elif target == 'check-password-object':
                body['checks'][0]['actual'] = {'password': {'nested': ['PRIVATE_CANARY_1234']}}
                result_body = json.loads(db.execute('SELECT result_json FROM run_results').fetchone()[0])
                result_body['checks'] = body['checks']
                text = canonical_json(result_body)
                db.execute('UPDATE run_results SET result_json=?,result_sha256=?',
                           (text, hashlib.sha256(text.encode()).hexdigest()))
            elif target == 'field-password-object': body['evaluation']['fields'][0]['actual'] = {'password': 'PRIVATE_CANARY_1234'}
            elif target == 'field-cookie-object': body['evaluation']['fields'][0]['actual'] = {'Cookie': 'PRIVATE_CANARY_1234'}
            elif target == 'field-pass-without-evidence': body['evaluation']['fields'][0]['evidence_ids'] = []
            else: body['evaluation']['fields'][0]['actual'] = 'password=PRIVATE_CANARY_1234'
            text = canonical_json(body)
            db.execute('UPDATE run_verifications SET content_json=?,content_sha256=?', (text, hashlib.sha256(text.encode()).hexdigest()))
    before = ledger(case[0].database)
    view = results(tmp_path, 'task-1')
    assert view['result_status'] == 'UNAVAILABLE' and view['result'] is None
    assert view['selected_run']['state'] == 'SUCCEEDED'
    assert not view['display_complete_success'] and 'PRIVATE_CANARY' not in json.dumps(view)
    assert ledger(case[0].database) == before


def test_http_no_store_authentication_and_run_ownership(tmp_path):
    case, _, _ = completed(tmp_path)
    with connect(case[0].database) as db, transaction(db):
        create_task(db, task_id='different-task', instruction='Another task', requested_fields=['contract'])
    with AuthenticatedTestClient(create_test_app(Settings(tmp_path))) as client:
        response = client.get('/v1/tasks/task-1/results')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert client.get('/v1/tasks/task-1/results', headers={'Authorization': ''}).status_code == 401
        assert client.get('/v1/tasks/different-task/results?run_id=run-1').status_code == 404
        empty = client.get('/v1/tasks/different-task/results').json()
        assert empty['selected_run'] is None and empty['runs'] == []
        assert client.get('/v1/tasks/nonexistent/results').status_code == 404


@pytest.mark.parametrize('query', ['before=0', 'before=01', 'before=9223372036854775808',
    'limit=0', 'limit=101', 'limit=-1', 'limit=01', 'limit=1&limit=2', 'before=1&before=2',
    'run_id=', 'run_id=one&run_id=two', 'run_id=../bad', 'after=1', 'limit=', 'before=1%20OR%201=1'])
def test_http_results_rejects_invalid_ambiguous_or_unbounded_parameters(database, query):
    with AuthenticatedTestClient(create_test_app(Settings(database.parent))) as client:
        assert client.get('/v1/tasks/task-1/results?' + query).status_code == 422


@pytest.mark.parametrize('storage', ['absent', 'old'])
def test_read_only_results_never_initializes_missing_or_old_storage(tmp_path, storage):
    path = tmp_path / 'business.sqlite3'
    if storage == 'old':
        with sqlite3.connect(path) as db: db.execute('PRAGMA user_version=1')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(BusinessError) as caught: results(tmp_path, 'task-1')
    assert caught.value.status == 503
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before
