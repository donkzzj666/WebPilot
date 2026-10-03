"""Local API with health and durable business event replay."""

from contextlib import asynccontextmanager, closing
import asyncio
import math
import sqlite3
from uuid import uuid4

from .config import Settings, disable_external_tracing

disable_external_tracing()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .security import LocalApiMiddleware, LocalApiPolicy, default_policy
from .db import StorageBusyError
from .errors import BusinessError
from .models.adapter import ModelError
from .sse import router as events_router
from .tasks.routes import router as tasks_router
from .tasks.compiler import COMPILER_MODE
from .settings.routes import router as settings_router
from .identities.routes import router as identities_router
from .scheduler.routes import router as scheduler_router
from .budgets.routes import router as budgets_router
from .evidence.routes import router as evidence_router
from .verification.routes import router as verification_router
from .graph.routes import router as graph_router
from .controls.routes import router as controls_router
from .writes.routes import router as writes_router
from .settings.secrets import MacOSKeychain, SecretStore

from .observability.http import RequestDiagnosticsMiddleware, emit_safely
from .observability.logging import SafeJSONLLogger, safe_error_class
from .observability.routes import router as diagnostics_router
from .workspace.routes import router as workspace_router
from .results.routes import router as results_router
from .runtime import check_runtime
from .storage import initialize_business_storage


def create_app(settings: Settings | None = None, *, secret_store: SecretStore | None = None,
               local_api_policy: LocalApiPolicy | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        disable_external_tracing()
        current = settings or Settings.from_env()
        app.state.settings = current
        app.state.diagnostic_logger = SafeJSONLLogger(current.data_dir / 'logs' / 'api.jsonl')
        app.state.local_api_policy = local_api_policy if local_api_policy is not None else default_policy(current)
        app.state.runtime = check_runtime()
        app.state.storage = initialize_business_storage(current)
        app.state.secret_store = secret_store if secret_store is not None else MacOSKeychain()
        app.state.diagnostic_logger.emit('service_started', service='api')
        try:
            yield
        finally:
            app.state.diagnostic_logger.emit('service_stopped', service='api')

    app = FastAPI(title="WebAgent foundation", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(LocalApiMiddleware)
    app.add_middleware(RequestDiagnosticsMiddleware)

    app.include_router(events_router)
    app.include_router(tasks_router)
    app.include_router(settings_router)
    app.include_router(identities_router)
    app.include_router(scheduler_router)
    app.include_router(budgets_router)
    app.include_router(evidence_router)
    app.include_router(verification_router)
    app.include_router(graph_router)
    app.include_router(controls_router)
    app.include_router(writes_router)
    app.include_router(diagnostics_router)
    app.include_router(workspace_router)
    app.include_router(results_router)

    @app.exception_handler(BusinessError)
    async def business_error(request: Request, error: BusinessError):
        request_id = getattr(request.state, 'diagnostic_request_id', None) or str(uuid4())
        await emit_safely(app.state.diagnostic_logger, 'api_error', service='api', request_id=request_id,
                                transport_request_id=request_id,
                                error_class=safe_error_class(error), http_status=error.status)
        headers = {"X-Request-ID": request_id, "X-Transport-Request-ID": request_id}
        code, field, reason = error.code, error.field, str(error)
        if request.url.path.startswith('/v1/settings'):
            headers['Cache-Control'] = 'no-store'
            # Keep the frozen public ApiError vocabulary. Settings/credential
            # diagnoses belong in details, not invented top-level error codes.
            public_codes = {'BAD_REQUEST', 'FORBIDDEN', 'NOT_FOUND', 'INVALID_PARAMETER',
                            'STATE_CONFLICT', 'SERVICE_UNAVAILABLE', 'INTERNAL_ERROR'}
            if code not in public_codes:
                code = {409: 'STATE_CONFLICT', 503: 'SERVICE_UNAVAILABLE'}.get(error.status, 'INTERNAL_ERROR')
                reason = error.code
                if error.code == 'VERSION_CONFLICT':
                    field = 'expected_version'
        if isinstance(error, ModelError) and error.retry_after_seconds is not None:
            headers['Retry-After'] = str(math.ceil(error.retry_after_seconds))
        return JSONResponse(status_code=error.status, content={
            "request_id": request_id, "status": error.status, "code": code,
            "message": str(error), "details": [{"field": field, "reason": reason}],
            "retryable": error.status in (429, 503),
            "current_contract_version": error.current_contract_version,
            "current_state_version": error.current_state_version,
        }, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_parameter(request: Request, error: RequestValidationError):
        return await business_error(request, BusinessError(
            'INVALID_PARAMETER', 'Invalid request parameters', field='body'))

    @app.exception_handler(StorageBusyError)
    async def storage_busy(request: Request, error: StorageBusyError):
        return await business_error(request, BusinessError(
            'SERVICE_UNAVAILABLE', 'Storage is busy; retry later', status=503))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception):
        # Keep SQL, paths and diagnostic details out of public error responses.
        return await business_error(request, BusinessError(
            'INTERNAL_ERROR', 'An internal error occurred', status=500))

    @app.get("/health")
    async def health() -> dict:
        def storage_ready():
            # Health does not migrate, create missing storage, flush budgets or
            # borrow Worker authority. It checks this API's local dependency.
            try:
                with closing(sqlite3.connect(app.state.settings.business_db.resolve().as_uri() + '?mode=ro',
                                             uri=True, timeout=.1)) as database:
                    database.execute('PRAGMA query_only=ON')
                    version = database.execute('PRAGMA user_version').fetchone()[0]
                    database.execute('SELECT 1 FROM tasks LIMIT 1').fetchone()
                    return version == app.state.storage['schema_version']
            except sqlite3.Error:
                return False
        if not await asyncio.to_thread(storage_ready):
            raise BusinessError('SERVICE_UNAVAILABLE', 'API storage is unavailable', status=503)
        return {
            "status": "ok", "service": "api", "stage": "M1-25",
            "health_scope": "local_api_and_storage", "tasks_success_implied": False,
            "external_tracing": "disabled",
            "diagnostic_logging": "ready" if app.state.diagnostic_logger.healthy else "unavailable",
            "task_execution_enabled": True, "run_controls_enabled": True,
            "task_creation_enabled": True, "task_compiler": COMPILER_MODE,
            "task_compiler_modes": ["fixture", "natural_language"],
            "browser_sessions": "owned_by_worker",
            "identity_preparation": "owned_by_worker",
            "budgets": "persistent_monotonic", "deadline_control": "independent",
            "scheduler": "durable_queue",
            "evidence": "immutable_private_originals_verified_derivatives",
            "graph": "worker_custom_stategraph", "graph_progress": "authenticated_read_only",
            "storage": {"business": "ready", "graph": "owned_by_worker",
                        "schema_version": app.state.storage["schema_version"]},
            "sqlite_version": sqlite3.sqlite_version,
        }

    return app
