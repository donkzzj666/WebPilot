"""Runtime task DTOs, aligned with the frozen m0-contract-v1 public contract.

These declarations deliberately have no dependency on preparation documents or
evaluation data. Request parameters may be incomplete; the compiler validates
every supplied field before asking for the remaining fields.
"""
from __future__ import annotations

import fnmatch
import json
import math
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal, Union
from urllib.parse import unquote, urlsplit

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, JsonValue,
    field_validator, model_validator,
)

SQLITE_MAX = 2**63 - 1
SCHEMA_VERSION = 'm0-contract-v1'


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError('must not be blank')
    return value


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('timestamps must carry UTC timezone')
    return value


def safe_path(value: str) -> str:
    if '\\' in value or any(p in ('', '.', '..') for p in value.split('/')) or value.startswith('/'):
        raise ValueError('a normalized relative POSIX path is required')
    return value


def http_url(value: str) -> str:
    if '\\' in value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError('URL must not contain whitespace, controls or backslashes')
    parts = urlsplit(value)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None or '%' in parts.netloc):
        raise ValueError('a credential-free HTTP(S) URL is required')
    try:
        if parts.port == 0:
            raise ValueError('invalid URL port')
    except ValueError as error:
        raise ValueError('invalid URL port') from error
    return value


