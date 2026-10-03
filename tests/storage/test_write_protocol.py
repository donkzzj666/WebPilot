from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import shutil
import sqlite3

import pytest

from webagent.db import connect, migrate, transaction
from webagent.db import migrations
from webagent.db.repository import canonical_json, utc_text
from webagent.errors import BusinessError
from webagent.evidence.service import EvidenceService
from webagent.writes import WriteClaim, WriteProtocolStore, business_key
from test_gateway_store import fixture, observe, action
from conftest import seed


def setup(path):
    f = fixture(path, write=True)
    snapshot = observe(f)
    EvidenceService(path.parent).publish_observation(snapshot, {'title': 'Fixture', 'text': 'Controlled state'},
                                                   execution_token=f.token)
    f.protocol = WriteProtocolStore(path, clock=f.clock.utcnow)
    f.claim = WriteClaim(identity_ref=f.contract['identity_ref'], target=dict(repository='fixture/project',
        branch='repair', base_sha='a' * 40, operation='edit_file', files=['src/app.py']),
        expected_change_sha256='d' * 64, precondition_version='page-v1', adapter_id='fixture-v1')
    return f


def register(f, operation='operation-1'):
    with connect(f.path) as db, transaction(db):
        return f.protocol.prepare_in_transaction(db, f.token, f.claim, operation_id=operation)


def facts(f, outcome='APPLIED', **changes):
    value = f.claim.model_dump(mode='json')
    value.pop('adapter_id')
    return {**value, 'outcome': outcome, 'snapshot_id': 'snapshot-1',
            'receipt': {'external_id': 'commit-fixture'} if outcome == 'APPLIED' else None,
            'observed_version': 'page-v1', **changes}


def check(f, *, operation='operation-1', check_id='check-1', payload=None, artifact_payload=None):
    payload = payload or facts(f)
    evidence = f.protocol.evidence.publish(f.token.run_id,
        canonical_json(artifact_payload if artifact_payload is not None else payload).encode('utf-8'),
        source_url=f.contract['start_urls'][0], captured_at=f.clock.utcnow(),
        object_id=operation, query_scope='write state', locator_or_page='fixture-state',
        snapshot_id='snapshot-1', execution_token=f.token, retain=True)
    return f.protocol.record_check(f.token, operation, check_id, payload, [evidence['evidence_id']])


def test_registration_reentry_uses_semantics_and_never_reauthorizes_intent(database):
    f = setup(database)
    first, second = register(f), register(f, 'model-generated-other-id')
    assert first['new'] and first['dispatch_allowed'] and first['status'] == 'INTENT'
    assert second['operation_id'] == first['operation_id'] and not second['dispatch_allowed'] and second['reused']
    assert first['business_key'] == business_key(f.contract['task_id'], f.claim)
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM write_intents').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM write_protocol_links').fetchone()[0] == 1


def test_concurrent_semantic_registration_cannot_create_two_operations(database):
    f = setup(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda identifier: register(f, identifier), ['operation-1', 'operation-2']))
    assert sum(r['dispatch_allowed'] for r in results) == 1
    assert len({r['operation_id'] for r in results}) == 1


def test_existing_operation_id_cannot_be_rebound_to_another_change(database):
    f = setup(database)
    register(f)
    f.claim = f.claim.model_copy(update={'expected_change_sha256': 'e' * 64})
    with pytest.raises(BusinessError):
        register(f)


@pytest.mark.parametrize('outcome,status', [('APPLIED', 'CONFIRMED'), ('NOT_APPLIED', 'NOT_APPLIED'), ('UNKNOWN', 'UNKNOWN')])
def test_authoritative_three_way_check_sets_the_four_state_ledger(database, outcome, status):
    f = setup(database)
    register(f)
    result = check(f, payload=facts(f, outcome))
    assert result['status'] == f.protocol.get('operation-1')['status'] == status
    assert result['verified_current'] == (status != 'UNKNOWN')
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM write_protocol_checks').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM write_intent_evidence').fetchone()[0] == 1


@pytest.mark.parametrize('changes', [{'identity_ref': 'different-account'}, {'expected_change_sha256': 'e' * 64},
                                    {'receipt': None}, {'target': dict(repository='fixture/project',branch='other',
                                        base_sha='a'*40,operation='edit_file',files=['src/app.py'])}])
def test_applied_claim_with_wrong_identity_target_effect_or_missing_receipt_stays_unknown(database, changes):
    f = setup(database)
    register(f)
    assert check(f, payload=facts(f, **changes))['status'] == 'UNKNOWN'


@pytest.mark.parametrize('changes', [{'precondition_version': 'changed-v2'}, {'observed_version': 'changed-v2'},
                                   {'observed_version': None}])
def test_absence_proof_does_not_allow_retry_after_precondition_change(database, changes):
    f = setup(database)
    register(f)
    assert check(f, payload=facts(f, 'NOT_APPLIED', **changes))['status'] == 'UNKNOWN'
    assert not register(f)['dispatch_allowed']


def test_facts_must_equal_the_saved_original_artifact(database):
    f = setup(database)
    register(f)
    with pytest.raises(BusinessError):
        check(f, artifact_payload=facts(f, 'UNKNOWN'))
    assert f.protocol.get('operation-1')['status'] == 'INTENT'


