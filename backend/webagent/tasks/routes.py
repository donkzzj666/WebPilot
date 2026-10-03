"""HTTP adapters for task preparation; no execution dispatch."""
import re
import math
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..errors import BusinessError
from ..observability.http import bind_committed_ids
from . import service
from .models import CreateTaskRequest, ClarificationRequest, RevisionRequest
from .catalog import list_tasks, MAX_PAGE, MAX_CURSOR

router = APIRouter(prefix='/v1/tasks')
TaskId = Annotated[str, Path(min_length=1, max_length=200)]


def idempotency_key(request: Request, body_key: str | None) -> str:
    values = request.headers.getlist('Idempotency-Key')
    if len(values) != 1 or re.fullmatch(r'[!-~]{1,200}', values[0]) is None:
        raise BusinessError('INVALID_PARAMETER', 'One Idempotency-Key header of 1-200 visible ASCII characters is required',
                            field='Idempotency-Key')
    if body_key is not None and body_key != values[0]:
        raise BusinessError('INVALID_PARAMETER', 'Body idempotency key must match the header', field='idempotency_key')
    return values[0]


def response(reply: service.Reply, request: Request) -> JSONResponse:
    headers = {'X-Request-ID': reply.body['request_id'], 'Cache-Control': 'no-store'}
    if 'task' in reply.body:
        headers['Location'] = '/v1/tasks/' + reply.body['task']['task_id']
        bind_committed_ids(request, task_id=reply.body['task']['task_id'],
                           run_id=reply.body['task']['current_run_id'])
    if reply.retry_after_seconds is not None:
        headers['Retry-After'] = str(math.ceil(reply.retry_after_seconds))
    return JSONResponse(status_code=reply.status, content=reply.body, headers=headers)


@router.post('')
async def create(request: Request, body: CreateTaskRequest):
    key = idempotency_key(request, body.idempotency_key)
    path = request.app.state.settings.business_db
    if body.compiler_mode == 'natural_language':
        from .natural_service import compile_request
        return response(await compile_request(path, request.app.state.secret_store, body, key), request)
    return response(await run_in_threadpool(service.create, path, body, key), request)


@router.get('')
def catalog(request: Request):
    values = request.query_params
    if (set(values) - {'before', 'limit'} or len(values.multi_items()) != len(values)
            or any(re.fullmatch(r'[1-9][0-9]{0,18}', value) is None for value in values.values())):
        raise BusinessError('INVALID_PARAMETER', '任务列表分页参数无效。')
    before = int(values['before']) if 'before' in values else None
    limit = int(values.get('limit', '20'))
    if (not 1 <= limit <= MAX_PAGE or before is not None and not 1 <= before <= MAX_CURSOR):
        raise BusinessError('INVALID_PARAMETER', '任务列表分页参数无效。')
    result = list_tasks(request.app.state.settings.business_db, before=before, limit=limit)
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.get('/{task_id}')
def detail(request: Request, task_id: TaskId):
    result = service.detail(request.app.state.settings.business_db, task_id)
    bind_committed_ids(request, task_id=result['task']['task_id'], run_id=result['task']['current_run_id'])
    return result


@router.get('/{task_id}/runs')
def runs(request: Request, task_id: TaskId):
    result = service.history(request.app.state.settings.business_db, task_id)
    bind_committed_ids(request, task_id=result['task_id'])
    return result


@router.get('/{task_id}/contracts')
def contracts(request: Request, task_id: TaskId):
    result = service.history(request.app.state.settings.business_db, task_id, contracts=True)
    bind_committed_ids(request, task_id=result['task_id'])
    return result


@router.post('/{task_id}/clarifications')
def clarify(request: Request, task_id: TaskId, body: ClarificationRequest):
    return response(service.change(request.app.state.settings.business_db, task_id, body,
                                   idempotency_key(request, body.idempotency_key), clarification=True), request)


@router.post('/{task_id}/revisions')
async def revise(request: Request, task_id: TaskId, body: RevisionRequest):
    key = idempotency_key(request, body.idempotency_key)
    path = request.app.state.settings.business_db
    if body.compiler_mode == 'natural_language':
        from .natural_service import compile_request
        return response(await compile_request(path, request.app.state.secret_store, body, key, task_id=task_id), request)
    return response(await run_in_threadpool(service.change, path, task_id, body, key, clarification=False), request)
