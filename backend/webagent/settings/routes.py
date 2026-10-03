"""Local model settings endpoints; credentials never enter response objects."""
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ..errors import BusinessError
from .models import ModelSettingsRequest
from . import service


router = APIRouter(prefix='/v1/settings')

def _response(body: dict) -> JSONResponse:
    return JSONResponse(body, headers={'Cache-Control': 'no-store'})


@router.get('')
def current(request: Request):
    return _response(service.get_settings(request.app.state.settings.business_db,
                                          request.app.state.secret_store))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError('nonfinite JSON number')


@router.put('/model')
async def update(request: Request):
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise BusinessError('INVALID_PARAMETER', '模型设置需要 application/json 正文。', field='settings')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise BusinessError('INVALID_PARAMETER', '模型设置正文过大。', field='settings')
    try:
        body = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        data = ModelSettingsRequest.model_validate(body)
    except (ValueError, TypeError, RecursionError, ValidationError):
        # Validation locations/values may contain an attacker-supplied secret.
        raise BusinessError('INVALID_PARAMETER', '模型设置参数无效。', field='settings') from None
    # Native credential IPC runs on a worker thread, never the async event loop.
    result = await run_in_threadpool(service.update_model, request.app.state.settings.business_db,
                                     request.app.state.secret_store, data)
    return _response(result)
