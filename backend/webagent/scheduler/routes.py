"""Read-only scheduler metadata; no public Run-start or lease-control input."""
from fastapi import APIRouter, Request

from .store import SchedulerStore

router = APIRouter(prefix='/v1/scheduler')


@router.get('')
def status(request: Request):
    return SchedulerStore(request.app.state.settings.business_db).snapshot()
