"""Short SQLite transactions for model attempts and their original Run budget.

Network I/O never holds a database lock. A STARTED row left after process death
means outcome/usage unknown; it must not cause an automatic provider retry.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_serializer, model_validator

from ..db.connection import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..tasks.models import Hash, Id, StrictModel
from .schema import ModelInput, Nonnegative
from .transport import ModelConfig

ErrorClass = Literal['timeout', 'rate_limit', 'invalid_credentials', 'invalid_output', 'provider_error']
UsageKey = Literal['prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'total_tokens',
                   'response_model', 'system_fingerprint', 'finish_reason', 'attempt_number']
SafeMetadata = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.:/-]{1,200}$')]


class ModelUsage(StrictModel):
    input_tokens: Nonnegative | None = None
    output_tokens: Nonnegative | None = None
    image_units: Nonnegative | None = None
    provider_usage: dict[UsageKey, Nonnegative | SafeMetadata | None] = Field(default_factory=dict)


class ModelCallRecord(StrictModel):
    run_id: Id
    request_id: Id
    provider_request_id: SafeMetadata | None
    provider: Literal['deepseek']
    model_id: SafeMetadata
    config_sha256: Hash
    prompt_version: SafeMetadata
    usage: ModelUsage
    duration_ms: Nonnegative
    format_repairs: Annotated[int, Field(ge=0, le=2)]
    error_class: ErrorClass | None
    price_version: SafeMetadata | None = None
    estimated_cost: Annotated[str, Field(pattern=r'^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$')] | None = None
    cost_currency: Literal['USD', 'CNY'] | None = None

    @model_serializer(mode='wrap')
    def preserve_legacy_record(self, handler):
        value = handler(self)
        if self.cost_currency is None:
            value.pop('cost_currency', None)
        return value

    @model_validator(mode='after')
    def priced_cost(self):
        if self.estimated_cost is not None and self.price_version is None:
            raise ValueError('cost requires a versioned price')
        if self.cost_currency is not None and self.estimated_cost is None:
            raise ValueError('currency requires a known estimate')
        return self


def _conflict(message: str) -> BusinessError:
    return BusinessError('STATE_CONFLICT', message, status=409)


def _context(db, model_input: ModelInput, config: ModelConfig):
    run = db.execute('SELECT * FROM runs WHERE run_id=?', (model_input.run_id,)).fetchone()
    if run is None:
        raise BusinessError('NOT_FOUND', 'Run not found', status=404)
    if run['state'] not in ('RUNNING', 'VERIFYING', 'RECONCILING'):
        raise _conflict('Run is not eligible for model proposals')
    digest = hashlib.sha256(canonical_json(model_input.contract.model_dump(mode='json')).encode()).hexdigest()
    if ((run['task_id'], run['contract_version'], run['contract_sha256']) != (
            model_input.contract.task_id, model_input.contract.contract_version, digest)
            or run['model_config_sha256'] != config.config_sha256):
        raise _conflict('Model input differs from frozen Run bindings')
    budget = db.execute('SELECT * FROM run_budgets WHERE run_id=?', (model_input.run_id,)).fetchone()
    if budget is None or budget['budget_record_id'] != model_input.verified_checkpoint.budget_record_ref:
        raise _conflict('Model input must reference the original Run budget')
    return run, budget


def _active_ms(budget, now: datetime) -> int:
    # A running interval may already include model time. Flush it once instead
    # of adding both the interval and provider duration to the same counter.
    if budget['active_interval_started_at'] is None:
        return 0
    since = datetime.fromisoformat(budget['active_interval_started_at'].replace('Z', '+00:00'))
    return max(0, int((now - since).total_seconds() * 1000))


def _unified_budget(path, db, run_id, execution_token=None, *, require_qualification=True):
    scheduled = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_queue'").fetchone()
    if not scheduled or db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (run_id,)).fetchone() is None:
        return None
    from ..budgets.store import BudgetStore
    from ..scheduler.store import validate_in_transaction
    from ..scheduler.models import ExecutionToken
    if require_qualification:
        if not isinstance(execution_token, ExecutionToken) or execution_token.run_id != run_id:
            raise _conflict('Scheduled model calls require current execution qualification')
        validate_in_transaction(db, execution_token, allow_reconciling=True)
    status = BudgetStore(path).flush_in_transaction(db, run_id)
    if status is None:
        raise _conflict('Scheduled Run has no unified budget')
    if require_qualification and status['exhausted']:
        raise BusinessError('BUDGET_EXCEEDED', 'Run budget exhausted', status=409, field=status['reason'])
    return status


def remaining_seconds(path: Path, model_input: ModelInput, config: ModelConfig, *, execution_token=None) -> float:
    with connect(path, busy_timeout_ms=100) as db, transaction(db):
        _, budget = _context(db, model_input, config)
        status = _unified_budget(path, db, model_input.run_id, execution_token)
        if status is not None:
            return status['remaining_active_ms'] / 1000
        used = budget['active_ms'] + _active_ms(budget, datetime.now(timezone.utc))
        return max(0, model_input.contract.budget_profile.max_active_seconds - used / 1000)


def reserve_attempt(path: Path, model_input: ModelInput, config: ModelConfig, *,
                    call_id: str, request_id: str, attempt_number: int, execution_token=None,
                    prompt_version: str | None = None) -> None:
    # A verifier uses the original Run configuration and budget while recording
    # its own protocol version. Never rewrite the frozen model configuration.
    protocol = prompt_version if prompt_version is not None else config.prompt_version
    TypeAdapter(SafeMetadata).validate_python(protocol, strict=True)
    with connect(path, busy_timeout_ms=100) as db, transaction(db):
        run, budget = _context(db, model_input, config)
        from ..controls.models import ControlPending
        from ..controls.store import ControlStore
        pending = ControlStore.pending_in_transaction(db, run['run_id'])
        if pending is not None:
            raise ControlPending(pending)
        unified = _unified_budget(path, db, model_input.run_id, execution_token)
        now = datetime.now(timezone.utc)
        elapsed = _active_ms(budget, now) if unified is None else 0
        if unified is None and budget['active_ms'] + elapsed >= model_input.contract.budget_profile.max_active_seconds * 1000:
            raise BusinessError('BUDGET_EXCEEDED', 'Run active time budget exhausted', status=409)
        if db.execute("SELECT 1 FROM model_attempts WHERE run_id=? AND status='STARTED'", (run['run_id'],)).fetchone():
            raise _conflict('Run already has an unfinished model attempt')
        generation = db.execute('SELECT * FROM model_generations WHERE call_id=?', (call_id,)).fetchone()
        if generation is None:
            if attempt_number != 1:
                raise _conflict('Missing model generation')
            db.execute('INSERT INTO model_generations VALUES (?,?,?,?,?,?,?)',
                       (call_id, run['run_id'], config.config_sha256, protocol,
                        model_input.contract.budget_profile.max_model_format_repairs,
                        run['state_version'], utc_text(now)))
        elif (generation['run_id'] != run['run_id'] or generation['run_state_version'] != run['state_version']
              or generation['prompt_version'] != protocol or generation['config_sha256'] != config.config_sha256):
            raise _conflict('Run changed during model generation')
        db.execute('''INSERT INTO model_attempts(request_id,call_id,run_id,attempt_number,started_at)
                      VALUES (?,?,?,?,?)''', (request_id, call_id, run['run_id'], attempt_number, utc_text(now)))
        db.execute('''UPDATE run_budgets SET model_calls_used=model_calls_used+1,
                      active_ms=active_ms+?,active_interval_started_at=?,
                      last_persisted_at=?,state_version=state_version+1 WHERE run_id=?''',
                   (elapsed, utc_text(now) if unified is None and budget['active_interval_started_at'] else None,
                    utc_text(now), run['run_id']))


def finish_attempt(path: Path, record: ModelCallRecord, *, status: str,
                   diagnostic_subtype: str | None = None, execution_token=None) -> bool:
    """Return whether the Run still matches; discard a stale candidate atomically."""
    with connect(path, busy_timeout_ms=100) as db, transaction(db):
        attempt = db.execute('''SELECT a.*,g.run_state_version FROM model_attempts a
            JOIN model_generations g USING(call_id,run_id) WHERE request_id=?''', (record.request_id,)).fetchone()
        if attempt is None or attempt['status'] != 'STARTED':
            raise _conflict('Model attempt is missing or already finalized')
        run = db.execute('SELECT state_version FROM runs WHERE run_id=?', (record.run_id,)).fetchone()
        current = run['state_version'] == attempt['run_state_version']
        scheduled = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_queue'").fetchone()
        if scheduled and db.execute('SELECT 1 FROM scheduler_queue WHERE run_id=?', (record.run_id,)).fetchone():
            from ..scheduler.models import ExecutionToken
            from ..scheduler.store import validate_in_transaction
            if not isinstance(execution_token, ExecutionToken) or execution_token.run_id != record.run_id:
                current = False
            else:
                try:
                    validate_in_transaction(db, execution_token, allow_reconciling=True)
                except BusinessError:
                    current = False
        unified = _unified_budget(path, db, record.run_id, require_qualification=False)
        budget_expired = current and unified is not None and unified['exhausted']
        if budget_expired:
            current = False
        if not current:
            status, diagnostic_subtype = 'CANCELLED', 'budget_exceeded' if budget_expired else 'context_changed'
            record = record.model_copy(update={'error_class': None})
        now = datetime.now(timezone.utc)
        db.execute('''UPDATE model_attempts SET finished_at=?,status=?,record_json=?,diagnostic_subtype=?
                      WHERE request_id=?''',
                   (max(utc_text(now), attempt['started_at']), status,
                    canonical_json(record.model_dump(mode='json')), diagnostic_subtype, record.request_id))
        budget = db.execute('SELECT * FROM run_budgets WHERE run_id=?', (record.run_id,)).fetchone()
        elapsed = 0 if unified is not None else _active_ms(budget, now) if budget['active_interval_started_at'] else record.duration_ms
        db.execute('''UPDATE run_budgets SET active_ms=active_ms+?,active_interval_started_at=?,
                      last_persisted_at=?,state_version=state_version+1 WHERE run_id=?''',
                   (elapsed, utc_text(now) if unified is None and budget['active_interval_started_at'] else None,
                    utc_text(now), record.run_id))
        return current


def list_attempts(path: Path, run_id: str) -> list[dict]:
    with connect(path) as db:
        result = []
        for row in db.execute('SELECT * FROM model_attempts WHERE run_id=? ORDER BY started_at,request_id', (run_id,)):
            value = dict(row)
            raw = value.pop('record_json')
            value['record'] = json.loads(raw) if raw is not None else None
            result.append(value)
        return result
