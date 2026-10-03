"""M1-21 acceptance helpers preserve public scope and actual stream causality."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'verification'))
probe = importlib.import_module('verify_workbench')


def event(event_id=12):
    return {'event_id': event_id, 'task_id': 'task-owned', 'run_id': 'run-owned',
            'event_type': 'state_changed', 'state_version': 7,
            'occurred_at': '2026-10-02T00:00:00.000000Z',
            'payload': {'event_type': 'state_changed', 'state': 'PAUSED', 'blocked_reason': None}}


def test_stream_attachment_exports_only_public_bindings_and_exact_cursor():
    scope = {'query_string': b'task_id=task-owned&run_id=run-owned&token=PRIVATE',
             'headers': [(b'authorization', b'Bearer PRIVATE'), (b'last-event-id', b'9007199254740993')]}
    assert probe.stream_attachment(scope) == {'task_id': 'task-owned', 'run_id': 'run-owned',
                                             'last_event_id': '9007199254740993'}
    assert 'PRIVATE' not in json.dumps(probe.stream_attachment(scope))


@pytest.mark.parametrize('cursor', [b'PRIVATE', b'-1', b'1.5', b'9' * 20])
def test_stream_attachment_rejects_non_decimal_or_unbounded_cursor(cursor):
    assert probe.stream_attachment({'headers': [(b'last-event-id', cursor)]})['last_event_id'] is None


def test_duplicate_delivery_is_exact_original_frame_and_does_not_mutate_row():
    original = event()
    before = deepcopy(original)
    assert probe.delivery_frames(original, 'duplicate') == (probe.encode_event(original) * 2).encode()
    assert original == before


def test_conflict_delivery_changes_only_metadata_and_retains_bound_business_payload():
    original = event()
    result = probe.delivery_frames(original, 'conflict').decode()
    payload = json.loads(result.split('data: ', 1)[1].strip())
    assert payload == {**original, 'state_version': 8}
    assert original['state_version'] == 7


def test_out_of_order_delivery_requires_lower_unknown_positive_metadata_id():
    original = event()
    payload = json.loads(probe.delivery_frames(original, 'out_of_order', lower_id=5)
                         .decode().split('data: ', 1)[1].strip())
    assert payload == {**original, 'event_id': 5}
    assert original['event_id'] == 12
    for invalid in (None, 0, -1, 12, 13, True, '5'):
        with pytest.raises(ValueError):
            probe.delivery_frames(original, 'out_of_order', lower_id=invalid)
    with pytest.raises(ValueError):
        probe.delivery_frames(original, 'unknown')


def test_transport_delegates_non_sse_without_exporting_headers():
    async def exercise():
        scopes = []
        async def app(scope, receive, send):
            scopes.append(scope)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        transport = probe.StreamTransport(app)
        messages = []
        async def send(message): messages.append(message)
        async def receive(): return {'type': 'http.request'}
        scope = {'type': 'http', 'path': '/v1/tasks/owned/workspace',
                 'headers': [(b'authorization', b'PRIVATE')]}
        await transport(scope, receive, send)
        assert scopes == [scope] and len(messages) == 2
        assert not transport.attachments and not transport.active and transport.disconnections == 0
    asyncio.run(exercise())


def test_owned_disconnect_ends_actual_stream_and_leaves_no_pending_tasks():
    async def exercise():
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'retry: 1000\n\n', 'more_body': True})
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        transport = probe.StreamTransport(app)
        transport.prefix = b'owned-injected-delivery\n\n'
        messages = []
        async def send(message): messages.append(message)
        async def receive(): return {'type': 'http.request'}
        stream = asyncio.create_task(transport({'type': 'http', 'path': '/v1/events',
            'query_string': b'task_id=owned&run_id=run-owned', 'headers': [(b'last-event-id', b'12')]}, receive, send))
        await asyncio.wait_for(started.wait(), 1)
        assert transport.disconnect() == 1
        await asyncio.wait_for(stream, 1)
        assert cancelled.is_set() and not transport.active and not transport.prefix
        assert messages[1] == {'type': 'http.response.body', 'body': b'owned-injected-delivery\n\n', 'more_body': True}
        assert messages[-1] == {'type': 'http.response.body', 'body': b'', 'more_body': False}
        assert transport.disconnections == 1
        assert transport.attachments[-1]['last_event_id'] == '12'
    asyncio.run(exercise())


def test_transport_application_failure_is_propagated_and_active_stream_is_removed():
    async def exercise():
        async def app(scope, receive, send): raise RuntimeError('PRIVATE')
        async def send(message): pass
        async def receive(): return {'type': 'http.request'}
        transport = probe.StreamTransport(app)
        with pytest.raises(RuntimeError):
            await transport({'type': 'http', 'path': '/v1/events'}, receive, send)
        assert not transport.active
    asyncio.run(exercise())


def test_other_task_event_fixture_cannot_poison_ui_start_targets(tmp_path):
    from webagent.controls.models import ControlRequest
    origin = 'http://127.0.0.1:9'
    directory = tmp_path / 'data'
    primary = probe.seed_task(directory, origin, 'primary')
    target = probe.additional_ready_task(directory / 'business.sqlite3', primary, origin, 'cancel')
    gap = probe.additional_ready_task(directory / 'business.sqlite3', primary, origin, 'filter-gap')
    probe.seed_filter_gap(directory / 'business.sqlite3', gap)
    with probe.connect(directory / 'business.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM runs WHERE task_id=?', (target,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM scheduler_queue').fetchone()[0] == 0
        event = db.execute('SELECT task_id FROM task_events').fetchone()
        assert event['task_id'] == gap
    controls = probe.ControlStore(directory / 'business.sqlite3', secret_store=probe.OwnedSecrets(),
        resource_factory=lambda contract, run_id: probe.owned_resources(origin, contract, run_id))
    receipt = controls.request(target, 'start',
        ControlRequest(**probe.command_body(directory, task_id=target)), 'owned-new-target-start')
    assert receipt['operation']['status'] == 'PENDING'
    assert receipt['operation']['task_id'] == target
