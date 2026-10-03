"""Snapshot historical outcomes and recheck present evidence without writes.

This reader never constructs VerificationService/EvidenceStore: their ordinary
reads can initialize storage or journal availability changes. Immutable blobs
are read through no-follow descriptors with creation disabled, and failure is
reported only in this transient projection. Historical outcomes remain facts.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from pydantic import BaseModel

from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..evidence.files import ArtifactFiles
from ..evidence.models import ARTIFACT_MIME, MAX_ARTIFACT_BYTES
from ..evidence.redaction import TextRedactor, is_neutral_png, safe_metadata, safe_url
from ..evidence.service import POLICY_VERSION
from ..evidence.store import EvidenceStore
from ..models.schema import ProposeResult
from ..scheduler.models import Resource
from ..tasks.models import TaskContract
from ..verification.models import Check, FieldBinding, Result, RuleEvaluation

MAX_CURSOR = 2**63 - 1
MAX_PAGE = 100
MAX_DOCUMENT = 4_000_000
MAX_REFERENCES = 256
MAX_READ_BYTES = 128 * 1024 * 1024
MAX_DISPLAY_BYTES = 16 * 1024 * 1024
_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
_RUN_FIELDS = ('run_id', 'task_id', 'contract_version', 'parent_run_id', 'state',
               'state_version', 'assistance_count', 'created_at', 'started_at', 'ended_at')


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError('ambiguous_json')
        result[key] = value
    return result


def _json(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_DOCUMENT:
        raise ValueError('oversized_document')
    return json.loads(text, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def _refs(value):
    # Only schema-declared evidence fields count. Dynamic maps such as
    # Grafana variables may legitimately contain a key named evidence_ids.
    if isinstance(value, BaseModel):
        for key in type(value).model_fields:
            child = getattr(value, key)
            if key == 'evidence_ids':
                yield from child
            else:
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def _safe_public(value):
    """A malformed historical payload must not bypass the output boundary."""
    # Whole JSON preserves credential key/value relationships, including
    # nested Check.actual objects whose values have no recognizable prefix.
    if TextRedactor().contains_sensitive(canonical_json(value)):
        raise ValueError('unsafe_public_result')
    return value


def _summary(row):
    for key in ('run_id', 'task_id', 'parent_run_id'):
        if row[key] is not None and not _ID.fullmatch(row[key]):
            raise ValueError('invalid_run_identity')
    return _safe_public({**{key: row[key] for key in _RUN_FIELDS}, 'has_result': bool(row['has_result'])})


def _contract(db, run):
    row = db.execute('SELECT content_json,contract_sha256 FROM contracts WHERE task_id=? AND contract_version=?',
                     (run['task_id'], run['contract_version'])).fetchone()
    if row is None:
        raise ValueError('contract_missing')
    data = _json(row['content_json'])
    contract = TaskContract.model_validate_json(canonical_json(data))
    if (_digest(data) != row['contract_sha256'] or row['contract_sha256'] != run['contract_sha256']
            or (contract.task_id, contract.contract_version) != (run['task_id'], run['contract_version'])):
        raise ValueError('contract_binding')
    return contract


def _result(db, run):
    row = db.execute('SELECT * FROM run_results WHERE run_id=?', (run['run_id'],)).fetchone()
    if row is None:
        return None
    document = _json(row['result_json'])
    if hashlib.sha256(row['result_json'].encode()).hexdigest() != row['result_sha256']:
        raise ValueError('result_integrity')
    result = Result.model_validate_json(canonical_json(document))
    if ((result.run_id, result.task_id, result.contract_version, result.assistance_count,
         result.outcome, row['state_version']) !=
        (run['run_id'], run['task_id'], run['contract_version'], run['assistance_count'],
         run['state'], run['state_version']) or row['outcome'] != result.outcome):
        raise ValueError('result_binding')
    record = db.execute('SELECT * FROM run_verifications WHERE verification_id=? AND run_id=?',
                        (row['verification_id'], run['run_id'])).fetchone()
    contract_row = db.execute('SELECT content_json,contract_sha256 FROM contracts WHERE task_id=? AND contract_version=?',
                              (run['task_id'], run['contract_version'])).fetchone()
    if not record or not contract_row:
        raise ValueError('verification_missing')
    capsule = _json(record['content_json'])
    contract_data = _json(contract_row['content_json'])
    contract = TaskContract.model_validate_json(canonical_json(contract_data))
    if (hashlib.sha256(record['content_json'].encode()).hexdigest() != record['content_sha256']
            or _digest(contract_data) != contract_row['contract_sha256']
            or contract_row['contract_sha256'] != run['contract_sha256']
            or record['contract_sha256'] != run['contract_sha256']
            or record['state_version'] + 1 != row['state_version']
            or (contract.task_id, contract.contract_version, contract.scenario) !=
               (run['task_id'], run['contract_version'], result.scenario)):
        raise ValueError('verification_binding')
    proposal = ProposeResult.model_validate_json(canonical_json(capsule['proposal']))
    bindings = [FieldBinding.model_validate_json(canonical_json(b)) for b in capsule['bindings']]
    evaluation = RuleEvaluation.model_validate_json(canonical_json(capsule['evaluation']))
    checks = [Check.model_validate_json(canonical_json(c)) for c in capsule['checks']]
    if (len(bindings) > 20_000 or len(evaluation.fields) > 20_000
            or _digest([capsule['proposal'], capsule['bindings']]) != record['input_sha256']
            or proposal.items != result.items or proposal.coverage != result.coverage
            or checks != result.checks
            or {(c.criterion_id, c.expected_rule) for c in checks} !=
               {(c.criterion_id, c.expected_rule) for c in contract.acceptance_criteria}
            or len({f.result_path for f in evaluation.fields}) != len(evaluation.fields)
            or any(f.verdict.value == 'PASS' and not f.evidence_ids for f in evaluation.fields)
            or any(len(f.result_path) > 4096 or f.result_path and not f.result_path.startswith('/')
                   or re.search(r'~(?![01])', f.result_path) for f in evaluation.fields)):
        raise ValueError('verification_content_binding')
    public = result.model_dump(mode='json')
    fields = [f.model_dump(mode='json') for f in evaluation.fields]
    _safe_public([public, fields])
    refs = set(_refs(result))
    for field in evaluation.fields:
        refs.update(field.evidence_ids)
    return public, fields, row['verification_id'], contract, sorted(refs)


class _ReadOnlyArtifactFiles(ArtifactFiles):
    """The display projection also opens existing control files read-only."""
    def _control(self, name, *, create=False):
        if create:
            raise BusinessError('EVIDENCE_STORAGE_UNAVAILABLE', 'Evidence unavailable', status=503)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.control_fd)
        item = os.fstat(fd)
        if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or item.st_uid != os.geteuid()
                or item.st_mode & 0o077):
            os.close(fd)
            raise BusinessError('EVIDENCE_STORAGE_UNAVAILABLE', 'Evidence unavailable', status=503)
        return fd


class _EvidenceReader:
    def __init__(self, db, data_dir, now):
        self.db, self.now = db, now
        self.files, self.remaining = None, MAX_READ_BYTES
        self.cache = {}
        try:
            domain = db.execute('SELECT faulted FROM evidence_domain WHERE singleton=1').fetchone()
            if domain is not None and not domain['faulted']:
                self.files = _ReadOnlyArtifactFiles(data_dir, create=False)
        except (OSError, BusinessError):
            pass

    def close(self):
        if self.files is not None:
            self.files.close()

    def physical(self, item):
        identifier = item['evidence_id']
        if identifier in self.cache:
            return self.cache[identifier]
        status, data = 'AVAILABLE', None
        if (item['artifact_kind'] not in ARTIFACT_MIME
                or item['mime_type'] != ARTIFACT_MIME[item['artifact_kind']]
                or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_ARTIFACT_BYTES
                or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                or not _ID.fullmatch(item['evidence_id'])):
            status = 'CORRUPT'
        elif item['availability'] != 'AVAILABLE':
            status = item['availability']
        elif item['expires_at'] is not None and item['expires_at'] <= self.now:
            status = 'EXPIRED'
        elif item['capture_status'] != 'COMPLETE':
            status = 'UNAVAILABLE'
        elif self.files is None or item['size_bytes'] > self.remaining:
            status = 'UNAVAILABLE'
        else:
            self.remaining -= item['size_bytes']
            try:
                self.files.assert_available()
                data = self.files.read(item['artifact_path'], size_bytes=item['size_bytes'], sha256=item['sha256'])
            except FileNotFoundError:
                status = 'MISSING'
            except (OSError, BusinessError):
                status = 'CORRUPT'
        self.cache[identifier] = status, data
        return status, data

    def chain(self, item, run_id):
        seen = set()
        while True:
            if item['run_id'] != run_id or item['evidence_id'] in seen or len(seen) >= 8:
                return 'CORRUPT'
            seen.add(item['evidence_id'])
            status, _ = self.physical(item)
            if status != 'AVAILABLE':
                return status
            if item['original_evidence_id'] is None:
                return 'AVAILABLE'
            try:
                parent = EvidenceStore._metadata(self.db, item['original_evidence_id'], run_id)
                if any(parent[key] != item[key] for key in
                       ('source_url', 'captured_at', 'object_id', 'artifact_kind', 'snapshot_id')):
                    return 'CORRUPT'
                item = parent
            except BusinessError:
                return 'MISSING'

    def display(self, item, run_id):
        status = self.chain(item, run_id)
        if status != 'AVAILABLE':
            return status
        if (item['sensitivity'] not in ('public', 'redacted') or item['redaction_status'] != 'FILTERED'
                or item['policy_version'] != POLICY_VERSION
                or item['artifact_kind'] not in ('text', 'diff', 'screenshot')
                or item['size_bytes'] > MAX_DISPLAY_BYTES):
            return 'BLOCKED'
        _, data = self.physical(item)
        try:
            if item['artifact_kind'] == 'screenshot':
                return 'AVAILABLE' if is_neutral_png(data) else 'BLOCKED'
            if TextRedactor().contains_sensitive(data.decode('utf-8')):
                return 'BLOCKED'
        except (UnicodeError, BusinessError, ValueError):
            return 'BLOCKED'
        return 'AVAILABLE'

    def project(self, identifier, run_id):
        nullable = ('artifact_kind', 'source_url', 'captured_at', 'object_id', 'locator_or_page',
                    'sha256', 'original_evidence_id', 'snapshot_id')
        output = {'evidence_id': identifier, 'run_id': run_id,
                  **dict.fromkeys(nullable), 'availability': 'MISSING',
                  'display_evidence_id': None, 'display_sha256': None,
                  'display_size_bytes': None, 'display_mime_type': None,
                  'display_status': 'MISSING', 'displayable': False}
        try:
            item = EvidenceStore._metadata(self.db, identifier, run_id)
        except BusinessError:
            return output
        output.update({key: item[key] for key in nullable})
        output['source_url'] = safe_url(item['source_url'])
        output['object_id'] = safe_metadata(item['object_id'])
        output['locator_or_page'] = safe_metadata(item['locator_or_page'])
        output['availability'] = self.chain(item, run_id)
        candidate, status = item, self.display(item, run_id)
        if status == 'BLOCKED':
            # Only direct approved copies of this exact capture are eligible.
            children = self.db.execute('''SELECT evidence_id FROM evidence WHERE original_evidence_id=?
                AND run_id=? ORDER BY captured_at DESC,evidence_id LIMIT 17''', (identifier, run_id)).fetchall()
            if len(children) > 16:
                status = 'UNAVAILABLE'
            else:
                for child in children:
                    derivative = EvidenceStore._metadata(self.db, child['evidence_id'], run_id)
                    if any(derivative[key] != item[key] for key in
                           ('source_url', 'captured_at', 'object_id', 'artifact_kind', 'snapshot_id')):
                        continue
                    child_status = self.display(derivative, run_id)
                    if child_status == 'AVAILABLE':
                        candidate, status = derivative, child_status
                        break
                    if child_status != 'BLOCKED':
                        status = child_status
        output['display_status'] = status
        if status == 'AVAILABLE':
            output.update(display_evidence_id=candidate['evidence_id'], display_sha256=candidate['sha256'],
                          display_size_bytes=candidate['size_bytes'], display_mime_type=candidate['mime_type'],
                          displayable=True)
        return output


def _writes(db, task_id, result):
    pending = db.execute("SELECT count(*) FROM write_intents WHERE task_id=? AND status IN ('INTENT','UNKNOWN')",
                         (task_id,)).fetchone()[0]
    rows = db.execute('''SELECT * FROM write_intents WHERE task_id=?
        ORDER BY CASE WHEN status IN ('INTENT','UNKNOWN') THEN 0 ELSE 1 END,rowid DESC LIMIT 101''',
                      (task_id,)).fetchall()
    recorded = {item['operation_id']: item for item in result['side_effects']} if result else {}
    items, contracts, policy_unavailable = [], {}, False
    for row in rows[:100]:
        if any(not _ID.fullmatch(row[key]) for key in ('operation_id', 'originating_run_id')):
            raise ValueError('invalid_write_identity')
        origin_id = row['originating_run_id']
        if origin_id not in contracts:
            # A task shares one ledger across its Runs. Historical authority
            # belongs to the originating Run's frozen contract, independently
            # of which result is selected or whether that result is readable.
            origin = db.execute('SELECT * FROM runs WHERE task_id=? AND run_id=?',
                                (task_id, origin_id)).fetchone()
            try:
                contracts[origin_id] = _contract(db, origin) if origin else None
            except (ValueError, KeyError, TypeError, BusinessError, RecursionError):
                contracts[origin_id] = None
        contract = contracts[origin_id]
        policy = contract.action_policy if contract else None
        # Missing policy is unknown, not evidence that an operation violated
        # it. A separate display blocker below still prevents complete success.
        policy_unavailable |= policy is None
        critical = policy is not None
        operation = row['expected_change']
        claim = db.execute('SELECT target_json FROM write_protocol_claims WHERE operation_id=?',
                           (row['operation_id'],)).fetchone()
        if claim:
            operation = _json(claim['target_json']).get('operation')
        if policy and policy.mode == 'repository_write':
            critical = not (row['target'] == Resource.repository_write(policy.repository).resource_key
                            and operation in policy.allowed_operations and row['identity_ref'] == contract.identity_ref)
        snapshot = recorded.get(row['operation_id'])
        critical = critical or bool(snapshot and snapshot['critical_violation'])
        items.append(dict(operation_id=row['operation_id'], originating_run_id=row['originating_run_id'],
                          status=row['status'], receipt_available=row['receipt'] is not None,
                          critical_violation=critical, recorded_in_selected_result=snapshot is not None))
    return _safe_public(items), pending, len(rows) > 100, policy_unavailable


def results(data_dir: Path, task_id: str, *, run_id: str | None = None,
            before: int | None = None, limit: int = 20) -> dict:
    if (not isinstance(task_id, str) or not _ID.fullmatch(task_id)
            or run_id is not None and (not isinstance(run_id, str) or not _ID.fullmatch(run_id))
            or type(limit) is not int or not 1 <= limit <= MAX_PAGE
            or before is not None and (type(before) is not int or not 1 <= before <= MAX_CURSOR)):
        raise BusinessError('INVALID_PARAMETER', '结果查询参数无效。')
    try:
        path = Path(data_dir) / 'business.sqlite3'
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                     isolation_level=None, timeout=.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            remaining = 5000
            def bounded():
                nonlocal remaining
                remaining -= 1
                return int(remaining < 0)
            db.set_progress_handler(bounded, 1000)
            db.execute('BEGIN')
            now = utc_text()
            task = db.execute('SELECT task_id,current_run_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if task is None:
                raise BusinessError('NOT_FOUND', '任务不存在。', status=404)
            query = '''SELECT r.*,r.rowid AS history_cursor,
                EXISTS(SELECT 1 FROM run_results x WHERE x.run_id=r.run_id) AS has_result
                FROM runs r WHERE r.task_id=?'''
            rows = db.execute(query + (' AND r.rowid<?' if before is not None else '')
                              + ' ORDER BY r.rowid DESC LIMIT ?',
                              (task_id, before, limit + 1) if before is not None else (task_id, limit + 1)).fetchall()
            latest = db.execute('SELECT run_id FROM runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1',
                                (task_id,)).fetchone()
            selected_id = run_id or task['current_run_id'] or (latest['run_id'] if latest else None)
            selected = db.execute(query + ' AND r.run_id=?', (task_id, selected_id)).fetchone() if selected_id else None
            if selected_id and selected is None:
                raise BusinessError('NOT_FOUND', '该任务中不存在所选运行。', status=404)
            output = dict(task_id=task_id, current_run_id=task['current_run_id'],
                          runs=[_summary(r) for r in rows[:limit]],
                          next_cursor=str(rows[limit - 1]['history_cursor']) if len(rows) > limit else None,
                          selected_run=_summary(selected) if selected else None, result_status='NOT_READY',
                          result=None, field_checks=[], verification_id=None,
                          assistance=('assisted' if selected['assistance_count'] else 'autonomous') if selected else None,
                          evidence=[], write_intents=[], pending_write_count=0, write_intents_truncated=False,
                          display_complete_success=False, display_blockers=[], as_of=now)
            contract = None
            if selected:
                try:
                    contract = _contract(db, selected)
                    found = _result(db, selected)
                    if found:
                        result, fields, verification_id, contract, refs = found
                        output.update(result_status='AVAILABLE', result=result, field_checks=fields,
                                      verification_id=verification_id)
                        if len(refs) > MAX_REFERENCES or any(not _ID.fullmatch(eid) for eid in refs):
                            raise ValueError('too_many_references')
                        reader = _EvidenceReader(db, data_dir, now)
                        try:
                            output['evidence'] = [reader.project(eid, selected_id) for eid in refs]
                        finally:
                            reader.close()
                except (ValueError, KeyError, TypeError, BusinessError, RecursionError):
                    output.update(result_status='UNAVAILABLE', result=None, field_checks=[], verification_id=None,
                                  evidence=[], display_blockers=['result_integrity_unavailable'])
                    contract = None
            writes, pending, truncated, policy_unavailable = _writes(db, task_id, output['result'])
            output.update(write_intents=writes, pending_write_count=pending, write_intents_truncated=truncated)
            blockers = output['display_blockers']
            result = output['result']
            if not result:
                blockers.append('result_not_available')
            else:
                if result['outcome'] != 'SUCCEEDED': blockers.append('business_outcome_not_succeeded')
                if not result['coverage']['complete']: blockers.append('coverage_incomplete')
                if result['unresolved']: blockers.append('unresolved_items')
                if not result['checks'] or any(c['verdict'] != 'PASS' for c in result['checks']):
                    blockers.append('criteria_not_passed')
                if not output['field_checks'] or any(f['verdict'] != 'PASS' for f in output['field_checks']):
                    blockers.append('fields_not_passed')
                if not output['evidence'] or any(not e['displayable'] or e['availability'] != 'AVAILABLE'
                                                  for e in output['evidence']):
                    blockers.append('evidence_not_readable')
                if any(e['status'] in ('INTENT', 'UNKNOWN') or e['critical_violation'] for e in result['side_effects']):
                    blockers.append('historical_side_effect_unresolved')
                if any(not w['recorded_in_selected_result'] for w in writes):
                    blockers.append('write_not_in_selected_result')
            if pending: blockers.append('task_writes_pending')
            if policy_unavailable: blockers.append('write_policy_unavailable')
            if any(w['critical_violation'] for w in writes): blockers.append('critical_side_effect')
            if truncated: blockers.append('write_history_truncated')
            output['display_complete_success'] = not blockers
            return output
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError):
        raise BusinessError('STORAGE_UNAVAILABLE', '暂时无法读取结果，请稍后刷新。', status=503) from None
