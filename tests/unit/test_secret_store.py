"""Secret-store tests inject synthetic backends; they never query real secrets."""
import ctypes
from types import SimpleNamespace
from uuid import uuid4

from pydantic import SecretStr
import pytest

from webagent.settings import secrets as module
from webagent.settings.secrets import (
    CredentialError, MacOSKeychainSecretStore, NativeKeychainBackend, SERVICE,
    SecretStore, _status, validate_reference,
)

SYNTHETIC = "synthetic-only-credential"


class FakeBackend:
    def __init__(self):
        self.values, self.calls = {}, []
        self.error = None

    def put(self, service, reference, data):
        self.calls.append(("put", service, reference))
        if self.error:
            raise self.error
        if (service, reference) in self.values:
            raise CredentialError("already_exists")
        self.values[service, reference] = data

    def get(self, service, reference):
        self.calls.append(("get", service, reference))
        if self.error:
            raise self.error
        if (service, reference) not in self.values:
            raise CredentialError("missing")
        return self.values[service, reference]

    def delete(self, service, reference):
        self.calls.append(("delete", service, reference))
        if self.error:
            raise self.error
        if (service, reference) not in self.values:
            raise CredentialError("missing")
        del self.values[service, reference]


def test_injected_store_keeps_version_references_immutable():
    backend = FakeBackend()
    store = MacOSKeychainSecretStore(backend=backend)
    assert isinstance(store, SecretStore)
    first_ref, next_ref = str(uuid4()), str(uuid4())
    store.put(first_ref, SecretStr(SYNTHETIC))
    assert store.get(first_ref).get_secret_value() == SYNTHETIC
    with pytest.raises(CredentialError) as error:
        store.put(first_ref, SecretStr("replacement"))
    assert error.value.reason == "already_exists"
    store.put(next_ref, SecretStr("replacement"))
    assert store.get(first_ref).get_secret_value() == SYNTHETIC
    assert store.get(next_ref).get_secret_value() == "replacement"
    assert all(call[1] == "WebPilot.model-credentials.v1" for call in backend.calls)
    assert SYNTHETIC not in repr(store) and SYNTHETIC not in repr(store.get(first_ref))
    store.delete(next_ref)
    assert store.get(first_ref).get_secret_value() == SYNTHETIC
    with pytest.raises(CredentialError) as error:
        store.get(next_ref)
    assert error.value.reason == "missing"


@pytest.mark.parametrize("reference", [None, 42, "", "keychain:account", "../../secret", SYNTHETIC,
    "00000000-0000-0000-0000-000000000000", "123e4567-e89b-12d3-a456-426614174000",
    str(uuid4()).upper(), uuid4().hex, "{" + str(uuid4()) + "}", "urn:uuid:" + str(uuid4())])
@pytest.mark.parametrize("operation", ["put", "get", "delete"])
def test_only_canonical_v4_uuid_can_address_credentials(reference, operation):
    backend = FakeBackend()
    store = MacOSKeychainSecretStore(backend=backend)
    with pytest.raises(CredentialError) as error:
        getattr(store, operation)(reference, SecretStr(SYNTHETIC)) if operation == "put" else getattr(store, operation)(reference)
    assert error.value.reason == "invalid_reference"
    assert not backend.calls
    assert SYNTHETIC not in str(error.value)


@pytest.mark.parametrize("secret", ["plain", b"bytes", None, SecretStr(""), SecretStr("space not allowed"),
                                    SecretStr("line\nbreak"), SecretStr("中文"), SecretStr("x" * 8193)])
def test_invalid_secret_never_reaches_native_backend(secret):
    backend = FakeBackend()
    with pytest.raises(CredentialError) as error:
        MacOSKeychainSecretStore(backend=backend).put(str(uuid4()), secret)
    assert error.value.reason == "invalid_secret"
    assert not backend.calls


@pytest.mark.parametrize("value", [b"", b"x" * 8193, b"invalid with space", b"\xff", "plain", None])
def test_malformed_native_payload_is_invalid_without_echo(value):
    backend = FakeBackend()
    reference = str(uuid4())
    backend.values[SERVICE, reference] = value
    with pytest.raises(CredentialError) as error:
        MacOSKeychainSecretStore(backend=backend).get(reference)
    assert error.value.reason == "invalid"


