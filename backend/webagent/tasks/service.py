"""Task preparation transactions and durable idempotency.

No model, browser or network work happens here. A READY task has a validated
contract, but execution/configuration/scheduling remain separate responsibilities.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ..db import connect, transaction
from ..db.repository import add_contract, canonical_json, create_task, get_contract, utc_text
from ..errors import BusinessError
from pydantic import ValidationError
from .compiler import compile_draft
from .models import CreateTaskRequest, TaskContract


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict
    retry_after_seconds: float | None = None


def request_content(model) -> dict:
    content = model.model_dump(mode='json', exclude={'idempotency_key'})
    # Keep existing fixture receipts byte-for-byte compatible across the DTO upgrade.
    if content.get('compiler_mode') == 'fixture':
        for key in ('compiler_mode', 'sources', 'start_urls', 'web_context', 'time_scope'):
            content.pop(key, None)
    return content


def body_digest(content: dict) -> str:
    return hashlib.sha256(canonical_json(content).encode('utf-8')).hexdigest()


def _task(db, task_id):
    row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise BusinessError('NOT_FOUND', 'Task not found', status=404)
    return row


def _runs(db, task_id) -> list[dict]:
    # Explicit public summary; never expose graph internals, configuration or secrets.
    rows = [dict(row) for row in db.execute('''SELECT run_id,task_id,contract_version,
        parent_run_id,state,state_version,blocked_reason,created_at,started_at,ended_at,
        assistance_count FROM runs WHERE task_id=? ORDER BY created_at,run_id''', (task_id,))]
    # Controls require the frozen version after the user updates current model
    # settings. Expose its integer identity, without loading credential/config.
    versions = {}
    if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='run_config_snapshots'").fetchone():
        versions = dict(db.execute('''SELECT s.run_id,s.settings_version FROM run_config_snapshots s
            JOIN runs r USING(run_id) WHERE r.task_id=?''', (task_id,)))
    for row in rows:
        row['settings_version'] = versions.get(row['run_id'], 0)
    return rows


def _contracts(db, task_id) -> list[dict]:
    return [json.loads(row[0]) for row in db.execute(
        'SELECT content_json FROM contracts WHERE task_id=? ORDER BY contract_version', (task_id,))]


def _detail(db, task_id: str, *, compilation_override=None) -> dict:
    row = _task(db, task_id)
    runs = _runs(db, task_id)
    revisions = db.execute('SELECT * FROM task_revisions WHERE task_id=? ORDER BY revision', (task_id,)).fetchall()
    current = revisions[-1] if revisions else None
    requested = json.loads(row['requested_fields_json'])
    task = {key: row[key] for key in ('task_id', 'original_instruction', 'preparation_status',
                                    'current_contract_version', 'current_run_id', 'state_version', 'created_at')}
    task['requested_fields'] = requested
    task['historical_run_ids'] = [run['run_id'] for run in runs]
    result = {
        'task': task,
        'contract_version': current['revision'] if current else row['current_contract_version'],
        'contract': get_contract(db, task_id, row['current_contract_version']) if row['current_contract_version'] else None,
        'missing_fields': requested,
        'draft': json.loads(current['request_json']) if current else None,
        'current_run': next((run for run in runs if run['run_id'] == row['current_run_id']), None),
        'historical_runs': runs,
        'contract_history': _contracts(db, task_id),
        'revisions': [{
            'revision': item['revision'], 'parent_revision': item['parent_revision'], 'kind': item['kind'],
            'contract_version': item['contract_version'], 'missing_fields': json.loads(item['missing_fields_json']),
            'created_at': item['created_at'], 'request_sha256': item['request_sha256'],
        } for item in revisions],
    }
    if current and json.loads(current['request_json']).get('compiler_mode') == 'natural_language':
        from .natural_service import preparation_details
        result.update(preparation_details(db, task_id, revisions, compilation_override))
    return result


def detail(path: Path, task_id: str) -> dict:
    with connect(path) as db:
        # Multiple SELECTs must observe one committed version, without a writer lock.
        db.execute('BEGIN')
        result = _detail(db, task_id)
        db.execute('COMMIT')
        return result


def history(path: Path, task_id: str, *, contracts: bool = False) -> dict:
    with connect(path) as db:
        db.execute('BEGIN')
        _task(db, task_id)
        result = {'task_id': task_id,
                  'contracts' if contracts else 'runs': _contracts(db, task_id) if contracts else _runs(db, task_id)}
        db.execute('COMMIT')
        return result


def _replay(db, scope, key, digest) -> Reply | None:
    row = db.execute('SELECT * FROM api_idempotency WHERE request_scope=? AND idempotency_key=?',
                     (scope, key)).fetchone()
    if row is None:
        compilation = db.execute('SELECT * FROM task_compilations WHERE request_scope=? AND idempotency_key=?',
                                  (scope, key)).fetchone()
        if compilation is None:
            return None
        if compilation['request_sha256'] != digest:
            raise BusinessError('IDEMPOTENCY_CONFLICT', 'Idempotency key was used with different request content', status=409)
        if compilation['status'] == 'STARTED':
            raise BusinessError('STATE_CONFLICT', '编译仍在进行或结果未知；此幂等键不会再次调用模型。', status=409,
                                field='compilation')
        return Reply(compilation['response_status'], json.loads(compilation['response_json']), compilation['retry_after_seconds'])
    if row['request_sha256'] != digest:
        raise BusinessError('IDEMPOTENCY_CONFLICT', 'Idempotency key was used with different request content', status=409)
    return Reply(row['response_status'], json.loads(row['response_json']))


def _persist_reply(db, *, scope, key, digest, task_id, request_id, status) -> Reply:
    body = json.loads(canonical_json({'request_id': request_id, **_detail(db, task_id)}))
    db.execute('''INSERT INTO api_idempotency(request_scope,idempotency_key,request_sha256,
        task_id,response_status,response_json,created_at) VALUES (?,?,?,?,?,?,?)''',
        (scope, key, digest, task_id, status, canonical_json(body), utc_text()))
    return Reply(status, body)


def _commit_revision(db, *, task_id, version, kind, content, submitted, original_instruction, request_id, prior_provenance=None):
    now = utc_text()
    digest = body_digest(submitted)
    natural = content.get('compiler_mode') == 'natural_language'
    trusted = {key: value for key, value in submitted.items() if key != 'web_context'} if natural else submitted
    if natural:
        from .natural import trusted_instruction
        instruction = trusted_instruction(content['instruction'])
        if 'instruction' in trusted:
            trusted['instruction'] = instruction
    provenance = list(prior_provenance or []) + [{'origin': 'api', 'reference': request_id,
                   'content_sha256': body_digest(trusted), 'authorizes_execution': True}]
    if natural:
        from .natural import compile_natural
        if kind != 'clarification':
            provenance.append({'origin': 'user', 'reference': request_id + ':instruction',
                'content_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
                'authorizes_execution': True})
            if instruction != content['instruction']:
                provenance.append({'origin': 'web_content', 'reference': request_id + ':mixed-instruction',
                    'content_sha256': hashlib.sha256(content['instruction'].encode()).hexdigest(),
                    'authorizes_execution': False})
            provenance.extend({'origin': 'web_content', 'reference': request_id + ':context:' + str(index),
                'content_sha256': hashlib.sha256(text.encode()).hexdigest(), 'authorizes_execution': False}
                for index, text in enumerate(content.get('web_context', [])))
        compiler = compile_natural
    else:
        compiler = compile_draft
    compilation = compiler(content, task_id=task_id, version=version, created_at=now, provenance=provenance)
    contract = compilation.contract
    if contract is not None:
        contract['original_instruction'] = original_instruction
        # Use JSON validation to accept serialized UTC timestamps while maintaining strict DTO fields.
        contract = TaskContract.model_validate_json(canonical_json(contract)).model_dump(mode='json')
        add_contract(db, contract)
    missing = compilation.missing_fields
    db.execute('''INSERT INTO task_revisions(task_id,revision,parent_revision,kind,request_json,
        submitted_json,request_sha256,missing_fields_json,contract_version,provenance_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (task_id, version, version-1 if version>1 else None, kind, canonical_json(content),
         canonical_json(submitted), digest, canonical_json(missing), version if contract else None,
         canonical_json(provenance), now))
    # Preserve the last committed contract while a replacement draft is incomplete.
    # Completed new revisions detach the previous current Run into immutable history.
    db.execute('''UPDATE tasks SET preparation_status=?,requested_fields_json=?,
        current_contract_version=COALESCE(?,current_contract_version),
        current_run_id=NULL,state_version=state_version+1 WHERE task_id=?''',
        ('READY' if contract else 'NEEDS_INPUT', canonical_json(missing), version if contract else None, task_id))


def create(path: Path, request, key: str) -> Reply:
    content = request_content(request)
    scope, digest = 'POST /v1/tasks', body_digest(content)
    with connect(path) as db, transaction(db):
        prior = _replay(db, scope, key, digest)
        if prior is not None:
            return prior
        task_id, request_id = 'task-' + uuid4().hex, str(uuid4())
        create_task(db, task_id=task_id, instruction=request.instruction, requested_fields=['compilation'])
        _commit_revision(db, task_id=task_id, version=1, kind='create', content=content,
                         submitted=content, original_instruction=request.instruction, request_id=request_id)
        reply = _persist_reply(db, scope=scope, key=key, digest=digest, task_id=task_id,
                               request_id=request_id, status=201)
    return reply


def _version_and_idle(db, task_id, expected, *, ignore_compilation=None):
    row = _task(db, task_id)
    revision = db.execute('SELECT * FROM task_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1',
                          (task_id,)).fetchone()
    if revision is None:
        raise BusinessError('CONTRACT_VERSION_CONFLICT', 'Task has no API preparation revision', status=409,
                            current_contract_version=row['current_contract_version'])
    if revision['revision'] != expected:
        raise BusinessError('CONTRACT_VERSION_CONFLICT', 'Task preparation version changed; reload before retrying',
                            status=409, current_contract_version=revision['revision'])
    pending = db.execute("SELECT call_id FROM task_compilations WHERE task_id=? AND status='STARTED'", (task_id,)).fetchone()
    if pending is not None and pending['call_id'] != ignore_compilation:
        raise BusinessError('STATE_CONFLICT', '该任务正在编译，请等待完成后重新读取。', status=409,
                            current_contract_version=revision['revision'])
    # Check all Runs, not just current_run_id: concurrent/older execution also binds authority.
    active = db.execute("SELECT state_version FROM runs WHERE task_id=? AND state NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED') LIMIT 1",
                        (task_id,)).fetchone()
    if active is not None:
        raise BusinessError('STATE_CONFLICT', 'End the existing run safely before changing task inputs', status=409,
                            current_state_version=active['state_version'], current_contract_version=revision['revision'])
    return row, revision


def change(path: Path, task_id: str, request, key: str, *, clarification: bool) -> Reply:
    operation = 'clarifications' if clarification else 'revisions'
    scope = f'POST /v1/tasks/{task_id}/{operation}'
    submitted = request_content(request)
    digest = body_digest(submitted)
    with connect(path) as db, transaction(db):
        # Replaying a completed operation must work even after later revisions or Run changes.
        prior = _replay(db, scope, key, digest)
        if prior is not None:
            return prior
        row, previous = _version_and_idle(db, task_id, request.contract_version)
        if clarification:
            missing = set(json.loads(previous['missing_fields_json']))
            if not request.values or not set(request.values).issubset(missing):
                raise BusinessError('INVALID_PARAMETER', 'Clarifications may only fill currently requested fields',
                                    field='values', current_contract_version=previous['revision'])
            content = json.loads(previous['request_json'])
            for field, value in request.values.items():
                # Only compiler-generated field paths can reach here, no recursive patch language.
                if field.startswith('parameters.'):
                    content['parameters'][field.removeprefix('parameters.')] = value
                else:
                    content[field] = value
            # Validate all merged top-level fields before compiling scenario-specific parameters.
            try:
                content = request_content(CreateTaskRequest.model_validate_json(canonical_json(content)))
            except ValidationError as error:
                raise BusinessError('INVALID_PARAMETER', 'Invalid clarification value', field='values') from error
        else:
            content = {field: value for field, value in submitted.items() if field != 'contract_version'}
        request_id = str(uuid4())
        _commit_revision(db, task_id=task_id, version=previous['revision']+1,
                         kind='clarification' if clarification else 'revision', content=content,
                         submitted=submitted, original_instruction=row['original_instruction'], request_id=request_id,
                         prior_provenance=json.loads(previous['provenance_json']) if clarification else None)
        reply = _persist_reply(db, scope=scope, key=key, digest=digest, task_id=task_id,
                               request_id=request_id, status=200)
    return reply
