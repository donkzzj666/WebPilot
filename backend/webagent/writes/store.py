"""Business-ledger authority for external writes and read-only reconciliation.

No method calls a browser, grants an execution lease, or rewrites a historical
step. Artifact bytes are verified before taking the short SQLite writer lock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..evidence.redaction import TextRedactor
from ..evidence.store import EvidenceStore
from ..scheduler.models import ExecutionToken, Resource, canonical_repository
from ..scheduler.store import validate_in_transaction
from ..tasks.models import RepositoryWritePolicy, TaskContract
from .models import WriteClaim, WriteCheckFacts, business_key


def _deny(message='Write operation requires current external-state reconciliation'):
    return BusinessError('STATE_CONFLICT', message, status=409, field='unknown_write')


def _identifier(value, field):
    if (type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BusinessError('INVALID_PARAMETER', 'Invalid write protocol metadata', field=field)
    return value


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _claim(value):
    try:
        value = value.model_dump(mode='json') if isinstance(value, WriteClaim) else value
        claim = WriteClaim.model_validate_json(canonical_json(value))
        serialized = canonical_json(claim.model_dump(mode='json'))
        if len(serialized.encode('utf-8')) > 32768 or TextRedactor().contains_sensitive(serialized):
            raise ValueError('sensitive claim')
        return claim
    except (ValidationError, TypeError, ValueError):
        raise BusinessError('INVALID_PARAMETER', 'Invalid immutable write claim', field='write_claim') from None


class WriteProtocolStore:
    def __init__(self, path: Path, *, clock=None):
        value = Path(path)
        self.path = value if value.suffix in ('.sqlite', '.sqlite3', '.db') else value / 'business.sqlite3'
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.evidence = EvidenceStore(self.path.parent)

    def _now(self, value=None):
        return utc_text(self.clock() if value is None else value)

    @staticmethod
    def _row(db, operation_id, task_id=None):
        row = db.execute('''SELECT w.*,c.target_json,c.expected_change_sha256,c.adapter_id,c.claim_sha256
            FROM write_intents w JOIN write_protocol_claims c USING(operation_id,business_key)
            WHERE w.operation_id=?''', (operation_id,)).fetchone()
        if row is None or task_id is not None and row['task_id'] != task_id:
            raise BusinessError('NOT_FOUND', 'Write operation not found', status=404)
        result = dict(row)
        result['target'] = json.loads(result.pop('target_json'))
        result['receipt'] = json.loads(result['receipt']) if result['receipt'] else None
        claim = WriteClaim(identity_ref=result['identity_ref'], target=result['target'],
            expected_change_sha256=result['expected_change_sha256'], precondition_version=result['precondition_version'],
            adapter_id=result['adapter_id'])
        if (_hash(claim.model_dump(mode='json')) != result['claim_sha256']
                or business_key(result['task_id'], claim) != result['business_key']):
            raise _deny('Immutable write semantics failed their integrity check')
        return result

    @staticmethod
    def _link(db, operation_id, run_id, stamp):
        if not db.execute('SELECT 1 FROM write_protocol_links WHERE operation_id=? AND run_id=?',
                          (operation_id, run_id)).fetchone():
            db.execute('INSERT INTO write_protocol_links VALUES(?,?,?)', (operation_id, run_id, stamp))

    @staticmethod
    def _authority(db, token, claim, now):
        validate_in_transaction(db, token, now=now, allow_reconciling=True)
        row = db.execute('''SELECT r.task_id,c.content_json FROM runs r JOIN contracts c
            ON c.task_id=r.task_id AND c.contract_version=r.contract_version
            AND c.contract_sha256=r.contract_sha256 WHERE r.run_id=?''', (token.run_id,)).fetchone()
        try:
            contract = TaskContract.model_validate_json(row['content_json']) if row else None
        except ValidationError:
            contract = None
        target = claim.target
        policy = contract.action_policy if contract is not None else None
        if (not isinstance(policy, RepositoryWritePolicy) or contract.identity_ref != claim.identity_ref
                or canonical_repository(policy.repository) != target.repository
                or policy.branch != target.branch or policy.base_sha != target.base_sha
                or target.operation not in policy.allowed_operations
                or any(not policy.permits_file(path) for path in target.files)):
            raise BusinessError('FORBIDDEN', 'Write semantics differ from the frozen policy', status=403)
        validate_in_transaction(db, token, Resource.repository_write(target.repository),
                                now=now, allow_reconciling=True)
        identity = db.execute('SELECT * FROM identities WHERE identity_ref=?', (claim.identity_ref,)).fetchone()
        if identity is None or identity['state'] != 'VERIFIED':
            raise _deny('Write identity is no longer verified')
        validate_in_transaction(db, token, Resource.site_identity(identity['site_id'], claim.identity_ref,
            realm=identity['realm']), now=now, allow_reconciling=True)
        return row['task_id'], contract

    def prepare_in_transaction(self, db, token, claim, operation_id=None, now=None):
        """Resolve immutable business semantics before a new budget debit.

        The fresh INTENT and its dispatch must be committed together by the
        gateway. Reentry of any existing INTENT requires a page check first.
        """
        claim, current = _claim(claim), self.clock() if now is None else now
        task_id, _ = self._authority(db, token, claim, current)
        key, stamp = business_key(task_id, claim), self._now(current)
        supplied = _identifier(operation_id, 'operation_id') if operation_id is not None else key
        by_id = db.execute('SELECT business_key FROM write_intents WHERE operation_id=?', (supplied,)).fetchone()
        if by_id is not None and by_id['business_key'] != key:
            raise _deny('Operation identifier is bound to another immutable business intent')
        old = db.execute('SELECT operation_id FROM write_protocol_claims WHERE business_key=?', (key,)).fetchone()
        new = old is None
        if new:
            if db.execute('SELECT 1 FROM write_intents WHERE business_key=?', (key,)).fetchone():
                raise _deny('Legacy operation requires explicit reconciliation')
            db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
                target,expected_change,identity_ref,precondition_version,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''', (supplied, key, task_id, token.run_id,
                Resource.repository_write(claim.target.repository).resource_key,
                claim.expected_change_sha256, claim.identity_ref, claim.precondition_version, stamp, stamp))
            db.execute('INSERT INTO write_protocol_claims VALUES(?,?,?,?,?,?,?)',
                (supplied, key, canonical_json(claim.target.model_dump(mode='json')),
                 claim.expected_change_sha256, claim.adapter_id, _hash(claim.model_dump(mode='json')), stamp))
            operation_id = supplied
        else:
            operation_id = old['operation_id']
        row = self._row(db, operation_id)
        if (row['identity_ref'] != claim.identity_ref or row['target'] != claim.target.model_dump(mode='json')
                or row['expected_change_sha256'] != claim.expected_change_sha256
                or row['adapter_id'] != claim.adapter_id):
            raise _deny('Immutable business semantics differ from the registered operation')
        self._link(db, operation_id, token.run_id, stamp)
        allowed = new
        if row['status'] == 'NOT_APPLIED':
            allowed = self._retry_check(db, token, row, claim.precondition_version, current) is not None
        verified = self._current_confirmed(db, token, row['operation_id'], current,
            claim.precondition_version) if row['status'] == 'CONFIRMED' else allowed
        return {**row, 'dispatch_allowed': allowed, 'reused': not new, 'new': new, 'verified_current': verified}

    @staticmethod
    def _version_current(db, token, check):
        run = db.execute('SELECT state,state_version FROM runs WHERE run_id=?', (token.run_id,)).fetchone()
        return (check['state_version'] == token.state_version or
                check['run_state'] == 'RECONCILING' and check['state_version'] + 1 == token.state_version
                and run is not None and run['state'] == 'RUNNING')

    @classmethod
    def _current_confirmed(cls, db, token, operation_id, now, precondition):
        check = cls.verified_check_for_run(db, operation_id, token.run_id)
        if (check is None or check['epoch'] != token.epoch or check['worker_id'] != token.worker_id
                or check['worker_generation'] != token.worker_generation
                or check['created_at'] < utc_text(now - timedelta(seconds=60))
                or not cls._version_current(db, token, check)):
            return False
        original = db.execute('''SELECT g.*,o.source_url FROM gateway_observations g
            JOIN observations o USING(run_id,snapshot_id) WHERE g.snapshot_id=?''', (check['snapshot_id'],)).fetchone()
        current = db.execute('''SELECT g.*,o.source_url FROM gateway_page_heads h JOIN gateway_observations g
            USING(run_id,snapshot_id) JOIN observations o USING(run_id,snapshot_id)
            WHERE h.run_id=? AND h.valid=1''', (token.run_id,)).fetchone()
        fields = ('session_id','manager_id','session_generation','tab_id','frame_id','page_version',
                  'source_url','width','height')
        if original is None or current is None or any(original[key] != current[key] for key in fields):
            return False
        observed_version = check['facts'].get('observed_version')
        if observed_version is not None and observed_version != precondition:
            return False
        return cls._available_check(db, check['check_id'])

    @staticmethod
    def _retry_check(db, token, row, precondition, now):
        latest = db.execute('''SELECT * FROM write_protocol_checks WHERE operation_id=?
            ORDER BY created_at DESC,rowid DESC LIMIT 1''', (row['operation_id'],)).fetchone()
        if (latest is None or latest['effective_status'] != 'NOT_APPLIED'
                or row['precondition_version'] != precondition or latest['run_id'] != token.run_id
                or latest['worker_id'] != token.worker_id or latest['worker_generation'] != token.worker_generation
                or latest['epoch'] != token.epoch
                or latest['created_at'] < utc_text(now - timedelta(seconds=60))
                or db.execute('SELECT 1 FROM write_protocol_dispatches WHERE check_id=?',
                              (latest['check_id'],)).fetchone()):
            return None
        if not WriteProtocolStore._version_current(db, token, latest):
            return None
        if not WriteProtocolStore._available_check(db, latest['check_id']):
            return None
        try:
            facts = WriteCheckFacts.model_validate_json(latest['facts_json'])
        except (ValueError, TypeError):
            return None
        if (hashlib.sha256(latest['facts_json'].encode('utf-8')).hexdigest() != latest['facts_sha256']
                or facts.outcome != 'NOT_APPLIED' or facts.receipt is not None
                or facts.identity_ref != row['identity_ref'] or facts.expected_change_sha256 != row['expected_change_sha256']
                or facts.target.model_dump(mode='json') != row['target']
                or facts.precondition_version != precondition or facts.observed_version != precondition):
            return None
        return latest['check_id']

    def link_attempt_in_transaction(self, db, token, operation_id, step_id, now=None, *, new=False):
        """Bind one counted gateway dispatch and consume one NOT_APPLIED proof."""
        current = self.clock() if now is None else now
        row = self._row(db, operation_id)
        claim = WriteClaim(identity_ref=row['identity_ref'], target=row['target'],
            expected_change_sha256=row['expected_change_sha256'], precondition_version=row['precondition_version'],
            adapter_id=row['adapter_id'])
        task_id, _ = self._authority(db, token, claim, current)
        _identifier(step_id, 'step_id')
        check_id = None
        if row['status'] == 'NOT_APPLIED':
            check_id = self._retry_check(db, token, row, claim.precondition_version, current)
            if check_id is None:
                raise _deny('A current unconsumed NOT_APPLIED proof is required for another dispatch')
        elif not (new is True and row['status'] == 'INTENT'
                  and row['originating_run_id'] == token.run_id
                  and not db.execute('SELECT 1 FROM write_protocol_dispatches WHERE operation_id=?',
                                     (operation_id,)).fetchone()
                  and not db.execute('SELECT 1 FROM write_protocol_checks WHERE operation_id=?',
                                     (operation_id,)).fetchone()):
            raise _deny()
        db.execute('''INSERT INTO write_protocol_dispatches VALUES(?,?,?,?,?,?,?,?,?)''',
            (step_id, operation_id, token.run_id, token.worker_id, token.worker_generation, token.epoch,
             token.state_version, check_id, self._now(current)))
        db.execute('INSERT INTO write_intent_attempts VALUES(?,?,?,?)',
                   (task_id, operation_id, token.run_id, step_id))
        if check_id is not None:
            db.execute("UPDATE write_intents SET status='INTENT',updated_at=? WHERE operation_id=?",
                       (self._now(current), operation_id))
        return {'operation_id': operation_id, 'step_id': step_id, 'check_id': check_id}

    def mark_unknown_in_transaction(self, db, operation_id, now=None, reason=None):
        """Cleanup preserves already-confirmed effects and never authorizes retry."""
        _identifier(operation_id, 'operation_id')
        db.execute("UPDATE write_intents SET status='UNKNOWN',updated_at=MAX(updated_at,?) "
                   "WHERE operation_id=? AND status<>'CONFIRMED'", (self._now(now), operation_id))
        row = db.execute('SELECT originating_run_id,status FROM write_intents WHERE operation_id=?',
                         (operation_id,)).fetchone()
        if row is None or row['status'] == 'CONFIRMED':
            return
        for lease in db.execute('''SELECT resource_key FROM resource_leases WHERE resource_type
            IN ('site_identity','repository_write','webarena_environment') AND holder_run_id IN (
              SELECT run_id FROM write_protocol_links WHERE operation_id=? UNION SELECT ?)''',
            (operation_id, row['originating_run_id'])).fetchall():
            db.execute('INSERT INTO resource_quarantines VALUES(?,?,?) ON CONFLICT DO NOTHING',
                       (lease['resource_key'], operation_id, self._now(now)))
            db.execute('UPDATE resource_leases SET logical_hold=1,state_version=state_version+1 WHERE resource_key=?',
                       (lease['resource_key'],))

    def mark_unknown(self, token, operation_id, *, reason=None):
        """Bound late cleanup cannot clear quarantine or create dispatch authority."""
        if not isinstance(token, ExecutionToken):
            raise _deny('Cleanup requires an execution qualification binding')
        with connect(self.path) as db, transaction(db):
            row = self._row(db, operation_id)
            run = db.execute('SELECT task_id FROM runs WHERE run_id=?', (token.run_id,)).fetchone()
            if run is None or run['task_id'] != row['task_id']:
                raise _deny('Cleanup belongs to another task')
            owned = db.execute('''SELECT 1 FROM write_protocol_dispatches WHERE operation_id=? AND run_id=?
                AND worker_id=? AND worker_generation=? AND epoch=? AND state_version=? UNION
                SELECT 1 FROM write_protocol_checks WHERE operation_id=? AND run_id=?
                AND worker_id=? AND worker_generation=? AND epoch=? AND state_version=? LIMIT 1''',
                (operation_id, token.run_id, token.worker_id, token.worker_generation, token.epoch, token.state_version,
                 operation_id, token.run_id, token.worker_id, token.worker_generation, token.epoch, token.state_version)).fetchone()
            if owned is None:
                validate_in_transaction(db, token, now=self.clock(), allow_reconciling=True)
                if not db.execute('SELECT 1 FROM write_protocol_links WHERE operation_id=? AND run_id=?',
                                  (operation_id, token.run_id)).fetchone():
                    raise _deny('Cleanup is not linked to this operation')
            self.mark_unknown_in_transaction(db, operation_id, reason=reason)

    def _check_binding(self, db, token, facts, metadata, contract):
        from ..gateway.store import GatewayStore
        snapshot = GatewayStore._snapshot(db, facts.snapshot_id)
        head = db.execute('SELECT * FROM gateway_page_heads WHERE run_id=?', (token.run_id,)).fetchone()
        if (snapshot['run_id'] != token.run_id or snapshot['epoch'] != token.epoch
                or snapshot['state_version'] != token.state_version or head is None or not head['valid']
                or head['snapshot_id'] != facts.snapshot_id):
            raise _deny('Write check does not refer to the current page observation')
        session = GatewayStore(self.path, clock=self.clock)._session(db, token, snapshot, allow_reconciling=True)
        if session['identity_ref'] != contract.identity_ref:
            raise _deny('Write check session identity differs from the frozen contract')
        self.evidence.filtered_observation_row(db, facts.snapshot_id, token.run_id)
        for item in metadata:
            current = self.evidence._metadata(db, item['evidence_id'], token.run_id)
            if (current['sha256'] != item['sha256'] or current['availability'] != 'AVAILABLE'
                    or current['capture_status'] != 'COMPLETE' or current['snapshot_id'] != facts.snapshot_id
                    or current['captured_at'] < snapshot['captured_at']
                    or current['source_url'] != snapshot['source_url']
                    or not any(source.permits(current['source_url']) for source in contract.sources)
                    or current['artifact_kind'] != 'text' or current['sensitivity'] != 'restricted'):
                raise _deny('Write check evidence is not an authoritative current page artifact')
        return snapshot

    def record_check(self, token, operation_id, check_id, facts, evidence_ids):
        """Commit one bounded trusted page proof and its conservative conclusion."""
        _identifier(operation_id, 'operation_id')
        _identifier(check_id, 'check_id')
        if (type(evidence_ids) not in (tuple, list) or not 1 <= len(evidence_ids) <= 8
                or len(set(evidence_ids)) != len(evidence_ids)):
            raise BusinessError('INVALID_PARAMETER', 'Write checks require bounded unique evidence', field='evidence_ids')
        try:
            raw_facts = facts.model_dump(mode='json') if isinstance(facts, WriteCheckFacts) else facts
            payload = canonical_json(raw_facts)
            if len(payload.encode('utf-8')) > 32768 or TextRedactor().contains_sensitive(payload):
                raise ValueError('unbounded or sensitive facts')
            facts = WriteCheckFacts.model_validate_json(payload)
        except (ValidationError, TypeError, ValueError):
            raise BusinessError('INVALID_PARAMETER', 'Invalid bounded write check facts', field='write_check') from None
        metadata = []
        for evidence_id in evidence_ids:
            _identifier(evidence_id, 'evidence_id')
            item, data = self.evidence.read(evidence_id, run_id=token.run_id, allow_restricted=True)
            if len(data) > 32768:
                raise _deny('Write check artifact exceeds its bound')
            try:
                if json.loads(data) != raw_facts:
                    raise ValueError('proof differs')
            except (UnicodeError, ValueError, TypeError):
                raise _deny('Write check facts differ from the saved original artifact') from None
            metadata.append(item)
        current, stamp = self.clock(), self._now()
        with connect(self.path) as db, transaction(db):
            row = self._row(db, operation_id)
            claim = WriteClaim(identity_ref=row['identity_ref'], target=row['target'],
                expected_change_sha256=row['expected_change_sha256'], precondition_version=row['precondition_version'],
                adapter_id=row['adapter_id'])
            task_id, contract = self._authority(db, token, claim, current)
            if task_id != row['task_id']:
                raise _deny('Write check Run belongs to another task')
            self._check_binding(db, token, facts, metadata, contract)
            old = db.execute('SELECT * FROM write_protocol_checks WHERE check_id=?', (check_id,)).fetchone()
            if old is not None:
                ids = [r[0] for r in db.execute('SELECT evidence_id FROM write_protocol_check_evidence '
                    'WHERE check_id=? ORDER BY evidence_id', (check_id,))]
                if (old['operation_id'] != operation_id or old['run_id'] != token.run_id
                        or old['epoch'] != token.epoch or old['facts_sha256'] != _hash(raw_facts)
                        or ids != sorted(evidence_ids)):
                    raise _deny('Write check identifier is bound to another observation')
                return {'operation_id': operation_id, 'status': row['status'], 'check_id': check_id,
                        'check_status': old['effective_status'], 'verified_current': old['effective_status'] in ('CONFIRMED','NOT_APPLIED'),
                        'duplicate': True}
            self._link(db, operation_id, token.run_id, stamp)
            same = (facts.identity_ref == row['identity_ref'] and facts.target.model_dump(mode='json') == row['target']
                    and facts.expected_change_sha256 == row['expected_change_sha256'])
            status, reason = 'UNKNOWN', 'check_unknown'
            if not same:
                reason = 'identity_or_target_changed'
            elif facts.outcome == 'APPLIED' and facts.receipt is not None:
                status, reason = 'CONFIRMED', 'effect_verified'
            elif (facts.outcome == 'NOT_APPLIED' and facts.receipt is None
                  and facts.precondition_version == row['precondition_version']
                  and facts.observed_version == row['precondition_version']):
                status, reason = 'NOT_APPLIED', 'absence_and_precondition_verified'
                inflight = db.execute('''SELECT 1 FROM gateway_attempts g JOIN steps s USING(step_id,run_id)
                    WHERE g.operation_id=? AND g.run_id=? AND g.epoch=? AND g.external_write=1
                    AND s.status='INTENT' LIMIT 1''', (operation_id, token.run_id, token.epoch)).fetchone()
                if inflight is not None:
                    status, reason = 'UNKNOWN', 'write_still_inflight'
            elif facts.outcome == 'NOT_APPLIED':
                reason = 'precondition_changed'
            if row['status'] == 'CONFIRMED' and status == 'NOT_APPLIED':
                status, reason = 'UNKNOWN', 'confirmed_effect_conflicts_with_absence'
            run_state = db.execute('SELECT state FROM runs WHERE run_id=?', (token.run_id,)).fetchone()[0]
            db.execute('''INSERT INTO write_protocol_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (check_id, operation_id, token.run_id, token.worker_id, token.worker_generation, token.epoch,
                 token.state_version, run_state, facts.snapshot_id, payload, _hash(raw_facts), status, reason, stamp))
            for item in metadata:
                db.execute('INSERT INTO write_protocol_check_evidence VALUES(?,?,?,?)',
                           (check_id, token.run_id, item['evidence_id'], item['sha256']))
                db.execute('INSERT INTO write_intent_evidence VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                           (task_id, operation_id, token.run_id, item['evidence_id']))
            if row['status'] != 'CONFIRMED':
                receipt = canonical_json(facts.receipt) if status == 'CONFIRMED' else None
                db.execute('UPDATE write_intents SET status=?,receipt=?,updated_at=? WHERE operation_id=?',
                           (status, receipt, stamp, operation_id))
            if status in ('CONFIRMED', 'NOT_APPLIED'):
                db.execute('DELETE FROM resource_quarantines WHERE operation_id=?', (operation_id,))
            elif row['status'] != 'CONFIRMED':
                self.mark_unknown_in_transaction(db, operation_id, now=current, reason=reason)
            return {'operation_id': operation_id, 'status': row['status'] if row['status'] == 'CONFIRMED' else status,
                    'check_status': status, 'verified_current': status in ('CONFIRMED', 'NOT_APPLIED'),
                    'check_id': check_id, 'reason': reason, 'duplicate': False, 'dispatch_allowed': False}

    @staticmethod
    def is_resolved_step(db, step_id):
        row = db.execute('''SELECT w.status,w.operation_id FROM gateway_attempts g
            JOIN write_intents w USING(operation_id) JOIN write_protocol_claims c USING(operation_id)
            WHERE g.step_id=? AND g.external_write=1''', (step_id,)).fetchone()
        if row is None or row['status'] not in ('CONFIRMED', 'NOT_APPLIED'):
            return False
        check = db.execute('''SELECT check_id,effective_status FROM write_protocol_checks WHERE operation_id=?
            ORDER BY created_at DESC,rowid DESC LIMIT 1''', (row['operation_id'],)).fetchone()
        if check is None or check['effective_status'] != row['status']:
            return False
        return WriteProtocolStore._available_check(db, check['check_id'])

    @staticmethod
    def _available_check(db, check_id):
        return not db.execute('''SELECT 1 FROM write_protocol_check_evidence p LEFT JOIN evidence e USING(evidence_id,run_id)
            LEFT JOIN evidence_availability v USING(evidence_id) WHERE p.check_id=?
            AND (e.evidence_id IS NULL OR v.status IS NULL OR p.sha256<>e.sha256
                 OR e.capture_status<>'COMPLETE' OR v.status<>'AVAILABLE')''',
            (check_id,)).fetchone() and bool(db.execute(
                'SELECT 1 FROM write_protocol_check_evidence WHERE check_id=?', (check_id,)).fetchone())

    @staticmethod
    def pending_for_task(db, task_id):
        return [dict(row) for row in db.execute('''SELECT operation_id,status,business_key FROM write_intents
            WHERE task_id=? AND status IN ('INTENT','UNKNOWN') ORDER BY created_at,operation_id''', (task_id,))]

    @staticmethod
    def verified_check_for_run(db, operation_id, run_id):
        """SQL proof references; callers additionally verify artifact bytes."""
        row = db.execute('''SELECT c.* FROM write_protocol_checks c JOIN write_intents w USING(operation_id)
            WHERE c.operation_id=? AND c.run_id=? AND w.status='CONFIRMED'
            ORDER BY c.created_at DESC,c.rowid DESC LIMIT 1''', (operation_id, run_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        operation = WriteProtocolStore._row(db, operation_id)
        try:
            facts = WriteCheckFacts.model_validate_json(result['facts_json'])
        except (ValueError, TypeError):
            return None
        if (result['effective_status'] != 'CONFIRMED' or facts.outcome != 'APPLIED' or facts.receipt is None
                or facts.identity_ref != operation['identity_ref']
                or facts.target.model_dump(mode='json') != operation['target']
                or facts.expected_change_sha256 != operation['expected_change_sha256']
                or hashlib.sha256(result['facts_json'].encode('utf-8')).hexdigest() != result['facts_sha256']):
            return None
        result['evidence_ids'] = [r[0] for r in db.execute(
            'SELECT evidence_id FROM write_protocol_check_evidence WHERE check_id=? ORDER BY evidence_id',
            (result['check_id'],))]
        if not result['evidence_ids']:
            return None
        result['facts'] = json.loads(result.pop('facts_json'))
        return result

    @classmethod
    def confirmed_for_run(cls, db, operation_id, run_id):
        return cls.verified_check_for_run(db, operation_id, run_id) is not None

    def get(self, operation_id, *, task_id=None):
        _identifier(operation_id, 'operation_id')
        with connect(self.path) as db:
            return self._row(db, operation_id, task_id)

    def assert_proof_integrity(self, operation_id, *, run_id=None):
        """Verify original proof bytes outside a gateway admission transaction."""
        _identifier(operation_id, 'operation_id')
        with connect(self.path) as db:
            row = self._row(db, operation_id)
            parameters = (operation_id,) if run_id is None else (operation_id, _identifier(run_id, 'run_id'))
            check = db.execute('SELECT * FROM write_protocol_checks WHERE operation_id=? ' +
                ('' if run_id is None else 'AND run_id=? ') + 'ORDER BY created_at DESC,rowid DESC LIMIT 1',
                parameters).fetchone()
            if check is None:
                return None
            check = dict(check)
            refs = [dict(r) for r in db.execute('SELECT * FROM write_protocol_check_evidence '
                                               'WHERE check_id=? ORDER BY evidence_id', (check['check_id'],))]
        if not 1 <= len(refs) <= 8 or hashlib.sha256(check['facts_json'].encode('utf-8')).hexdigest() != check['facts_sha256']:
            raise _deny('Write check proof references failed their integrity check')
        expected = json.loads(check['facts_json'])
        for reference in refs:
            metadata, data = self.evidence.read(reference['evidence_id'], run_id=reference['run_id'],
                                                allow_restricted=True)
            if (metadata['sha256'] != reference['sha256'] or metadata['capture_status'] != 'COMPLETE'
                    or metadata['snapshot_id'] != check['snapshot_id'] or len(data) > 32768):
                raise _deny('Write check proof is incomplete or belongs to another page')
            try:
                if json.loads(data) != expected:
                    raise ValueError('proof mismatch')
            except (ValueError, TypeError, UnicodeError):
                raise _deny('Write check proof facts differ from the original bytes') from None
        return check

    def assert_claim_proof_integrity(self, run_id, claim):
        claim = _claim(claim)
        with connect(self.path) as db:
            run = db.execute('SELECT task_id FROM runs WHERE run_id=?', (_identifier(run_id, 'run_id'),)).fetchone()
            if run is None:
                raise BusinessError('NOT_FOUND', 'Run not found', status=404)
            operation = db.execute('SELECT operation_id FROM write_protocol_claims WHERE business_key=?',
                (business_key(run['task_id'], claim),)).fetchone()
        return self.assert_proof_integrity(operation['operation_id']) if operation is not None else None

    def list_for_run(self, run_id, *, after=0, limit=100):
        _identifier(run_id, 'run_id')
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise BusinessError('INVALID_PARAMETER', 'Invalid bounded write page')
        with connect(self.path) as db:
            ids = [row[0] for row in db.execute('''SELECT l.operation_id FROM write_protocol_links l
                WHERE l.run_id=? ORDER BY l.operation_id LIMIT ? OFFSET ?''', (run_id, limit, after))]
            return [self._row(db, identifier) for identifier in ids]