@pytest.mark.parametrize("operation", ["put", "get", "delete"])
@pytest.mark.parametrize("reason", ["missing", "locked", "access_denied", "unavailable", "unsupported", "already_exists", "invalid"])
def test_safe_failures_remain_distinct(operation, reason):
    backend = FakeBackend()
    backend.error = CredentialError(reason)
    store = MacOSKeychainSecretStore(backend=backend)
    with pytest.raises(CredentialError) as error:
        if operation == "put":
            store.put(str(uuid4()), SecretStr(SYNTHETIC))
        else:
            getattr(store, operation)(str(uuid4()))
    assert error.value.reason == reason


@pytest.mark.parametrize("operation", ["put", "get", "delete"])
def test_raw_backend_failures_are_redacted(operation):
    backend = FakeBackend()
    backend.error = RuntimeError("native failed with " + SYNTHETIC)
    store = MacOSKeychainSecretStore(backend=backend)
    with pytest.raises(CredentialError) as error:
        if operation == "put":
            store.put(str(uuid4()), SecretStr(SYNTHETIC))
        else:
            getattr(store, operation)(str(uuid4()))
    assert error.value.reason == "unavailable"
    assert SYNTHETIC not in repr(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_initialization_is_lazy_and_nonmacos_fails_closed(monkeypatch):
    calls = []
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *_: calls.append(True))
    store = MacOSKeychainSecretStore()
    assert not calls
    with pytest.raises(CredentialError) as error:
        store.get(str(uuid4()))
    assert error.value.reason == "unsupported" and not calls


