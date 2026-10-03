"""Bounded internal evidence metadata; paths never originate with a caller."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
ARTIFACT_MIME = {'screenshot': 'image/png', 'text': 'text/plain; charset=utf-8',
                 'pdf': 'application/pdf', 'ci': 'application/json',
                 'har': 'application/json', 'diff': 'text/plain; charset=utf-8'}
Id = Annotated[str, Field(min_length=1, max_length=200, pattern=r'^[^\x00-\x1f\x7f]+$')]


class Publication(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    run_id: Id
    evidence_id: Id
    source_url: Annotated[str, Field(min_length=1, max_length=8192)]
    captured_at: AwareDatetime
    object_id: Id
    query_scope: Annotated[str, Field(max_length=16384)]
    locator_or_page: Annotated[str, Field(max_length=16384)]
    excerpt: Annotated[str, Field(max_length=16384)] = ''
    artifact_kind: Literal['screenshot', 'text', 'pdf', 'ci', 'har', 'diff'] = 'text'
    sensitivity: Literal['public', 'restricted', 'redacted'] = 'restricted'
    original_evidence_id: Id | None = None
    redaction_status: Literal['BLOCKED', 'FILTERED'] = 'BLOCKED'
    policy_version: Id | None = None
    snapshot_id: Id | None = None
    step_id: Id | None = None
    commit_sha: Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')] | None = None
    test_run_id: Id | None = None


def unavailable():
    from ..errors import BusinessError
    return BusinessError('EVIDENCE_STORAGE_UNAVAILABLE', 'Evidence storage is unavailable', status=503)
