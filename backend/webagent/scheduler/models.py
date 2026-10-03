"""Nonsecret, normalized resource names and durable execution qualifications.

Resource names describe logical ownership, not a URL to visit or credentials to
load. Callers derive the complete requirement set before entering a transaction.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import re
from urllib.parse import urlsplit

from ..errors import BusinessError

RESOURCE_ORDER = {'active_slot': 0, 'site_identity': 1, 'repository_write': 2,
                  'webarena_environment': 3, 'browser_context': 4}
_SITE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,99}\Z', re.ASCII)
_OWNER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z', re.ASCII)
_REPOSITORY = re.compile(r'[A-Za-z0-9_.-]{1,100}\Z', re.ASCII)
_DIGEST = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_SITE_ALIASES = {'github.com': 'github', 'github.dev': 'github', 'github_editor': 'github',
                 'github-editor': 'github', 'github-web-editor': 'github',
                 'github_web_editor': 'github', 'www.github.com': 'github'}


def _invalid(field='resource'):
    return BusinessError('INVALID_PARAMETER', 'Invalid scheduler resource metadata', field=field)


def _identifier(value, field):
    if (type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise _invalid(field)
    return value


def canonical_site(site_id):
    """GitHub and its web editor share a logical site, across spelling/case."""
    if type(site_id) is not str or site_id != site_id.strip():
        raise _invalid('site_id')
    site = site_id.lower()
    if _SITE.fullmatch(site) is None:
        raise _invalid('site_id')
    return _SITE_ALIASES.get(site, site)


def canonical_repository(repository):
    """Normalize a GitHub owner/repo pair or its exact HTTPS root URL."""
    if type(repository) is not str or repository != repository.strip():
        raise _invalid('repository')
    value = repository
    if '://' in value:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != 'https' or parsed.hostname not in ('github.com', 'github.dev')
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
                raise ValueError
            value = parsed.path.strip('/')
        except (ValueError, TypeError):
            raise _invalid('repository') from None
    if value.count('/') != 1:
        raise _invalid('repository')
    owner, name = value.split('/')
    if name.lower().endswith('.git'):
        name = name[:-4]
    if (_OWNER.fullmatch(owner) is None or _REPOSITORY.fullmatch(name) is None
            or name in ('.', '..') or '--' in owner):
        raise _invalid('repository')
    return owner.lower() + '/' + name.lower()


@dataclass(frozen=True)
class Resource:
    resource_type: str
    resource_key: str
    logical_hold: bool = False

    def __post_init__(self):
        if type(self.resource_type) is not str or self.resource_type not in RESOURCE_ORDER or type(self.logical_hold) is not bool:
            raise _invalid()
        _identifier(self.resource_key, 'resource_key')
        prefix, separator, value = self.resource_key.partition(':')
        if not separator or prefix != self.resource_type:
            raise _invalid()
        if self.resource_type == 'active_slot':
            valid = value in ('0', '1') and not self.logical_hold
        elif self.resource_type == 'browser_context':
            valid = _DIGEST.fullmatch(value) is not None
        elif self.resource_type == 'webarena_environment':
            valid = value == 'global'
        elif self.resource_type == 'repository_write':
            valid = value.startswith('github:') and canonical_repository(value[7:]) == value[7:]
        else:
            parts = value.split(':')
            valid = (len(parts) == 3 and parts[0] in ('public', 'webarena')
                     and canonical_site(parts[1]) == parts[1]
                     and (parts[2] == 'anonymous' or _DIGEST.fullmatch(parts[2]) is not None))
        if not valid:
            raise _invalid()

    @classmethod
    def active_slot(cls, index):
        if type(index) is not int or index not in (0, 1):
            raise _invalid('active_slot')
        return cls('active_slot', 'active_slot:' + str(index))

    @classmethod
    def site_identity(cls, site_id, identity_ref=None, *, realm='public'):
        if realm not in ('public', 'webarena'):
            raise _invalid('realm')
        identity = 'anonymous' if identity_ref is None else hashlib.sha256(
            _identifier(identity_ref, 'identity_ref').encode('utf-8')).hexdigest()
        return cls('site_identity', f'site_identity:{realm}:{canonical_site(site_id)}:{identity}', True)

    @classmethod
    def repository_write(cls, repository):
        return cls('repository_write', 'repository_write:github:' + canonical_repository(repository), True)

    @classmethod
    def webarena_environment(cls):
        return cls('webarena_environment', 'webarena_environment:global', True)

    @classmethod
    def browser_context(cls, run_id):
        digest = hashlib.sha256(_identifier(run_id, 'run_id').encode('utf-8')).hexdigest()
        return cls('browser_context', 'browser_context:' + digest, True)

    def as_dict(self):
        return asdict(self)


def resource_site(resource):
    """Return a site's realm/name for the conservative login-versus-Run gate."""
    key = resource.resource_key if isinstance(resource, Resource) else resource
    if type(key) is not str or not key.startswith('site_identity:'):
        return None
    Resource('site_identity', key, True)
    _, realm, site, _ = key.split(':')
    return realm, site


def ordered_resources(resources):
    """Deduplicate before acquisition; one deterministic order for every stage."""
    if type(resources) not in (tuple, list) or len(resources) > 100:
        raise _invalid('resources')
    unique = {}
    for resource in resources:
        if not isinstance(resource, Resource):
            raise _invalid('resources')
        previous = unique.get(resource.resource_key)
        if previous is not None and previous.logical_hold != resource.logical_hold:
            raise _invalid('logical_hold')
        unique[resource.resource_key] = resource
    return tuple(sorted(unique.values(), key=lambda item: (RESOURCE_ORDER[item.resource_type], item.resource_key)))


@dataclass(frozen=True)
class ExecutionToken:
    run_id: str
    worker_id: str
    worker_generation: int
    epoch: int
    state_version: int
    expires_at: str
    resources: tuple[str, ...]

    def __post_init__(self):
        _identifier(self.run_id, 'run_id')
        _identifier(self.worker_id, 'worker_id')
        for field in ('worker_generation', 'epoch', 'state_version'):
            value = getattr(self, field)
            if type(value) is not int or value < (0 if field == 'state_version' else 1):
                raise _invalid(field)
        try:
            if (type(self.expires_at) is not str or len(self.expires_at) != 27
                    or datetime.strptime(self.expires_at, '%Y-%m-%dT%H:%M:%S.%fZ').strftime(
                        '%Y-%m-%dT%H:%M:%S.%fZ') != self.expires_at):
                raise ValueError
        except ValueError:
            raise _invalid('expires_at') from None
        if type(self.resources) is not tuple or not 1 <= len(self.resources) <= 100:
            raise _invalid('resources')
        parsed = []
        for key in self.resources:
            if type(key) is not str:
                raise _invalid('resources')
            parsed.append(Resource(key.split(':', 1)[0], key))
        if len(set(self.resources)) != len(self.resources):
            raise _invalid('resources')
        if tuple(item.resource_key for item in ordered_resources(parsed)) != self.resources:
            raise _invalid('resources')

    def as_dict(self):
        return {**asdict(self), 'resources': list(self.resources)}

    @classmethod
    def from_dict(cls, value):
        fields = {'run_id', 'worker_id', 'worker_generation', 'epoch', 'state_version', 'expires_at', 'resources'}
        if type(value) is not dict or set(value) != fields or type(value.get('resources')) is not list:
            raise _invalid('execution_token')
        return cls(**{**value, 'resources': tuple(value['resources'])})


LeaseToken = ExecutionToken
