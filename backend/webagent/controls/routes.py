"""Protected request/receipt adapters; callers cannot invoke a graph directly."""
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..errors import BusinessError
from ..tasks.routes import idempotency_key
from ..observability.http import bind_committed_ids
from .models import ControlRequest
from .store import ControlStore

router = APIRouter(prefix='/v1')


def _store(request):
    # This is dependency injection by trusted application composition, never a
    # public request field. Production always uses the fixed public resource set.
    injected = getattr(request.app.state,'control_store',None)
    return injected if injected is not None else ControlStore(
        request.app.state.settings.business_db,secret_store=request.app.state.secret_store)


async def _request(request, target_id, action, body):
    accepted = await run_in_threadpool(_store(request).request,target_id,action,body,
        idempotency_key(request,body.idempotency_key))
    _bind_operation(request, accepted['operation'])
    return JSONResponse(status_code=202,content=accepted,headers={
        'Cache-Control':'no-store','Location':'/v1/operations/'+accepted['operation']['operation_id']})


def _bind_operation(request, receipt):
    bind_committed_ids(request, task_id=receipt['task_id'], run_id=receipt['run_id'],
        operation_id=receipt['operation_id'], event_id=receipt['requested_event_id'])


@router.post('/tasks/{task_id}/start')
async def start(request:Request,task_id:str,body:ControlRequest):
    return await _request(request,task_id,'start',body)


@router.post('/tasks/{task_id}/retry')
async def retry(request:Request,task_id:str,body:ControlRequest):
    return await _request(request,task_id,'retry',body)


@router.post('/runs/{run_id}/pause')
async def pause(request:Request,run_id:str,body:ControlRequest):
    return await _request(request,run_id,'pause',body)


@router.post('/runs/{run_id}/resume')
async def resume(request:Request,run_id:str,body:ControlRequest):
    return await _request(request,run_id,'resume',body)


@router.post('/runs/{run_id}/cancel')
async def cancel(request:Request,run_id:str,body:ControlRequest):
    return await _request(request,run_id,'cancel',body)


@router.get('/operations/{operation_id}')
def operation(request:Request,operation_id:str):
    receipt = _store(request).read(operation_id)
    _bind_operation(request, receipt)
    return JSONResponse(content={'operation':receipt},headers={'Cache-Control':'no-store'})


@router.get('/runs/{run_id}/operations')
def operations(request:Request,run_id:str):
    values=request.query_params
    if (set(values)-{'after','limit'} or len(values.multi_items())!=len(values)
            or any(not re.fullmatch(r'0|[1-9][0-9]{0,18}',v) for v in values.values())):
        raise BusinessError('INVALID_PARAMETER','Invalid operation cursor')
    after,limit=int(values.get('after','0')),int(values.get('limit','100'))
    rows=_store(request).list_operations(run_id,after=after,limit=limit)
    return JSONResponse(content={'run_id':run_id,'operations':rows,
        'next_after':rows[-1]['operation_seq'] if rows else after},headers={'Cache-Control':'no-store'})
