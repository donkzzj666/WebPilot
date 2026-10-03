"""Transactional identity publication; browser and credential I/O stay outside.

Candidate identifiers are reservations on a login request, not identities. The
caller must obtain a fresh adapter verification before preparing a candidate,
save its protected state outside SQLite, reverify, then publish with the CAS
version. This store checks all persisted bindings again in one transaction.
"""
from dataclasses import fields
from pathlib import Path
import re
from uuid import uuid4

from ..db import connect, transaction
from ..db.repository import utc_text
from ..errors import BusinessError
from .models import FAILURE_REASONS, IdentityInfo, LoginInfo, account, origin as canonicalize_origin, receipt, text, version


_ACTIVE = ('OPENING', 'AWAITING_USER', 'NEEDS_LOGIN', 'VERIFYING')
_EXPIRED = frozenset({'not_authenticated', 'not_logged_in', 'account_mismatch', 'wrong_account', 'auth_unavailable'})


def _conflict(row=None):
    raise BusinessError('STATE_CONFLICT', 'Login state changed; reload before continuing', status=409,
                        current_state_version=row['state_version'] if row is not None else None)


class IdentityStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _login(db, login_id):
        text(login_id, 'login_id')
        row = db.execute('''SELECT l.*,a.sha256 AS auth_sha256,r.sha256 AS restore_auth_sha256 FROM login_requests l
            LEFT JOIN browser_auth_snapshots a ON a.auth_ref=l.auth_ref
            LEFT JOIN browser_auth_snapshots r ON r.auth_ref=l.restore_auth_ref WHERE login_id=?''', (login_id,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Login request not found', status=404)
        return row

    @staticmethod
    def _identity(db, identity_ref):
        text(identity_ref, 'identity_ref')
        row = db.execute('''SELECT i.*,a.sha256 AS auth_sha256 FROM identities i
            JOIN browser_auth_snapshots a ON a.auth_ref=i.auth_ref WHERE i.identity_ref=?''', (identity_ref,)).fetchone()
        if row is None:
            raise BusinessError('NOT_FOUND', 'Verified identity not found', status=404)
        return row

    @staticmethod
    def _info(row):
        return LoginInfo(**{field.name: row[field.name] for field in fields(LoginInfo)})

    @staticmethod
    def _identity_info(row):
        return IdentityInfo(**{field.name: row[field.name] for field in fields(IdentityInfo)
                              if not field.name.startswith('requires_')})

    @staticmethod
    def _cas(row, expected_version, allowed):
        version(expected_version)
        if row['state_version'] != expected_version or row['state'] not in allowed:
            _conflict(row)

    @staticmethod
    def _session(db, row, *, require_open=True):
        session = db.execute('SELECT * FROM browser_sessions WHERE session_id=?', (row['session_id'],)).fetchone()
        if (session is None or session['owner_kind'] != 'login' or session['owner_id'] != row['login_id']
                or session['site_id'] != row['site_id'] or session['realm'] != row['realm']
                or session['identity_ref'] != row['expected_identity_ref'] or session['manager_id'] != row['manager_id']
                or session['auth_ref'] != row['restore_auth_ref']
                or (require_open and session['state'] != 'OPEN')):
            _conflict(row)
        return session

    def _change(self, db, row, state, *, reason=None, **values):
        # Column names only come from fixed internal call sites.
        columns = {'state': state, 'reason': reason, 'updated_at': utc_text(), **values}
        db.execute('UPDATE login_requests SET ' + ','.join(key + '=?' for key in columns)
                   + ',state_version=state_version+1 WHERE login_id=?', (*columns.values(), row['login_id']))
        return self._info(self._login(db, row['login_id']))

    def create(self, *, site_id, realm, origin: str, expected_account=None, expected_identity_ref=None):
        text(site_id, 'site_id')
        if realm not in ('public', 'webarena'):
            raise BusinessError('INVALID_PARAMETER', 'Invalid identity realm', field='realm')
        canonical_origin = canonicalize_origin(origin)
        if expected_account is not None:
            account(expected_account)
        with connect(self.path) as db, transaction(db):
            restore_auth_ref = None
            if expected_identity_ref is not None:
                identity = self._identity(db, expected_identity_ref)
                if (identity['site_id'], identity['realm'], identity['origin']) != (site_id, realm, canonical_origin):
                    raise BusinessError('FORBIDDEN', 'Identity belongs to another site or realm', status=403)
                if expected_account is not None and expected_account != identity['normalized_account']:
                    raise BusinessError('STATE_CONFLICT', 'Expected account does not match the verified identity', status=409)
                expected_account = identity['normalized_account']
                restore_auth_ref = identity['auth_ref']
            login_id, now = 'login-' + uuid4().hex, utc_text()
            db.execute('''INSERT INTO login_requests(login_id,site_id,realm,origin,expected_account,
                expected_identity_ref,restore_auth_ref,state,state_version,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'OPENING',0,?,?)''',
                (login_id, site_id, realm, canonical_origin, expected_account, expected_identity_ref, restore_auth_ref, now, now))
            return self._info(self._login(db, login_id))

    def get(self, login_id):
        with connect(self.path) as db:
            return self._info(self._login(db, login_id))

    def get_identity(self, identity_ref):
        with connect(self.path) as db:
            return self._identity_info(self._identity(db, identity_ref))

    def list_identities(self):
        with connect(self.path) as db:
            rows = db.execute('''SELECT i.*,a.sha256 AS auth_sha256 FROM identities i
                JOIN browser_auth_snapshots a ON a.auth_ref=i.auth_ref ORDER BY i.created_at,i.identity_ref''')
            return [self._identity_info(row) for row in rows]

    def attach_session(self, login_id, expected_version, session_id, manager_id):
        text(session_id, 'session_id')
        text(manager_id, 'manager_id')
        with connect(self.path) as db, transaction(db):
            row = self._login(db, login_id)
            self._cas(row, expected_version, ('OPENING',))
            attached = dict(row) | {'session_id': session_id, 'manager_id': manager_id}
            self._session(db, attached)
            return self._change(db, row, 'AWAITING_USER', session_id=session_id, manager_id=manager_id)

    def begin_confirm(self, login_id, expected_version):
        with connect(self.path) as db, transaction(db):
            row = self._login(db, login_id)
            self._cas(row, expected_version, ('AWAITING_USER', 'NEEDS_LOGIN'))
            self._session(db, row)
            return self._change(db, row, 'VERIFYING', candidate_identity_ref=None, candidate_account=None)

    def identity_candidate(self, login_id, expected_version, normalized_account):
        account(normalized_account)
        with connect(self.path) as db, transaction(db):
            row = self._login(db, login_id)
            self._cas(row, expected_version, ('VERIFYING',))
            self._session(db, row)
            if row['expected_account'] is not None and row['expected_account'] != normalized_account:
                raise BusinessError('STATE_CONFLICT', 'Verified account does not match the expected account', status=409)
            identity = db.execute('SELECT * FROM identities WHERE site_id=? AND realm=? AND normalized_account=?',
                                 (row['site_id'], row['realm'], normalized_account)).fetchone()
            if identity is not None and identity['origin'] != row['origin']:
                raise BusinessError('STATE_CONFLICT', 'Identity origin changed; verification cannot be published', status=409)
            candidate = identity['identity_ref'] if identity is not None else 'identity-' + uuid4().hex
            if row['expected_identity_ref'] is not None and candidate != row['expected_identity_ref']:
                raise BusinessError('STATE_CONFLICT', 'Verified identity does not match the expected identity', status=409)
            return self._change(db, row, 'VERIFYING', candidate_identity_ref=candidate,
                                candidate_account=normalized_account)

    def finalize_verified(self, login_id, expected_version, *, identity_ref, normalized_account,
                          auth_ref, auth_sha256, verification_origin, adapter_id, evidence_sha256):
        text(identity_ref, 'identity_ref')
        account(normalized_account)
        receipt(auth_ref, auth_sha256)
        canonical_origin = canonicalize_origin(verification_origin)
        text(adapter_id, 'adapter_id')
        if type(evidence_sha256) is not str or re.fullmatch('[0-9a-f]{64}', evidence_sha256) is None:
            raise BusinessError('INVALID_PARAMETER', 'Invalid identity evidence digest', field='evidence_sha256')
        with connect(self.path) as db, transaction(db):
            row = self._login(db, login_id)
            self._cas(row, expected_version, ('VERIFYING',))
            self._session(db, row)
            if (row['candidate_identity_ref'], row['candidate_account'], row['origin']) != (
                    identity_ref, normalized_account, canonical_origin):
                _conflict(row)
            existing = db.execute('SELECT * FROM identities WHERE site_id=? AND realm=? AND normalized_account=?',
                                  (row['site_id'], row['realm'], normalized_account)).fetchone()
            if existing is not None and (existing['identity_ref'] != identity_ref or existing['origin'] != canonical_origin):
                # A competing successful login owns the canonical ref. Never
                # attach a file encrypted under a different candidate's AAD.
                _conflict(row)
            if db.execute('SELECT 1 FROM browser_auth_snapshots WHERE auth_ref=?', (auth_ref,)).fetchone():
                _conflict(row)
            now, verification_id = utc_text(), 'verification-' + uuid4().hex
            db.execute('INSERT INTO browser_auth_snapshots VALUES(?,?,?,?,?,?)',
                       (auth_ref, row['site_id'], identity_ref, row['realm'], auth_sha256, now))
            if existing is None:
                db.execute('''INSERT INTO identities(identity_ref,site_id,realm,origin,normalized_account,
                    state,state_version,auth_ref,last_verification_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,'VERIFIED',0,?,?,?,?)''',
                    (identity_ref, row['site_id'], row['realm'], canonical_origin, normalized_account,
                     auth_ref, verification_id, now, now))
            db.execute('''INSERT INTO identity_verifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (verification_id, login_id, expected_version, identity_ref, row['session_id'], row['site_id'],
                 row['realm'], normalized_account, canonical_origin, adapter_id, evidence_sha256, auth_ref, now))
            if existing is not None:
                db.execute('''UPDATE identities SET state='VERIFIED',state_version=state_version+1,
                    auth_ref=?,last_verification_id=?,updated_at=? WHERE identity_ref=?''',
                    (auth_ref, verification_id, now, identity_ref))
            return self._change(db, row, 'VERIFIED', identity_ref=identity_ref, auth_ref=auth_ref,
                                verification_id=verification_id)

    def fail_confirm(self, login_id, expected_version, reason, state='NEEDS_LOGIN'):
        reason = reason if type(reason) is str and reason in FAILURE_REASONS else 'unknown'
        if state not in ('NEEDS_LOGIN', 'FAILED', 'LOST'):
            raise BusinessError('INVALID_PARAMETER', 'Invalid login failure state', field='state')
        with connect(self.path) as db, transaction(db):
            row = self._login(db, login_id)
            self._cas(row, expected_version, _ACTIVE)
            if row['expected_identity_ref'] is not None and reason in _EXPIRED:
                # Only invalidate the snapshot this session actually restored.
                # An older failing login must not revoke a newer success.
                if row['restore_auth_ref'] is not None:
                    db.execute('''UPDATE identities SET state='NEEDS_LOGIN',state_version=state_version+1,
                        updated_at=? WHERE identity_ref=? AND auth_ref=? AND state<>'NEEDS_LOGIN' ''',
                        (utc_text(), row['expected_identity_ref'], row['restore_auth_ref']))
            return self._change(db, row, 'FAILED' if row['state'] == 'OPENING' else state,
                                reason=reason, candidate_identity_ref=None, candidate_account=None)

    def close(self, login_id, expected_version):
        with connect(self.path) as db, transaction(db):
            row = self._login(db, login_id)
            self._cas(row, expected_version, (*_ACTIVE, 'VERIFIED'))
            return self._change(db, row, 'CLOSED', reason='session_closed')

    def reconcile(self, login_id):
        with connect(self.path) as db, transaction(db):
            row = self._login(db, login_id)
            if row['state'] not in (*_ACTIVE, 'VERIFIED') or row['session_id'] is None:
                return self._info(row)
            session = self._session(db, row, require_open=False)
            if session['state'] == 'LOST':
                return self._change(db, row, 'LOST', reason='session_lost')
            if session['state'] in ('CLOSING', 'CLOSED'):
                return self._change(db, row, 'CLOSED', reason='session_closed')
            return self._info(row)

    def recover_orphans(self, manager_id):
        text(manager_id, 'manager_id')
        with connect(self.path) as db, transaction(db):
            rows = db.execute('''SELECT * FROM login_requests WHERE state IN
                ('OPENING','AWAITING_USER','VERIFYING','NEEDS_LOGIN','VERIFIED')
                AND (manager_id IS NULL OR manager_id<>?)''', (manager_id,)).fetchall()
            return [self._change(db, row, 'LOST', reason='manager_restarted') for row in rows]


IdentitiesStore = IdentityStore
