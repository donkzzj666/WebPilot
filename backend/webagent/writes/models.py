"""Strict, bounded facts from trusted page adapters, never model authority."""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..db.repository import canonical_json
from ..errors import BusinessError
from ..scheduler.models import canonical_repository
from ..tasks.models import Hash, Id, Path, SHA, StrictModel, unique


class WriteTarget(StrictModel):
    repository: str = Field(min_length=3, max_length=201)
    branch: str = Field(min_length=1, max_length=200)
    base_sha: SHA
    operation: Literal['edit_file', 'create_branch', 'commit', 'create_pr', 'update_pr']
    files: list[Path] = Field(max_length=100)

    @field_validator('repository')
    @classmethod
    def repository_name(cls, value):
        try:
            return canonical_repository(value)
        except BusinessError:
            raise ValueError('invalid repository target') from None

    @field_validator('branch')
    @classmethod
    def branch_name(cls, value):
        if value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError('branch must be a bounded reference')
        return value

    @field_validator('files')
    @classmethod
    def file_names(cls, value):
        unique(value, 'files')
        if any(len(path) > 1024 for path in value):
            raise ValueError('file path is too long')
        return sorted(value)


class WriteClaim(StrictModel):
    identity_ref: Id
    target: WriteTarget
    expected_change_sha256: Hash
    precondition_version: Id
    adapter_id: Id = 'gateway-structured-v1'


class WriteCheckFacts(StrictModel):
    outcome: Literal['APPLIED', 'NOT_APPLIED', 'UNKNOWN']
    identity_ref: Id
    target: WriteTarget
    expected_change_sha256: Hash
    precondition_version: Id
    receipt: dict | None
    snapshot_id: Id
    observed_version: Id | None = None

    @model_validator(mode='after')
    def bounded_receipt(self):
        if self.receipt is not None:
            payload = canonical_json(self.receipt)
            if not self.receipt or len(payload.encode('utf-8')) > 8192:
                raise ValueError('receipt must be a nonempty bounded object')
            def bounded(value, depth=0):
                if depth > 4:
                    raise ValueError('receipt nesting exceeds the limit')
                if isinstance(value, dict):
                    if len(value) > 32 or any(type(k) is not str or len(k) > 200 for k in value):
                        raise ValueError('receipt object exceeds the limit')
                    for child in value.values():
                        bounded(child, depth + 1)
                elif isinstance(value, list):
                    if len(value) > 32:
                        raise ValueError('receipt list exceeds the limit')
                    for child in value:
                        bounded(child, depth + 1)
                elif type(value) not in (str, int, bool, float, type(None)):
                    raise ValueError('receipt must contain JSON values')
            bounded(self.receipt)
        return self


def business_key(task_id: str, claim: WriteClaim | dict) -> str:
    """One semantic operation across graph reentry, epochs and related Runs."""
    claim = claim if isinstance(claim, WriteClaim) else WriteClaim.model_validate(claim)
    if (type(task_id) is not str or not 1 <= len(task_id) <= 200 or task_id != task_id.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in task_id)):
        raise ValueError('invalid task identifier')
    payload = ['write-protocol-v1', task_id, claim.identity_ref,
               claim.target.model_dump(mode='json'), claim.expected_change_sha256]
    return 'write-' + hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()
