"""Real session guards distinguish fenced confirmed writes from live calls."""
from dataclasses import replace

import pytest

from webagent.db import connect, migrate, transaction
from webagent.db.repository import canonical_json
from webagent.evidence.service import EvidenceService
from webagent.errors import BusinessError
from webagent.writes.store import WriteProtocolStore
from test_gateway_store import action, fixture, observe
from test_write_protocol import setup, facts


def prove(f, outcome='APPLIED', *, suffix='check'):
    snapshot = observe(f, suffix)
    EvidenceService(f.path.parent).publish_observation(snapshot,
        {'title': 'Fixture', 'text': 'Independent write result'}, execution_token=f.token)
    payload = facts(f, outcome, snapshot_id=snapshot['snapshot_id'])
    evidence = f.protocol.evidence.publish(f.token.run_id, canonical_json(payload).encode('utf-8'),
        source_url=f.contract['start_urls'][0], captured_at=f.clock.utcnow(),
        object_id='operation-step-1', query_scope='write state', locator_or_page='fixture-state',
        snapshot_id=snapshot['snapshot_id'], execution_token=f.token, retain=True)
    return f.protocol.record_check(f.token, 'operation-step-1', 'proof-' + suffix,
                                   payload, [evidence['evidence_id']])


def historical_write(path, *, recover=True, outcome='APPLIED'):
    f = setup(path)
    old_token = f.token
    first = f.store.prepare(f.token, action(f, kind='input', write=True), f.binding,
                            external_write=True, write_claim=f.claim)
    assert first['dispatch_allowed']
    if recover:
        f.scheduler.abandon(f.token)
        f.token = f.scheduler.claim(f.token.worker_id, f.token.worker_generation)
        assert f.token.epoch > old_token.epoch
    assert prove(f, outcome)['status'] == {'APPLIED': 'CONFIRMED', 'NOT_APPLIED': 'NOT_APPLIED'}[outcome]
    if recover:
        f.token = f.scheduler.reconcile(f.token.run_id, f.token.state_version)
    return f, old_token


def next_write(f):
    # A distinct trusted change on a freshly observed page, not cached reuse.
    f.binding['page_version'] = 'next-target-page'
    snapshot = observe(f, 'next', binding=f.binding)
    claim = f.claim.model_copy(update={'expected_change_sha256': 'e' * 64})
    result = f.store.prepare(f.token,
        action(f, step='next-write', snapshot=snapshot['snapshot_id'], kind='input', write=True),
        f.binding, external_write=True, write_claim=claim)
    assert result['dispatch_allowed'] and result['operation_id'] != 'operation-step-1'
    return result


def validate(f, token=None):
    return f.registry.validate_gateway_dispatch(f.session.session_id, f.session.owner,
        execution_token=token or f.token, step_id='next-write', operation_id='operation-next-write')


def test_fenced_intent_with_current_applied_original_allows_a_different_write(database):
    f, _ = historical_write(database)
    next_write(f)
    assert validate(f) == f.token
    with connect(database) as db:
        assert db.execute('SELECT status FROM steps WHERE step_id="step-1"').fetchone()[0] == 'INTENT'
        assert db.execute('SELECT status FROM write_intents WHERE operation_id="operation-step-1"').fetchone()[0] == 'CONFIRMED'
        assert db.execute('SELECT count(*) FROM write_protocol_dispatches').fetchone()[0] == 2


def test_confirmed_history_survives_cache_age_and_a_different_current_page(database):
    f, _ = historical_write(database)
    for _ in range(5):
        f.clock.advance(15)
        f.token = f.scheduler.heartbeat(f.token)
    with connect(database) as db:
        assert not WriteProtocolStore._current_confirmed(db, f.token, 'operation-step-1',
            f.clock.utcnow(), f.claim.precondition_version)
    next_write(f)
    assert validate(f) == f.token


def test_current_epoch_intent_is_not_ignored_even_with_an_applied_receipt(database):
    f, _ = historical_write(database, recover=False)
    next_write(f)
    with pytest.raises(BusinessError):
        validate(f)