def scoped_path(value: str) -> str:
    """Compare URL paths after one unambiguous decoding, never dot normalization.

Encoded separators, nested percent encodings and dot segments are rejected
rather than interpreted differently by browsers, gateways and site routers.
Runtime redirects and DNS still require the execution gateway's own checks.
"""
    if (not value.startswith('/') or '\\' in value or '?' in value or '#' in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise ValueError('a normalized absolute URL path is required')
    if re.search(r'%(?![0-9a-fA-F]{2})|%(?:2f|5c|25)', value, re.I):
        raise ValueError('ambiguous encoded path is not allowed')
    try:
        decoded = unquote(value, errors='strict')
    except UnicodeError:
        raise ValueError('invalid encoded path') from None
    if (any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(part in ('.', '..') for part in decoded.split('/')) or '//' in decoded):
        raise ValueError('path must not contain dot segments, duplicate separators or controls')
    return decoded


def unique(values: list, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f'{label} must be unique')


def finite_json(value):
    """JSON floating point extension values must never enter canonical storage."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('JSON numbers must be finite')
    if isinstance(value, dict):
        for child in value.values():
            finite_json(child)
    elif isinstance(value, list):
        for child in value:
            finite_json(child)
    return value


Id = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(nonblank)]
Text = Annotated[str, Field(min_length=1), AfterValidator(nonblank)]
UTC = Annotated[datetime, AfterValidator(utc)]
Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
Repo = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
Path = Annotated[str, Field(min_length=1), AfterValidator(safe_path)]
URL = Annotated[str, AfterValidator(http_url)]
Positive = Annotated[int, Field(strict=True, ge=1, le=SQLITE_MAX)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200, pattern=r'^[!-~]+$')]
Scenario = Literal['finance', 'operations', 'research', 'monitoring']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


class BudgetProfile(StrictModel):
    max_actions: Annotated[int, Field(ge=1, le=150)] = 150
    max_content_pages: Annotated[int, Field(ge=1, le=25)] = 25
    max_active_seconds: Annotated[int, Field(ge=1, le=1200)] = 1200
    max_recoveries_per_obstacle: Annotated[int, Field(ge=0, le=3)] = 3
    min_site_interval_seconds: Annotated[int, Field(ge=3)] = 3
    max_ci_wait_seconds: Annotated[int, Field(ge=0, le=1200)] = 1200
    min_ci_poll_seconds: Annotated[int, Field(ge=30)] = 30
    max_handoff_seconds: Annotated[int, Field(ge=1, le=86400)] = 86400
    action_timeout_seconds: Annotated[int, Field(ge=1, le=30)] = 30
    max_model_format_repairs: Annotated[int, Field(ge=0, le=2)] = 2


class SourceScope(StrictModel):
    source_id: Id
    site_id: Id
    origin: URL
    path_prefix: str = '/'

    @model_validator(mode='after')
    def origin_only(self):
        p = urlsplit(self.origin)
        if p.path not in ('', '/') or p.query or p.fragment:
            raise ValueError('origin must not contain path or query')
        scoped_path(self.path_prefix)
        return self

    def permits(self, value: str) -> bool:
        try:
            http_url(value)
            p, origin = urlsplit(value), urlsplit(self.origin)
            path = scoped_path(p.path or '/')
            prefix = scoped_path(self.path_prefix).rstrip('/')
        except ValueError:
            return False
        def origin_tuple(parts):
            return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)
        return origin_tuple(p) == origin_tuple(origin) and (
            not prefix or path == prefix or path.startswith(prefix + '/'))


class Target(StrictModel):
    object_id: Id
    kind: Literal['entity', 'repository', 'dashboard', 'publication', 'monitor_source']
    canonical_name: Text


class ReadOnlyPolicy(StrictModel):
    mode: Literal['read_only'] = 'read_only'


class RepositoryWritePolicy(StrictModel):
    mode: Literal['repository_write']
    repository: Repo
    base_branch: Text
    branch: Text
    base_sha: SHA
    task_kind: Literal['ordinary_repair', 'workflow_repair']
    allowed_files: Annotated[list[Path], Field(min_length=1)]
    workflow_exception_files: list[Path]
    protected_patterns: Annotated[list[Text], Field(min_length=1)]
    required_checks: Annotated[list[Text], Field(min_length=1)]
    independent_rules_ref: Id
    allowed_operations: Annotated[list[Literal[
        'edit_file', 'create_branch', 'commit', 'create_pr', 'update_pr']], Field(min_length=1)]

    @model_validator(mode='after')
    def boundaries(self):
        for field in ('allowed_files', 'required_checks', 'allowed_operations', 'workflow_exception_files'):
            unique(getattr(self, field), field)
        if self.branch == self.base_branch:
            raise ValueError('write branch must differ from base branch')
        if any(not re.fullmatch(r'\.github/workflows/[^/]+\.(?:yml|yaml)', path)
               for path in self.workflow_exception_files):
            raise ValueError('workflow exceptions require explicitly named workflow YAML files')
        if not set(self.workflow_exception_files) <= set(self.allowed_files):
            raise ValueError('workflow exceptions must be explicit allowed files')
        if self.task_kind == 'ordinary_repair' and self.workflow_exception_files:
            raise ValueError('ordinary repair cannot declare workflow exceptions')
        if any(not self.permits_file(path) for path in self.allowed_files):
            raise ValueError('allowed file violates protected scope')
        return self

    def permits_file(self, path: str) -> bool:
        if path not in self.allowed_files:
            return False
        if any(fnmatch.fnmatchcase(path, pattern)
               for pattern in ('tests/*', 'test/*', 'acceptance/*', 'evaluation/*')):
            return False
        protected = path.startswith('.github/') or any(
            fnmatch.fnmatchcase(path, pattern) for pattern in self.protected_patterns)
        return not protected or (self.task_kind == 'workflow_repair' and path in self.workflow_exception_files)


ActionPolicy = Annotated[Union[ReadOnlyPolicy, RepositoryWritePolicy], Field(discriminator='mode')]


class Criterion(StrictModel):
    criterion_id: Id
    expected_rule: Text
    check_method: Literal['rule', 'semantic', 'independent_test']
    critical: bool


class OutputField(StrictModel):
    field_id: Id
    required: bool
    description: Text


class TimeScope(StrictModel):
    start: UTC | None
    end: UTC | None
    basis: Text

    @model_validator(mode='after')
    def chronological(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError('time scope start is after end')
        return self


class Provenance(StrictModel):
    origin: Literal['user', 'api', 'evaluation_manifest', 'web_content', 'explicit_test_configuration']
    reference: Id
    content_sha256: Hash
    authorizes_execution: bool

    @model_validator(mode='after')
    def no_web_authority(self):
        if self.origin == 'web_content' and self.authorizes_execution:
            raise ValueError('web content cannot authorize execution')
        return self


class FinanceParameters(StrictModel):
    scenario: Literal['finance']
    entity_id: Id
    report_version: Text
    period_type: Literal['annual', 'quarterly', 'year_to_date', 'point_in_time']
    metrics: Annotated[list[Text], Field(min_length=1)]
    currency: Annotated[str, Field(pattern=r'^[A-Z]{3}$')]


class CodeParameters(StrictModel):
    scenario: Literal['operations']
    operation_kind: Literal['code_repair']
    repository: Repo
    base_sha: SHA
    branch: Text
    failure_run_id: Id
    required_checks: Annotated[list[Text], Field(min_length=1)]
    independent_rules_ref: Id


class GrafanaParameters(StrictModel):
    scenario: Literal['operations']
    operation_kind: Literal['grafana_read']
    dashboard_id: Id
    panel_ids: Annotated[list[Id], Field(min_length=1)]
    variables: dict[Id, str]
    timezone: Text


OperationParameters = Annotated[Union[CodeParameters, GrafanaParameters], Field(discriminator='operation_kind')]


class ResearchParameters(StrictModel):
    scenario: Literal['research']
    queries: Annotated[list[Text], Field(min_length=1)]
    topic_criteria: Annotated[list[Text], Field(min_length=1)]
    cutoff_at: UTC
    max_items: Positive


class MonitoringParameters(StrictModel):
    scenario: Literal['monitoring']
    source_id: Id
    source_kind: Literal['security_community', 'cisa_kev']
    baseline: bool
    scheduled_at: UTC
    max_list_items: Annotated[int, Field(ge=1, le=10)] = 10
    max_details: Annotated[int, Field(ge=1, le=5)] = 5
    confirmed_boundary: Id | None


Parameters = Union[FinanceParameters, OperationParameters, ResearchParameters, MonitoringParameters]


class TaskContract(StrictModel):
    schema_version: Literal['m0-contract-v1'] = SCHEMA_VERSION
    task_id: Id
    contract_version: Positive
    scenario: Scenario
    objective: Text
    original_instruction: Text
    targets: Annotated[list[Target], Field(min_length=1)]
    sources: Annotated[list[SourceScope], Field(min_length=1)]
    start_urls: Annotated[list[URL], Field(min_length=1)]
    parameters: Parameters
    time_scope: TimeScope
    output_schema: Annotated[list[OutputField], Field(min_length=1)]
    acceptance_criteria: Annotated[list[Criterion], Field(min_length=1)]
    action_policy: ActionPolicy = Field(default_factory=ReadOnlyPolicy)
    identity_ref: Id | None
    budget_profile: BudgetProfile
    memory_mode: Literal['disabled', 'trusted', 'evaluation_snapshot']
    snapshot_id: Id | None
    batch_id: Id | None
    schedule_slot: Id | None
    provenance: Annotated[list[Provenance], Field(min_length=1)]
    created_at: UTC

    @model_validator(mode='after')
    def consistent(self):
        if self.scenario != self.parameters.scenario:
            raise ValueError('scenario and parameters disagree')
        unique([c.criterion_id for c in self.acceptance_criteria], 'criterion IDs')
        unique([f.field_id for f in self.output_schema], 'output field IDs')
        unique([s.source_id for s in self.sources], 'source IDs')
        if not any(c.critical for c in self.acceptance_criteria):
            raise ValueError('at least one critical acceptance criterion is required')
        if not all(any(source.permits(url) for source in self.sources) for url in self.start_urls):
            raise ValueError('start URL is outside source scope')
        if (self.memory_mode == 'evaluation_snapshot') != (self.snapshot_id is not None):
            raise ValueError('snapshot_id is required only for evaluation_snapshot')
        if not any(p.authorizes_execution for p in self.provenance):
            raise ValueError('executable contract needs non-web authorization provenance')
        if isinstance(self.parameters, CodeParameters):
            policy = self.action_policy
            if not isinstance(policy, RepositoryWritePolicy) or self.identity_ref is None:
                raise ValueError('code repair requires explicit write policy and identity reference')
            for key in ('repository', 'base_sha', 'branch', 'independent_rules_ref'):
                if getattr(policy, key) != getattr(self.parameters, key):
                    raise ValueError(f'code parameters and write policy disagree: {key}')
            if set(policy.required_checks) != set(self.parameters.required_checks):
                raise ValueError('required check sets disagree')
        elif not isinstance(self.action_policy, ReadOnlyPolicy):
            raise ValueError('only code_repair contracts may authorize repository writes')
        if isinstance(self.parameters, MonitoringParameters) and self.schedule_slot is None:
            raise ValueError('monitoring contract must reference its schedule slot')
        return self


class CreateTaskRequest(StrictModel):
    instruction: Text
    compiler_mode: Literal['fixture', 'natural_language'] = 'fixture'
    source_ids: Annotated[list[Id], Field(min_length=1)] | None = None
    sources: Annotated[list[SourceScope], Field(min_length=1, max_length=20)] | None = None
    start_urls: Annotated[list[URL], Field(min_length=1, max_length=20)] | None = None
    web_context: Annotated[list[Annotated[str, Field(min_length=1, max_length=20000)]], Field(max_length=20)] = Field(default_factory=list)
    time_scope: TimeScope | None = None
    scenario: Scenario | None = None
    parameters: dict[Id, JsonValue] = Field(default_factory=dict)
    action_policy: ActionPolicy = Field(default_factory=ReadOnlyPolicy)
    identity_ref: Id | None = None
    idempotency_key: IdempotencyKey | None = None

    @field_validator('time_scope', mode='before')
    @classmethod
    def json_time_scope(cls, value):
        # HTTP request dicts contain JSON timestamp strings; preserve all other
        # strictness instead of enabling Pydantic coercion for the whole model.
        if isinstance(value, dict):
            return TimeScope.model_validate_json(json.dumps(value, allow_nan=False))
        return value

    @model_validator(mode='after')
    def compiler_inputs(self):
        if self.compiler_mode == 'fixture':
            if self.sources is not None or self.start_urls is not None or self.web_context or self.time_scope is not None:
                raise ValueError('natural-language fields require natural_language compiler mode')
        elif self.source_ids is not None:
            raise ValueError('natural_language requires explicit sources, not fixture source IDs')
        if self.sources is not None:
            unique([source.source_id for source in self.sources], 'source IDs')
        if self.start_urls is not None:
            unique(self.start_urls, 'start URLs')
        return self

    @field_validator('source_ids')
    @classmethod
    def unique_sources(cls, value):
        if value is not None:
            unique(value, 'source IDs')
        return value

    @field_validator('parameters')
    @classmethod
    def json_numbers(cls, value):
        return finite_json(value)


class ClarificationRequest(StrictModel):
    contract_version: Positive
    values: Annotated[dict[Id, JsonValue], Field(min_length=1)]
    idempotency_key: IdempotencyKey | None = None

    @field_validator('values')
    @classmethod
    def json_numbers(cls, value):
        return finite_json(value)


class RevisionRequest(CreateTaskRequest):
    """An explicit replacement draft; omission never inherits old permissions."""
    contract_version: Positive
