"""One bounded model-assisted draft extraction, with no database or execution.

The caller reserves and journals this attempt and its frozen configuration.
The returned object is only a proposal: ``natural.prepare`` and the task compiler
remain responsible for grounding it in user input and enforcing local policy.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import re
import time
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from ..db.repository import canonical_json
from ..errors import BusinessError
from ..evidence.redaction import filter_compilation_text, TextRedactor
from ..models.adapter import ModelError
from ..models.journal import ModelUsage
from ..models.transport import (
    COMPILER_PROMPT_VERSION, MAX_REQUEST_TEXT_BYTES, ModelConfig, ProviderFailure, ProviderReply,
)
from .models import CodeParameters, FinanceParameters, GrafanaParameters, MonitoringParameters, ResearchParameters
from .natural import InvalidNaturalProposal, NaturalProposal, extract_schema, parse_proposal, trusted_instruction


class CompilationProvider(Protocol):
    config: ModelConfig

    async def complete_compilation(self, payload: dict, schema: dict) -> ProviderReply: ...


class ExtractionError(ModelError):
    """Safe upstream failure with optional usage; raw model output is discarded."""

    def __init__(self, error_class: str, subtype: str, *, reply: ProviderReply | None = None,
                 retry_after_seconds: float | None = None):
        super().__init__(error_class, subtype, call_id=str(uuid4()), retry_after_seconds=retry_after_seconds)
        self.reply = reply


_PARAMETER_FIELDS = set().union(*(set(model.model_fields) for model in (
    FinanceParameters, CodeParameters, GrafanaParameters, MonitoringParameters, ResearchParameters,
)))
_SENSITIVE_KEYS = frozenset({"apikey", "password", "authorization", "accesstoken", "refreshtoken",
                           "token", "secret", "credential", "credentials", "credentialref", "identityref"})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_PROVIDER_ERROR_CLASSES = frozenset({"timeout", "rate_limit", "invalid_credentials", "provider_error"})
_PROVIDER_SUBTYPES = frozenset({"model_timeout", "transport_error", "invalid_request", "provider_authentication",
    "insufficient_balance", "model_service", "provider_error", "provider_unavailable", "http_error",
    "redirect_refused", "response_too_large", "malformed_response", "invalid_provider_reply", "invalid_provider_metadata"})


def _without_sensitive_fields(value):
    if isinstance(value, dict):
        return {key: _without_sensitive_fields(child) for key, child in value.items()
                if isinstance(key, str) and re.sub(r"[^a-z]", "", key.lower()) not in _SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_without_sensitive_fields(child) for child in value]
    return value


def _payload(content: dict) -> dict:
    """Send only compiler inputs, never whole API requests/settings/credentials."""
    try:
        if type(content) is not dict or not isinstance(content.get("instruction"), str) or not content["instruction"].strip():
            raise ValueError("invalid instruction")
        parameters = content.get("parameters", {})
        if type(parameters) is not dict:
            raise ValueError("invalid parameters")
        scenario = content.get("scenario")
        if scenario is not None and scenario not in ("finance", "operations", "research", "monitoring"):
            raise ValueError("invalid scenario")
        payload = _without_sensitive_fields({
            "instruction": trusted_instruction(content["instruction"]),
            "explicit_scenario": scenario,
            "explicit_parameters": {key: value for key, value in parameters.items() if key in _PARAMETER_FIELDS},
            "web_context": content.get("web_context"),
        })
        serialized = canonical_json(payload)
        if len(serialized.encode("utf-8")) > MAX_REQUEST_TEXT_BYTES:
            raise ValueError("input too large")
        return deepcopy(payload)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise BusinessError("INVALID_PARAMETER", "任务编译输入无效。", field="compilation") from None


def _metadata(reply: ProviderReply | None, known_secrets=()) -> ProviderReply | None:
    """Validate/retain an owned copy of approved metadata; never retain content."""
    if reply is None:
        return None
    if not isinstance(reply, ProviderReply):
        raise ValueError("invalid provider reply")
    if reply.invalid_reason is not None and not isinstance(reply.invalid_reason, str):
        raise ValueError("invalid provider reply")
    provider_id = reply.provider_request_id
    if provider_id is not None and (not isinstance(provider_id, str) or not _SAFE_TOKEN.fullmatch(provider_id)):
        raise ValueError("invalid provider metadata")
    usage = ModelUsage.model_validate(deepcopy(reply.usage))
    if TextRedactor(tuple(known_secrets)).contains_sensitive(canonical_json(
            {'provider_request_id': provider_id, 'usage': usage.model_dump(mode='json')})):
        raise ValueError('invalid provider metadata')
    return ProviderReply("", provider_id, usage.model_dump(mode="json"))


async def extract(provider: CompilationProvider, content: dict) -> tuple[NaturalProposal, ProviderReply]:
    """One provider attempt, preserving cancellation and a single total deadline."""
    payload = filter_compilation_text(_payload(content),
        known_secrets=getattr(provider, 'sensitive_literals', ()))
    try:
        config = ModelConfig.model_validate(provider.config.model_dump(mode="json"))
        config_hash = config.config_sha256
    except Exception:
        raise BusinessError("SERVICE_UNAVAILABLE", "模型编译配置无效。", status=503, field="model_settings") from None
    deadline = time.monotonic() + min(60.0, config.total_seconds)
    schema = extract_schema()
    reply = None
    metadata = None
    try:
        if len(canonical_json(payload).encode()) + len(canonical_json(schema).encode()) > MAX_REQUEST_TEXT_BYTES:
            raise BusinessError("INVALID_PARAMETER", "任务编译输入过大。", field="compilation")
        try:
            async with asyncio.timeout_at(deadline):
                reply = await provider.complete_compilation(payload, schema)
        except (asyncio.CancelledError, TimeoutError, ProviderFailure):
            raise
        except Exception:
            raise ExtractionError("provider_error", "compiler_provider_exception") from None
        metadata = _metadata(reply, getattr(provider, 'sensitive_literals', ()))
        if metadata is None:
            raise ValueError("invalid provider reply")
        try:
            current_hash = ModelConfig.model_validate(provider.config.model_dump(mode="json")).config_sha256
        except Exception:
            raise ExtractionError("provider_error", "compiler_config_changed", reply=metadata) from None
        if current_hash != config_hash:
            raise ExtractionError("provider_error", "compiler_config_changed", reply=metadata)
        if time.monotonic() >= deadline:
            raise TimeoutError
        if reply.invalid_reason is not None:
            raise InvalidNaturalProposal()
        proposal = parse_proposal(reply.content)
        if time.monotonic() >= deadline:
            raise TimeoutError
        # Parsed DTO replaces generated content, and usage is separately owned.
        # No parent needs the raw response to audit or publish the draft.
        return proposal, metadata
    except asyncio.CancelledError:
        raise
    except ExtractionError:
        raise
    except InvalidNaturalProposal:
        raise ExtractionError("invalid_output", "invalid_compiler_output", reply=metadata) from None
    except (TimeoutError, ProviderFailure) as error:
        if isinstance(error, ProviderFailure):
            error_class = error.error_class if type(error.error_class) is str and error.error_class in _PROVIDER_ERROR_CLASSES else "provider_error"
            subtype = error.subtype if type(error.subtype) is str and error.subtype in _PROVIDER_SUBTYPES else "provider_error"
            retry_after = error.retry_after_seconds
            if (type(retry_after) not in (int, float) or not math.isfinite(retry_after)
                    or not 0 <= retry_after <= 86400 or error_class != "rate_limit"):
                retry_after = None
            try:
                metadata = _metadata(error.reply, getattr(provider, 'sensitive_literals', ())) or metadata
            except (ValueError, TypeError):
                metadata = None
                error_class, subtype, retry_after = "provider_error", "invalid_provider_metadata", None
        else:
            error_class, subtype, retry_after = "timeout", "compiler_deadline", None
        raise ExtractionError(error_class, subtype, reply=metadata, retry_after_seconds=retry_after) from None
    except BusinessError:
        raise
    except (ValueError, TypeError, ValidationError):
        raise ExtractionError("provider_error", "invalid_provider_metadata") from None
    except Exception:
        raise ExtractionError("provider_error", "compiler_provider_exception") from None
