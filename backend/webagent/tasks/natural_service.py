"""Natural-language preparation: reserve, await without a DB lock, commit once.

Unknown STARTED calls are deliberately retained after a crash. Replaying such a
key never repeats provider work. A completed failure also replays its receipt.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import time
from uuid import uuid4
from starlette.concurrency import run_in_threadpool

from ..db import connect, transaction
from ..db.repository import canonical_json, create_task, utc_text
from ..errors import BusinessError
from ..models.adapter import ModelError
from ..settings.service import provider_for_compilation
from . import service
from .extraction import COMPILER_PROMPT_VERSION, extract
from .natural import compile_natural, prepare_content


def _prior(db, scope, key, digest):
    # Shared lookup also prevents a fixture request from reusing a natural
    # request's in-flight or failed key with different content.
    return service._replay(db, scope, key, digest)


def _public_call(row):
    return {key: row[key] for key in (
        'call_id', 'expected_revision', 'settings_version', 'model_config_sha256',
        'runtime_config_sha256', 'prompt_version', 'status', 'provider_request_id',
        'duration_ms', 'error_class', 'created_at', 'finished_at')} | {
        'usage': json.loads(row['usage_json']) if row['usage_json'] else None,
        'extracted_fields': json.loads(row['extracted_fields_json']),
        'estimated_cost': None,
    }


def preparation_details(db, task_id, revisions, override=None):
    calls = [dict(row) for row in db.execute(
        'SELECT * FROM task_compilations WHERE reserved_task_id=? ORDER BY created_at,call_id', (task_id,))]
    if override:
        calls = [row | override if row['call_id'] == override['call_id'] else row for row in calls]
    origins = {}
    for revision in revisions:
        content, submitted = json.loads(revision['request_json']), json.loads(revision['submitted_json'])
        if content.get('compiler_mode') != 'natural_language':
            origins = {}
            continue
        if revision['kind'] != 'clarification':
            origins = {'instruction': 'user_input', 'sources': 'api', 'start_urls': 'api',
                       'action_policy': 'api', 'identity_ref': 'api', 'web_context': 'web_content'}
            extracted = set(next((json.loads(c['extracted_fields_json']) for c in calls
                if c['status'] == 'SUCCEEDED' and c['expected_revision'] + 1 == revision['revision']), []))
            fields = ['scenario', *('parameters.' + name for name in content['parameters'])]
            origins.update({field: 'model_grounded_in_user_input' if field in extracted else 'api' for field in fields})
        else:
            origins.update({field: 'api' for field in submitted['values']})
    missing = json.loads(revisions[-1]['missing_fields_json'])
    return {
        'compiler': {'mode': 'natural_language', 'prompt_version': COMPILER_PROMPT_VERSION},
        'compilations': [_public_call(row) for row in calls], 'field_origins': origins,
        'provenance': json.loads(revisions[-1]['provenance_json']),
        'clarification_questions': [{'field': field, 'message': _question(field)} for field in missing],
    }


def _question(field):
    names = {'sources': '请明确允许访问的来源、域名和路径范围。',
             'start_urls': '请提供允许来源范围内的起始 URL。',
             'scenario': '请明确任务属于财务、运维、科研或监控场景。',
             'parameters.entity_id': '请明确要读取的唯一企业对象。',
             'parameters.report_version': '请明确财报年份或报告版本，不能仅写最近一期。',
             'parameters.period_type': '请明确年度、季度、年初至今或时点口径。',
             'action_policy': '请显式声明仓库、分支、文件白名单和允许的写操作。',
             'identity_ref': '请明确执行写入所用的账号引用。',
             'time_scope': '请明确时间窗口的 UTC 开始、结束时间及依据。'}
    return names.get(field, '请补充并确认此场景的必需字段：' + field)


def _error_body(error, request_id):
    return json.loads(canonical_json({'request_id': request_id, 'status': error.status, 'code': error.code,
            'message': str(error), 'details': [{'field': error.field, 'reason': str(error)}],
            # A completed preparation failure is durable, never automatically retried.
            'retryable': False, 'current_contract_version': error.current_contract_version,
            'current_state_version': error.current_state_version}))


def _finished_values(call_id, started, reply, status, error_class, fields=()):
    return {'call_id': call_id, 'status': status,
            'provider_request_id': reply.provider_request_id if reply else None,
            'usage_json': canonical_json(reply.usage) if reply else None,
            'extracted_fields_json': canonical_json(list(fields)),
            'duration_ms': max(0, math.ceil((time.monotonic() - started) * 1000)),
            'error_class': error_class, 'finished_at': utc_text()}


def _finish(db, values, response, *, task_id=None):
    db.execute('''UPDATE task_compilations SET status=?,task_id=COALESCE(?,task_id),
        provider_request_id=?,usage_json=?,extracted_fields_json=?,duration_ms=?,error_class=?,
        response_status=?,response_json=?,retry_after_seconds=?,finished_at=? WHERE call_id=?''',
        (values['status'], task_id, values['provider_request_id'], values['usage_json'],
         values['extracted_fields_json'], values['duration_ms'], values['error_class'], response.status,
         canonical_json(response.body), response.retry_after_seconds, values['finished_at'], values['call_id']))


async def compile_request(path: Path, store, request, key: str, *, task_id: str | None = None):
    """Create/replace a draft; clarification uses service.change without HTTP."""
    submitted = service.request_content(request)
    content = {k: v for k, v in submitted.items() if k != 'contract_version'}
    creating = task_id is None
    expected = 0 if creating else request.contract_version
    scope = 'POST /v1/tasks' if creating else f'POST /v1/tasks/{task_id}/revisions'
    digest = service.body_digest(submitted)
    with connect(path) as db:
        prior = _prior(db, scope, key, digest)
        if prior is not None:
            return prior
        if not creating:
            service._version_and_idle(db, task_id, expected)
    call_id, request_id = str(uuid4()), str(uuid4())
    reserved_id = task_id or 'task-' + uuid4().hex
    # Reject malformed explicit parameters/scopes before any credential or HTTP work.
    compile_natural(content, task_id=reserved_id, version=expected+1, created_at=utc_text(),
                    provenance=[{'origin': 'api', 'reference': request_id,
                                 'content_sha256': digest, 'authorizes_execution': True}])
    # OS credential IPC must not block the ASGI event loop.
    from ..evidence.store import EvidenceStore
    await run_in_threadpool(EvidenceStore(path.parent).assert_dispatch_allowed)
    snapshot, provider = await run_in_threadpool(provider_for_compilation, path, store)
    reserved, started, provider_reply = False, time.monotonic(), None
    try:
        with connect(path) as db, transaction(db):
            prior = _prior(db, scope, key, digest)
            if prior is not None:
                return prior
            if not creating:
                service._version_and_idle(db, task_id, expected)
            db.execute('''INSERT INTO task_compilations(call_id,request_scope,idempotency_key,
                request_sha256,reserved_task_id,task_id,expected_revision,settings_version,
                model_config_sha256,runtime_config_sha256,prompt_version,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'STARTED',?)''',
                (call_id, scope, key, digest, reserved_id, task_id, expected, snapshot.version,
                 snapshot.model.config_sha256, service.body_digest(snapshot.runtime.model_dump(mode='json')),
                 COMPILER_PROMPT_VERSION, utc_text()))
        reserved = True
        if provider.config.config_sha256 != snapshot.model.config_sha256:
            raise BusinessError('SERVICE_UNAVAILABLE', '编译模型配置与快照不一致。', status=503)
        await run_in_threadpool(EvidenceStore(path.parent).assert_dispatch_allowed)
        proposal, provider_reply = await extract(provider, deepcopy(content))
        prepared = prepare_content(content, proposal)
        extracted_fields = []
        if content.get('scenario') is None and prepared.get('scenario') is not None:
            extracted_fields.append('scenario')
        extracted_fields.extend('parameters.' + name for name in prepared['parameters'] if name not in content['parameters'])
        with connect(path) as db, transaction(db):
            if creating:
                create_task(db, task_id=reserved_id, instruction=content['instruction'], requested_fields=['compilation'])
                original = content['instruction']
            else:
                row, _ = service._version_and_idle(db, task_id, expected, ignore_compilation=call_id)
                original = row['original_instruction']
            service._commit_revision(db, task_id=reserved_id, version=expected+1,
                kind='create' if creating else 'revision', content=prepared, submitted=submitted,
                original_instruction=original, request_id=request_id)
            values = _finished_values(call_id, started, provider_reply, 'SUCCEEDED', None, extracted_fields)
            body = json.loads(canonical_json({'request_id': request_id,
                **service._detail(db, reserved_id, compilation_override=values)}))
            response = service.Reply(201 if creating else 200, body)
            _finish(db, values, response, task_id=reserved_id)
            db.execute('''INSERT INTO api_idempotency(request_scope,idempotency_key,request_sha256,
                task_id,response_status,response_json,created_at) VALUES (?,?,?,?,?,?,?)''',
                (scope, key, digest, reserved_id, response.status, canonical_json(body), utc_text()))
        return response
    except BaseException as error:
        if EvidenceStore._full(error):
            from ..evidence.models import unavailable
            EvidenceStore(path.parent)._storage_fault()
            raise unavailable() from None
        if not reserved:
            raise
        cancelled = isinstance(error, asyncio.CancelledError)
        if not isinstance(error, (Exception, asyncio.CancelledError)):
            raise  # Process interruption keeps the durable STARTED/unknown record.
        if cancelled:
            safe_error = BusinessError('SERVICE_UNAVAILABLE', '编译被取消；此幂等键不会重复调用模型。', status=503)
            error_class = 'cancelled'
        elif isinstance(error, BusinessError):
            safe_error = error
            error_class = error.error_class if isinstance(error, ModelError) else (
                'state_conflict' if error.status == 409 else 'invalid_parameter' if error.status == 422 else 'provider_error')
        else:
            safe_error = BusinessError('INTERNAL_ERROR', '任务编译未完成；请读取任务状态。', status=500)
            error_class = 'provider_error'
        response = service.Reply(safe_error.status, _error_body(safe_error, request_id),
                                 getattr(safe_error, 'retry_after_seconds', None))
        values = _finished_values(call_id, started, getattr(error, 'reply', None) or provider_reply,
                                  'CANCELLED' if cancelled else 'FAILED', error_class)
        # If commit outcome is uncertain, don't overwrite success or repeat HTTP.
        with connect(path) as db, transaction(db):
            row = db.execute('SELECT status FROM task_compilations WHERE call_id=?', (call_id,)).fetchone()
            if row['status'] == 'STARTED':
                _finish(db, values, response)
            else:
                response = _prior(db, scope, key, digest)
        if cancelled:
            raise
        return response
    finally:
        # Closing a transport must not invalidate a committed preparation receipt.
        try:
            async with asyncio.timeout(1):
                await provider.aclose()
        except Exception:
            pass
