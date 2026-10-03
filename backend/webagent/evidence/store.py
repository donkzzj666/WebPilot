"""Atomic evidence index with verified immutable artifacts and durable fault gate."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from pydantic import ValidationError

from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..scheduler.store import validate_in_transaction
from .files import ArtifactFiles
from .models import ARTIFACT_MIME, Publication, unavailable


class EvidenceStore:
    def __init__(self, data_dir: Path, *, fault_hook=None, initialize=True):
        self.data_dir = Path(data_dir)
        self.database = self.data_dir / 'business.sqlite3'
        self.fault_hook = fault_hook
        self.files = ArtifactFiles(self.data_dir, fault_hook=fault_hook, create=initialize)

    def _hook(self, stage):
        if self.fault_hook is not None:
            self.fault_hook(stage)

    @staticmethod
    def _full(error):
        return (isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT)
                or isinstance(error, sqlite3.Error) and
                getattr(error, 'sqlite_errorcode', 0) & 0xFF == sqlite3.SQLITE_FULL
                or isinstance(error, sqlite3.OperationalError) and str(error) == 'database or disk is full')

    def _storage_fault(self):
        self.files.latch_fault()
        try:
            with connect(self.database) as db, transaction(db):
                db.execute("UPDATE evidence_domain SET faulted=1,fault_code='STORAGE_FULL',changed_at=? WHERE singleton=1",
                           (utc_text(),))
        except Exception:
            pass

    @contextmanager
    def _storage_errors(self):
        try:
            yield
        except BaseException as error:
            if self._full(error):
                self._storage_fault()
                raise unavailable() from None
            raise

    def assert_dispatch_allowed(self, *, db=None):
        self.files.assert_available()
        if db is not None:
            row = db.execute('SELECT faulted FROM evidence_domain WHERE singleton=1').fetchone()
            if row is None or row['faulted']:
                raise unavailable()
            return
        try:
            with connect(self.database) as db:
                row = db.execute('SELECT faulted FROM evidence_domain WHERE singleton=1').fetchone()
                if row is None or row['faulted']:
                    raise unavailable()
        except sqlite3.Error:
            raise unavailable() from None

    @staticmethod
    def _qualify(db, run_id, execution_token, expected_state_version):
        row = db.execute('SELECT state_version FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Run not found', status=404)
        if expected_state_version is not None and (type(expected_state_version) is not int
                or expected_state_version < 0 or row['state_version'] != expected_state_version):
            raise BusinessError('STATE_CONFLICT', 'Run state has changed', status=409,
                                current_state_version=row['state_version'])
        scheduled = db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone()
        if execution_token is not None:
            if getattr(execution_token, 'run_id', None) != run_id:
                raise BusinessError('RESOURCE_CONFLICT', 'Evidence qualification belongs to another Run', status=409)
            # Evidence bookkeeping may preserve UNKNOWN writes, never dispatches a new action.
            validate_in_transaction(db, execution_token, allow_reconciling=True)
        elif scheduled:
            raise BusinessError('RESOURCE_CONFLICT', 'Scheduled evidence requires execution qualification', status=409)
        return row

    @staticmethod
    def _event(db, run_id, evidence_id, event_type, payload):
        db.execute('''INSERT INTO evidence_events(run_id,evidence_id,event_type,occurred_at,payload_json)
                      VALUES(?,?,?,?,?)''', (run_id, evidence_id, event_type, utc_text(), canonical_json(payload)))

    def publish(self, run_id, data: bytes, *, source_url, captured_at, object_id, query_scope,
                locator_or_page, excerpt='', artifact_kind='text', sensitivity='restricted', evidence_id=None,
                original_evidence_id=None, redaction_status='BLOCKED', policy_version=None, snapshot_id=None,
                step_id=None, commit_sha=None, test_run_id=None, execution_token=None,
                expected_state_version=None, retain=False):
        identifier = evidence_id if evidence_id is not None else 'evidence-' + uuid4().hex
        try:
            if type(retain) is not bool:
                raise ValueError('Invalid retention option')
            if isinstance(captured_at, str):
                captured_at = datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
            content = Publication(run_id=run_id, evidence_id=identifier, source_url=source_url,
                captured_at=captured_at, object_id=object_id, query_scope=query_scope,
                locator_or_page=locator_or_page, excerpt=excerpt, artifact_kind=artifact_kind,
                sensitivity=sensitivity, original_evidence_id=original_evidence_id,
                redaction_status=redaction_status, policy_version=policy_version, snapshot_id=snapshot_id,
                step_id=step_id, commit_sha=commit_sha, test_run_id=test_run_id)
            if (content.sensitivity == 'redacted' and content.original_evidence_id is None
                    or content.redaction_status == 'FILTERED' and (content.policy_version is None
                        or content.sensitivity == 'restricted')
                    or content.artifact_kind == 'ci' and (content.commit_sha is None or content.test_run_id is None)):
                raise ValueError('Invalid evidence provenance')
        except (ValueError, TypeError, ValidationError):
            raise BusinessError('INVALID_PARAMETER', 'Invalid evidence metadata') from None
        try:
            self.assert_dispatch_allowed()
            with self.files.locked():
                self.assert_dispatch_allowed()
                # The preflight avoids producing an orphan for a known invalid identity.
                with connect(self.database) as db:
                    self._qualify(db, run_id, execution_token, expected_state_version)
                    if db.execute('SELECT 1 FROM evidence WHERE evidence_id=?', (identifier,)).fetchone():
                        raise BusinessError('STATE_CONFLICT', 'Evidence identity already exists', status=409)
                path, digest, size = self.files.publish(data)
                self._hook('before_index')
                with connect(self.database) as db, transaction(db):
                    self._qualify(db, run_id, execution_token, expected_state_version)
                    fault = db.execute('SELECT faulted FROM evidence_domain WHERE singleton=1').fetchone()
                    if not fault or fault['faulted']:
                        raise unavailable()
                    if content.original_evidence_id is not None:
                        original = db.execute('SELECT run_id FROM evidence WHERE evidence_id=?',
                                              (content.original_evidence_id,)).fetchone()
                        if not original or original['run_id'] != run_id:
                            raise BusinessError('INVALID_PARAMETER', 'Derivative must reference an original in the same Run')
                    db.execute('''INSERT INTO evidence(evidence_id,run_id,source_url,captured_at,object_id,
                        query_scope,artifact_path,sha256,locator_or_page,excerpt,sensitivity,capture_status,
                        artifact_kind,original_evidence_id,commit_sha,test_run_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,'COMPLETE',?,?,?,?)''',
                        (identifier, run_id, content.source_url, utc_text(content.captured_at), content.object_id,
                         content.query_scope, path, digest, content.locator_or_page, content.excerpt,
                         content.sensitivity, content.artifact_kind, content.original_evidence_id,
                         content.commit_sha, content.test_run_id))
                    now = utc_text()
                    db.execute('INSERT INTO evidence_artifacts VALUES(?,?,?,?,?,?,?,?)',
                        (identifier, size, ARTIFACT_MIME[artifact_kind], content.redaction_status,
                         content.policy_version, content.snapshot_id, content.step_id, now))
                    db.execute("INSERT INTO evidence_availability VALUES(?,'AVAILABLE',?)", (identifier, now))
                    expires = None if retain else utc_text(datetime.fromisoformat(now.replace('Z', '+00:00')) + timedelta(days=30))
                    db.execute('INSERT INTO evidence_retention VALUES(?,?,?)', (identifier, expires, int(retain)))
                    if content.snapshot_id is not None:
                        db.execute('INSERT INTO observations_evidence VALUES(?,?,?)',
                                   (run_id, content.snapshot_id, identifier))
                    if content.step_id is not None:
                        db.execute('INSERT INTO steps_evidence VALUES(?,?,?)', (run_id, content.step_id, identifier))
                    if not db.execute('SELECT 1 FROM evidence_run_guards WHERE run_id=?', (run_id,)).fetchone():
                        db.execute('INSERT INTO evidence_run_guards VALUES(?,?)', (run_id, now))
                    self._event(db, run_id, identifier, 'published', {'sha256': digest, 'size_bytes': size,
                                'redaction_status': content.redaction_status, 'artifact_kind': artifact_kind})
                    self._hook('before_commit')
                return self.metadata(identifier, run_id=run_id)
        except BaseException as error:
            if self._full(error):
                self._storage_fault()
                raise unavailable() from None
            if isinstance(error, sqlite3.IntegrityError):
                raise BusinessError('STATE_CONFLICT', 'Evidence binding or immutable identity conflict', status=409) from None
            raise

    @staticmethod
    def _metadata(db, evidence_id, run_id=None):
        row = db.execute('''SELECT e.*,a.size_bytes,a.mime_type,a.redaction_status,a.policy_version,
            a.snapshot_id,a.step_id,a.published_at,v.status AS availability,v.checked_at,
            t.expires_at,t.keep_until_explicit_cleanup FROM evidence e
            JOIN evidence_artifacts a USING(evidence_id) JOIN evidence_availability v USING(evidence_id)
            JOIN evidence_retention t USING(evidence_id)
            WHERE e.evidence_id=?''', (evidence_id,)).fetchone()
        if row is None or run_id is not None and row['run_id'] != run_id:
            raise BusinessError('NOT_FOUND', 'Evidence not found', status=404)
        return dict(row)

    def metadata(self, evidence_id, run_id=None):
        with connect(self.database) as db:
            return self._metadata(db, evidence_id, run_id)

    def _availability(self, evidence_id, status):
        with self._storage_errors(), connect(self.database) as db, transaction(db):
            metadata = self._metadata(db, evidence_id)
            if metadata['availability'] == 'EXPIRED' or metadata['availability'] == status:
                return
            db.execute('UPDATE evidence_availability SET status=?,checked_at=? WHERE evidence_id=?',
                       (status, utc_text(), evidence_id))
            self._event(db, metadata['run_id'], evidence_id, status.lower(), {})

    def expire(self, evidence_id, *, run_id=None):
        self.metadata(evidence_id, run_id)
        self._availability(evidence_id, 'EXPIRED')

    def expire_due(self, *, now=None):
        try:
            if type(now) is str:
                now = datetime.fromisoformat(now.replace('Z', '+00:00'))
            current = utc_text(now)
        except (ValueError, TypeError, AttributeError):
            raise BusinessError('INVALID_PARAMETER', 'Expiry time must be an aware UTC timestamp') from None
        result = []
        with self._storage_errors(), connect(self.database) as db, transaction(db):
            for row in db.execute('''SELECT e.evidence_id,e.run_id FROM evidence e
                JOIN evidence_retention t USING(evidence_id) JOIN evidence_availability v USING(evidence_id)
                WHERE t.keep_until_explicit_cleanup=0 AND t.expires_at<=? AND v.status<>'EXPIRED'
                ORDER BY e.evidence_id''', (current,)).fetchall():
                db.execute("UPDATE evidence_availability SET status='EXPIRED',checked_at=? WHERE evidence_id=?",
                           (current, row['evidence_id']))
                self._event(db, row['run_id'], row['evidence_id'], 'expired', {'reason': 'retention'})
                result.append(row['evidence_id'])
        return result

    def read(self, evidence_id, *, run_id=None, allow_restricted=False):
        if type(allow_restricted) is not bool:
            raise BusinessError('INVALID_PARAMETER', 'Invalid evidence read permission')
        metadata = self.metadata(evidence_id, run_id)
        if not allow_restricted and (metadata['sensitivity'] not in ('public', 'redacted')
                or metadata['redaction_status'] != 'FILTERED'):
            raise BusinessError('FORBIDDEN', 'Sensitive evidence has no approved display copy', status=403)
        if metadata['expires_at'] is not None and metadata['expires_at'] <= utc_text():
            self._availability(evidence_id, 'EXPIRED')
            metadata = self.metadata(evidence_id, run_id)
        if metadata['availability'] == 'EXPIRED':
            raise BusinessError('EVIDENCE_EXPIRED', 'Evidence has expired', status=410)
        if metadata['availability'] != 'AVAILABLE':
            raise BusinessError('EVIDENCE_' + metadata['availability'], 'Evidence is unavailable', status=409)
        try:
            with self.files.locked():
                data = self.files.read(metadata['artifact_path'], size_bytes=metadata['size_bytes'], sha256=metadata['sha256'])
                # Re-read policy/expiry before bytes leave the trusted storage boundary.
                current = self.metadata(evidence_id, run_id)
                if current['expires_at'] is not None and current['expires_at'] <= utc_text():
                    self._availability(evidence_id, 'EXPIRED')
                    current = self.metadata(evidence_id, run_id)
                if current['availability'] != 'AVAILABLE':
                    raise BusinessError('EVIDENCE_' + current['availability'], 'Evidence is unavailable', status=409)
            return current, data
        except FileNotFoundError:
            self._availability(evidence_id, 'MISSING')
            raise BusinessError('EVIDENCE_MISSING', 'Evidence artifact is missing', status=409) from None
        except (OSError, BusinessError) as error:
            if isinstance(error, BusinessError) and error.code not in ('EVIDENCE_CORRUPT',):
                raise
            self._availability(evidence_id, 'CORRUPT')
            raise BusinessError('EVIDENCE_CORRUPT', 'Evidence artifact failed verification', status=409) from None

    def scan_orphans(self, *, grace_seconds=3600, cleanup=False):
        if type(grace_seconds) not in (int, float) or not 0 <= grace_seconds <= 31536000 or type(cleanup) is not bool:
            raise BusinessError('INVALID_PARAMETER', 'Invalid orphan cleanup policy')
        result = []
        with self._storage_errors(), self.files.locked():
            cutoff = time.time() - grace_seconds
            candidates = self.files.candidates()
            with connect(self.database) as db, transaction(db):
                for path, size, modified, identity in candidates:
                    if modified > cutoff or db.execute('SELECT 1 FROM evidence WHERE artifact_path=?', (path,)).fetchone():
                        continue
                    now = utc_text()
                    db.execute('''INSERT INTO evidence_orphans VALUES(?,?,?,?,'REGISTERED')
                        ON CONFLICT(artifact_path) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                        size_bytes=excluded.size_bytes,status='REGISTERED' ''', (path, now, now, size))
                    if cleanup:
                        # Both DB writer lock and filesystem publication lock remain held.
                        if not db.execute('SELECT 1 FROM evidence WHERE artifact_path=?', (path,)).fetchone():
                            self.files.unlink(path, identity)
                            db.execute("UPDATE evidence_orphans SET status='CLEANED' WHERE artifact_path=?", (path,))
                    result.append(dict(db.execute('SELECT * FROM evidence_orphans WHERE artifact_path=?', (path,)).fetchone()))
        return result

    def record_filtered_observation(self, snapshot_id, sanitized: dict, *, policy_version,
                                    evidence_ids=(), execution_token=None, expected_state_version=None):
        if (type(sanitized) is not dict or type(policy_version) is not str or not 1 <= len(policy_version) <= 200
                or type(evidence_ids) not in (tuple, list) or any(type(item) is not str for item in evidence_ids)):
            raise BusinessError('INVALID_PARAMETER', 'Invalid filtered observation metadata')
        try:
            payload = canonical_json(sanitized)
            if len(payload.encode()) > 1024 * 1024:
                raise ValueError()
        except (TypeError, ValueError):
            raise BusinessError('INVALID_PARAMETER', 'Invalid filtered observation content') from None
        self.assert_dispatch_allowed()
        try:
            with connect(self.database) as db, transaction(db):
                row = db.execute('SELECT run_id FROM observations WHERE snapshot_id=?', (snapshot_id,)).fetchone()
                if row is None:
                    raise BusinessError('NOT_FOUND', 'Observation not found', status=404)
                self._qualify(db, row['run_id'], execution_token, expected_state_version)
                if (sanitized.get('snapshot_id') != snapshot_id or sanitized.get('run_id') != row['run_id']
                        or sanitized.get('redaction_status') != 'FILTERED'
                        or type(sanitized.get('evidence_ids')) is not list
                        or sanitized['evidence_ids'] != list(evidence_ids)):
                    raise BusinessError('INVALID_PARAMETER', 'Filtered DTO binding mismatch')
                for evidence_id in evidence_ids:
                    evidence = self._metadata(db, evidence_id, row['run_id'])
                    if (evidence['redaction_status'] != 'FILTERED' or evidence['availability'] != 'AVAILABLE'
                            or evidence['policy_version'] != policy_version or evidence['snapshot_id'] != snapshot_id):
                        raise BusinessError('FORBIDDEN', 'Filtered observation requires available display artifacts', status=403)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                db.execute('INSERT INTO filtered_observations VALUES(?,?,?,?,?,?)',
                           (snapshot_id, row['run_id'], policy_version, payload, digest, utc_text()))
                if not db.execute('SELECT 1 FROM evidence_run_guards WHERE run_id=?', (row['run_id'],)).fetchone():
                    db.execute('INSERT INTO evidence_run_guards VALUES(?,?)', (row['run_id'], utc_text()))
                self._event(db, row['run_id'], None, 'filtered_observation', {'snapshot_id': snapshot_id,
                            'policy_version': policy_version, 'evidence_ids': list(evidence_ids), 'sha256': digest})
                return self.filtered_observation_row(db, snapshot_id, row['run_id'])
        except BaseException as error:
            if self._full(error):
                self._storage_fault()
                raise unavailable() from None
            if isinstance(error, sqlite3.IntegrityError):
                raise BusinessError('STATE_CONFLICT', 'Filtered observation is immutable', status=409) from None
            raise

    @staticmethod
    def filtered_observation_row(db, snapshot_id, run_id=None):
        row = db.execute('SELECT * FROM filtered_observations WHERE snapshot_id=?', (snapshot_id,)).fetchone()
        if row is None or run_id is not None and row['run_id'] != run_id:
            raise BusinessError('NOT_FOUND', 'Filtered observation not found', status=404)
        result = dict(row)
        payload = result.pop('content_json')
        if hashlib.sha256(payload.encode()).hexdigest() != result['sha256']:
            raise BusinessError('EVIDENCE_CORRUPT', 'Filtered observation integrity mismatch', status=409)
        result['content'] = json.loads(payload)
        return result

    def filtered_observation(self, snapshot_id, run_id=None):
        with connect(self.database) as db:
            return self.filtered_observation_row(db, snapshot_id, run_id)

    def assert_run_ready(self, run_id, db=None):
        """Verify success dependencies without mutating an active state transaction.

        The caller may already hold SQLite's write lock. Reads use that exact
        connection, and filesystem operations only verify immutable blobs.
        Availability marking is intentionally left to later controlled reads.
        """
        if db is None:
            with connect(self.database) as connection:
                return self.assert_run_ready(run_id, connection)
        self.assert_dispatch_allowed(db=db)
        rows = db.execute('SELECT evidence_id FROM evidence WHERE run_id=?', (run_id,)).fetchall()
        if not rows:
            raise BusinessError('EVIDENCE_MISSING', 'Run has no evidence', status=409)
        artifacts = {}
        for row in rows:
            try:
                item = self._metadata(db, row['evidence_id'], run_id)
            except BusinessError:
                raise BusinessError('EVIDENCE_MISSING', 'Evidence publication details are missing', status=409) from None
            if (item['capture_status'] != 'COMPLETE' or item['availability'] != 'AVAILABLE'
                    or item['expires_at'] is not None and item['expires_at'] <= utc_text()):
                raise BusinessError('EVIDENCE_MISSING', 'Run evidence is unavailable', status=409)
            try:
                self.files.read(item['artifact_path'], size_bytes=item['size_bytes'], sha256=item['sha256'])
            except FileNotFoundError:
                raise BusinessError('EVIDENCE_MISSING', 'Run evidence artifact is missing', status=409) from None
            except (OSError, BusinessError):
                raise BusinessError('EVIDENCE_CORRUPT', 'Run evidence artifact failed verification', status=409) from None
            artifacts[item['evidence_id']] = item
        observations = db.execute('''SELECT DISTINCT o.* FROM observations o
            WHERE o.run_id=? AND o.source_url<>'about:blank' AND (
                EXISTS(SELECT 1 FROM gateway_observations g WHERE g.snapshot_id=o.snapshot_id)
                OR EXISTS(SELECT 1 FROM evidence_artifacts a WHERE a.snapshot_id=o.snapshot_id AND a.step_id IS NULL))''',
            (run_id,)).fetchall()
        for observation in observations:
            try:
                view = self.filtered_observation_row(db, observation['snapshot_id'], run_id)
            except BusinessError as error:
                code = 'EVIDENCE_CORRUPT' if error.code == 'EVIDENCE_CORRUPT' else 'EVIDENCE_MISSING'
                raise BusinessError(code, 'Captured observation has no complete verified display view', status=409) from None
            content = view['content']
            refs = content.get('evidence_ids') if type(content) is dict else None
            if (type(refs) is not list or not refs or any(type(ref) is not str for ref in refs)
                    or len(refs) != len(set(refs)) or content.get('redaction_status') != 'FILTERED'
                    or any(content.get(key) != observation[key] for key in (
                        'snapshot_id', 'run_id', 'source_url', 'tab_id', 'frame_id', 'page_version', 'width', 'height'))):
                raise BusinessError('EVIDENCE_MISSING', 'Captured observation display binding is incomplete', status=409)
            for ref in refs:
                item = artifacts.get(ref)
                if (item is None or item['snapshot_id'] != observation['snapshot_id']
                        or item['redaction_status'] != 'FILTERED' or item['sensitivity'] not in ('public', 'redacted')
                        or item['policy_version'] != view['policy_version']):
                    raise BusinessError('EVIDENCE_MISSING', 'Captured observation display artifacts are incomplete', status=409)