def test_missing_original_proof_blocks_cached_reuse_and_retry(database):
    f = setup(database)
    register(f)
    check(f, payload=facts(f, 'NOT_APPLIED'))
    assert f.protocol.assert_claim_proof_integrity(f.token.run_id, f.claim) is not None
    with connect(database) as db:
        identifier = db.execute('SELECT evidence_id FROM write_protocol_check_evidence').fetchone()[0]
    metadata = f.protocol.evidence.metadata(identifier)
    (database.parent / metadata['artifact_path']).unlink()
    with pytest.raises(BusinessError):
        f.protocol.assert_claim_proof_integrity(f.token.run_id, f.claim)
    assert not register(f)['dispatch_allowed']


def test_late_or_wrong_epoch_check_cannot_confirm_the_write(database):
    f = setup(database)
    register(f)
    original = f.token
    f.token = replace(f.token, epoch=f.token.epoch + 1)
    with pytest.raises(BusinessError):
        check(f)
    f.token = original
    assert f.protocol.get('operation-1')['status'] == 'INTENT'


def test_absence_cannot_authorize_retry_while_current_epoch_dispatch_is_still_inflight(database):
    f = setup(database)
    dispatched = f.store.prepare(f.token, action(f, kind='input', write=True), f.binding,
                                external_write=True, write_claim=f.claim)
    assert dispatched['dispatch_allowed']
    operation_id = dispatched['operation_id']
    # A separate controlled read can finish while this action's local outcome
    # is still outstanding. Absence cannot pretend that the action was revoked.
    snapshot = observe(f, 'check-current')
    EvidenceService(database.parent).publish_observation(snapshot, {'title': 'Fixture', 'text': 'Currently absent'},
                                                         execution_token=f.token)
    payload = facts(f, 'NOT_APPLIED', snapshot_id='snapshot-check-current')
    evidence = f.protocol.evidence.publish(f.token.run_id, canonical_json(payload).encode('utf-8'),
        source_url=f.contract['start_urls'][0], captured_at=f.clock.utcnow(), object_id=operation_id,
        query_scope='write state', locator_or_page='fixture-state', snapshot_id='snapshot-check-current',
        execution_token=f.token, retain=True)
    result = f.protocol.record_check(f.token, operation_id, 'inflight-check', payload, [evidence['evidence_id']])
    assert result['status'] == 'UNKNOWN' and result['reason'] == 'write_still_inflight'
    with connect(database) as db:
        assert db.execute('SELECT status FROM steps WHERE step_id="step-1"').fetchone()[0] == 'INTENT'
        assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0] == 2


def test_confirmed_history_is_retained_but_latest_unknown_cannot_prove_current_reuse(database):
    f = setup(database)
    register(f)
    assert check(f)['verified_current']
    assert register(f)['verified_current']
    result = check(f, check_id='check-2', payload=facts(f, 'UNKNOWN'))
    assert result['status'] == 'CONFIRMED' and result['check_status'] == 'UNKNOWN' and not result['verified_current']
    assert not register(f)['verified_current']
    with connect(database) as db:
        assert f.protocol.verified_check_for_run(db, 'operation-1', f.token.run_id) is None


def test_unknown_cleanup_cannot_erase_confirmed_effect(database):
    f = setup(database)
    register(f)
    check(f)
    with connect(database) as db, transaction(db):
        f.protocol.mark_unknown_in_transaction(db, 'operation-1')
    assert f.protocol.get('operation-1')['status'] == 'CONFIRMED'


def test_not_applied_retry_proof_expires_and_cannot_cross_epochs(database):
    f = setup(database)
    register(f)
    check(f, payload=facts(f, 'NOT_APPLIED'))
    assert register(f)['dispatch_allowed']
    f.clock.advance(61)
    # The expired lease itself is fenced before checking an old absence proof.
    with pytest.raises(BusinessError):
        register(f)


@pytest.mark.parametrize('table,sql', [
    ('claims', 'UPDATE write_protocol_claims SET adapter_id="changed"'),
    ('claims', 'DELETE FROM write_protocol_claims'),
    ('checks', 'UPDATE write_protocol_checks SET effective_status="UNKNOWN"'),
    ('checks', 'DELETE FROM write_protocol_checks'),
    ('links', 'DELETE FROM write_protocol_links'),
    ('evidence', 'UPDATE write_protocol_check_evidence SET sha256="'+'e'*64+'"'),
])
def test_claims_checks_and_proof_links_are_immutable_sql_history(database, table, sql):
    f = setup(database)
    register(f)
    check(f)
    with connect(database) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(sql)


def test_v17_upgrade_and_failed_v18_upgrade_preserve_existing_history(tmp_path, monkeypatch):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=17)
    with connect(path) as db, transaction(db):
        seed(db)
        before = [tuple(r) for r in db.execute('SELECT * FROM runs')]
        history = [tuple(r) for r in db.execute('SELECT * FROM schema_migrations')]
    sql = tmp_path / 'sql'
    shutil.copytree(migrations.SQL_DIR, sql)
    with (sql / '0018_write_protocol.sql').open('a') as stream:
        stream.write('INVALID MIGRATION;\n')
    monkeypatch.setattr(migrations, 'SQL_DIR', sql)
    with pytest.raises(sqlite3.OperationalError):
        migrate(path)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 17
        assert [tuple(r) for r in db.execute('SELECT * FROM runs')] == before
        assert [tuple(r) for r in db.execute('SELECT * FROM schema_migrations')] == history
    monkeypatch.undo()
    assert migrate(path) == {'previous_version': 17, 'schema_version': 18, 'applied': 1}
    with connect(path) as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM runs')] == before
        assert [tuple(r) for r in db.execute('SELECT * FROM schema_migrations WHERE version<=17')] == history
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
