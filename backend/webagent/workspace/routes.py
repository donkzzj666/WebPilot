"""Authenticated read adapters for atomic workbench state and bounded replay."""
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..errors import BusinessError
from .store import MAX_CURSOR, run_events, workspace

router = APIRouter(prefix='/v1')


def _query(request, allowed):
    values = request.query_params
    if set(values) - allowed or len(values.multi_items()) != len(values):
        raise BusinessError('INVALID_PARAMETER', '工作台查询参数无效。')
    return values


@router.get('/tasks/{task_id}/workspace')
def task_workspace(request: Request, task_id: str):
    values = _query(request, {'run_id'})
    result = workspace(request.app.state.settings.business_db, task_id,
                       run_id=values.get('run_id'))
    return JSONResponse(content=result, headers={'Cache-Control': 'no-store'})


@router.get('/runs/{run_id}/events')
def event_page(request: Request, run_id: str):
    values = _query(request, {'after', 'limit'})
    if any(not re.fullmatch(r'0|[1-9][0-9]{0,18}', value) for value in values.values()):
        raise BusinessError('INVALID_PARAMETER', '事件分页参数无效。')
    after, limit = int(values.get('after', '0')), int(values.get('limit', '100'))
    if after > MAX_CURSOR:
        raise BusinessError('INVALID_PARAMETER', '事件分页参数无效。')
    result = run_events(request.app.state.settings.business_db, run_id, after=after, limit=limit)
    return JSONResponse(content=result, headers={'Cache-Control': 'no-store'})
