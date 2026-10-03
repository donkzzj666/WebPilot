"""Separate, read-only semantic checks using the original Run's model budget.

The provider receives a small value capsule, never a Run lease, filesystem path,
DB connection, executor history, action schema or credentials reference. Its
checks remain evidence claims; the business aggregator retains final authority.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
import time
from typing import Literal, Protocol
import uuid

from pydantic import Field, model_validator

from ..db.connection import connect
from ..db.repository import canonical_json
from ..errors import BusinessError
from ..evidence.redaction import TextRedactor
from ..models.journal import ModelCallRecord, ModelUsage, finish_attempt, remaining_seconds, reserve_attempt
from ..models.pricing import estimate_token_cost
from ..models.schema import ProposeResult
from ..models.transport import (MAX_REQUEST_TEXT_BYTES, MAX_RESPONSE_BYTES, ModelConfig,
                                ProviderFailure, ProviderReply, VERIFIER_PROMPT_VERSION)
from ..tasks.models import Criterion, Id, StrictModel, TaskContract, unique
from .models import Check, EvidenceDocument, Verdict


class SemanticProvider(Protocol):
    config: ModelConfig

    async def complete_verification(self, payload: dict, schema: dict,
                                    repair_errors: list | None = None) -> ProviderReply: ...


class _Diagnostic(StrictModel):
    code: Literal['supported', 'contradicted', 'missing_support', 'conflicting_sources']


class _SemanticCheck(StrictModel):
    criterion_id: Id
    verdict: Verdict
    evidence_ids: list[Id] = Field(max_length=1000)
    actual: _Diagnostic

    @model_validator(mode='after')
    def consistent_diagnostic(self):
        unique(self.evidence_ids, 'semantic evidence')
        expected = {Verdict.PASS: 'supported', Verdict.FAIL: 'contradicted',
                    Verdict.INSUFFICIENT: 'missing_support', Verdict.CONFLICT: 'conflicting_sources'}
        if self.actual.code != expected[self.verdict]:
            raise ValueError('diagnostic must match verdict')
        if self.verdict in (Verdict.PASS, Verdict.FAIL, Verdict.CONFLICT) and not self.evidence_ids:
            raise ValueError('a conclusive check requires original evidence')
        if self.verdict == Verdict.CONFLICT and len(self.evidence_ids) < 2:
            raise ValueError('conflict requires distinct original sources')
        return self


class _SemanticResponse(StrictModel):
    checks: list[_SemanticCheck] = Field(min_length=1, max_length=100)


@dataclass(frozen=True)
class _BudgetReference:
    budget_record_ref: str


@dataclass(frozen=True)
class _JournalContext:
    run_id: str
    contract: TaskContract
    verified_checkpoint: _BudgetReference


def semantic_output_schema() -> dict:
    return _SemanticResponse.model_json_schema()


def _insufficient(criteria, reason):
    now = datetime.now(timezone.utc)
    return ([Check(criterion_id=c.criterion_id, expected_rule=c.expected_rule,
                   actual={'code': reason}, verdict=Verdict.INSUFFICIENT, evidence_ids=[],
                   checked_at=now, checker_version=VERIFIER_PROMPT_VERSION) for c in criteria],
            [reason])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON property')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonfinite JSON value')


def _claim_facts(value):
    # Only typed scenario facts enter this function, not arbitrary graph state.
    # topic_basis is an executor's assessment and must never support itself.
    if isinstance(value, dict):
        return {key: _claim_facts(item) for key, item in value.items() if key != 'topic_basis'}
    if isinstance(value, list):
        return [_claim_facts(item) for item in value]
    return value


def _evidence_groups(value):
    groups = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'evidence_ids' and isinstance(item, list) and item:
                groups.append(set(item))
            else:
                groups.extend(_evidence_groups(item))
    elif isinstance(value, list):
        for item in value:
            groups.extend(_evidence_groups(item))
    return groups


def _payload(contract, proposal, documents, criteria, redactor):
    facts = _claim_facts(proposal.items.model_dump(mode='json'))
    requirements = {'scenario': contract.scenario, 'parameters': contract.parameters.model_dump(mode='json'),
                    'criteria': [c.model_dump(mode='json') for c in criteria]}
    # Altering criteria, object IDs or candidate values by redaction changes the
    # question being verified. Fail closed; only source text can be filtered.
    if redactor.contains_sensitive(canonical_json({'requirements': requirements, 'facts': facts})):
        raise BusinessError('INPUT_BLOCKED', 'Verification claims contain sensitive bindings', status=409)
    evidence = []
    for document in documents:
        metadata = document.model_dump(mode='json', exclude={'content', 'readable', 'problem'})
        if redactor.contains_sensitive(canonical_json(metadata)):
            raise BusinessError('INPUT_BLOCKED', 'Verification evidence metadata is sensitive', status=409)
        content = json.loads(redactor.filter(canonical_json(document.content)))
        evidence.append({**metadata, 'content': content, 'trust': 'untrusted_original_evidence'})
    payload = {'protocol_version': VERIFIER_PROMPT_VERSION, 'frozen_requirements': requirements,
               'candidate_facts': facts, 'untrusted_evidence': evidence}
    if len(canonical_json(payload).encode()) + len(canonical_json(semantic_output_schema()).encode()) > MAX_REQUEST_TEXT_BYTES:
        raise BusinessError('INPUT_BLOCKED', 'Verification context exceeds the text limit', status=409)
    return payload


def _parse(reply, criteria, allowed, groups, redactor):
    if (reply.invalid_reason or type(reply.content) is not str or
            not 0 < len(reply.content.encode()) <= MAX_RESPONSE_BYTES):
        raise ValueError('invalid verification output')
    parsed = json.loads(reply.content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    output = _SemanticResponse.model_validate_json(canonical_json(parsed))
    expected = {c.criterion_id for c in criteria}
    if len(output.checks) != len(expected) or {c.criterion_id for c in output.checks} != expected:
        raise ValueError('criteria differ from frozen request')
    for check in output.checks:
        ids = set(check.evidence_ids)
        if not ids.issubset(allowed):
            raise ValueError('evidence outside verified capsule')
        # A global PASS may not silently skip one publication or summary claim.
        if check.verdict == Verdict.PASS and any(not ids.intersection(group) for group in groups):
            raise ValueError('semantic pass omitted a candidate claim')
    if redactor.contains_sensitive(canonical_json(output.model_dump(mode='json'))):
        raise ValueError('sensitive semantic output')
    return output


async def verify_semantics(database: Path, provider: SemanticProvider | None, *, run,
                           contract: TaskContract, proposal: ProposeResult,
                           documents: tuple[EvidenceDocument, ...] | list[EvidenceDocument],
                           criteria: list[Criterion], execution_token=None, revalidate=None
                           ) -> tuple[list[Check], list[str]]:
    """Check immutable criteria using a distinct model call and safe source capsule.

    `run` is the trusted current database row, and `revalidate` must recheck the
    storage/lease/evidence bindings in scheduled production calls. Authority or
    budget loss raises BusinessError; provider uncertainty returns INSUFFICIENT.
    Caller cancellation propagates and never initiates an automatic retry.
    """
    path = Path(database)
    contract = TaskContract.model_validate_json(canonical_json(contract.model_dump(mode='json')))
    proposal = ProposeResult.model_validate_json(canonical_json(proposal.model_dump(mode='json')))
    documents = tuple(EvidenceDocument.model_validate_json(canonical_json(d.model_dump(mode='json'))) for d in documents)
    criteria = [Criterion.model_validate_json(canonical_json(c.model_dump(mode='json'))) for c in criteria]
    frozen = {c.criterion_id: c for c in contract.acceptance_criteria}
    if len({c.criterion_id for c in criteria}) != len(criteria) or any(frozen.get(c.criterion_id) != c for c in criteria):
        raise BusinessError('STATE_CONFLICT', 'Semantic criteria differ from frozen contract', status=409)
    if not criteria:
        return [], []
    run_id = run['run_id']
    with connect(path) as db:
        current = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        budget = db.execute('SELECT budget_record_id FROM run_budgets WHERE run_id=?', (run_id,)).fetchone()
        scheduled = db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone()
    contract_sha = hashlib.sha256(canonical_json(contract.model_dump(mode='json')).encode()).hexdigest()
    if (current is None or budget is None or current['state_version'] != run['state_version']
            or current['state'] not in ('RUNNING', 'VERIFYING', 'RECONCILING')
            or (current['task_id'], current['contract_version'], current['contract_sha256']) !=
               (contract.task_id, contract.contract_version, contract_sha)):
        raise BusinessError('STATE_CONFLICT', 'Semantic verifier needs the current frozen Run', status=409)
    if scheduled and revalidate is None:
        raise BusinessError('STATE_CONFLICT', 'Scheduled semantic checks need evidence revalidation', status=409)

    async def guard():
        if revalidate is not None:
            result = revalidate()
            if inspect.isawaitable(result):
                await result

    await guard()
    if provider is None or not callable(getattr(provider, 'complete_verification', None)):
        return _insufficient(criteria, 'semantic_provider_unavailable')
    config = ModelConfig.model_validate_json(canonical_json(provider.config.model_dump(mode='json')))
    if current['model_config_sha256'] != config.config_sha256:
        raise BusinessError('STATE_CONFLICT', 'Semantic provider differs from the frozen Run configuration', status=409)
    context = _JournalContext(run_id, contract, _BudgetReference(budget['budget_record_id']))
    available = []
    declared = set(proposal.evidence_ids)
    seen = set()
    for document in documents:
        if document.evidence_id in seen:
            raise BusinessError('STATE_CONFLICT', 'Duplicate semantic evidence', status=409)
        seen.add(document.evidence_id)
        if document.run_id != run_id:
            raise BusinessError('STATE_CONFLICT', 'Semantic evidence belongs to another Run', status=409)
        if (document.evidence_id in declared and document.readable and document.content is not None
                and document.artifact_kind != 'screenshot'
                and any(source.permits(document.source_url) for source in contract.sources)):
            available.append(document)
    if not available:
        return _insufficient(criteria, 'semantic_original_evidence_missing')
    redactor = TextRedactor(tuple(getattr(provider, 'sensitive_literals', ())))
    try:
        payload = _payload(contract, proposal, available, criteria, redactor)
    except BusinessError as error:
        if error.code != 'INPUT_BLOCKED':
            raise
        return _insufficient(criteria, 'semantic_input_blocked')
    schema = semantic_output_schema()
    groups = _evidence_groups(payload['candidate_facts'])
    allowed = {d.evidence_id for d in available}
    if not groups or any(not group.intersection(allowed) for group in groups):
        return _insufficient(criteria, 'semantic_claim_evidence_missing')
    start = time.monotonic()
    deadline = min(start + config.total_seconds,
                   start + remaining_seconds(path, context, config, execution_token=execution_token))
    call_id = str(uuid.uuid4())
    repairs = contract.budget_profile.max_model_format_repairs
    errors = None
    for number in range(1, repairs + 2):
        await guard()
        if time.monotonic() >= deadline:
            return _insufficient(criteria, 'semantic_deadline')
        reserve_attempt(path, context, config, call_id=call_id, request_id=(request_id := str(uuid.uuid4())),
                        attempt_number=number, execution_token=execution_token,
                        prompt_version=VERIFIER_PROMPT_VERSION)
        attempt_start = time.monotonic()
        reply = None
        finalized = False

        def record(error_class=None):
            usage = ModelUsage.model_validate(reply.usage) if reply is not None else ModelUsage()
            usage = usage.model_copy(update={'provider_usage': {**usage.provider_usage, 'attempt_number': number}})
            value = ModelCallRecord(run_id=run_id, request_id=request_id,
                provider_request_id=reply.provider_request_id if reply else None, provider=config.provider,
                model_id=config.model_id, config_sha256=config.config_sha256, prompt_version=VERIFIER_PROMPT_VERSION,
                usage=usage, duration_ms=max(0, math.ceil((time.monotonic() - attempt_start) * 1000)),
                format_repairs=number - 1, error_class=error_class, price_version=config.price_version,
                estimated_cost=(cost := estimate_token_cost(config.pricing, usage)),
                cost_currency=config.pricing.currency if cost is not None else None)
            if redactor.contains_sensitive(canonical_json(value.model_dump(mode='json'))):
                raise ValueError('invalid provider metadata')
            return value

        def finish(status, error_class=None, subtype=None):
            nonlocal finalized
            value = record(error_class)
            finalized = True
            current = finish_attempt(path, value, status=status, diagnostic_subtype=subtype,
                                     execution_token=execution_token)
            if not current:
                remaining_seconds(path, context, config, execution_token=execution_token)
                raise BusinessError('STATE_CONFLICT', 'Run changed during semantic verification', status=409)

        try:
            await guard()
            if time.monotonic() >= deadline:
                raise TimeoutError
            if provider.config.config_sha256 != config.config_sha256:
                raise ProviderFailure('provider_error', 'config_changed')
            async with asyncio.timeout_at(deadline):
                reply = await provider.complete_verification(deepcopy(payload), deepcopy(schema),
                                                             repair_errors=deepcopy(errors))
            if not isinstance(reply, ProviderReply):
                reply = None
                raise ProviderFailure('provider_error', 'invalid_provider_reply')
            try:
                record()
            except (ValueError, TypeError):
                reply = None
                raise ProviderFailure('provider_error', 'invalid_provider_metadata') from None
            await guard()
            if provider.config.config_sha256 != config.config_sha256:
                raise ProviderFailure('provider_error', 'config_changed')
            if time.monotonic() >= deadline:
                raise TimeoutError
            try:
                output = _parse(reply, criteria, allowed, groups, redactor)
            except (ValueError, TypeError, RecursionError):
                finish('INVALID', 'invalid_output', 'schema_validation')
                if number > repairs:
                    return _insufficient(criteria, 'semantic_invalid_output')
                errors = [{'field': '$', 'reason': 'invalid_output'}]
                continue
        except asyncio.CancelledError:
            # A failed cancellation journal write remains STARTED/unknown and
            # cannot authorize another provider attempt.
            try:
                finish('CANCELLED', subtype='caller_cancelled')
            except Exception:
                pass
            raise
        except BusinessError:
            if not finalized:
                finish('ERROR', 'provider_error', 'input_blocked')
            raise
        except (TimeoutError, ProviderFailure) as error:
            kind = 'timeout' if isinstance(error, TimeoutError) else 'provider_error'
            if isinstance(error, ProviderFailure):
                if error.error_class in ('timeout', 'rate_limit', 'invalid_credentials', 'provider_error'):
                    kind = error.error_class
                reply = error.reply or reply
            # Retain known safe usage, never provider error text or its subtype.
            try:
                if reply is not None and not isinstance(reply, ProviderReply):
                    raise ValueError('invalid reply')
                record(kind)
            except (ValueError, TypeError):
                reply = None
                kind = 'provider_error'
            finish('ERROR', kind, 'semantic_provider_error')
            return _insufficient(criteria, 'semantic_provider_error')
        except Exception:
            if finalized:
                raise
            reply = None
            finish('ERROR', 'provider_error', 'semantic_provider_exception')
            return _insufficient(criteria, 'semantic_provider_error')
        else:
            finish('VALID')
            now = datetime.now(timezone.utc)
            by_id = {c.criterion_id: c for c in output.checks}
            checks = [Check(criterion_id=c.criterion_id, expected_rule=c.expected_rule,
                            actual=by_id[c.criterion_id].actual.model_dump(mode='json'),
                            verdict=by_id[c.criterion_id].verdict, evidence_ids=by_id[c.criterion_id].evidence_ids,
                            checked_at=now, checker_version=VERIFIER_PROMPT_VERSION) for c in criteria]
            return checks, (['semantic_criteria_unresolved'] if any(c.verdict != Verdict.PASS for c in checks) else [])
    raise AssertionError('bounded verification loop cannot fall through')
