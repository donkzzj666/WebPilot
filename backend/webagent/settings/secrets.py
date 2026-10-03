"""Opaque credential references backed by native macOS Keychain Services.

No credentials are accepted through process arguments, written to plaintext
files, or fetched from environment variables. Every configuration version uses
a new UUID reference; ``put`` deliberately never updates an existing item.

Native Keychain operations are synchronous. Call them off the async event loop.
UI authentication is disabled; an unavailable/locked store fails closed.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import sys
import threading
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import SecretStr

SERVICE = "WebPilot.model-credentials.v1"
AUTH_SERVICE = "WebPilot.browser-auth-keys.v1"
MAX_SECRET_BYTES = 8192
_REASONS = frozenset({"missing", "locked", "access_denied", "unavailable", "unsupported", "invalid",
                      "already_exists", "invalid_reference", "invalid_secret"})


class CredentialError(Exception):
    """Only a fixed reason crosses the secret boundary; no native error text."""

    def __init__(self, reason: str):
        self.reason = reason if isinstance(reason, str) and reason in _REASONS else "unavailable"
        super().__init__("Credential store: " + self.reason)


@runtime_checkable
class SecretStore(Protocol):
    def put(self, reference: str, secret: SecretStr) -> None: ...
    def get(self, reference: str) -> SecretStr: ...
    def delete(self, reference: str) -> None: ...


class KeychainBackend(Protocol):
    """Injection seam for a native implementation or isolated unit-test fake."""

    def put(self, service: str, reference: str, data: bytes) -> None: ...
    def get(self, service: str, reference: str) -> bytes: ...
    def delete(self, service: str, reference: str) -> None: ...


def validate_reference(reference: str) -> str:
    try:
        value = UUID(reference) if type(reference) is str else None
    except (ValueError, TypeError, AttributeError):
        value = None
    if value is None or value.version != 4 or str(value) != reference:
        raise CredentialError("invalid_reference") from None
    return reference


def _secret_bytes(secret: SecretStr) -> bytes:
    if not isinstance(secret, SecretStr):
        raise CredentialError("invalid_secret")
    text = secret.get_secret_value()
    if not 1 <= len(text) <= MAX_SECRET_BYTES or any(not 33 <= ord(char) <= 126 for char in text):
        raise CredentialError("invalid_secret")
    return text.encode("ascii")


class MacOSKeychainSecretStore:
    """Lazy native store: default initialization touches no secrets."""

    def __init__(self, *, backend: KeychainBackend | None = None, service: str = SERVICE):
        if service not in (SERVICE, AUTH_SERVICE):
            raise CredentialError("invalid_reference")
        self._backend = backend
        self._service = service
        self._init_lock = threading.Lock()

    @property
    def service(self) -> str:
        """Fixed namespace selected at construction, never by a reference API."""
        return self._service

    def _native(self) -> KeychainBackend:
        if self._backend is None:
            with self._init_lock:
                if self._backend is None:
                    self._backend = NativeKeychainBackend()
        return self._backend

    def put(self, reference: str, secret: SecretStr) -> None:
        validate_reference(reference)
        data = _secret_bytes(secret)
        try:
            self._native().put(self._service, reference, data)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("unavailable") from None

    def get(self, reference: str) -> SecretStr:
        validate_reference(reference)
        try:
            data = self._native().get(self._service, reference)
            if type(data) is not bytes or len(data) > MAX_SECRET_BYTES:
                raise CredentialError("invalid")
            try:
                secret = SecretStr(data.decode("ascii"))
            except UnicodeError:
                raise CredentialError("invalid") from None
            _secret_bytes(secret)
            return secret
        except CredentialError as error:
            if error.reason == "invalid_secret":
                raise CredentialError("invalid") from None
            raise
        except Exception:
            raise CredentialError("unavailable") from None

    def delete(self, reference: str) -> None:
        validate_reference(reference)
        try:
            self._native().delete(self._service, reference)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("unavailable") from None


# Short name retained for callers that select the backend by platform.
MacOSKeychain = MacOSKeychainSecretStore


def _status(status: int) -> None:
    if status == 0:
        return
    reason = {
        -25300: "missing",                  # errSecItemNotFound
        -25299: "already_exists",           # errSecDuplicateItem
        -25293: "access_denied",            # errSecAuthFailed
        -25308: "access_denied",            # errSecInteractionNotAllowed (ACL; lock checked separately)
        -25315: "access_denied",            # errSecInteractionRequired
        -128: "access_denied",              # errSecUserCanceled
        -61: "access_denied",               # errSecWrPerm
        -25292: "access_denied",            # errSecReadOnly
    }.get(status, "unavailable")
    raise CredentialError(reason)


class NativeKeychainBackend:
    """ctypes bridge to SecItem APIs, scoped to one file-based keychain.

    ``keychain_ref`` is only for an explicitly owned temporary probe keychain.
    Without it, each operation gets the default keychain reference (metadata
    only) and scopes every search to it. No global search list is changed.
    File-based Keychain supports this unsigned Python local service without
    the app/provisioning entitlements required by the data-protection store.
    """

    def __init__(self, *, keychain_ref: int | None = None):
        if sys.platform != "darwin":
            raise CredentialError("unsupported")
        if keychain_ref is not None and (type(keychain_ref) is not int or keychain_ref <= 0):
            raise CredentialError("unavailable")
        try:
            self._security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self._keychain_ref = keychain_ref
            self._configure()
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("unavailable") from None

    def _configure(self):
        ref, count, status = ctypes.c_void_p, ctypes.c_long, ctypes.c_int32
        self._cf.CFRelease.argtypes = [ref]
        self._cf.CFRelease.restype = None
        self._cf.CFStringCreateWithBytes.argtypes = [ref, ref, count, ctypes.c_uint32, ctypes.c_ubyte]
        self._cf.CFStringCreateWithBytes.restype = ref
        self._cf.CFDataCreate.argtypes = [ref, ref, count]
        self._cf.CFDataCreate.restype = ref
        self._cf.CFDataGetLength.argtypes = [ref]
        self._cf.CFDataGetLength.restype = count
        self._cf.CFDataGetBytePtr.argtypes = [ref]
        self._cf.CFDataGetBytePtr.restype = ref
        self._cf.CFGetTypeID.argtypes = [ref]
        self._cf.CFGetTypeID.restype = ctypes.c_ulong
        self._cf.CFDataGetTypeID.argtypes = []
        self._cf.CFDataGetTypeID.restype = ctypes.c_ulong
        self._cf.CFDictionaryCreate.argtypes = [ref, ctypes.POINTER(ref), ctypes.POINTER(ref), count, ref, ref]
        self._cf.CFDictionaryCreate.restype = ref
        self._cf.CFArrayCreate.argtypes = [ref, ctypes.POINTER(ref), count, ref]
        self._cf.CFArrayCreate.restype = ref
        self._security.SecItemAdd.argtypes = [ref, ctypes.POINTER(ref)]
        self._security.SecItemAdd.restype = status
        self._security.SecItemCopyMatching.argtypes = [ref, ctypes.POINTER(ref)]
        self._security.SecItemCopyMatching.restype = status
        self._security.SecItemDelete.argtypes = [ref]
        self._security.SecItemDelete.restype = status
        self._security.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(ref)]
        self._security.SecKeychainCopyDefault.restype = status
        self._security.SecKeychainGetStatus.argtypes = [ref, ctypes.POINTER(ctypes.c_uint32)]
        self._security.SecKeychainGetStatus.restype = status
        names = ("kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
                 "kSecValueData", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
                 "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail", "kSecUseKeychain",
                 "kSecMatchSearchList")
        self._keys = {name: ref.in_dll(self._security, name).value for name in names}
        self._true = ref.in_dll(self._cf, "kCFBooleanTrue").value
        self._dictionary_keys = ctypes.addressof(ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryKeyCallBacks"))
        self._dictionary_values = ctypes.addressof(ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryValueCallBacks"))
        self._array_callbacks = ctypes.addressof(ctypes.c_byte.in_dll(self._cf, "kCFTypeArrayCallBacks"))

    @contextmanager
    def _keychain(self):
        owned = self._keychain_ref is None
        value = ctypes.c_void_p(self._keychain_ref)
        if owned:
            _status(self._security.SecKeychainCopyDefault(ctypes.byref(value)))
        if not value.value:
            raise CredentialError("unavailable")
        try:
            flags = ctypes.c_uint32()
            _status(self._security.SecKeychainGetStatus(value, ctypes.byref(flags)))
            if not flags.value & 1:  # kSecUnlockStateStatus; never attempt an interactive unlock
                raise CredentialError("locked")
            yield value.value
        finally:
            if owned:
                self._cf.CFRelease(value)

    @contextmanager
    def _query(self, service: str, reference: str, operation: str, data: bytes | None = None):
        owned = []

        def retain_created(value):
            if not value:
                raise CredentialError("unavailable")
            owned.append(value)
            return value

        def string(text):
            raw = text.encode("utf-8")
            return retain_created(self._cf.CFStringCreateWithBytes(None, raw, len(raw), 0x08000100, False))

        try:
            with self._keychain() as keychain:
                pairs = {
                    self._keys["kSecClass"]: self._keys["kSecClassGenericPassword"],
                    self._keys["kSecAttrService"]: string(service),
                    self._keys["kSecAttrAccount"]: string(reference),
                    self._keys["kSecUseAuthenticationUI"]: self._keys["kSecUseAuthenticationUIFail"],
                }
                if operation == "put":
                    pairs[self._keys["kSecUseKeychain"]] = keychain
                    pairs[self._keys["kSecValueData"]] = retain_created(self._cf.CFDataCreate(None, data, len(data)))
                else:
                    keychains = (ctypes.c_void_p * 1)(keychain)
                    pairs[self._keys["kSecMatchSearchList"]] = retain_created(
                        self._cf.CFArrayCreate(None, keychains, 1, self._array_callbacks))
                    if operation == "get":
                        pairs[self._keys["kSecReturnData"]] = self._true
                        pairs[self._keys["kSecMatchLimit"]] = self._keys["kSecMatchLimitOne"]
                keys = (ctypes.c_void_p * len(pairs))(*pairs)
                values = (ctypes.c_void_p * len(pairs))(*pairs.values())
                query = retain_created(self._cf.CFDictionaryCreate(
                    None, keys, values, len(pairs), self._dictionary_keys, self._dictionary_values))
                yield query
        finally:
            for value in reversed(owned):
                self._cf.CFRelease(value)

    def put(self, service: str, reference: str, data: bytes) -> None:
        with self._query(service, reference, "put", data) as query:
            _status(self._security.SecItemAdd(query, None))

    def get(self, service: str, reference: str) -> bytes:
        result = ctypes.c_void_p()
        with self._query(service, reference, "get") as query:
            try:
                _status(self._security.SecItemCopyMatching(query, ctypes.byref(result)))
                if not result.value or self._cf.CFGetTypeID(result) != self._cf.CFDataGetTypeID():
                    raise CredentialError("unavailable")
                length = self._cf.CFDataGetLength(result)
                if not 0 < length <= MAX_SECRET_BYTES:
                    raise CredentialError("invalid")
                pointer = self._cf.CFDataGetBytePtr(result)
                if not pointer:
                    raise CredentialError("unavailable")
                return ctypes.string_at(pointer, length)
            finally:
                if result.value:
                    self._cf.CFRelease(result)

    def delete(self, service: str, reference: str) -> None:
        with self._query(service, reference, "delete") as query:
            _status(self._security.SecItemDelete(query))
