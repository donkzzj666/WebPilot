"""Encrypted auth snapshot tests: synthetic secrets, no real OS credential reads."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from uuid import uuid4

from pydantic import SecretStr
import pytest

from webagent.sessions import auth
from webagent.sessions.auth import AuthSnapshot, AuthStateError, AuthStateStore
from webagent.settings.secrets import AUTH_SERVICE, CredentialError, MacOSKeychainSecretStore, SERVICE

TOKEN = 'SYNTHETIC_AUTH_SECRET_NOT_FOR_PRODUCTION'
BINDING = {'site_id': 'site-one', 'identity_ref': 'account-one', 'realm': 'public'}
STATE = {
    'cookies': [{'name': 'session', 'value': TOKEN, 'domain': 'example.com', 'path': '/',
                 'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}],
    'origins': [{'origin': 'https://example.com', 'localStorage': [{'name': 'token', 'value': TOKEN}],
                 'indexedDB': [{'name': 'auth-db', 'version': 1, 'stores': [
                     {'name': 'sessions', 'autoIncrement': False, 'keyPath': 'id', 'indexes': [],
                      'records': [{'value': {'id': 'current', 'token': TOKEN}}]},
                     {'name': 'dates', 'autoIncrement': False, 'indexes': [],
                      'records': [{'key': 'date', 'valueEncoded': {'d': '2026-09-29T00:00:00Z'}}]},
                 ]}]}],
}


class FakeStore:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()
        self.get_error = self.put_error = self.delete_error = None

    def put(self, reference, secret):
        with self.lock:
            if self.put_error:
                raise self.put_error
            if reference in self.values:
                raise CredentialError('already_exists')
            self.values[reference] = secret

    def get(self, reference):
        if self.get_error:
            raise self.get_error
        if reference not in self.values:
            raise CredentialError('missing')
        return self.values[reference]

    def delete(self, reference):
        if self.delete_error:
            raise self.delete_error
        del self.values[reference]


@pytest.fixture
def vault(tmp_path):
    keys = FakeStore()
    return AuthStateStore(tmp_path.resolve() / 'encrypted-auth', keys), keys


def file_for(vault, snapshot):
    return vault.directory / (snapshot.ref + '.auth')


def test_aead_roundtrip_is_private_and_contains_only_encrypted_state(vault):
    store, keys = vault
    original = deepcopy(STATE)
    snapshot = store.save(original, **BINDING)
    assert isinstance(snapshot, AuthSnapshot)
    path = file_for(store, snapshot)
    ciphertext = path.read_bytes()
    assert ciphertext.startswith(auth.MAGIC)
    assert TOKEN.encode() not in ciphertext and b'cookies' not in ciphertext
    assert snapshot.sha256 == hashlib.sha256(ciphertext).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_nlink == 1
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert len(base64.b64decode(keys.values[snapshot.ref].get_secret_value())) == 32
    assert original == STATE
    assert store.load(snapshot.ref, expected_sha256=snapshot.sha256, **BINDING) == STATE
    original['cookies'][0]['value'] = 'changed after save'
    assert store.load(snapshot.ref, **BINDING) == STATE
    assert [item.name for item in store.directory.iterdir()] == [snapshot.ref + '.auth']
    assert TOKEN not in repr(snapshot) and TOKEN not in repr(store) and TOKEN not in repr(keys.values)


def test_new_instances_read_snapshots_and_each_save_uses_fresh_key_and_nonce(vault):
    store, keys = vault
    first, second = [store.save(STATE, **BINDING) for _ in range(2)]
    assert first.ref != second.ref and first.sha256 != second.sha256
    assert keys.values[first.ref] != keys.values[second.ref]
    first_bytes, second_bytes = file_for(store, first).read_bytes(), file_for(store, second).read_bytes()
    assert first_bytes[len(auth.MAGIC):len(auth.MAGIC) + 12] != second_bytes[len(auth.MAGIC):len(auth.MAGIC) + 12]
    assert AuthStateStore(store.directory, keys).load(first.ref, **BINDING) == STATE


@pytest.mark.parametrize('binding', [
    {**BINDING, 'site_id': 'other'}, {**BINDING, 'identity_ref': 'other'}, {**BINDING, 'realm': 'webarena'},
])
def test_aad_prevents_cross_site_account_and_realm_restore(vault, binding):
    store, _ = vault
    snapshot = store.save(STATE, **BINDING)
    with pytest.raises(AuthStateError) as error:
        store.load(snapshot.ref, **binding)
    assert error.value.reason == 'integrity_failed' and TOKEN not in str(error.value)


def test_renamed_ciphertext_cannot_be_restored_with_the_same_key(vault):
    store, keys = vault
    snapshot = store.save(STATE, **BINDING)
    other = str(uuid4())
    file_for(store, snapshot).rename(store.directory / (other + '.auth'))
    keys.values[other] = keys.values[snapshot.ref]
    with pytest.raises(AuthStateError) as error:
        store.load(other, **BINDING)
    assert error.value.reason == 'integrity_failed'


@pytest.mark.parametrize('target', ['header', 'nonce', 'ciphertext', 'digest'])
def test_corruption_and_external_digest_mismatch_fail_closed(vault, target):
    store, _ = vault
    snapshot = store.save(STATE, **BINDING)
    if target != 'digest':
        path = file_for(store, snapshot)
        payload = bytearray(path.read_bytes())
        index = 0 if target == 'header' else len(auth.MAGIC) if target == 'nonce' else -1
        payload[index] ^= 1
        path.write_bytes(payload)
    with pytest.raises(AuthStateError) as error:
        store.load(snapshot.ref, expected_sha256='0' * 64 if target == 'digest' else None, **BINDING)
    assert error.value.reason == 'integrity_failed'


@pytest.mark.parametrize('fault', ['missing', 'locked', 'access_denied', 'unavailable', 'unsupported'])
def test_unreadable_key_fails_without_fallback_or_secret_echo(vault, fault, monkeypatch):
    store, keys = vault
    snapshot = store.save(STATE, **BINDING)
    keys.get_error = CredentialError(fault)
    monkeypatch.setenv('WEBAGENT_AUTH_KEY', keys.values[snapshot.ref].get_secret_value())
    with pytest.raises(AuthStateError) as error:
        store.load(snapshot.ref, **BINDING)
    assert error.value.reason == 'key_unavailable' and TOKEN not in str(error.value)


@pytest.mark.parametrize('value', [SecretStr('not base64'), SecretStr(''), SecretStr(base64.b64encode(b'short').decode()), 'raw-key'])
def test_malformed_key_is_never_used(vault, value):
    store, keys = vault
    snapshot = store.save(STATE, **BINDING)
    keys.values[snapshot.ref] = value
    with pytest.raises(AuthStateError) as error:
        store.load(snapshot.ref, **BINDING)
    assert error.value.reason == 'key_unavailable'


def test_wrong_valid_aes_key_is_detected(vault):
    store, keys = vault
    snapshot = store.save(STATE, **BINDING)
    keys.values[snapshot.ref] = SecretStr(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(AuthStateError) as error:
        store.load(snapshot.ref, **BINDING)
    assert error.value.reason == 'integrity_failed'


@pytest.mark.parametrize('reference', ['../../outside', '/tmp/secret', '', uuid4().hex, str(uuid4()).upper(), None, 4])
def test_only_opaque_uuid_references_can_address_auth_files(vault, reference):
    store, keys = vault
    with pytest.raises(AuthStateError) as error:
        store.load(reference, **BINDING)
    assert error.value.reason == 'invalid_reference' and not keys.values


@pytest.mark.parametrize('change', [{'site_id': ''}, {'identity_ref': None}, {'identity_ref': 'a\nb'}, {'realm': 'other'}])
def test_bindings_must_be_explicit_and_bounded(vault, change):
    store, keys = vault
    with pytest.raises(AuthStateError) as error:
        store.save(STATE, **{**BINDING, **change})
    assert error.value.reason == 'invalid_binding' and not keys.values


@pytest.mark.parametrize('state', [
    {}, {'cookies': [], 'origins': [], 'sessionStorage': {}},
    {'cookies': [], 'origins': [{'origin': 'https://example.com', 'localStorage': [], 'sessionStorage': {}}]},
    {'cookies': [], 'origins': [{'origin': 'https://example.com', 'localStorage': [], 'opfs': []}]},
    {'cookies': 'not a list', 'origins': []},
    {'cookies': [{'name': 'incomplete'}], 'origins': []},
    {'cookies': [], 'origins': [{'origin': 'https://user:secret@example.com', 'localStorage': []}]},
    {'cookies': [], 'origins': [{'origin': 'https://example.com/path', 'localStorage': []}]},
    {'cookies': [], 'origins': [{'origin': 'https://example.com', 'localStorage': [{'name': 'key', 'value': 1}]}]},
])
def test_only_complete_supported_storage_state_is_accepted(vault, state):
    store, keys = vault
    with pytest.raises(AuthStateError) as error:
        store.save(state, **BINDING)
    assert error.value.reason == 'invalid_state' and not keys.values
    assert not store.directory.exists()


@pytest.mark.parametrize('field,value', [('expires', float('nan')), ('expires', True), ('sameSite', 'Unknown'), ('secure', 'false')])
def test_cookie_fields_have_strict_types(vault, field, value):
    state = deepcopy(STATE)
    state['cookies'][0][field] = value
    with pytest.raises(AuthStateError):
        vault[0].save(state, **BINDING)


def test_oversized_and_deep_payloads_fail_before_files_or_keys_are_created(vault):
    store, keys = vault
    state = deepcopy(STATE)
    state['origins'][0]['indexedDB'][0]['stores'][0]['records'][0]['value'] = 'x' * (auth.MAX_STATE_BYTES + 1)
    with pytest.raises(AuthStateError):
        store.save(state, **BINDING)
    deep = nested = {}
    for _ in range(50):
        nested['value'] = {}
        nested = nested['value']
    with pytest.raises(AuthStateError):
        store.save(deep, **BINDING)
    assert not keys.values and not store.directory.exists()


@pytest.mark.parametrize('attack', ['symlink', 'hardlink', 'fifo', 'permissions'])
def test_load_rejects_unsafe_file_objects(vault, tmp_path, attack):
    store, _ = vault
    snapshot = store.save(STATE, **BINDING)
    path = file_for(store, snapshot)
    if attack == 'symlink':
        original = tmp_path / 'outside'
        path.rename(original)
        path.symlink_to(original)
    elif attack == 'hardlink':
        os.link(path, tmp_path / 'alias')
    elif attack == 'fifo':
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.chmod(0o644)
    with pytest.raises(AuthStateError) as error:
        store.load(snapshot.ref, **BINDING)
    assert error.value.reason == 'unsafe_storage'


@pytest.mark.parametrize('attack', ['final_symlink', 'parent_symlink', 'broad_permissions'])
def test_directory_symlinks_and_broad_permissions_fail_before_storing_keys(tmp_path, attack):
    target = tmp_path.resolve() / 'private'
    target.mkdir(mode=0o700)
    if attack == 'final_symlink':
        directory = tmp_path / 'alias'
        directory.symlink_to(target)
    elif attack == 'parent_symlink':
        parent = tmp_path / 'alias'
        parent.symlink_to(target)
        directory = parent / 'child'
    else:
        target.chmod(0o755)
        directory = target
    keys = FakeStore()
    store = AuthStateStore(directory, keys)
    with pytest.raises(AuthStateError) as error:
        store.save(STATE, **BINDING)
    assert error.value.reason == 'unsafe_storage' and not keys.values


def test_snapshot_collision_never_overwrites_an_existing_file_or_key(vault, monkeypatch):
    store, keys = vault
    snapshot = store.save(STATE, **BINDING)
    from uuid import UUID
    monkeypatch.setattr(auth, 'uuid4', lambda: UUID(snapshot.ref))
    before = file_for(store, snapshot).read_bytes()
    with pytest.raises(AuthStateError):
        store.save({'cookies': [], 'origins': []}, **BINDING)
    assert file_for(store, snapshot).read_bytes() == before
    assert len(keys.values) == 1 and store.load(snapshot.ref, **BINDING) == STATE


def test_write_failure_removes_unpublished_key_and_encrypted_temporary_file(vault, monkeypatch):
    store, keys = vault
    def fail(*_):
        raise OSError('synthetic error includes ' + TOKEN)
    monkeypatch.setattr(auth.os, 'write', fail)
    with pytest.raises(AuthStateError) as error:
        store.save(STATE, **BINDING)
    assert TOKEN not in str(error.value)
    assert not keys.values and not list(store.directory.iterdir())


def test_atomic_publication_refuses_preexisting_final_name(vault, monkeypatch):
    store, keys = vault
    store.directory.mkdir(mode=0o700)
    fixed = uuid4()
    final = store.directory / (str(fixed) + '.auth')
    final.write_bytes(b'preexisting')
    final.chmod(0o600)
    monkeypatch.setattr(auth, 'uuid4', lambda: fixed)
    with pytest.raises(AuthStateError):
        store.save(STATE, **BINDING)
    assert final.read_bytes() == b'preexisting' and not keys.values
    assert list(store.directory.iterdir()) == [final]


def test_cleanup_failure_is_reported_without_claiming_removal(vault, monkeypatch):
    store, keys = vault
    keys.delete_error = CredentialError('locked')
    monkeypatch.setattr(auth.os, 'write', lambda *_: (_ for _ in ()).throw(OSError('failure')))
    with pytest.raises(AuthStateError) as error:
        store.save(STATE, **BINDING)
    assert error.value.reason == 'cleanup_required' and len(keys.values) == 1


def test_short_writes_are_completed_and_concurrent_saves_are_independent(vault, monkeypatch):
    store, _ = vault
    original = os.write
    monkeypatch.setattr(auth.os, 'write', lambda fd, data: original(fd, data[:17]))
    with ThreadPoolExecutor(max_workers=4) as executor:
        snapshots = list(executor.map(lambda _: store.save(STATE, **BINDING), range(8)))
    assert len({snapshot.ref for snapshot in snapshots}) == 8
    assert all(store.load(snapshot.ref, expected_sha256=snapshot.sha256, **BINDING) == STATE for snapshot in snapshots)


def test_browser_and_model_keys_have_distinct_native_namespaces(tmp_path):
    class Backend:
        def __init__(self):
            self.values = {}
        def put(self, service, reference, data):
            self.values[service, reference] = data
        def get(self, service, reference):
            if (service, reference) not in self.values:
                raise CredentialError('missing')
            return self.values[service, reference]
        def delete(self, service, reference):
            del self.values[service, reference]
    backend = Backend()
    model = MacOSKeychainSecretStore(backend=backend)
    browser = MacOSKeychainSecretStore(backend=backend, service=AUTH_SERVICE)
    store = AuthStateStore(tmp_path.resolve() / 'auth', browser)
    snapshot = store.save(STATE, **BINDING)
    assert (AUTH_SERVICE, snapshot.ref) in backend.values
    assert (SERVICE, snapshot.ref) not in backend.values
    with pytest.raises(CredentialError):
        model.get(snapshot.ref)
    with pytest.raises(AuthStateError) as error:
        AuthStateStore(tmp_path.resolve() / 'other', model)
    assert error.value.reason == 'invalid_key_store'
    assert store.load(snapshot.ref, **BINDING) == STATE


def test_empty_state_is_supported_and_missing_files_fail_closed(vault):
    store, _ = vault
    with pytest.raises(AuthStateError) as error:
        store.load(str(uuid4()), **BINDING)
    assert error.value.reason == 'missing'
    snapshot = store.save({'cookies': [], 'origins': []}, **BINDING)
    assert store.load(snapshot.ref, **BINDING) == {'cookies': [], 'origins': []}
