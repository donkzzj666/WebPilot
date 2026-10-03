"""Immutable encrypted browser storage snapshots, bound to one account/realm.

Only cookies, localStorage and Playwright's IndexedDB state are persisted. The
session module receives a decrypted dict in memory; no plaintext file, export,
environment fallback, sessionStorage reconstruction or logging is supported.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from ..settings.secrets import AUTH_SERVICE, CredentialError, MacOSKeychainSecretStore, SecretStore, validate_reference

AUTH_KEY_SERVICE = AUTH_SERVICE
MAGIC = b'WPAUTH1\x00'
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = MAX_STATE_BYTES + len(MAGIC) + 12 + 16
Realm = Literal['public', 'webarena']


class AuthStateError(Exception):
    """Only fixed reason codes are public; never expose state/key/path/native text."""
    def __init__(self, reason: str = 'unavailable'):
        allowed = {'invalid_state', 'invalid_binding', 'invalid_reference', 'invalid_digest',
                   'invalid_key_store', 'unsafe_storage', 'missing', 'integrity_failed',
                   'key_unavailable', 'storage_unavailable', 'cleanup_required', 'unavailable'}
        self.reason = reason if reason in allowed else 'unavailable'
        super().__init__('Authentication snapshot: ' + self.reason)


@dataclass(frozen=True)
class AuthSnapshot:
    ref: str
    sha256: str


def _require(condition: bool) -> None:
    if not condition:
        raise AuthStateError('invalid_state')


def _string(value, *, maximum=2 * 1024 * 1024, nonempty=False) -> bool:
    return type(value) is str and (bool(value) or not nonempty) and len(value) <= maximum


def _shape(value, required: set[str], optional=frozenset()) -> None:
    _require(type(value) is dict and required <= set(value) <= required | set(optional))


def _json_tree(value) -> None:
    pending = [(value, 0)]
    count = 0
    text_bytes = 0
    while pending:
        child, depth = pending.pop()
        count += 1
        _require(depth <= 48 and count <= 100000)
        if type(child) is dict:
            _require(all(type(key) is str and len(key) <= 65536 for key in child))
            text_bytes += sum(len(key.encode('utf-8')) for key in child)
            pending.extend((item, depth + 1) for item in child.values())
        elif type(child) is list:
            pending.extend((item, depth + 1) for item in child)
        elif type(child) is float:
            _require(math.isfinite(child))
        elif type(child) is int:
            _require(child.bit_length() <= 1024)
        else:
            _require(child is None or type(child) in (bool, str))
            if type(child) is str:
                _require(len(child) <= MAX_STATE_BYTES)
                text_bytes += len(child.encode('utf-8'))
        _require(text_bytes <= MAX_STATE_BYTES)


def _origin(value) -> None:
    _require(_string(value, maximum=2048, nonempty=True))
    try:
        parts = urlsplit(value)
        _require(parts.scheme in ('http', 'https') and bool(parts.hostname)
                 and parts.username is None and parts.password is None
                 and parts.path in ('', '/') and not parts.query and not parts.fragment
                 and '%' not in parts.netloc and parts.port != 0
                 and '\\' not in value and not any(ord(c) < 33 or ord(c) == 127 for c in value))
    except ValueError:
        raise AuthStateError('invalid_state') from None


def _indexed_db(databases) -> None:
    _require(type(databases) is list and len(databases) <= 100)
    names = set()
    for database in databases:
        _shape(database, {'name', 'version', 'stores'})
        _require(_string(database['name'], maximum=1024, nonempty=True) and database['name'] not in names)
        names.add(database['name'])
        _require(type(database['version']) is int and 1 <= database['version'] <= 2**53 - 1)
        stores = database['stores']
        _require(type(stores) is list and len(stores) <= 1000)
        store_names = set()
        for store in stores:
            _shape(store, {'name', 'autoIncrement', 'records', 'indexes'}, {'keyPath', 'keyPathArray'})
            _require(_string(store['name'], maximum=1024) and store['name'] not in store_names)
            store_names.add(store['name'])
            _require(type(store['autoIncrement']) is bool)
            _key_path(store)
            _require(type(store['records']) is list and len(store['records']) <= 20000)
            for record in store['records']:
                _shape(record, set(), {'key', 'keyEncoded', 'value', 'valueEncoded'})
                _require(('value' in record) != ('valueEncoded' in record))
                _require(not ('key' in record and 'keyEncoded' in record))
            _require(type(store['indexes']) is list and len(store['indexes']) <= 1000)
            for index in store['indexes']:
                _shape(index, {'name', 'multiEntry', 'unique'}, {'keyPath', 'keyPathArray'})
                _require(_string(index['name'], maximum=1024))
                _require(type(index['multiEntry']) is bool and type(index['unique']) is bool)
                _key_path(index)


def _key_path(value: dict) -> None:
    _require(not ('keyPath' in value and 'keyPathArray' in value))
    if 'keyPath' in value:
        _require(_string(value['keyPath'], maximum=1024))
    if 'keyPathArray' in value:
        keys = value['keyPathArray']
        _require(type(keys) is list and len(keys) <= 100 and all(_string(key, maximum=1024) for key in keys))


def validate_state(state: dict) -> bytes:
    """Validate the pinned Playwright storage-state subset and snapshot JSON."""
    try:
        _json_tree(state)
        # Validate exactly the bytes that will be encrypted, detached from any
        # caller-owned mutable dictionaries before applying the shape checks.
        raw = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        _require(len(raw) <= MAX_STATE_BYTES)
        state = json.loads(raw)
        _shape(state, {'cookies', 'origins'})
        _require(type(state['cookies']) is list and len(state['cookies']) <= 10000)
        for cookie in state['cookies']:
            _shape(cookie, {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'}, {'partitionKey'})
            _require(_string(cookie['name'], maximum=4096) and _string(cookie['value'], maximum=65536))
            _require(_string(cookie['domain'], maximum=1024, nonempty=True)
                     and _string(cookie['path'], maximum=4096, nonempty=True) and cookie['path'].startswith('/'))
            _require(type(cookie['expires']) in (int, float) and math.isfinite(cookie['expires']))
            _require(type(cookie['httpOnly']) is bool and type(cookie['secure']) is bool)
            _require(cookie['sameSite'] in ('Strict', 'Lax', 'None'))
            if 'partitionKey' in cookie:
                _require(_string(cookie['partitionKey'], maximum=2048))
        _require(type(state['origins']) is list and len(state['origins']) <= 1000)
        origins = set()
        for origin in state['origins']:
            _shape(origin, {'origin', 'localStorage'}, {'indexedDB'})
            _origin(origin['origin'])
            _require(origin['origin'] not in origins)
            origins.add(origin['origin'])
            entries = origin['localStorage']
            _require(type(entries) is list and len(entries) <= 20000)
            entry_names = set()
            for entry in entries:
                _shape(entry, {'name', 'value'})
                _require(_string(entry['name'], maximum=65536) and _string(entry['value']))
                _require(entry['name'] not in entry_names)
                entry_names.add(entry['name'])
            if 'indexedDB' in origin:
                _indexed_db(origin['indexedDB'])
        return raw
    except AuthStateError:
        raise
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError, RuntimeError):
        raise AuthStateError('invalid_state') from None


def _binding(ref: str, site_id: str, identity_ref: str, realm: Realm) -> bytes:
    for value in (site_id, identity_ref):
        if (type(value) is not str or not value.strip() or len(value) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise AuthStateError('invalid_binding')
    if realm not in ('public', 'webarena'):
        raise AuthStateError('invalid_binding')
    return json.dumps({'format': 'webpilot-auth-state-v1', 'ref': ref, 'site_id': site_id,
                       'identity_ref': identity_ref, 'realm': realm},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _reference(ref: str) -> str:
    try:
        return validate_reference(ref)
    except CredentialError:
        raise AuthStateError('invalid_reference') from None


def _safe_file(info, *, links=1) -> bool:
    return (stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == os.geteuid() and info.st_nlink == links)


class AuthStateStore:
    def __init__(self, directory: Path, key_store: SecretStore | None = None):
        self.directory = Path(directory)
        if not self.directory.is_absolute() or '..' in self.directory.parts:
            raise AuthStateError('unsafe_storage')
        if isinstance(key_store, MacOSKeychainSecretStore) and key_store.service != AUTH_KEY_SERVICE:
            raise AuthStateError('invalid_key_store')
        self._key_store = key_store if key_store is not None else MacOSKeychainSecretStore(service=AUTH_KEY_SERVICE)

    @contextmanager
    def _directory(self, *, create=False):
        """Open every path component without following symlinks; pin with dirfd."""
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open('/', flags)
            for part in self.directory.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise AuthStateError('missing') from None
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.geteuid()):
                raise AuthStateError('unsafe_storage')
            yield descriptor
        except AuthStateError:
            raise
        except OSError:
            raise AuthStateError('unsafe_storage') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def save(self, state: dict, *, site_id: str, identity_ref: str, realm: Realm) -> AuthSnapshot:
        raw = validate_state(state)
        reference = str(uuid4())
        aad = _binding(reference, site_id, identity_ref, realm)
        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        encrypted = MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, aad)
        temporary = '.' + reference + '.' + uuid4().hex + '.tmp'
        filename = reference + '.auth'
        stored_key = False
        published = False
        descriptor = None
        try:
            with self._directory(create=True) as directory:
                try:
                    self._key_store.put(reference, SecretStr(base64.b64encode(key).decode('ascii')))
                    stored_key = True
                except Exception:
                    raise AuthStateError('key_unavailable') from None
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory)
                    info = os.fstat(descriptor)
                    if not _safe_file(info):
                        raise AuthStateError('unsafe_storage')
                    view = memoryview(encrypted)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise AuthStateError('storage_unavailable')
                        view = view[written:]
                    os.fsync(descriptor)
                    named = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                    if (named.st_ino, named.st_dev) != (info.st_ino, info.st_dev) or not _safe_file(named):
                        raise AuthStateError('unsafe_storage')
                    # link() is atomic and fails if the immutable final name exists.
                    os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    published = True
                    named = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                    if (named.st_ino, named.st_dev) != (info.st_ino, info.st_dev) or not _safe_file(named, links=2):
                        raise AuthStateError('unsafe_storage')
                    os.unlink(temporary, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                        descriptor = None
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            return AuthSnapshot(reference, hashlib.sha256(encrypted).hexdigest())
        except Exception as error:
            # Once a final file may exist, retain its key: deleting it on an
            # uncertain fsync outcome could strand a committed encrypted state.
            if stored_key and not published:
                try:
                    self._key_store.delete(reference)
                except Exception:
                    raise AuthStateError('cleanup_required') from None
            if isinstance(error, AuthStateError):
                raise
            raise AuthStateError('storage_unavailable') from None

    def load(self, ref: str, *, site_id: str, identity_ref: str, realm: Realm,
             expected_sha256: str | None = None) -> dict:
        reference = _reference(ref)
        aad = _binding(reference, site_id, identity_ref, realm)
        if expected_sha256 is not None and (type(expected_sha256) is not str
                                            or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None):
            raise AuthStateError('invalid_digest')
        try:
            with self._directory() as directory:
                try:
                    descriptor = os.open(reference + '.auth', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    raise AuthStateError('missing') from None
                try:
                    before = os.fstat(descriptor)
                    if not _safe_file(before) or not len(MAGIC) + 28 < before.st_size <= MAX_FILE_BYTES:
                        raise AuthStateError('unsafe_storage')
                    chunks, total = [], 0
                    while True:
                        chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise AuthStateError('unsafe_storage')
                    after = os.fstat(descriptor)
                    if (not _safe_file(after) or total != before.st_size
                            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                        raise AuthStateError('unsafe_storage')
                    encrypted = b''.join(chunks)
                finally:
                    os.close(descriptor)
        except AuthStateError:
            raise
        except OSError:
            raise AuthStateError('unsafe_storage') from None
        digest = hashlib.sha256(encrypted).hexdigest()
        if not encrypted.startswith(MAGIC) or (expected_sha256 is not None and not hmac.compare_digest(digest, expected_sha256)):
            raise AuthStateError('integrity_failed')
        try:
            secret = self._key_store.get(reference)
            if not isinstance(secret, SecretStr):
                raise ValueError('invalid key')
            key = base64.b64decode(secret.get_secret_value(), validate=True)
            if len(key) != 32:
                raise ValueError('invalid key size')
        except Exception:
            raise AuthStateError('key_unavailable') from None
        nonce = encrypted[len(MAGIC):len(MAGIC) + 12]
        try:
            raw = AESGCM(key).decrypt(nonce, encrypted[len(MAGIC) + 12:], aad)
            state = json.loads(raw)
            if validate_state(state) != raw:
                raise AuthStateError('integrity_failed')
            return state
        except (InvalidTag, ValueError, TypeError, OverflowError, UnicodeError, RecursionError, AuthStateError):
            raise AuthStateError('integrity_failed') from None
