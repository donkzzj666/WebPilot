"""Publish nonsecret snapshots; OS credential work stays outside transactions.

Key rotation allocates a new immutable OS reference. Older settings and Runs
retain their original reference, including when the model parameters coincide.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr, ValidationError

from ..db import StorageBusyError, connect, transaction
from ..db.repository import canonical_json, create_run, utc_text
from ..errors import BusinessError
from ..models.transport import DeepSeekTransport, ModelConfig
from .models import DISCLOSURE, DISCLOSURE_VERSION, ModelConnection, ModelSettingsRequest, RuntimeConfig
from .secrets import CredentialError, SecretStore

REASONS = {
    'not_configured': '尚未保存模型配置。',
    'missing': '模型密钥缺失，请保存密钥后重试。',
    'locked': '系统凭据库已锁定，请解锁后重试。',
    'access_denied': '无法访问系统凭据，请检查本机访问权限。',
    'unavailable': '系统凭据服务暂时不可用。',
    'unsupported': '当前系统尚未支持安全凭据存储。',
    'invalid': '已保存的模型密钥格式无效，请重新保存。',
}


def _sha(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _latest(db):
    return db.execute('SELECT * FROM model_settings_versions ORDER BY version DESC LIMIT 1').fetchone()


def _version(row) -> int:
    return row['version'] if row else 0


def _expect(row, expected: int) -> None:
    if type(expected) is not int or expected < 0:
        raise BusinessError('INVALID_PARAMETER', '配置版本必须为非负整数。')
    if _version(row) != expected:
        raise BusinessError('VERSION_CONFLICT', '配置已更新，请重新读取后再保存。', status=409)


def _snapshot(row) -> tuple[ModelConfig, RuntimeConfig]:
    try:
        model = ModelConnection.model_validate_json(row['model_json'])
        runtime = RuntimeConfig.model_validate_json(row['runtime_json'])
        if (model.config_sha256 != row['model_config_sha256']
                or _sha(runtime.model_dump(mode='json')) != row['runtime_config_sha256']
                or runtime.settings_version != row['version']
                or runtime.model_config_sha256 != model.config_sha256
                or runtime.credential_ref != row['credential_ref']
                or runtime.disclosure_version != row['disclosure_version']):
            raise ValueError('snapshot mismatch')
        # Return the provider-neutral model type with precisely the same digest.
        return ModelConfig.model_validate(model.model_dump(mode='json')), runtime
    except (ValueError, TypeError, KeyError):
        raise BusinessError('CONFIG_SNAPSHOT_INVALID', '配置快照校验失败，无法使用此配置。', status=503) from None


def _read_secret(store: SecretStore, reference: str | None) -> SecretStr:
    if reference is None:
        raise CredentialError('missing')
    try:
        value = store.get(reference)
        raw = value.get_secret_value() if isinstance(value, SecretStr) else None
        if (type(raw) is not str or not 1 <= len(raw) <= 8192
                or any(not 33 <= ord(char) <= 126 for char in raw)):
            raise CredentialError('invalid')
        return value
    except CredentialError:
        raise
    except Exception:
        raise CredentialError('unavailable') from None


def _credential_reason(error: CredentialError) -> str:
    return error.reason if error.reason in REASONS else 'unavailable'


def _view(row, store: SecretStore) -> dict:
    disclosure = {**deepcopy(DISCLOSURE), 'accepted': row is not None}
    model, status = None, 'not_configured'
    if row is not None:
        config, _ = _snapshot(row)
        model = config.model_dump(mode='json')
        try:
            _read_secret(store, row['credential_ref'])
            status = 'available'
        except CredentialError as error:
            status = _credential_reason(error)
    return {
        'version': _version(row), 'model': model,
        'model_config_sha256': row['model_config_sha256'] if row else None,
        'runtime_config_sha256': row['runtime_config_sha256'] if row else None,
        'readiness': {'ready': status == 'available', 'credential_status': status,
                      'provider_verified': False,
                      'reasons': [] if status == 'available' else [
                          {'code': 'MODEL_NOT_CONFIGURED' if status == 'not_configured' else 'CREDENTIAL_' + status.upper(),
                           'message': REASONS[status]}]},
        'disclosure': disclosure, 'task_execution_enabled': status == 'available',
    }


def get_settings(path: Path, store: SecretStore) -> dict:
    with connect(path) as db:
        row = _latest(db)
    # A settings view describes this one immutable version even if another
    # process publishes a newer version while the OS credential is being read.
    return _view(row, store)


def _cleanup_unreferenced(path: Path, store: SecretStore, reference: str) -> bool:
    """Never remove a credential if a commit succeeded or its result is unknown."""
    try:
        with connect(path) as db:
            if db.execute('SELECT 1 FROM model_settings_versions WHERE credential_ref=?', (reference,)).fetchone():
                return True
        store.delete(reference)
        return True
    except CredentialError as error:
        return error.reason == 'missing'
    except Exception:
        return False


def update_model(path: Path, store: SecretStore, request: ModelSettingsRequest) -> dict:
    # Revalidate internal callers too; model_copy() can bypass frozen DTO validation.
    content = {'expected_version': request.expected_version,
               'model': request.model.model_dump(mode='json'),
               'accept_data_sharing': request.accept_data_sharing}
    if 'api_key' in request.model_fields_set:
        content['api_key'] = request.api_key
    try:
        request = ModelSettingsRequest.model_validate(content)
    except ValidationError:
        raise BusinessError('INVALID_PARAMETER', '模型设置参数无效。', field='settings') from None
    with connect(path) as db:
        previous = _latest(db)
        _expect(previous, request.expected_version)
        if previous is not None:
            _snapshot(previous)
    reference = previous['credential_ref'] if previous else None
    fresh_reference = None
    if request.api_key is not None:
        reference = str(uuid4())
        try:
            store.put(reference, request.api_key)
        except CredentialError as error:
            reason = _credential_reason(error)
            raise BusinessError('CREDENTIAL_' + reason.upper(), REASONS[reason], status=503) from None
        except Exception:
            raise BusinessError('CREDENTIAL_UNAVAILABLE', REASONS['unavailable'], status=503) from None
        fresh_reference = reference
    version = request.expected_version + 1
    model = request.model
    runtime = RuntimeConfig(settings_version=version, model_config_sha256=model.config_sha256,
                            credential_ref=reference)
    runtime_hash = _sha(runtime.model_dump(mode='json'))
    try:
        with connect(path) as db, transaction(db):
            _expect(_latest(db), request.expected_version)
            db.execute('INSERT INTO model_settings_versions VALUES (?,?,?,?,?,?,?,?)',
                       (version, canonical_json(model.model_dump(mode='json')), model.config_sha256,
                        reference, canonical_json(runtime.model_dump(mode='json')), runtime_hash,
                        DISCLOSURE_VERSION, utc_text()))
            published = db.execute('SELECT * FROM model_settings_versions WHERE version=?', (version,)).fetchone()
    except BaseException as error:
        cleaned = fresh_reference is None or _cleanup_unreferenced(path, store, fresh_reference)
        if not isinstance(error, Exception):
            raise
        if not cleaned:
            raise BusinessError('CREDENTIAL_CLEANUP_REQUIRED',
                                '配置保存未完成，临时凭据可能需要清理；请先读取当前配置状态。', status=503) from None
        if isinstance(error, (BusinessError, StorageBusyError)):
            raise
        raise BusinessError('SETTINGS_UPDATE_FAILED', '配置保存失败，请重新读取当前版本。', status=503) from None
    return _view(published, store)


@dataclass(frozen=True)
class ResolvedRunConfig:
    version: int
    model: ModelConfig
    runtime: RuntimeConfig
    api_key: SecretStr = field(repr=False)


def _ready_secret(row, store: SecretStore) -> SecretStr:
    try:
        return _read_secret(store, row['credential_ref'])
    except CredentialError as error:
        reason = _credential_reason(error)
        raise BusinessError('CONFIG_NOT_READY', REASONS[reason], status=409) from None


def create_configured_run(path: Path, store: SecretStore, *, expected_settings_version: int,
                          run_id: str, task_id: str, contract_version: int, graph_version: str,
                          graph_state_schema_version: str, parent_run_id: str | None = None) -> dict:
    """Internal future-scheduler entry: freeze settings and create one queued Run.

    This does not dispatch or deduct a future scheduling quota. A budget record
    exists for the model adapter; quota allocation remains the scheduler's job.
    """
    with connect(path) as db:
        snapshot = _latest(db)
        _expect(snapshot, expected_settings_version)
    if snapshot is None:
        raise BusinessError('CONFIG_NOT_READY', REASONS['not_configured'], status=409)
    _snapshot(snapshot)
    _ready_secret(snapshot, store)
    budget_id = str(uuid4())
    with connect(path) as db, transaction(db):
        _expect(_latest(db), expected_settings_version)
        task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if task is None:
            raise BusinessError('NOT_FOUND', '任务不存在。', status=404)
        if task['preparation_status'] != 'READY' or task['current_contract_version'] != contract_version:
            raise BusinessError('STATE_CONFLICT', '任务契约尚未就绪或版本已变化。', status=409)
        if db.execute("SELECT 1 FROM task_compilations WHERE task_id=? AND status='STARTED'", (task_id,)).fetchone():
            raise BusinessError('STATE_CONFLICT', '该任务的编译仍在进行或结果未知，请先处理编译状态。', status=409)
        if db.execute("SELECT 1 FROM runs WHERE task_id=? AND state NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')", (task_id,)).fetchone():
            raise BusinessError('STATE_CONFLICT', '该任务已有未结束的运行。', status=409)
        create_run(db, run_id=run_id, task_id=task_id, contract_version=contract_version,
                   graph_version=graph_version, graph_state_schema_version=graph_state_schema_version,
                   model_config_sha256=snapshot['model_config_sha256'],
                   runtime_config_sha256=snapshot['runtime_config_sha256'], parent_run_id=parent_run_id)
        db.execute('INSERT INTO run_config_snapshots VALUES (?,?,?,?,?)',
                   (run_id, snapshot['version'], snapshot['model_config_sha256'], snapshot['runtime_config_sha256'], utc_text()))
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES (?,?)', (budget_id, run_id))
        db.execute('UPDATE tasks SET current_run_id=?,state_version=state_version+1 WHERE task_id=?', (run_id, task_id))
    return {'run_id': run_id, 'budget_record_ref': budget_id, 'settings_version': snapshot['version'],
            'model_config_sha256': snapshot['model_config_sha256'],
            'runtime_config_sha256': snapshot['runtime_config_sha256']}


def load_run_config(path: Path, store: SecretStore, run_id: str) -> ResolvedRunConfig:
    with connect(path) as db:
        row = db.execute('''SELECT v.*,r.model_config_sha256 AS run_model_hash,
            r.runtime_config_sha256 AS run_runtime_hash FROM run_config_snapshots s
            JOIN model_settings_versions v ON v.version=s.settings_version
            JOIN runs r ON r.run_id=s.run_id WHERE s.run_id=?''', (run_id,)).fetchone()
    if row is None:
        raise BusinessError('CONFIG_SNAPSHOT_MISSING', '该运行没有配置快照，不能采用当前设置替代。', status=409)
    model, runtime = _snapshot(row)
    if row['run_model_hash'] != row['model_config_sha256'] or row['run_runtime_hash'] != row['runtime_config_sha256']:
        raise BusinessError('CONFIG_SNAPSHOT_INVALID', '运行配置摘要不一致。', status=503)
    return ResolvedRunConfig(row['version'], model, runtime, _ready_secret(row, store))


def provider_for_run(path: Path, store: SecretStore, run_id: str) -> DeepSeekTransport:
    snapshot = load_run_config(path, store, run_id)
    return DeepSeekTransport(snapshot.model, snapshot.api_key)


def provider_for_compilation(path: Path, store: SecretStore) -> tuple[ResolvedRunConfig, DeepSeekTransport]:
    """Resolve one immutable settings version before task preparation HTTP.

    A compilation has its own journal and never invents an execution Run. A
    concurrent settings update does not change this already selected snapshot.
    """
    with connect(path) as db:
        row = _latest(db)
    if row is None:
        raise BusinessError('STATE_CONFLICT', '请先保存模型配置和密钥，再编译自然语言任务。', status=409,
                            field='model_settings')
    try:
        model, runtime = _snapshot(row)
        secret = _ready_secret(row, store)
    except BusinessError as error:
        raise BusinessError('STATE_CONFLICT' if error.status == 409 else 'SERVICE_UNAVAILABLE',
                            str(error), status=error.status, field='model_settings') from None
    snapshot = ResolvedRunConfig(row['version'], model, runtime, secret)
    return snapshot, DeepSeekTransport(model, secret)
