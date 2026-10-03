"""Safe identity metadata: credentials and page contents never belong here."""
from dataclasses import asdict, dataclass
import re
from uuid import UUID

from ..errors import BusinessError
from ..network.policy import Endpoint, NetworkDenied


LOGIN_STATES = frozenset({'OPENING', 'AWAITING_USER', 'VERIFYING', 'VERIFIED',
                          'NEEDS_LOGIN', 'LOST', 'CLOSED', 'FAILED'})
FAILURE_REASONS = frozenset({'not_logged_in', 'wrong_account', 'wrong_origin', 'ambiguous_identity',
    'verification_timeout', 'session_lost', 'session_closed', 'manager_restarted',
    'auth_unavailable', 'verification_failed', 'operation_cancelled', 'launch_failed',
    'identity_conflict', 'unknown', 'browser_unavailable', 'navigation_failed', 'not_authenticated',
    'account_mismatch', 'unverifiable', 'verification_changed', 'storage_unavailable'})


def text(value, field, limit=200):
    if (type(value) is not str or not 1 <= len(value) <= limit or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BusinessError('INVALID_PARAMETER', 'Invalid identity metadata', field=field)
    return value


def account(value):
    # The site adapter defines its account's canonical form; do not case-fold a
    # case-sensitive provider's identifier here and accidentally merge users.
    return text(value, 'normalized_account', 256)


def origin(value):
    try:
        endpoint = Endpoint.from_url(value)
    except NetworkDenied:
        raise BusinessError('INVALID_PARAMETER', 'Invalid identity origin', field='origin') from None
    host = '[' + endpoint.host + ']' if ':' in endpoint.host else endpoint.host
    suffix = '' if endpoint.port == (443 if endpoint.scheme == 'https' else 80) else ':' + str(endpoint.port)
    return endpoint.scheme + '://' + host + suffix


def version(value):
    if type(value) is not int or value < 0:
        raise BusinessError('INVALID_PARAMETER', 'Invalid login state version', field='expected_version')
    return value


def receipt(ref, sha256):
    try:
        parsed = UUID(ref) if type(ref) is str else None
        if parsed is None or parsed.version != 4 or str(parsed) != ref:
            raise ValueError
    except (ValueError, TypeError):
        raise BusinessError('INVALID_PARAMETER', 'Invalid protected authentication reference', field='auth_ref') from None
    if type(sha256) is not str or re.fullmatch('[0-9a-f]{64}', sha256) is None:
        raise BusinessError('INVALID_PARAMETER', 'Invalid protected authentication digest', field='auth_ref')


@dataclass(frozen=True)
class LoginInfo:
    login_id: str
    site_id: str
    realm: str
    origin: str
    expected_account: str | None
    expected_identity_ref: str | None
    restore_auth_ref: str | None
    restore_auth_sha256: str | None
    session_id: str | None
    manager_id: str | None
    state: str
    state_version: int
    identity_ref: str | None
    auth_ref: str | None
    auth_sha256: str | None
    verification_id: str | None
    reason: str | None
    created_at: str
    updated_at: str
    candidate_identity_ref: str | None
    candidate_account: str | None

    def as_dict(self):
        result = asdict(self)
        # A candidate is internal unpublished verification work, never an
        # account reference consumers may use to authorize a task.
        for key in ('candidate_identity_ref', 'candidate_account', 'auth_ref', 'auth_sha256', 'manager_id',
                    'restore_auth_ref', 'restore_auth_sha256'):
            result.pop(key)
        if self.state != 'VERIFIED':
            result['identity_ref'] = None
        result['capture_blocked'] = True
        return result


@dataclass(frozen=True)
class IdentityInfo:
    identity_ref: str
    site_id: str
    realm: str
    origin: str
    normalized_account: str
    state: str
    state_version: int
    auth_ref: str
    auth_sha256: str
    last_verification_id: str
    created_at: str
    updated_at: str
    requires_identity_check: bool = True
    requires_business_check: bool = True

    @property
    def status(self):
        return self.state

    def as_dict(self):
        result = asdict(self)
        result.pop('auth_ref')
        result.pop('auth_sha256')
        result['requires_recheck'] = True
        return result
