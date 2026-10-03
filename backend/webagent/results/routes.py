"""Bounded result-page reads behind the existing local API gate."""
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..errors import BusinessError
from .store import results

router = APIRouter(prefix='/v1/tasks')


@router.get('/{task_id}/results')
def task_results(request: Request, task_id: str):
    values = request.query_params
    if (set(values) - {'run_id', 'before', 'limit'}
            or len(values.multi_items()) != len(values)
            or any(not re.fullmatch(r'[1-9][0-9]{0,18}', values[key])
                   for key in ('before', 'limit') if key in values)):
        raise BusinessError('INVALID_PARAMETER', '结果分页参数无效。')
    data = results(request.app.state.settings.data_dir, task_id,
                   run_id=values.get('run_id'),
                   before=int(values['before']) if 'before' in values else None,
                   limit=int(values.get('limit', '20')))
    return JSONResponse(content=data, headers={'Cache-Control': 'no-store'})