def test_latest_unknown_proof_cannot_resolve_confirmed_history(database):
    f, _ = historical_write(database)
    next_write(f)
    result = prove(f, 'UNKNOWN', suffix='unknown')
    assert result['status'] == 'CONFIRMED' and not result['verified_current']
    with pytest.raises(BusinessError):
        validate(f)


@pytest.mark.parametrize('changed', ['epoch', 'worker_generation', 'state_version'])
def test_stale_dispatch_qualification_never_uses_confirmed_history(database, changed):
    f, _ = historical_write(database)
    next_write(f)
    stale = replace(f.token, **{changed: getattr(f.token, changed) + 1})
    with pytest.raises(BusinessError):
        validate(f, stale)


def test_applied_proof_from_an_earlier_epoch_cannot_resolve_history(database):
    f, _ = historical_write(database)
    f.scheduler.abandon(f.token)
    f.token = f.scheduler.claim(f.token.worker_id, f.token.worker_generation)
    f.token = f.scheduler.reconcile(f.token.run_id, f.token.state_version)
    next_write(f)
    with pytest.raises(BusinessError):
        validate(f)


def test_expired_lease_cannot_use_confirmed_history(database):
    f, _ = historical_write(database)
    next_write(f)
    f.clock.advance(31)
    assert f.scheduler.sweep_expired() == 1
    with pytest.raises(BusinessError):
        validate(f)


def test_consumed_absence_proof_still_qualifies_the_same_operation_replacement(database):
    f, _ = historical_write(database, outcome='NOT_APPLIED')
    snapshot = observe(f, 'replacement')
    replacement = f.store.prepare(f.token,
        action(f, step='replacement', snapshot=snapshot['snapshot_id'], kind='input', write=True),
        f.binding, external_write=True, write_claim=f.claim)
    assert replacement['dispatch_allowed'] and replacement['operation_id'] == 'operation-step-1'
    assert f.registry.validate_gateway_dispatch(f.session.session_id, f.session.owner,
        execution_token=f.token, step_id='replacement', operation_id='operation-step-1') == f.token


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_missing_or_corrupt_applied_original_does_not_allow_dispatch(database, damage):
    f, _ = historical_write(database)
    next_write(f)
    with connect(database) as db:
        artifact = db.execute('''SELECT e.artifact_path FROM write_protocol_check_evidence p
            JOIN evidence e USING(evidence_id,run_id) WHERE p.check_id='proof-check' ''').fetchone()[0]
    artifact = database.parent / artifact
    if damage == 'missing':
        artifact.unlink()
    else:
        artifact.write_bytes(b'{"receipt":"corrupted independent original"}')
    with pytest.raises(BusinessError):
        validate(f)


def test_original_read_occurs_outside_writer_and_revocation_is_rechecked(database, monkeypatch):
    f, _ = historical_write(database)
    next_write(f)
    original = WriteProtocolStore.assert_proof_integrity
    checked = []
    def interrupted(store, *args, **kwargs):
        # A second connection can take the writer lock during original reads.
        with connect(database, busy_timeout_ms=0) as db, transaction(db):
            pass
        proof = original(store, *args, **kwargs)
        checked.append(proof['check_id'])
        f.scheduler.abandon(f.token)
        return proof
    monkeypatch.setattr(WriteProtocolStore, 'assert_proof_integrity', interrupted)
    with pytest.raises(BusinessError):
        validate(f)
    assert checked == ['proof-check']


@pytest.mark.parametrize('version', [12, 13])
def test_legacy_schema_without_protocol_tables_keeps_denial(tmp_path, version):
    database = tmp_path / 'business.sqlite3'
    migrate(database, target=version)
    f = fixture(database, write=True)
    with pytest.raises(BusinessError):
        f.registry.validate_gateway_dispatch(f.session.session_id, f.session.owner,
            execution_token=f.token, step_id='unknown-write', operation_id='unknown-operation')
