"""HTTP correlation and content-free standard library diagnostics."""
import asyncio
import json
import logging
import time
from uuid import UUID

from api_support import AuthenticatedTestClient, create_test_app, TEST_TOKEN
from webagent.config import Settings
from webagent.observability.logging import SafeJSONLLogger
from webagent.observability.standard import SafeLibraryHandler

CANARY = 'OBSERVABILITY_PRIVATE_PAYLOAD_DO_NOT_LOG_611'


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_every_response_has_a_generated_correlated_id_without_request_content(tmp_path):
    app = create_test_app(Settings(tmp_path))
    with AuthenticatedTestClient(app) as client:
        good = client.get('/health', headers={'X-Request-ID': CANARY})
        denied = client.get('/health', headers={'Authorization': 'Bearer ' + CANARY})
        invalid = client.post('/v1/tasks', json={'instruction': CANARY}, headers={'X-Request-ID': CANARY})
        assert good.status_code == 200 and denied.status_code == 401 and invalid.status_code == 422
        identifiers = [r.headers['x-request-id'] for r in (good, denied, invalid)]
        assert len(set(identifiers)) == 3 and all(UUID(value).version == 4 for value in identifiers)
        assert denied.json()['request_id'] == identifiers[1]
        assert invalid.json()['request_id'] == identifiers[2]
    data = (tmp_path/'logs/api.jsonl').read_text()
    assert CANARY not in data and TEST_TOKEN not in data and 'instruction' not in data
    requests = [r for r in records(tmp_path/'logs/api.jsonl') if r['event'] == 'api_request']
    assert [r['request_id'] for r in requests] == identifiers
    assert [r['http_status'] for r in requests] == [200, 401, 422]


def test_unexpected_error_is_publicly_safe_and_logging_failure_does_not_override_response(tmp_path, monkeypatch):
    app = create_test_app(Settings(tmp_path))
    @app.get('/owned-error')
    async def owned_error():
        raise RuntimeError(CANARY)
    with AuthenticatedTestClient(app, raise_server_exceptions=False) as client:
        response = client.get('/owned-error')
        assert response.status_code == 500 and CANARY not in response.text
        assert response.headers['x-request-id'] == response.json()['request_id']
        assert response.headers['x-transport-request-id'] == response.json()['request_id']
        monkeypatch.setattr(app.state.diagnostic_logger, 'emit', lambda *args, **kwargs: False)
        assert client.get('/health').status_code == 200
        def unavailable(*args, **kwargs):
            raise OSError(CANARY)
        monkeypatch.setattr(app.state.diagnostic_logger, 'emit', unavailable)
        assert client.get('/health').status_code == 200
        assert client.post('/v1/tasks', json={}).status_code == 422
        monkeypatch.setattr(app.state.diagnostic_logger, 'emit', lambda *args, **kwargs: False)
    assert CANARY not in (tmp_path/'logs/api.jsonl').read_text()


def test_idempotent_business_request_id_is_preserved_with_unique_transport_attempts(tmp_path):
    from storage.test_task_api import FINANCE
    app = create_test_app(Settings(tmp_path))
    with AuthenticatedTestClient(app) as client:
        first = client.post('/v1/tasks', json=FINANCE, headers={'Idempotency-Key': 'owned-id'})
        replay = client.post('/v1/tasks', json=FINANCE, headers={'Idempotency-Key': 'owned-id'})
        assert first.status_code == replay.status_code == 201
        assert first.content == replay.content
        assert first.headers['x-request-id'] == replay.headers['x-request-id'] == first.json()['request_id']
        assert first.headers['x-transport-request-id'] != replay.headers['x-transport-request-id']
    events = [r for r in records(tmp_path/'logs/api.jsonl') if r['event'] == 'api_request']
    assert len(events) == 2
    assert all(r['request_id'] == first.json()['request_id'] for r in events)
    assert [r['transport_request_id'] for r in events] == [first.headers['x-transport-request-id'],
                                                        replay.headers['x-transport-request-id']]
    assert all(r['task_id'] == first.json()['task']['task_id'] for r in events)


def test_api_health_detects_missing_storage_without_creating_it(tmp_path):
    app = create_test_app(Settings(tmp_path))
    with AuthenticatedTestClient(app) as client:
        assert client.get('/health').json()['tasks_success_implied'] is False
        # Only this test owns the file; the health reader must not recreate it.
        (tmp_path/'business.sqlite3').unlink()
        response = client.get('/health')
        assert response.status_code == 503 and not (tmp_path/'business.sqlite3').exists()


def test_standard_library_handler_never_formats_message_arguments_or_traceback(tmp_path):
    class Unformattable:
        def __str__(self):
            raise AssertionError('raw content was inspected')
        def __repr__(self):
            raise AssertionError('raw content was inspected')
    logger = SafeJSONLLogger(tmp_path/'logs/worker.jsonl')
    handler = SafeLibraryHandler(logger, 'worker')
    try:
        error = RuntimeError(CANARY)
        record = logging.LogRecord('uvicorn.error', logging.ERROR, 'owned-file', 1,
            Unformattable(), (Unformattable(),), (RuntimeError, error, None))
        handler.emit(record)
        deadline = time.monotonic() + 2
        while not logger.path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        value = records(logger.path)
        assert value[0]['event'] == 'service_failed'
        assert CANARY not in logger.path.read_text()
    finally:
        handler.close()


def test_library_queue_is_bounded_and_does_not_block_the_caller(tmp_path):
    import threading
    gate = threading.Event()
    class SlowSink:
        def emit(self, *args, **kwargs):
            gate.wait(2)
    handler = SafeLibraryHandler(SlowSink(), 'worker')
    record = logging.LogRecord('asyncio', logging.ERROR, '', 1, CANARY, (), None)
    try:
        started = time.monotonic()
        for _ in range(512):
            handler.emit(record)
        assert time.monotonic() - started < .5
        assert handler.records.qsize() <= 128 and handler.dropped > 0
    finally:
        gate.set()
        handler.close()
