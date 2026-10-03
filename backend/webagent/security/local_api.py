"""Exact local Host/Origin checks plus bearer auth on every HTTP route, including SSE."""
from dataclasses import dataclass, field
import hmac
import os
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.responses import JSONResponse

from .token import TOKEN_PATTERN, load_or_create_token


@dataclass(frozen=True)
class LocalApiPolicy:
    token: str = field(repr=False)
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]

    def __post_init__(self):
        if not TOKEN_PATTERN.fullmatch(self.token) or not self.allowed_hosts:
            raise ValueError('Invalid local API policy')
        for host in self.allowed_hosts:
            try:
                parsed = urlsplit('http://' + host)
                valid = (parsed.hostname in ('127.0.0.1', 'localhost', '::1')
                         and parsed.netloc == host and not parsed.path and not parsed.query
                         and not parsed.fragment and parsed.username is None and parsed.password is None
                         and parsed.port != 0 and not any(c.isspace() for c in host))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Invalid local API policy')
        for origin in self.allowed_origins:
            try:
                parsed = urlsplit(origin)
                valid = (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
                         and origin == 'http://' + parsed.netloc and not parsed.path
                         and parsed.username is None and parsed.password is None
                         and parsed.port != 0 and not any(c.isspace() for c in origin))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Invalid local API policy')


def default_policy(settings) -> LocalApiPolicy:
    ports = []
    for name, default in [('WEBAGENT_API_PORT', '8000'), ('WEBAGENT_UI_PORT', '5173'),
                          ('WEBAGENT_PREVIEW_PORT', '4173')]:
        value = os.environ.get(name, default)
        if not value.isascii() or not value.isdecimal() or not 1024 <= int(value) <= 65535:
            raise ValueError('Invalid local API port')
        ports.append(int(value))
    hosts = frozenset(f'127.0.0.1:{port}' for port in ports)
    return LocalApiPolicy(load_or_create_token(settings.data_dir), frozenset({f'127.0.0.1:{ports[0]}'}),
                          frozenset('http://' + host for host in hosts))


class LocalApiMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        policy = scope['app'].state.local_api_policy
        headers = {}
        for key, value in scope.get('headers', []):
            headers.setdefault(key.lower(), []).append(value.decode('latin-1'))
        hosts, origins = headers.get(b'host', []), headers.get(b'origin', [])
        sites = headers.get(b'sec-fetch-site', [])
        auth = headers.get(b'authorization', [])
        boundary = (len(hosts) == 1 and hosts[0] in policy.allowed_hosts
                    and len(origins) <= 1 and (not origins or origins[0] in policy.allowed_origins)
                    and (not sites or sites == ['same-origin']))
        authenticated = (len(auth) == 1 and hmac.compare_digest(
            auth[0].encode('latin-1'), ('Bearer ' + policy.token).encode('ascii')))
        if not boundary or not authenticated:
            status = 403 if not boundary else 401
            request_id = scope.get('state', {}).get('diagnostic_request_id') or str(uuid4())
            response = JSONResponse(status_code=status, content={
                'request_id': request_id, 'status': status,
                'code': 'FORBIDDEN' if status == 403 else 'UNAUTHENTICATED',
                'message': 'Local API access denied',
                'details': [{'field': 'request', 'reason': 'Local API access denied'}], 'retryable': False,
                'current_contract_version': None, 'current_state_version': None,
            }, headers={'Cache-Control': 'no-store', 'X-Request-ID': request_id,
                        'Cross-Origin-Resource-Policy': 'same-origin',
                        'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'})
            await response(scope, receive, send)
            return

        async def protected_send(message):
            if message['type'] == 'http.response.start':
                response_headers = [(key, value) for key, value in message.get('headers', [])
                                    if key.lower() not in (b'cache-control', b'access-control-allow-origin',
                                                           b'access-control-allow-credentials')]
                response_headers.extend([(b'cache-control', b'no-store, no-transform' if scope['path'] == '/v1/events' else b'no-store'),
                                         (b'cross-origin-resource-policy', b'same-origin'),
                                         (b'x-content-type-options', b'nosniff'),
                                         (b'x-frame-options', b'DENY')])
                message = {**message, 'headers': response_headers}
            await send(message)
        await self.app(scope, receive, protected_send)
