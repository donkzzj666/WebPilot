"""Provider-neutral, asynchronous proposals with durable bounded repair attempts.

The adapter returns validated proposals only. The graph/dispatcher still owns
execution, lease checks, evidence verification, and the Run's terminal outcome.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Protocol
import uuid

from ..db.connection import StorageBusyError
from ..db.repository import canonical_json
from ..errors import BusinessError
from ..evidence.service import EvidenceService
from .journal import ModelCallRecord, ModelUsage, finish_attempt, remaining_seconds, reserve_attempt
from .pricing import estimate_token_cost
from .schema import (
    InvalidModelOutput, ModelInput, ModelOutput, output_json_schema,
    parse_model_output, validate_output_for_input,
)
from .transport import ModelConfig, ModelImage, ProviderFailure, ProviderReply, validate_images


class ModelProvider(Protocol):
    config: ModelConfig

    async def complete(self, model_input: dict, schema: dict,
                       images: tuple[ModelImage, ...] = (),
                       repair_errors: list | None = None) -> ProviderReply: ...


class ModelError(BusinessError):
    def __init__(self, error_class: str, subtype: str, *, call_id: str,
                 retry_after_seconds: float | None = None):
        code, status = {
            'timeout': ('TIMEOUT', 504),
            'rate_limit': ('MODEL_RATE_LIMIT', 429),
        }.get(error_class, ('UPSTREAM_ERROR', 502))
        super().__init__(code, 'Model request failed: ' + error_class, status=status)
        self.error_class = error_class
        self.subtype = subtype
        self.call_id = call_id
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class GenerationResult:
    output: ModelOutput
    call_id: str
    records: list[ModelCallRecord]


class ModelAdapter:
    def __init__(self, database: Path, provider: ModelProvider):
        self.database = Path(database)
        self.provider = provider

    async def generate(self, model_input: ModelInput, *, images: tuple[ModelImage, ...] = (),
                       deadline: float | None = None, execution_token=None) -> GenerationResult:
        # Frozen DTOs can still contain mutable lists. Snapshot/revalidate before
        # the first await, and use this same input for every format repair.
        model_input = ModelInput.model_validate_json(canonical_json(model_input.model_dump(mode='json')))
        payload = model_input.model_dump(mode='json')
        validate_images(payload, images)
        evidence = EvidenceService(self.database.parent)
        def storage_operation(function, *args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as error:
                evidence.check_storage_error(error)
                raise
        payload = evidence.guard_model_input(payload, images,
            known_secrets=getattr(self.provider, 'sensitive_literals', ()))
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError('deadline must be a finite monotonic timestamp')
        config = self.provider.config
        call_id = str(uuid.uuid4())
        started = time.monotonic()
        total_deadline = min(started + config.total_seconds,
                             deadline if deadline is not None else math.inf,
                             started + storage_operation(remaining_seconds, self.database, model_input, config, execution_token=execution_token))
        schema = output_json_schema()
        errors = None
        records: list[ModelCallRecord] = []
        limit = model_input.contract.budget_profile.max_model_format_repairs
        for number in range(1, limit + 2):
            if time.monotonic() >= total_deadline:
                raise ModelError('timeout', 'generation_deadline', call_id=call_id)
            request_id = str(uuid.uuid4())
            storage_operation(reserve_attempt, self.database, model_input, config, call_id=call_id,
                            request_id=request_id, attempt_number=number, execution_token=execution_token)
            attempt_started = time.monotonic()
            reply = None

            def record(error_class=None):
                usage = ModelUsage.model_validate(reply.usage) if reply else ModelUsage()
                usage = usage.model_copy(update={'provider_usage': {
                    **usage.provider_usage, 'attempt_number': number}})
                item = ModelCallRecord(
                    run_id=model_input.run_id, request_id=request_id,
                    provider_request_id=reply.provider_request_id if reply else None,
                    provider=config.provider, model_id=config.model_id,
                    config_sha256=config.config_sha256, prompt_version=config.prompt_version,
                    usage=usage, duration_ms=max(0, math.ceil((time.monotonic() - attempt_started) * 1000)),
                    format_repairs=number - 1, error_class=error_class,
                    price_version=config.price_version,
                    estimated_cost=(cost := estimate_token_cost(config.pricing, usage)),
                    cost_currency=config.pricing.currency if cost is not None else None)
                from ..evidence.redaction import TextRedactor
                if TextRedactor(tuple(getattr(self.provider, 'sensitive_literals', ()))).contains_sensitive(
                        canonical_json(item.model_dump(mode='json'))):
                    raise ValueError('invalid provider metadata')
                return item

            def finish(status, error_class=None, subtype=None):
                item = record(error_class)
                current = storage_operation(finish_attempt, self.database, item, status=status, diagnostic_subtype=subtype,
                                         execution_token=execution_token)
                records.append(item)
                if not current:
                    # A local provider timeout may win the race with the
                    # independent watcher. Report the committed Run stop so
                    # the Worker fences it as a budget failure immediately.
                    remaining_seconds(self.database, model_input, config, execution_token=execution_token)
                    raise BusinessError('STATE_CONFLICT', 'Run changed during model generation', status=409)

            try:
                # Reservation itself may wait for SQLite. Do not start HTTP if
                # that local wait consumed the last available time.
                if time.monotonic() >= total_deadline:
                    raise TimeoutError
                if self.provider.config.config_sha256 != config.config_sha256:
                    raise ProviderFailure('provider_error', 'config_changed')
                # Reservations and repairs may wait. Reverify bytes, expiry,
                # storage fault and immutable snapshot immediately before send.
                payload = evidence.guard_model_input(model_input.model_dump(mode='json'), images,
                    known_secrets=getattr(self.provider, 'sensitive_literals', ()))
                # Absolute deadline is shared across all attempts. Incoming
                # bytes, whitespace, and format repair never reset this clock.
                async with asyncio.timeout_at(total_deadline):
                    reply = await self.provider.complete(deepcopy(payload), deepcopy(schema),
                                                         images=images, repair_errors=deepcopy(errors))
                if not isinstance(reply, ProviderReply):
                    reply = None
                    raise ProviderFailure('provider_error', 'invalid_provider_reply')
                # Provider metadata is validated before any durable final row.
                try:
                    record()
                except ValueError:
                    reply = None
                    raise ProviderFailure('provider_error', 'invalid_provider_metadata') from None
                if self.provider.config.config_sha256 != config.config_sha256:
                    raise ProviderFailure('provider_error', 'config_changed')
                if time.monotonic() >= total_deadline:
                    raise TimeoutError
                if reply.invalid_reason:
                    raise InvalidModelOutput()
                output = validate_output_for_input(parse_model_output(reply.content), model_input)
                from ..evidence.redaction import TextRedactor
                if TextRedactor(tuple(getattr(self.provider, 'sensitive_literals', ()))).contains_sensitive(
                        canonical_json(output.model_dump(mode='json'))):
                    raise InvalidModelOutput()
                # A neutral full-image mask supplies no visual information and
                # therefore cannot support a coordinate action proposal.
                from .schema import CoordinateLocator
                if getattr(output, 'action', None) is not None and isinstance(output.action.target.locator, CoordinateLocator):
                    raise InvalidModelOutput()
                if time.monotonic() >= total_deadline:
                    raise TimeoutError
            except asyncio.CancelledError:
                try:
                    finish('CANCELLED', subtype='caller_cancelled')
                except (StorageBusyError, BusinessError):
                    # Preserve cancellation. A failed local finalization remains
                    # STARTED/unknown for reconciliation, never a retry signal.
                    pass
                raise
            except InvalidModelOutput as error:
                finish('INVALID', 'invalid_output', 'schema_validation')
                if number > limit:
                    raise ModelError('invalid_output', 'format_repairs_exhausted', call_id=call_id) from None
                errors = error.errors
                continue
            except BusinessError:
                finish('ERROR', 'provider_error', 'input_blocked')
                raise
            except (TimeoutError, ProviderFailure) as error:
                if isinstance(error, ProviderFailure):
                    reply = error.reply or reply
                    error_class = error.error_class if type(error.error_class) is str and error.error_class in (
                        'timeout', 'rate_limit', 'invalid_credentials', 'provider_error'
                    ) else 'provider_error'
                    allowed_subtypes = {
                        'model_timeout', 'transport_error', 'invalid_request', 'provider_authentication',
                        'insufficient_balance', 'model_service', 'provider_error', 'provider_unavailable',
                        'http_error', 'redirect_refused', 'response_too_large', 'malformed_response',
                        'invalid_provider_reply', 'invalid_provider_metadata',
                        'config_changed',
                    }
                    subtype = error.subtype if type(error.subtype) is str and error.subtype in allowed_subtypes else 'provider_error'
                    retry_after = error.retry_after_seconds
                    if (type(retry_after) not in (int, float) or not math.isfinite(retry_after)
                            or not 0 <= retry_after <= 86400 or error_class != 'rate_limit'):
                        retry_after = None
                    try:
                        if reply is not None and not isinstance(reply, ProviderReply):
                            raise ValueError('invalid reply')
                        record(error_class)
                    except (ValueError, TypeError):
                        reply = None
                        error_class, subtype = 'provider_error', 'invalid_provider_metadata'
                        retry_after = None
                else:
                    error_class, subtype, retry_after = 'timeout', 'generation_deadline', None
                finish('ERROR', error_class, subtype)
                raise ModelError(error_class, subtype, call_id=call_id, retry_after_seconds=retry_after) from None
            except Exception:
                # A replacement provider must not leak its raw exception (which
                # may include URLs, credentials or response bodies).
                reply = None
                finish('ERROR', 'provider_error', 'provider_exception')
                raise ModelError('provider_error', 'provider_exception', call_id=call_id) from None
            else:
                finish('VALID')
                return GenerationResult(output=output, call_id=call_id, records=records)
        raise AssertionError('bounded model loop cannot fall through')
