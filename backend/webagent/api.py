"""Independent API skeleton; no task mutations or background Worker."""

from contextlib import asynccontextmanager
import sqlite3

from fastapi import FastAPI

from .config import Settings, disable_external_tracing
from .runtime import check_runtime
from .storage import initialize_business_storage


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        disable_external_tracing()
        current = settings or Settings.from_env()
        app.state.runtime = check_runtime()
        initialize_business_storage(current)
        yield

    app = FastAPI(title="WebAgent foundation", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok", "service": "api", "stage": "M1-01",
            "task_execution_enabled": False,
            "storage": {"business": "ready", "graph": "owned_by_worker"},
            "sqlite_version": sqlite3.sqlite_version,
        }

    return app