def test_native_library_loading_failure_is_safe(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    def fail(*args):
        raise OSError(SYNTHETIC)
    monkeypatch.setattr(module.ctypes, "CDLL", fail)
    store = MacOSKeychainSecretStore()
    with pytest.raises(CredentialError) as error:
        store.get(str(uuid4()))
    assert error.value.reason == "unavailable" and SYNTHETIC not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(("status", "reason"), [
    (-25300, "missing"), (-25299, "already_exists"), (-25293, "access_denied"),
    (-25308, "access_denied"), (-25315, "access_denied"), (-128, "access_denied"),
    (-61, "access_denied"), (-25292, "access_denied"), (-25291, "unavailable"),
    (-25294, "unavailable"), (-25307, "unavailable"), (-99999, "unavailable"),
])
def test_native_status_classification(status, reason):
    with pytest.raises(CredentialError) as error:
        _status(status)
    assert error.value.reason == reason


def test_success_status_and_unknown_error_reason():
    assert _status(0) is None
    assert CredentialError(SYNTHETIC).reason == "unavailable"
    assert CredentialError({"secret": SYNTHETIC}).reason == "unavailable"
    assert validate_reference(str(uuid4()))


class FakeCF:
    """Model reference lifetimes/query construction without loading Apple code."""
    def __init__(self):
        self.values, self.released, self.buffers = {}, [], []

    def create(self, value):
        reference = 100 + len(self.values)
        self.values[reference] = value
        return reference

    def CFRelease(self, value):
        self.released.append(value.value if isinstance(value, ctypes.c_void_p) else value)

    def CFStringCreateWithBytes(self, _, data, length, encoding, external):
        assert encoding == 0x08000100 and external is False
        return self.create(data[:length].decode())

    def CFDataCreate(self, _, data, length):
        return self.create(data[:length])

    def CFArrayCreate(self, _, values, length, callbacks):
        return self.create(list(values[:length]))

    def CFDictionaryCreate(self, _, keys, values, length, key_callbacks, value_callbacks):
        return self.create(dict(zip(keys[:length], values[:length])))

    def CFGetTypeID(self, reference):
        return 1 if type(self.values[reference.value]) is bytes else 2

    def CFDataGetTypeID(self):
        return 1

    def CFDataGetLength(self, reference):
        return len(self.values[reference.value])

    def CFDataGetBytePtr(self, reference):
        buffer = ctypes.create_string_buffer(self.values[reference.value])
        self.buffers.append(buffer)
        return ctypes.addressof(buffer)


def native_fake(*, explicit=False, flags=7, status=0, payload=SYNTHETIC.encode()):
    native = NativeKeychainBackend.__new__(NativeKeychainBackend)
    native._cf, native._keychain_ref = FakeCF(), 900 if explicit else None
    names = ("kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
             "kSecValueData", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
             "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail", "kSecUseKeychain", "kSecMatchSearchList")
    native._keys = {name: index + 1 for index, name in enumerate(names)}
    native._true = 80
    native._dictionary_keys = native._dictionary_values = native._array_callbacks = 81
    native.calls = []

    def default(result):
        native.calls.append("default")
        result._obj.value = 900
        return 0

    def get_status(reference, result):
        native.calls.append("status")
        result._obj.value = flags
        return 0

    def put(query, result):
        native.calls.append(("put", native._cf.values[query]))
        return status

    def get(query, result):
        native.calls.append(("get", native._cf.values[query]))
        result._obj.value = native._cf.create(payload)
        return status

    def delete(query):
        native.calls.append(("delete", native._cf.values[query]))
        return status

    native._security = SimpleNamespace(SecKeychainCopyDefault=default, SecKeychainGetStatus=get_status,
                                      SecItemAdd=put, SecItemCopyMatching=get, SecItemDelete=delete)
    return native


@pytest.mark.parametrize("operation", ["put", "get", "delete"])
@pytest.mark.parametrize("explicit", [False, True])
def test_native_query_is_scoped_noninteractive_and_releases_objects(operation, explicit):
    native = native_fake(explicit=explicit)
    reference = str(uuid4())
    if operation == "put":
        native.put(SERVICE, reference, SYNTHETIC.encode())
    elif operation == "get":
        assert native.get(SERVICE, reference) == SYNTHETIC.encode()
    else:
        native.delete(SERVICE, reference)
    query = [call[1] for call in native.calls if isinstance(call, tuple)][0]
    keys = native._keys
    assert query[keys["kSecClass"]] == keys["kSecClassGenericPassword"]
    assert native._cf.values[query[keys["kSecAttrService"]]] == SERVICE
    assert native._cf.values[query[keys["kSecAttrAccount"]]] == reference
    assert query[keys["kSecUseAuthenticationUI"]] == keys["kSecUseAuthenticationUIFail"]
    if operation == "put":
        assert query[keys["kSecUseKeychain"]] == 900
        assert native._cf.values[query[keys["kSecValueData"]]] == SYNTHETIC.encode()
    else:
        assert native._cf.values[query[keys["kSecMatchSearchList"]]] == [900]
        if operation == "get":
            assert query[keys["kSecReturnData"]] == native._true
            assert query[keys["kSecMatchLimit"]] == keys["kSecMatchLimitOne"]
    assert ("default" in native.calls) is (not explicit)
    assert (900 in native._cf.released) is (not explicit)
    assert set(native._cf.values).issubset(native._cf.released)


@pytest.mark.parametrize("operation", ["put", "get", "delete"])
def test_locked_keychain_does_not_attempt_any_item_operation(operation):
    native = native_fake(flags=6)
    with pytest.raises(CredentialError) as error:
        getattr(native, operation)(SERVICE, str(uuid4()), SYNTHETIC.encode()) if operation == "put" else getattr(native, operation)(SERVICE, str(uuid4()))
    assert error.value.reason == "locked"
    assert native.calls == ["default", "status"]
    assert native._cf.released == [900]


def test_native_item_error_releases_query_and_result_references():
    native = native_fake(status=-25308)
    with pytest.raises(CredentialError) as error:
        native.get(SERVICE, str(uuid4()))
    assert error.value.reason == "access_denied"
    assert set(native._cf.values).issubset(native._cf.released)
    assert 900 in native._cf.released


@pytest.mark.parametrize("payload", ["not CFData", b"", b"x" * 8193])
def test_native_invalid_cfdata_is_rejected_and_released(payload):
    native = native_fake(payload=payload)
    with pytest.raises(CredentialError) as error:
        native.get(SERVICE, str(uuid4()))
    assert error.value.reason == ("unavailable" if isinstance(payload, str) else "invalid")
    assert set(native._cf.values).issubset(native._cf.released)
