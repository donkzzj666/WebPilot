"""M1-07 native Keychain acceptance using a new isolated synthetic keychain."""
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

from pydantic import SecretStr
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from webagent.settings.secrets import CredentialError, MacOSKeychainSecretStore, NativeKeychainBackend, _status
from webagent.api import create_app
from webagent.security import LocalApiPolicy, load_or_create_token
from webagent.config import Settings
from webagent.db import connect
from webagent.settings.service import create_configured_run, load_run_config


def verify_api(output: Path, report: dict, store: MacOSKeychainSecretStore):
    """Exercise real API/service/native-store bindings through ASGI, no sockets."""
    data = output / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    token = load_or_create_token(data)
    api_headers = {'Authorization': 'Bearer ' + token}
    policy = LocalApiPolicy(token, frozenset({'127.0.0.1'}), frozenset({'http://127.0.0.1'}))
    first, second = "synthetic-api-" + uuid4().hex, "synthetic-api-" + uuid4().hex
    responses, references = [], []

    def record(name):
        report["checks"].append({"name": name, "passed": True})

    try:
        with TestClient(create_app(settings, secret_store=store, local_api_policy=policy), base_url="http://127.0.0.1", headers=api_headers) as client:
            initial = client.get("/v1/settings")
            assert initial.status_code == 200 and initial.json()["version"] == 0
            assert initial.json()["readiness"]["credential_status"] == "not_configured"
            initial_saved = client.put("/v1/settings/model", json={
                "expected_version": 0, "model": {}, "api_key": first, "accept_data_sharing": True,
            })
            responses.extend([initial.json(), initial_saved.json()])
            assert initial_saved.status_code == 200
            assert initial_saved.json()["version"] == 1 and initial_saved.json()["readiness"]["ready"]
            assert initial_saved.json()["readiness"]["provider_verified"] is False
            assert initial_saved.headers["cache-control"] == "no-store"
            with connect(settings.business_db) as db:
                references.append(db.execute("SELECT credential_ref FROM model_settings_versions WHERE version=1").fetchone()[0])
            assert store.get(references[0]).get_secret_value() == first
            readback = client.get("/v1/settings")
            assert readback.status_code == 200 and readback.json() == initial_saved.json()
            responses.append(readback.json())
            record("api_put_get_uses_native_store_and_no_store_responses")

            task = client.post("/v1/tasks", json={
                "instruction": "Read synthetic revenue", "source_ids": ["local-fixture"], "scenario": "finance",
                "parameters": {"entity_id": "fixture-company", "report_version": "2025", "period_type": "annual",
                               "metrics": ["revenue"], "currency": "USD"},
            }, headers={"Idempotency-Key": "native-keychain-probe-task"})
            assert task.status_code == 201
            create_configured_run(settings.business_db, store, expected_settings_version=1,
                                  run_id="native-probe-old-run", task_id=task.json()["task"]["task_id"],
                                  contract_version=1, graph_version="probe-v1", graph_state_schema_version="probe-v1")
            rotated = client.put("/v1/settings/model", json={
                "expected_version": 1, "model": {"max_tokens": 512}, "api_key": second,
                "accept_data_sharing": True,
            })
            assert rotated.status_code == 200 and rotated.json()["version"] == 2
            assert rotated.json()["readiness"]["ready"]
            with connect(settings.business_db) as db:
                references.append(db.execute("SELECT credential_ref FROM model_settings_versions WHERE version=2").fetchone()[0])
            assert references[0] != references[1]
            assert store.get(references[0]).get_secret_value() == first
            assert store.get(references[1]).get_secret_value() == second
            old_run = load_run_config(settings.business_db, store, "native-probe-old-run")
            assert old_run.version == 1 and old_run.model.max_tokens == 1024
            assert old_run.api_key.get_secret_value() == first
            responses.append(rotated.json())
            record("native_rotation_preserves_frozen_old_run_credentials")

            # A fresh API instance and new native wrapper still resolve from
            # SQLite and the same explicitly scoped Keychain, not request state.
        with TestClient(create_app(settings, secret_store=store, local_api_policy=policy), base_url="http://127.0.0.1", headers=api_headers) as restarted:
            current = restarted.get("/v1/settings")
            assert current.status_code == 200 and current.json()["version"] == 2
            assert current.json()["readiness"]["ready"]
            responses.append(current.json())
        record("new_api_instance_reads_native_settings")
        with connect(settings.business_db) as db:
            dump = "\n".join(db.iterdump())
            assert first not in dump and second not in dump
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        serialized = json.dumps(responses, ensure_ascii=False)
        assert first not in serialized and second not in serialized
        assert all(reference not in serialized for reference in references)
        assert "api_key" not in serialized and "credential_ref" not in serialized
        for path in data.iterdir():
            if path.is_file():
                raw = path.read_bytes()
                assert first.encode() not in raw and second.encode() not in raw
        record("api_database_and_responses_exclude_secret_values")
        (output / "api-responses.json").write_text(json.dumps(responses, ensure_ascii=False, indent=2) + "\n")
    finally:
        # The whole isolated keychain is deleted by verify even if one step
        # fails, so this never leaves synthetic credentials in a user keychain.
        for reference in references:
            try:
                store.delete(reference)
            except CredentialError as error:
                if error.reason != "missing":
                    raise


