"""Serializable orchestration references, never execution authority or content."""
from __future__ import annotations

from typing import Literal, TypedDict

from ..models.schema import ModelOutput, Nonnegative
from ..tasks.models import Id, Positive, StrictModel, unique
from pydantic import Field, model_validator

GRAPH_VERSION = 'browser-loop-v1'
STATE_SCHEMA_VERSION = 'browser-loop-state-v1'
GRAPH_STATE_SCHEMA_VERSION = STATE_SCHEMA_VERSION
Phase = Literal['reconcile', 'observe', 'decide', 'dispatch', 'confirm',
                'verify', 'aggregate', 'wait', 'recover', 'stopped']
Route = Literal['reconcile', 'observe', 'decide', 'dispatch', 'confirm',
                'verify', 'aggregate', 'wait', 'recover', 'stopped', 'end']
Diagnostic = Literal['evidence_required', 'input_required', 'verification_incomplete',
                     'invalid_model_output', 'model_failed', 'page_changed',
                     'budget_exceeded', 'recovery_required', 'run_finished',
                     'configuration_required', 'identity_recheck_required',
                     'write_adapter_unavailable', 'graph_preparation_failed']


class GraphState(TypedDict):
    graph_version: str
    state_schema_version: str
    run_id: str
    contract_version: int
    state_version: int
    business_event_id: int
    business_checkpoint_id: str | None
    progress_id: int | None
    snapshot_id: str | None
    route: Route
    iteration: int
    observations: int
    decisions: int
    actions: int
    verifications: int
    diagnostic: Diagnostic | None
    evidence_ids: list[str]
    verified_summary_refs: list[str]
    wait_id: str | None
    completed: bool


class GraphSnapshot(StrictModel):
    """Strict validation before every framework save/read boundary.

    The state contains no model reply, browser handle, token, raw page content,
    free-form summary or pending action. A new process must reconcile refs.
    """
    graph_version: Literal['browser-loop-v1'] = GRAPH_VERSION
    state_schema_version: Literal['browser-loop-state-v1'] = STATE_SCHEMA_VERSION
    run_id: Id
    contract_version: Positive
    state_version: Nonnegative
    business_event_id: Nonnegative
    business_checkpoint_id: Id | None = None
    progress_id: Positive | None = None
    snapshot_id: Id | None = None
    route: Route = 'reconcile'
    iteration: Nonnegative = 0
    observations: Nonnegative = 0
    decisions: Nonnegative = 0
    actions: Nonnegative = 0
    verifications: Nonnegative = 0
    diagnostic: Diagnostic | None = None
    evidence_ids: list[Id] = Field(default_factory=list)
    verified_summary_refs: list[Id] = Field(default_factory=list)
    wait_id: Id | None = None
    completed: bool = False

    @model_validator(mode='after')
    def distinct_refs(self):
        unique(self.evidence_ids, 'graph evidence')
        unique(self.verified_summary_refs, 'graph summary refs')
        return self

    def state(self) -> GraphState:
        return self.model_dump(mode='json')


def validate_graph_state(value: dict) -> GraphState:
    return GraphSnapshot.model_validate(value).state()


class EphemeralOutput:
    """A process-local slot intentionally rejected by pickle/framework savers."""
    __slots__ = ('_output',)

    def __init__(self):
        self._output: ModelOutput | None = None

    def put(self, output: ModelOutput) -> None:
        if self._output is not None:
            raise RuntimeError('Unconsumed model output')
        self._output = output

    def take(self) -> ModelOutput:
        if self._output is None:
            raise RuntimeError('Model output must be regenerated after recovery')
        output, self._output = self._output, None
        return output

    def clear(self) -> None:
        self._output = None

    def __getstate__(self):
        raise TypeError('Runtime model outputs cannot be serialized')
