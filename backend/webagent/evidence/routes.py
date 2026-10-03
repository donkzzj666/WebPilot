"""ID-only, authenticated reads of verified display derivatives."""
import re
from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..errors import BusinessError
from .service import EvidenceService

router = APIRouter(prefix='/v1/evidence')
_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')


def _read(request, evidence_id):
    if not _ID.fullmatch(evidence_id) or request.query_params:
        raise BusinessError('INVALID_PARAMETER', 'Evidence reads accept an artifact ID only')
    return EvidenceService(request.app.state.settings.data_dir).display(evidence_id)


@router.get('/{evidence_id}')
def metadata(request: Request, evidence_id: str):
    value, _ = _read(request, evidence_id)
    return value


@router.get('/{evidence_id}/content')
def content(request: Request, evidence_id: str):
    value, data = _read(request, evidence_id)
    extension = 'png' if value['artifact_kind'] == 'screenshot' else 'txt'
    return Response(data, media_type=value['mime_type'], headers={
        'Content-Disposition': f'inline; filename="{evidence_id}.{extension}"'})
