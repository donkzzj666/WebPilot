"""Read-only runtime results behind the local authenticated API boundary."""
import re
from fastapi import APIRouter, Request

from ..errors import BusinessError
from .service import VerificationService

router = APIRouter(prefix='/v1/runs')


@router.get('/{run_id}/result')
def result(request: Request, run_id: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',run_id) or request.query_params:
        raise BusinessError('INVALID_PARAMETER','Result reads accept a Run ID only')
    return VerificationService(request.app.state.settings.data_dir).read(run_id)
