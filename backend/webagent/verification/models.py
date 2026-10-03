"""Frozen runtime verification protocol, with no execution authority."""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models.schema import Coverage, Nonnegative, ScenarioResult
from ..tasks.models import Hash, Id, Positive, SHA, Scenario, StrictModel, Text, UTC, finite_json, unique


class Verdict(str, Enum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    INSUFFICIENT = 'INSUFFICIENT'
    CONFLICT = 'CONFLICT'


class EvidenceDocument(StrictModel):
    """Verified original bytes parsed by the trusted, read-only loading service.

    ``content`` is never supplied by a model. Readability is checked again by
    the aggregator before committing a result.
    """
    evidence_id: Id
    run_id: Id
    source_url: str
    captured_at: UTC
    artifact_kind: Literal['screenshot', 'text', 'pdf', 'ci', 'har', 'diff']
    sha256: Hash
    object_id: Id
    snapshot_id: Id | None = None
    locator_or_page: Text
    commit_sha: SHA | None = None
    test_run_id: Id | None = None
    content: JsonValue | None = None
    readable: bool = True
    problem: str | None = None

    _finite = field_validator('content')(finite_json)


def _pointer(value: str) -> str:
    if len(value) > 4096 or (value and not value.startswith('/')) or re.search(r'~(?![01])', value):
        raise ValueError('a bounded RFC 6901 JSON pointer is required')
    return value


class FieldBinding(StrictModel):
    result_path: Annotated[str, Field(max_length=4096)]
    evidence_id: Id
    evidence_path: Annotated[str, Field(max_length=4096)]

    _pointers = field_validator('result_path', 'evidence_path')(_pointer)


class FieldCheck(StrictModel):
    result_path: str
    verdict: Verdict
    evidence_ids: list[Id]
    actual: JsonValue


class Check(StrictModel):
    criterion_id: Id
    expected_rule: Text
    actual: JsonValue
    verdict: Verdict
    evidence_ids: list[Id]
    checked_at: UTC
    checker_version: Id

    @model_validator(mode='after')
    def evidence_required(self):
        unique(self.evidence_ids, 'check evidence')
        if self.verdict == Verdict.PASS and not self.evidence_ids:
            raise ValueError('PASS requires evidence')
        return self


class SideEffect(StrictModel):
    operation_id: Id
    target: Text
    effect_type: Literal['branch', 'commit', 'pr', 'benchmark_write']
    status: Literal['INTENT', 'CONFIRMED', 'NOT_APPLIED', 'UNKNOWN']
    receipt: Text | None
    evidence_ids: list[Id]
    critical_violation: bool

    @model_validator(mode='after')
    def confirmed_evidence(self):
        unique(self.evidence_ids, 'side effect evidence')
        if self.status == 'CONFIRMED' and (self.receipt is None or not self.evidence_ids):
            raise ValueError('confirmed effect needs receipt and evidence')
        return self


class Result(StrictModel):
    task_id: Id
    run_id: Id
    contract_version: Positive
    scenario: Scenario
    outcome: Literal['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED']
    assistance_count: Nonnegative
    items: ScenarioResult
    checks: list[Check]
    coverage: Coverage
    evidence_ids: list[Id]
    unresolved: list[Text]
    side_effects: list[SideEffect]
    generated_by: Literal['business_aggregator']

    @model_validator(mode='after')
    def result_shape(self):
        if self.scenario != self.items.scenario:
            raise ValueError('result scenario mismatch')
        unique([c.criterion_id for c in self.checks], 'result criteria')
        unique(self.evidence_ids, 'result evidence')
        unique([s.operation_id for s in self.side_effects], 'side effects')
        if self.outcome == 'SUCCEEDED':
            if not self.checks or not self.evidence_ids:
                raise ValueError('success requires checks and evidence')
            if any(s.status in {'UNKNOWN', 'INTENT'} or s.critical_violation for s in self.side_effects):
                raise ValueError('success forbids unresolved writes or critical side effects')
        return self


class RuleEvaluation(StrictModel):
    checks: list[Check]
    fields: list[FieldCheck]
    semantic_criterion_ids: list[Id]
    violations: list[str]
    deliverable_paths: list[str]
    unresolved: list[str]
