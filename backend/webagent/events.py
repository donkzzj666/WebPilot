"""Append-only business events and bounded committed replay reads."""
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

from .db import connect
from .db.repository import canonical_json, utc_text, writing
from .errors import BusinessError

Id = Annotated[str, Field(min_length=1, max_length=200)]


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ActionEvent(Payload):
    event_type: Literal['action_recorded'] = 'action_recorded'
    step_id: Id
    action_type: Literal['navigate', 'click', 'input', 'keypress', 'select', 'scroll',
                         'switch_tab', 'read_visible', 'screenshot', 'download_attachment']
    attempt_status: Literal['INTENT', 'COMPLETED', 'FAILED', 'UNKNOWN']
    evidence_ids: list[Id]


class WaitingEvent(Payload):
    event_type: Literal['wait_registered'] = 'wait_registered'
    wait_id: Id
    reason: Literal['ci', 'site', 'handoff', 'pause']
    deadline: AwareDatetime | None = None


class ResultEvent(Payload):
    event_type: Literal['result_ready'] = 'result_ready'
    result_ref: Id
    outcome: Literal['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED']


class OperationRequestedEvent(Payload):
    event_type: Literal['operation_requested'] = 'operation_requested'
    operation_id: Id
    action: Literal['start','retry','pause','resume','cancel']


class OperationCompletedEvent(Payload):
    event_type: Literal['operation_completed'] = 'operation_completed'
    operation_id: Id
    action: Literal['start','retry','pause','resume','cancel']
    status: Literal['APPLIED','REJECTED']
    result_ref: Id


def event_dict(row) -> dict:
    result = dict(row)
    result['payload'] = json.loads(result.pop('payload_json'))
    return result


def append_event(db, *, run_id: str, expected_state_version: int,
                 payload: ActionEvent | WaitingEvent | ResultEvent | OperationRequestedEvent | OperationCompletedEvent) -> dict:
    """Append structured, sanitized metadata inside caller's explicit transaction.

    State events are exclusively produced by the state transition trigger.
    Raw model/graph messages are not an accepted payload type.
    """
    writing(db)
    if type(expected_state_version) is not int or not 0 <= expected_state_version <= 2**63 - 1:
        raise BusinessError('INVALID_PARAMETER', 'Invalid expected version', field='expected_state_version')
    if type(payload) not in (ActionEvent, WaitingEvent, ResultEvent, OperationRequestedEvent, OperationCompletedEvent):
        raise BusinessError('INVALID_PARAMETER', 'Unsupported business event payload')
    # Revalidate mutable instances too; callers may have changed a list after construction.
    content = type(payload).model_validate(payload.model_dump()).model_dump(mode='json')
    if isinstance(payload, WaitingEvent) and payload.deadline is not None:
        content['deadline'] = utc_text(payload.deadline)
    row = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        raise BusinessError('NOT_FOUND', 'Run not found', status=404)
    if row['state_version'] != expected_state_version:
        raise BusinessError('STATE_CONFLICT', 'Run state has changed', status=409,
                            current_state_version=row['state_version'],
                            current_contract_version=row['contract_version'])
    if isinstance(payload, ResultEvent) and payload.outcome != row['state']:
        raise BusinessError('INVALID_PARAMETER', 'Result outcome must match terminal run state')
    cursor = db.execute('''INSERT INTO task_events(task_id,run_id,event_type,state_version,occurred_at,payload_json)
                           VALUES (?,?,?,?,?,?)''',
                        (row['task_id'], run_id, payload.event_type, expected_state_version,
                         utc_text(), canonical_json(content)))
    return event_dict(db.execute('SELECT * FROM task_events WHERE event_id=?', (cursor.lastrowid,)).fetchone())


def read_events(path: Path, *, after: int = 0, task_id: str | None = None,
                run_id: str | None = None, limit: int = 100) -> list[dict]:
    """One short WAL read; no connection/transaction survives a client yield."""
    if type(after) is not int or not 0 <= after <= 2**63 - 1:
        raise ValueError('Invalid event cursor')
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Page size must be between 1 and 1000')
    clauses, values = ['event_id>?'], [after]
    for column, value in (('task_id', task_id), ('run_id', run_id)):
        if value is not None:
            clauses.append(column + '=?')
            values.append(value)
    with connect(path) as db:
        return [event_dict(row) for row in db.execute(
            'SELECT * FROM task_events WHERE ' + ' AND '.join(clauses) + ' ORDER BY event_id LIMIT ?',
            (*values, limit))]


def validate_cursor(path: Path, cursor: str | None) -> int:
    if cursor is None or cursor == '':
        return 0
    if len(cursor) > 19 or not cursor.isascii() or not cursor.isdecimal() or int(cursor) > 2**63 - 1:
        raise BusinessError('INVALID_PARAMETER', 'Last-Event-ID must be a nonnegative decimal integer',
                            field='Last-Event-ID')
    value = int(cursor)
    with connect(path) as db:
        maximum = db.execute('SELECT COALESCE(MAX(event_id),0) FROM task_events').fetchone()[0]
    if value > maximum:
        raise BusinessError('INVALID_PARAMETER', 'Last-Event-ID is ahead of stored history',
                            field='Last-Event-ID')
    return value
