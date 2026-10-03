"""Strict model boundary DTOs, independent of provider clients and execution.

The model proposes one typed action, evidence request, result, or clarification.
Validation checks declared references; it never dispatches a browser action,
verifies evidence bytes, grants authority, or changes the committed Run state.
"""
from __future__ import annotations

import json
import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from ..tasks.models import (
    CodeParameters, GrafanaParameters, Hash, Id, Path, Positive, Repo,
    RepositoryWritePolicy, SHA, SQLITE_MAX, StrictModel, TaskContract, Text,
    TimeScope, UTC, URL, unique,
)

Nonnegative = Annotated[int, Field(strict=True, ge=0, le=SQLITE_MAX)]
DecimalString = Annotated[str, Field(pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]


class SemanticLocator(StrictModel):
    strategy: Literal["semantic"]
    role: Text | None
    accessible_name: Text | None
    label: Text | None

    @model_validator(mode="after")
    def nonempty(self):
        if not any((self.role, self.accessible_name, self.label)):
            raise ValueError("semantic locator cannot be empty")
        return self


class DOMLocator(StrictModel):
    strategy: Literal["dom"]
    attribute: Literal["id", "data-testid", "name", "href"]
    value: Text


class CoordinateLocator(StrictModel):
    strategy: Literal["coordinate"]
    screenshot_evidence_id: Id
    snapshot_id: Id
    tab_id: Id
    frame_id: Id
    width: Positive
    height: Positive
    x: Nonnegative
    y: Nonnegative

    @model_validator(mode="after")
    def dimensions(self):
        if self.x >= self.width or self.y >= self.height:
            raise ValueError("coordinate lies outside screenshot")
        return self


Locator = Annotated[Union[SemanticLocator, DOMLocator, CoordinateLocator], Field(discriminator="strategy")]


class WriteScope(StrictModel):
    repository: Repo
    branch: Text
    base_sha: SHA
    operation: Literal["edit_file", "create_branch", "commit", "create_pr", "update_pr"]
    files: list[Path]
    operation_id: Id
    identity_ref: Id
    target_rechecked_at: UTC


class ActionTarget(StrictModel):
    page_url: URL
    tab_id: Id
    frame_id: Id
    locator: Locator | None
    write_scope: WriteScope | None


class EmptyArgs(StrictModel):
    pass


class NavigateArgs(StrictModel):
    url: URL


class InputArgs(StrictModel):
    text: str


class KeyArgs(StrictModel):
    key: Literal["Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Control+A", "Meta+A"]


class SelectArgs(StrictModel):
    option_label: Text


class ScrollArgs(StrictModel):
    direction: Literal["up", "down", "left", "right"]
    pixels: Annotated[int, Field(strict=True, ge=1, le=5000)]


class SwitchTabArgs(StrictModel):
    tab_id: Id


class DownloadArgs(StrictModel):
    attachment_url: URL
    link_evidence_id: Id


class ActionBase(StrictModel):
    run_id: Id
    step_id: Id
    epoch: Positive
    snapshot_id: Id
    target: ActionTarget
    expected_effect: Literal["read", "write"]

    @model_validator(mode="after")
    def action_scope(self):
        if (self.expected_effect == "write") != (self.target.write_scope is not None):
            raise ValueError("write effect must carry explicit write scope; read must not")
        if isinstance(self.target.locator, CoordinateLocator):
            loc = self.target.locator
            if (loc.snapshot_id, loc.tab_id, loc.frame_id) != (self.snapshot_id, self.target.tab_id, self.target.frame_id):
                raise ValueError("coordinate locator is bound to another observation")
        return self


class Navigate(ActionBase):
    action_type: Literal["navigate"]
    expected_effect: Literal["read"]
    args: NavigateArgs


class Click(ActionBase):
    action_type: Literal["click"]
    args: EmptyArgs


class Input(ActionBase):
    action_type: Literal["input"]
    args: InputArgs


class Keypress(ActionBase):
    action_type: Literal["keypress"]
    args: KeyArgs


class Select(ActionBase):
    action_type: Literal["select"]
    args: SelectArgs


class Scroll(ActionBase):
    action_type: Literal["scroll"]
    expected_effect: Literal["read"]
    args: ScrollArgs


class SwitchTab(ActionBase):
    action_type: Literal["switch_tab"]
    expected_effect: Literal["read"]
    args: SwitchTabArgs


class ReadVisible(ActionBase):
    action_type: Literal["read_visible"]
    expected_effect: Literal["read"]
    args: EmptyArgs


class Screenshot(ActionBase):
    action_type: Literal["screenshot"]
    expected_effect: Literal["read"]
    args: EmptyArgs


class Download(ActionBase):
    action_type: Literal["download_attachment"]
    expected_effect: Literal["read"]
    args: DownloadArgs


Action = Annotated[Union[Navigate, Click, Input, Keypress, Select, Scroll, SwitchTab, ReadVisible, Screenshot, Download], Field(discriminator="action_type")]


class Observation(StrictModel):
    snapshot_id: Id
    run_id: Id
    captured_at: UTC
    source_url: URL
    title: str
    tab_id: Id
    frame_id: Id
    page_version: Id
    width: Positive
    height: Positive
    visible_excerpt: str
    evidence_ids: list[Id]
    redaction_status: Literal["FILTERED", "BLOCKED"]




class Coverage(StrictModel):
    searched_sources: list[Id]
    queries: list[str]
    cutoff_at: UTC | None
    content_pages: Nonnegative
    unread_candidates: list[Id]
    gaps: list[Text]
    complete: bool

    @model_validator(mode="after")
    def gaps_not_complete(self):
        if self.complete and (self.gaps or self.unread_candidates):
            raise ValueError("coverage with known gaps cannot be complete")
        return self


class FinanceItem(StrictModel):
    field_id: Id
    entity_id: Id
    report_version: Text
    period_start: UTC
    period_end: UTC
    period_type: Literal["annual", "quarterly", "year_to_date", "point_in_time"]
    metric_definition: Text
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    raw_value: Text
    disclosed_unit: Text
    normalized_value: DecimalString
    value_origin: Literal["disclosed", "derived"]
    formula: Text | None
    rounding_rule: Text
    rounding_lower: DecimalString
    rounding_upper: DecimalString
    channel: Id
    evidence_ids: Annotated[list[Id], Field(min_length=1)]

    @model_validator(mode="after")
    def financial_semantics(self):
        from decimal import Decimal
        if self.period_start > self.period_end:
            raise ValueError("period start is after end")
        if self.value_origin == "derived" and self.formula is None:
            raise ValueError("derived value needs formula")
        if not Decimal(self.rounding_lower) <= Decimal(self.normalized_value) <= Decimal(self.rounding_upper):
            raise ValueError("normalized value lies outside rounding interval")
        return self


class TestResult(StrictModel):
    name: Text
    test_run_id: Id
    commit_sha: SHA
    conclusion: Literal["success", "failure", "pending", "skipped", "cancelled", "missing"]
    evidence_ids: list[Id]


class CodeResult(StrictModel):
    scenario: Literal["operations"]
    operation_kind: Literal["code_repair"]
    repository: Repo
    base_sha: SHA
    branch: Text
    pr_url: URL | None
    head_sha: SHA | None
    head_before_verification: SHA | None
    head_after_verification: SHA | None
    changed_files: list[Path]
    required_checks: list[TestResult]
    independent_rules_ref: Id
    independent_test_result: TestResult | None
    evidence_ids: list[Id]


class GrafanaPoint(StrictModel):
    observed_at: UTC
    value: DecimalString | None
    label: Text


class GrafanaPanel(StrictModel):
    panel_id: Id
    unit: Text
    points: list[GrafanaPoint]
    evidence_ids: Annotated[list[Id], Field(min_length=1)]


class GrafanaResult(StrictModel):
    scenario: Literal["operations"]
    operation_kind: Literal["grafana_read"]
    dashboard_id: Id
    time_range: TimeScope
    variables: dict[Id, str]
    timezone: Text
    captured_at: UTC
    panels: list[GrafanaPanel]


class Claim(StrictModel):
    statement: Text
    evidence_ids: Annotated[list[Id], Field(min_length=1)]


class ResearchRelation(StrictModel):
    target_id: Id
    relation: Literal["citation", "discussion", "author_implementation", "third_party_implementation"]
    evidence_ids: Annotated[list[Id], Field(min_length=1)]


class ResearchItem(StrictModel):
    canonical_id: Id
    version: Text
    title: Text
    authors: Annotated[list[Text], Field(min_length=1)]
    first_published_at: UTC
    revised_at: UTC | None
    source_url: URL
    topic_basis: Text
    claims: list[Claim]
    relations: list[ResearchRelation]
    evidence_ids: Annotated[list[Id], Field(min_length=1)]


class MonitorEvent(StrictModel):
    source_id: Id
    object_id: Id
    semantic_version: Id
    content_fingerprint: Hash
    event_type: Literal["new", "updated", "deleted", "unavailable"]
    before_excerpt: str | None
    after_excerpt: str | None
    source_event_at: UTC | None
    observed_at: UTC
    detected_at: UTC
    evidence_ids: Annotated[list[Id], Field(min_length=1)]

    @model_validator(mode="after")
    def temporal(self):
        if self.detected_at < self.observed_at:
            raise ValueError("detection cannot precede observation")
        return self


class FinanceResult(StrictModel):
    scenario: Literal["finance"]
    values: list[FinanceItem]


class ResearchResult(StrictModel):
    scenario: Literal["research"]
    publications: list[ResearchItem]


class MonitorResult(StrictModel):
    scenario: Literal["monitoring"]
    source_id: Id
    baseline: bool
    scheduled_at: UTC
    started_at: UTC
    observed_at: UTC
    discovered_boundary: Id | None
    verified_boundary: Id | None
    verified_contiguous: bool
    examined_ranges: list[Text]
    pending_items: list[Id]
    gaps: list[Text]
    events: list[MonitorEvent]
    notification_keys: list[Id]

    @model_validator(mode="after")
    def monitor_semantics(self):
        keys = [(e.source_id, e.object_id, e.semantic_version, e.event_type) for e in self.events]
        unique(keys, "monitor semantic events")
        unique(self.notification_keys, "notification keys")
        if any(e.source_id != self.source_id for e in self.events):
            raise ValueError("event source mismatch")
        if self.baseline and (self.events or self.notification_keys):
            raise ValueError("baseline cannot report all existing content as new events")
        if self.pending_items and self.verified_boundary == self.discovered_boundary and self.discovered_boundary is not None:
            raise ValueError("pending items prevent verified boundary reaching discovered head")
        if self.verified_boundary is not None and not self.verified_contiguous:
            raise ValueError("verified boundary must be continuously verified")
        return self


OperationResult = Annotated[Union[CodeResult, GrafanaResult], Field(discriminator="operation_kind")]
ScenarioResult = Union[FinanceResult, OperationResult, ResearchResult, MonitorResult]




class RunCheckpoint(StrictModel):
    checkpoint_id: Id
    task_id: Id
    run_id: Id
    contract_version: Positive
    current_subgoal: Id
    verified_item_ids: list[Id]
    pending_item_ids: list[Id]
    current_object_id: Id
    current_object_version: Id | None
    current_snapshot_id: Id | None
    flow_version: Id | None
    action_sequence: Nonnegative
    business_event_id: Nonnegative
    budget_record_ref: Id
    identity_ref: Id | None
    pending_operation_ids: list[Id]
    epoch: Positive
    evidence_ids: list[Id]
    saved_at: UTC




class ModelAction(StrictModel):
    type: Literal["Action"]
    action: Action


class RequestEvidence(StrictModel):
    type: Literal["RequestEvidence"]
    criterion_ids: Annotated[list[Id], Field(min_length=1)]
    needed: Text
    source_ids: Annotated[list[Id], Field(min_length=1)]


class ProposeResult(StrictModel):
    type: Literal["ProposeResult"]
    items: ScenarioResult
    coverage: Coverage
    evidence_ids: list[Id]
    unresolved: list[Text]
    existing_operation_ids: list[Id]


class RequestInput(StrictModel):
    type: Literal["RequestInput"]
    requested_fields: Annotated[list[Text], Field(min_length=1)]
    reason: Text


ModelOutput = Annotated[Union[ModelAction, RequestEvidence, ProposeResult, RequestInput], Field(discriminator="type")]




class ModelInput(StrictModel):
    run_id: Id
    contract: TaskContract
    observation: Observation
    verified_checkpoint: RunCheckpoint
    image_evidence_ids: list[Id]
    allowed_action_schema_ref: Literal["urn:webagent:m0-contract-v1:Action"]
    selected_flow_versions: list[Id]

    @model_validator(mode="after")
    def input_bindings(self):
        cp = self.verified_checkpoint
        if self.observation.run_id != self.run_id or cp.run_id != self.run_id:
            raise ValueError("model input run references disagree")
        if (cp.task_id, cp.contract_version) != (self.contract.task_id, self.contract.contract_version):
            raise ValueError("model input checkpoint contract mismatch")
        if self.observation.redaction_status != "FILTERED":
            raise ValueError("unfiltered observation cannot be sent to model")
        if not set(self.image_evidence_ids) <= set(self.observation.evidence_ids):
            raise ValueError("model images must reference current observation evidence")
        if self.contract.memory_mode == "disabled" and self.selected_flow_versions:
            raise ValueError("disabled memory cannot supply historical flows")
        if cp.current_snapshot_id is not None and cp.current_snapshot_id != self.observation.snapshot_id:
            raise ValueError("checkpoint and observation snapshot disagree")
        if cp.identity_ref != self.contract.identity_ref:
            raise ValueError("checkpoint identity differs from frozen contract")
        if not any(source.permits(self.observation.source_url) for source in self.contract.sources):
            raise ValueError("observation URL outside declared sources")
        for name, values in (
            ("image evidence", self.image_evidence_ids),
            ("observation evidence", self.observation.evidence_ids),
            ("checkpoint evidence", cp.evidence_ids),
            ("selected flows", self.selected_flow_versions),
            ("verified items", cp.verified_item_ids),
            ("pending items", cp.pending_item_ids),
            ("pending operations", cp.pending_operation_ids),
        ):
            unique(values, name)
        if set(cp.verified_item_ids) & set(cp.pending_item_ids):
            raise ValueError("checkpoint verified and pending items overlap")
        return self


MODEL_OUTPUT_ADAPTER = TypeAdapter(ModelOutput)
MAX_OUTPUT_BYTES = 1024 * 1024


class InvalidModelOutput(ValueError):
    """Safe diagnostics suitable for repair prompts and ordinary call records.

    Raw model content, unexpected field names, values and Pydantic error context
    are deliberately excluded. Callers must not log the original exception.
    """

    def __init__(self, errors: list[dict[str, str]] | None = None):
        super().__init__("Model output failed local validation")
        self.errors = errors or [{"field": "$", "reason": "invalid_output"}]


def _duplicate_free(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidModelOutput([{"field": "$", "reason": "duplicate_key"}])
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise InvalidModelOutput([{"field": "$", "reason": "nonfinite_number"}])


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _nonfinite(value)
    return parsed


def _schema_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        names.update(value.get("properties", {}))
        for child in value.values():
            names.update(_schema_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_schema_names(child))
    return names


_SAFE_ERROR_LOCATIONS = _schema_names(MODEL_OUTPUT_ADAPTER.json_schema()) | {
    "Action", "RequestEvidence", "ProposeResult", "RequestInput",
    "FinanceResult", "CodeResult", "GrafanaResult", "ResearchResult", "MonitorResult",
    "navigate", "click", "input", "keypress", "select", "scroll", "switch_tab",
    "read_visible", "screenshot", "download_attachment", "semantic", "dom", "coordinate",
    "code_repair", "grafana_read",
}


def parse_model_output(raw: str) -> ModelOutput:
    """Accept one strict JSON document, with no code-fence/shape repair heuristics."""
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise InvalidModelOutput([{"field": "$", "reason": "invalid_size_or_type"}])
        json.loads(raw, object_pairs_hook=_duplicate_free, parse_constant=_nonfinite, parse_float=_finite_float)
        return MODEL_OUTPUT_ADAPTER.validate_json(raw, strict=True)
    except InvalidModelOutput:
        raise
    except (ValueError, RecursionError, UnicodeError) as error:
        if isinstance(error, ValidationError):
            diagnostics = []
            for detail in error.errors(include_input=False, include_context=False, include_url=False)[:20]:
                path = ".".join(
                    str(part) if isinstance(part, int) or part in _SAFE_ERROR_LOCATIONS else "[unknown]"
                    for part in detail["loc"]
                )
                diagnostics.append({"field": path or "$", "reason": detail["type"]})
            raise InvalidModelOutput(diagnostics) from None
        raise InvalidModelOutput([{"field": "$", "reason": "invalid_json"}]) from None


def output_json_schema() -> dict:
    """Return a fresh provider-neutral schema; JSON mode still needs local validation."""
    return MODEL_OUTPUT_ADAPTER.json_schema()


def _reject(field: str, reason: str) -> None:
    raise InvalidModelOutput([{"field": field, "reason": reason}])


def _evidence_references(value: object):
    if isinstance(value, BaseModel):
        for key in type(value).model_fields:
            child = getattr(value, key)
            if key == "evidence_ids":
                yield from child
            else:
                yield from _evidence_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _evidence_references(child)


def validate_output_for_input(output: ModelOutput, model_input: ModelInput) -> ModelOutput:
    """Check proposal declarations against the supplied, frozen input.

    This does not establish database ownership, current leases, actual evidence
    contents or success. The dispatcher and result aggregator must recheck them.
    """
    contract = model_input.contract
    cp = model_input.verified_checkpoint
    observation = model_input.observation
    source_ids = {source.source_id for source in contract.sources}
    available_evidence = set(observation.evidence_ids) | set(cp.evidence_ids)
    if isinstance(output, ModelAction):
        action = output.action
        target = action.target
        if action.run_id != model_input.run_id or action.epoch != cp.epoch:
            _reject("action", "run_or_epoch_mismatch")
        if (action.snapshot_id, target.tab_id, target.frame_id, target.page_url) != (
            observation.snapshot_id, observation.tab_id, observation.frame_id, observation.source_url
        ):
            _reject("action.target", "observation_binding_mismatch")
        urls = [target.page_url]
        if isinstance(action, Navigate):
            urls.append(action.args.url)
        if isinstance(action, Download):
            urls.append(action.args.attachment_url)
            if action.args.link_evidence_id not in observation.evidence_ids:
                _reject("action.args.link_evidence_id", "unknown_current_evidence")
        if not all(any(source.permits(url) for source in contract.sources) for url in urls):
            _reject("action.target", "url_outside_source_scope")
        locator = target.locator
        if isinstance(locator, CoordinateLocator):
            if (locator.width, locator.height) != (observation.width, observation.height):
                _reject("action.target.locator", "screenshot_dimensions_mismatch")
            if locator.screenshot_evidence_id not in model_input.image_evidence_ids:
                _reject("action.target.locator", "screenshot_not_supplied")
        if isinstance(action, (Click, Input, Select)) and locator is None:
            _reject("action.target.locator", "locator_required")
        if action.expected_effect == "write":
            policy = contract.action_policy
            scope = target.write_scope
            if not isinstance(policy, RepositoryWritePolicy):
                _reject("action.expected_effect", "read_only_contract")
            if (scope.repository, scope.branch, scope.base_sha, scope.identity_ref) != (
                policy.repository, policy.branch, policy.base_sha, contract.identity_ref
            ):
                _reject("action.target.write_scope", "frozen_authorization_mismatch")
            if scope.operation not in policy.allowed_operations or any(
                not policy.permits_file(path) for path in scope.files
            ):
                _reject("action.target.write_scope", "operation_outside_allowlist")
            if scope.operation in ("edit_file", "commit") and not scope.files:
                _reject("action.target.write_scope.files", "explicit_files_required")
            if scope.target_rechecked_at < observation.captured_at:
                _reject("action.target.write_scope.target_rechecked_at", "recheck_precedes_observation")
    elif isinstance(output, RequestEvidence):
        if not set(output.criterion_ids) <= {item.criterion_id for item in contract.acceptance_criteria}:
            _reject("criterion_ids", "unknown_criterion")
        if not set(output.source_ids) <= source_ids:
            _reject("source_ids", "unknown_source")
    elif isinstance(output, ProposeResult):
        if output.items.scenario != contract.scenario:
            _reject("items.scenario", "contract_scenario_mismatch")
        if not set(output.coverage.searched_sources) <= source_ids:
            _reject("coverage.searched_sources", "unknown_source")
        refs = set(_evidence_references(output))
        if not refs <= set(output.evidence_ids) or not refs <= available_evidence:
            _reject("evidence_ids", "missing_or_unknown_evidence")
        # A checkpoint lists pending operations only. Completed operation IDs
        # require the later write-intent repository, so do not invent that check.
        _validate_proposal_scope(output, contract)
    return output


def _validate_proposal_scope(output: ProposeResult, contract: TaskContract) -> None:
    """Preserve declared scope without treating a proposal as verified success."""
    items, parameters = output.items, contract.parameters
    if isinstance(items, FinanceResult):
        fields = {field.field_id for field in contract.output_schema}
        if len({item.field_id for item in items.values}) != len(items.values):
            _reject("items.values", "duplicate_output_field")
        for item in items.values:
            if item.field_id not in fields or (
                item.entity_id, item.report_version, item.period_type, item.currency
            ) != (
                parameters.entity_id, parameters.report_version, parameters.period_type,
                parameters.currency
            ):
                _reject("items.values", "financial_scope_mismatch")
            if ((contract.time_scope.start is not None and item.period_start != contract.time_scope.start)
                    or (contract.time_scope.end is not None and item.period_end != contract.time_scope.end)):
                _reject("items.values", "financial_period_mismatch")
    elif isinstance(items, CodeResult):
        if not isinstance(parameters, CodeParameters):
            _reject("items.operation_kind", "operations_subtype_mismatch")
        if (items.repository, items.base_sha, items.branch, items.independent_rules_ref) != (
            parameters.repository, parameters.base_sha, parameters.branch, parameters.independent_rules_ref
        ):
            _reject("items", "repository_scope_mismatch")
        if any(not contract.action_policy.permits_file(path) for path in items.changed_files):
            _reject("items.changed_files", "file_outside_allowlist")
        if not {check.name for check in items.required_checks} <= set(parameters.required_checks):
            _reject("items.required_checks", "unknown_required_check")
    elif isinstance(items, GrafanaResult):
        if not isinstance(parameters, GrafanaParameters):
            _reject("items.operation_kind", "operations_subtype_mismatch")
        if (items.dashboard_id, items.variables, items.timezone, items.time_range) != (
            parameters.dashboard_id, parameters.variables, parameters.timezone, contract.time_scope
        ):
            _reject("items", "dashboard_scope_mismatch")
        if not {panel.panel_id for panel in items.panels} <= set(parameters.panel_ids):
            _reject("items.panels", "unknown_panel")
    elif isinstance(items, ResearchResult):
        if len(items.publications) > parameters.max_items:
            _reject("items.publications", "item_limit_exceeded")
        if output.coverage.cutoff_at != parameters.cutoff_at or not set(output.coverage.queries) <= set(parameters.queries):
            _reject("coverage", "research_scope_mismatch")
        for item in items.publications:
            if not any(source.permits(item.source_url) for source in contract.sources):
                _reject("items.publications", "url_outside_source_scope")
            if item.first_published_at > parameters.cutoff_at or (item.revised_at and item.revised_at > parameters.cutoff_at):
                _reject("items.publications", "publication_after_cutoff")
    elif isinstance(items, MonitorResult):
        if (items.source_id, items.baseline, items.scheduled_at) != (
            parameters.source_id, parameters.baseline, parameters.scheduled_at
        ):
            _reject("items", "monitor_scope_mismatch")