def verify(output: Path, report: dict):
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

    def record(name):
        report["checks"].append({"name": name, "passed": True})

    try:
        # Native SecKeychainCreate leaves private keychains out of the user's
        # search list. Never use login.keychain/System.keychain as a filename.
        with tempfile.TemporaryDirectory(prefix="webpilot-native-probe-") as directory:
            location = Path(directory) / ("synthetic-" + uuid4().hex + ".keychain")
            keychain = ref()
            password = secrets.token_hex(32).encode("ascii")
            _status(security.SecKeychainCreate(str(location).encode(), len(password), password,
                                               False, None, ctypes.byref(keychain)))
            assert keychain.value
            try:
                store = MacOSKeychainSecretStore(backend=NativeKeychainBackend(keychain_ref=keychain.value))
                reference, replacement_ref = str(uuid4()), str(uuid4())
                first, second = SecretStr("synthetic-" + uuid4().hex), SecretStr("synthetic-" + uuid4().hex)
                try:
                    store.get(reference)
                except CredentialError as error:
                    assert error.reason == "missing"
                else:
                    raise AssertionError("missing credential unexpectedly existed")
                record("isolated_missing_reference")
                store.put(reference, first)
                assert store.get(reference).get_secret_value() == first.get_secret_value()
                record("native_put_and_get_roundtrip")
                # A new store object proves the first object holds no required
                # plaintext cache; Keychain remains the source of the value.
                restarted = MacOSKeychainSecretStore(backend=NativeKeychainBackend(keychain_ref=keychain.value))
                assert restarted.get(reference).get_secret_value() == first.get_secret_value()
                record("new_store_reads_native_persistence")
                try:
                    store.put(reference, second)
                except CredentialError as error:
                    assert error.reason == "already_exists"
                else:
                    raise AssertionError("immutable credential was overwritten")
                assert store.get(reference).get_secret_value() == first.get_secret_value()
                store.put(replacement_ref, second)
                assert store.get(replacement_ref).get_secret_value() == second.get_secret_value()
                record("new_reference_preserves_original_version")
                _status(security.SecKeychainLock(keychain))
                try:
                    store.get(reference)
                except CredentialError as error:
                    assert error.reason == "locked"
                else:
                    raise AssertionError("locked keychain unexpectedly readable")
                record("locked_store_fails_without_prompt")
                _status(security.SecKeychainUnlock(keychain, len(password), password, True))
                assert store.get(reference).get_secret_value() == first.get_secret_value()
                store.delete(reference)
                store.delete(replacement_ref)
                try:
                    store.get(reference)
                except CredentialError as error:
                    assert error.reason == "missing"
                else:
                    raise AssertionError("deleted credential remained readable")
                record("native_compensation_delete")
                verify_api(output, report, store)
                serialized = json.dumps(report)
                assert first.get_secret_value() not in serialized and second.get_secret_value() not in serialized
                assert password.decode() not in serialized
                record("report_contains_no_credentials")
            finally:
                try:
                    _status(security.SecKeychainDelete(keychain))
                finally:
                    cf.CFRelease(keychain)
            assert not location.exists()
            record("temporary_keychain_deleted")
        current_list, current_default = ref(), ref()
        try:
            _status(security.SecKeychainCopySearchList(ctypes.byref(current_list)))
            _status(security.SecKeychainCopyDefault(ctypes.byref(current_default)))
            assert cf.CFEqual(prior_list, current_list)
            assert cf.CFEqual(prior_default, current_default)
            record("default_and_global_search_list_unchanged")
        finally:
            if current_list.value:
                cf.CFRelease(current_list)
            if current_default.value:
                cf.CFRelease(current_default)
    finally:
        cf.CFRelease(prior_list)
        cf.CFRelease(prior_default)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = (args.output_dir or ROOT / "artifacts/verification/M1-07" / ("keychain-" + stamp)).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"task": "M1-07", "passed": False, "checks": [],
              "scope": "Native macOS Keychain operations in a new temporary private keychain. Synthetic secrets only. Existing credential values are never read; default/search-list metadata is compared only."}
    try:
        verify(output, report)
        report["passed"] = True
    except CredentialError as error:
        report["error"] = {"type": "CredentialError", "reason": error.reason}
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "reason": "native_probe_failed"}
    report["artifact_sha256"] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(output.rglob("*")) if path.is_file() and '.security' not in path.parts}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "report": str(output / "report.json"),
                      "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
