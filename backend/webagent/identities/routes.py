"""Authenticated local metadata endpoints; browser work stays in the Worker."""
import json

from fastapi import APIRouter, Request
from pydantic import ValidationError

from ..errors import BusinessError
from .api_models import LoginRequest, LoginCommand
from .rpc import LoginClient, _pairs

router = APIRouter(prefix='/v1/identities')


async def body(request, model):
    if request.headers.get('content-type', '').split(';')[0].lower().strip() != 'application/json':
        raise BusinessError('INVALID_PARAMETER', '登录接口需要 JSON 元数据。', field='body')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise BusinessError('INVALID_PARAMETER', '登录请求过大。', field='body')
    try:
        return model.model_validate(json.loads(raw, object_pairs_hook=_pairs)).model_dump()
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise BusinessError('INVALID_PARAMETER', '登录参数无效；请在站点窗口中输入密码和验证码。', field='body') from None


def client(request):
    return LoginClient(request.app.state.settings)


@router.get('')
async def identities(request: Request):
    return await client(request).call('list_identities')


@router.get('/sites')
async def sites(request: Request):
    return await client(request).call('list_sites')


@router.post('/login-sessions', status_code=201)
async def create(request: Request):
    return await client(request).call('create', await body(request, LoginRequest))


@router.get('/login-sessions/{login_session_id}')
async def current(login_session_id: str, request: Request):
    return await client(request).call('get', {'login_session_id': login_session_id})


@router.post('/login-sessions/{login_session_id}/confirm')
async def confirm(login_session_id: str, request: Request):
    return await client(request).call('confirm', {'login_session_id': login_session_id,
                                                **await body(request, LoginCommand)})


@router.post('/login-sessions/{login_session_id}/close')
async def close(login_session_id: str, request: Request):
    return await client(request).call('close', {'login_session_id': login_session_id,
                                              **await body(request, LoginCommand)})
