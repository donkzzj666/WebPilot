"""Nonsecret ownership and lifecycle records, safe to keep in business SQLite."""
from dataclasses import asdict, dataclass

from ..errors import BusinessError


def identifier(value, name):
    if type(value) is not str or not 1 <= len(value) <= 200 or not value.strip() or any(ord(c) < 32 for c in value):
        raise BusinessError('INVALID_PARAMETER', 'Invalid session ownership identifier', field=name)
    return value


@dataclass(frozen=True)
class SessionOwner:
    kind: str
    owner_id: str
    site_id: str
    identity_ref: str | None = None
    realm: str = 'public'

    def __post_init__(self):
        if self.kind not in ('run', 'login', 'verification') or self.realm not in ('public', 'webarena'):
            raise BusinessError('INVALID_PARAMETER', 'Invalid session owner kind or realm', field='owner')
        identifier(self.owner_id, 'owner_id')
        identifier(self.site_id, 'site_id')
        if self.identity_ref is not None:
            identifier(self.identity_ref, 'identity_ref')


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    owner: SessionOwner
    manager_id: str
    state: str
    generation: int
    state_version: int
    auth_ref: str | None
    auth_sha256: str | None
    requires_identity_check: bool
    requires_business_check: bool
    restored_from_session_id: str | None
    created_at: str
    closed_at: str | None
    loss_reason: str | None

    def as_dict(self):
        return asdict(self)


LOSS_REASONS = frozenset({'window_closed', 'context_closed', 'page_crashed', 'browser_disconnected',
    'manager_restarted', 'launch_failed', 'context_create_failed', 'auth_unavailable',
    'close_failed', 'shutdown_failed', 'operation_cancelled', 'unknown'})
