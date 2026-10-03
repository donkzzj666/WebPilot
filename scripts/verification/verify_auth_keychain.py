#!/usr/bin/env python3
"""M1-08 AES-GCM authentication snapshots with an isolated native Keychain.

Creates and deletes one private temporary macOS keychain, using synthetic state
only. Existing credential values are never read; default/search-list metadata
is compared before and after to verify that it was not changed.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

from webagent.sessions.auth import AuthStateError, AuthStateStore
from webagent.settings.secrets import (
    AUTH_SERVICE, CredentialError, MacOSKeychainSecretStore, NativeKeychainBackend, _status,
)


def verify(output: Path, report: dict) -> None:
    native = NativeKeychainBackend()
    security, cf = native._security, native._cf
    ref = ctypes.c_void_p
    security.SecKeychainCreate.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ref, ctypes.c_ubyte, ref, ctypes.POINTER(ref)]
    security.SecKeychainCreate.restype = ctypes.c_int32
    security.SecKeychainDelete.argtypes = [ref]
    security.SecKeychainDelete.restype = ctypes.c_int32
    security.SecKeychainLock.argtypes = [ref]
    security.SecKeychainLock.restype = ctypes.c_int32
    security.SecKeychainUnlock.argtypes = [ref, ctypes.c_uint32, ref, ctypes.c_ubyte]
    security.SecKeychainUnlock.restype = ctypes.c_int32
    security.SecKeychainCopySearchList.argtypes = [ctypes.POINTER(ref)]
    security.SecKeychainCopySearchList.restype = ctypes.c_int32
    cf.CFEqual.argtypes = [ref, ref]
    cf.CFEqual.restype = ctypes.c_ubyte
    prior_list, prior_default = ref(), ref()
    _status(security.SecKeychainCopySearchList(ctypes.byref(prior_list)))
    _status(security.SecKeychainCopyDefault(ctypes.byref(prior_default)))

    def record(name: str, **details) -> None:
        report['checks'].append({'name': name, 'passed': True, **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    def rejected(call, expected: str) -> None:
        try:
            call()
        except AuthStateError as error:
            assert error.reason == expected
        else:
            raise AssertionError('invalid authentication snapshot was accepted')

    try:
        with tempfile.TemporaryDirectory(prefix='webpilot-auth-keychain-probe-') as directory:
            location = Path(directory).resolve() / ('synthetic-' + uuid4().hex + '.keychain')
            keychain = ref()
            password = secrets.token_hex(32).encode('ascii')
            _status(security.SecKeychainCreate(str(location).encode(), len(password), password,
                                               False, None, ctypes.byref(keychain)))
            assert keychain.value
            try:
                backend = NativeKeychainBackend(keychain_ref=keychain.value)
                keys = MacOSKeychainSecretStore(backend=backend, service=AUTH_SERVICE)
                vault = AuthStateStore(output / 'encrypted-auth', keys)
                token = 'SYNTHETIC_AUTH_' + uuid4().hex
                state = {
                    'cookies': [{'name': 'session', 'value': token, 'domain': 'example.invalid', 'path': '/',
                                 'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}],
                    'origins': [{'origin': 'https://example.invalid', 'localStorage': [{'name': 'token', 'value': token}]}],
                }
                binding = {'site_id': 'synthetic-site', 'identity_ref': 'synthetic-account', 'realm': 'public'}
                snapshot = vault.save(state, **binding)
                assert vault.load(snapshot.ref, expected_sha256=snapshot.sha256, **binding) == state
                encrypted_path = vault.directory / (snapshot.ref + '.auth')
                encrypted = encrypted_path.read_bytes()
                assert token.encode() not in encrypted
                record('native_key_aesgcm_roundtrip_and_ciphertext_only', ciphertext_file=str(encrypted_path.relative_to(output)))

                restarted_keys = MacOSKeychainSecretStore(backend=NativeKeychainBackend(keychain_ref=keychain.value), service=AUTH_SERVICE)
                restarted = AuthStateStore(vault.directory, restarted_keys)
                assert restarted.load(snapshot.ref, expected_sha256=snapshot.sha256, **binding) == state
                record('new_vault_and_native_store_reload_same_snapshot')

                try:
                    MacOSKeychainSecretStore(backend=backend).get(snapshot.ref)
                except CredentialError as error:
                    assert error.reason == 'missing'
                else:
                    raise AssertionError('browser key escaped into model namespace')
                record('browser_key_not_available_in_model_namespace')

                rejected(lambda: restarted.load(snapshot.ref, **{**binding, 'identity_ref': 'other-account'}), 'integrity_failed')
                rejected(lambda: restarted.load(snapshot.ref, **{**binding, 'site_id': 'other-site'}), 'integrity_failed')
                rejected(lambda: restarted.load(snapshot.ref, **{**binding, 'realm': 'webarena'}), 'integrity_failed')
                record('account_site_and_realm_swap_fail_authentication')

                corrupted = bytearray(encrypted)
                corrupted[-1] ^= 1
                encrypted_path.write_bytes(corrupted)
                rejected(lambda: restarted.load(snapshot.ref, **binding), 'integrity_failed')
                encrypted_path.write_bytes(encrypted)
                assert restarted.load(snapshot.ref, expected_sha256=snapshot.sha256, **binding) == state
                record('ciphertext_tamper_rejected')

                _status(security.SecKeychainLock(keychain))
                rejected(lambda: restarted.load(snapshot.ref, **binding), 'key_unavailable')
                record('locked_native_keychain_fails_without_prompt_or_plaintext_fallback')
                _status(security.SecKeychainUnlock(keychain, len(password), password, True))
                assert restarted.load(snapshot.ref, **binding) == state
                record('unlock_restores_access_to_same_bound_snapshot')

                key_text = keys.get(snapshot.ref).get_secret_value()
                keys.delete(snapshot.ref)
                rejected(lambda: restarted.load(snapshot.ref, **binding), 'key_unavailable')
                record('missing_native_key_fails_closed')
                for path in output.rglob('*'):
                    if path.is_file():
                        raw = path.read_bytes()
                        assert token.encode() not in raw and key_text.encode() not in raw and password not in raw
                serialized = json.dumps(report).encode()
                assert token.encode() not in serialized and key_text.encode() not in serialized and password not in serialized
                record('artifacts_and_report_exclude_plaintext_auth_and_keys')
            finally:
                try:
                    _status(security.SecKeychainDelete(keychain))
                finally:
                    cf.CFRelease(keychain)
            assert not location.exists()
            record('temporary_native_keychain_deleted')

        current_list, current_default = ref(), ref()
        try:
            _status(security.SecKeychainCopySearchList(ctypes.byref(current_list)))
            _status(security.SecKeychainCopyDefault(ctypes.byref(current_default)))
            assert cf.CFEqual(prior_list, current_list) and cf.CFEqual(prior_default, current_default)
            record('default_keychain_and_global_search_list_unchanged')
        finally:
            if current_list.value:
                cf.CFRelease(current_list)
            if current_default.value:
                cf.CFRelease(current_default)
    finally:
        cf.CFRelease(prior_list)
        cf.CFRelease(prior_default)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='A new evidence directory; existing directories are refused')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT / 'artifacts/verification/M1-08' / ('auth-keychain-' + stamp)).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-08', 'probe': 'native-auth-keychain', 'passed': False, 'checks': [],
              'scope': 'AES-256-GCM auth snapshots with synthetic state and browser keys in a new private temporary macOS Keychain. Existing user secrets are never read.'}
    try:
        verify(output, report)
        report['passed'] = True
    except (AuthStateError, CredentialError) as error:
        report['error'] = {'type': type(error).__name__, 'reason': error.reason}
    except Exception as error:
        report['error'] = {'type': type(error).__name__, 'reason': 'native_auth_probe_failed'}
    report['artifact_sha256'] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob('*')) if path.is_file()
    }
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'report': str(output / 'report.json'), 'error': report.get('error')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
