"""Authenticated, read-only diagnostics and persisted Worker lease health."""
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..errors import BusinessError
from .http import bind_committed_ids
from .store import ObservabilityStore, MAX_PAGE

router = APIRouter(prefix='/v1')


def _cursor(request):
    values = request.query_params
    if (set(values) - {'after', 'model_after', 'limit'} or len(values.multi_items()) != len(values)
            or any(not re.fullmatch(r'0|[1-9][0-9]{0,18}', value) for value in values.values())):
        raise BusinessError('INVALID_PARAMETER', 'Invalid diagnostic cursor')
    result = {key: int(values.get(key, default)) for key, default in
              (('after', '0'), ('model_after', '0'), ('limit', '100'))}
    if (not 0 <= result['after'] < 2**63 - 1 or not 0 <= result['model_after'] < 2**63 - 1
            or not 1 <= result['limit'] <= MAX_PAGE):
        raise BusinessError('INVALID_PARAMETER', 'Invalid diagnostic cursor or page size')
    return result


def _no_query(request):
    if request.query_params:
        raise BusinessError('INVALID_PARAMETER', 'This read does not accept query parameters')


@router.get('/diagnostics/runs/{run_id}')
def diagnostics(request: Request, run_id: str):
    result = ObservabilityStore(request.app.state.settings.business_db).diagnostics(run_id, **_cursor(request))
    bind_committed_ids(request, task_id=result['run']['task_id'], run_id=result['run']['run_id'])
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.get('/metrics')
def metrics(request: Request):
    _no_query(request)
    return JSONResponse(ObservabilityStore(request.app.state.settings.business_db).metrics(),
                        headers={'Cache-Control': 'no-store'})


@router.get('/health/worker')
def worker_health(request: Request):
    _no_query(request)
    result = ObservabilityStore(request.app.state.settings.business_db).worker_health()
    return JSONResponse(result, status_code=200 if result['ready'] else 503,
                        headers={'Cache-Control': 'no-store'})
