"""Request correlation without logging headers, bodies or user-controlled URLs."""
import asyncio
import time
from uuid import UUID, uuid4

from .logging import safe_error_class

METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'})


def bind_committed_ids(request, *, task_id=None, run_id=None, operation_id=None, event_id=None):
    """Called only after a service returned persisted references, never from input."""
    request.scope.setdefault('state', {})['diagnostic_bindings'] = {
        key: value for key, value in {'task_id': task_id, 'run_id': run_id,
            'operation_id': operation_id, 'event_id': event_id}.items() if value is not None}


async def emit_safely(logger, event, **fields):
    # Diagnostic failure must not replace a response or cancel a business action.
    try:
        await asyncio.to_thread(logger.emit, event, **fields)
    except Exception:
        pass


class RequestDiagnosticsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        transport_request_id = request_id = str(uuid4())
        scope.setdefault('state', {})['diagnostic_request_id'] = transport_request_id
        started = time.monotonic()
        status = 500

        async def correlated_send(message):
            nonlocal status, request_id
            if message['type'] == 'http.response.start':
                status = message['status']
                headers = list(message.get('headers', []))
                # Idempotent task replies have a persisted business request ID.
                # Preserve that contract while assigning a new transport ID to
                # every HTTP attempt. Only trust application response headers.
                for key, value in headers:
                    if key.lower() == b'x-request-id':
                        try:
                            parsed = UUID(value.decode('ascii'))
                            if parsed.version == 4 and str(parsed).encode() == value:
                                request_id = str(parsed)
                        except (ValueError, UnicodeError):
                            pass
                headers = [(k, v) for k, v in headers
                           if k.lower() not in {b'x-request-id', b'x-transport-request-id'}]
                message = {**message, 'headers': [*headers, (b'x-request-id', request_id.encode()),
                    (b'x-transport-request-id', transport_request_id.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, correlated_send)
        except BaseException as error:
            logger = getattr(scope['app'].state, 'diagnostic_logger', None)
            if logger is not None:
                await emit_safely(logger, 'api_error', service='api', request_id=request_id,
                    transport_request_id=transport_request_id, error_class=safe_error_class(error))
            raise
        finally:
            logger = getattr(scope['app'].state, 'diagnostic_logger', None)
            if logger is not None:
                fields = {'service': 'api', 'request_id': request_id, 'http_status': status,
                          'duration_ms': min(2**63 - 1, max(0, int((time.monotonic() - started) * 1000)))}
                if scope.get('method') in METHODS:
                    fields['method'] = scope['method']
                fields['transport_request_id'] = transport_request_id
                fields.update(scope.get('state', {}).get('diagnostic_bindings', {}))
                await emit_safely(logger, 'api_request', **fields)
