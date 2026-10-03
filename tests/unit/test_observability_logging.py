"""Content-free records, bounded retention and real process file coordination."""
import fcntl
import errno
import json
import multiprocessing
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from webagent.errors import BusinessError
from webagent.db.connection import StorageBusyError
from webagent.models.transport import ProviderFailure
from webagent.observability.logging import SafeJSONLLogger, safe_error_class, safe_error_code


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _writer(path, index, result):
    logger = SafeJSONLLogger(path, lock_timeout=1)
    result.put(all(logger.emit('worker_ready', service='worker', pid=os.getpid(),
                              worker_generation=1, duration_ms=index * 100 + i) for i in range(50)))


def test_valid_program_record_has_fixed_schema_and_private_permissions(tmp_path):
    path = tmp_path / 'logs' / 'api.jsonl'
    logger = SafeJSONLLogger(path)
    assert logger.last_write_succeeded is None
    assert logger.emit('api_request', service='api', request_id=str(uuid4()), transport_request_id=str(uuid4()),
                       method='GET', http_status=200, duration_ms=5, pid=os.getpid())
    row = records(path)[0]
    assert row['schema_version'] == 1 and row['event'] == 'api_request'
    assert row['http_status'] == 200 and row['timestamp_utc']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name('api.jsonl.lock').stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert logger.last_write_succeeded and logger.healthy


@pytest.mark.parametrize('field', ['prompt', 'response', 'url', 'cookie', 'token', 'api_key',
                                  'metadata', 'exception', 'traceback', 'headers', 'body'])
def test_unknown_content_fields_are_rejected_without_creating_a_file(tmp_path, field):
    logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
    assert not logger.emit('worker_ready', **{field: 'PRIVATE_CONTENT_CANARY'})
    assert logger.dropped == 1 and not logger.path.exists()
    assert logger.last_write_succeeded is None


@pytest.mark.parametrize('fields', [
    {'request_id': 'UNTRUSTED_HEADER_CANARY'}, {'transport_request_id': 'UNTRUSTED_HEADER_CANARY'},
    {'node': 'unknown-node'},
    {'error_code': 'PROVIDER_RESPONSE_CANARY'}, {'error_class': 'ArbitraryExceptionName'},
    {'service': 'unsafe-service'}, {'http_status': True}, {'http_status': 999},
    {'duration_ms': float('nan')}, {'duration_ms': True}, {'duration_ms': -1},
    {'run_id': 'https://private.example/token'}, {'run_id': 'run-1\nSECRET'},
    {'graph_version': 'spoofed'}, {'state_schema_version': 'spoofed'},
])
def test_malformed_identifiers_enums_and_numbers_are_dropped(tmp_path, fields):
    logger = SafeJSONLLogger(tmp_path / 'graph.jsonl')
    assert not logger.emit('graph_node_started', **fields)
    assert not logger.path.exists()


@pytest.mark.parametrize('identifier', ['sk-proj-PRIVATE_IDENTIFIER_CANARY',
    'ghp_PRIVATE_IDENTIFIER_CANARY', 'token:PRIVATE_IDENTIFIER_CANARY'])
def test_committed_operation_identifiers_cannot_smuggle_recognized_credentials(tmp_path, identifier):
    logger = SafeJSONLLogger(tmp_path / 'graph.jsonl')
    assert not logger.emit('graph_node_completed', node='confirm', service='graph',
                           run_id='run-1', operation_id=identifier)
    assert not logger.path.exists()


def test_errors_are_classified_without_formatting_the_exception(tmp_path):
    class PrivateFailure(BusinessError):
        def __str__(self):
            raise AssertionError('Raw exception formatting was attempted')
    error = PrivateFailure('SERVICE_UNAVAILABLE', 'API_KEY_COOKIE_PROVIDER_CANARY')
    logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
    assert logger.emit('service_failed', error_code=safe_error_code(error),
                       error_class=safe_error_class(error), service='worker')
    assert records(logger.path)[0]['error_code'] == 'SERVICE_UNAVAILABLE'
    assert 'CANARY' not in logger.path.read_text()


@pytest.mark.parametrize('error,code,category', [
    (StorageBusyError('PRIVATE_CANARY'), 'STORAGE_UNAVAILABLE', 'storage'),
    (TimeoutError('PRIVATE_CANARY'), 'TIMEOUT', 'timeout'),
    (ProviderFailure('provider_error', 'PRIVATE_CANARY'), 'UPSTREAM_ERROR', 'provider'),
    (ProviderFailure('timeout', 'PRIVATE_CANARY'), 'TIMEOUT', 'timeout'),
    (ProviderFailure('PRIVATE_CANARY', 'PRIVATE_CANARY'), 'INTERNAL_ERROR', 'internal'),
    (OSError(errno.ENOSPC, 'PRIVATE_CANARY'), 'STORAGE_UNAVAILABLE', 'storage'),
    (OSError(errno.ECONNRESET, 'PRIVATE_CANARY'), 'INTERNAL_ERROR', 'internal'),
])
def test_error_classes_follow_known_types_and_enums_without_copying_messages(error, code, category):
    assert safe_error_code(error) == code and safe_error_class(error) == category


def test_pydantic_error_does_not_expose_the_rejected_input(tmp_path):
    class Input(BaseModel):
        count: int
    with pytest.raises(ValidationError) as failure:
        Input(count='PRIVATE_REJECTED_INPUT_CANARY')
    logger = SafeJSONLLogger(tmp_path / 'api.jsonl')
    assert logger.emit('api_error', error_code=safe_error_code(failure.value),
                       error_class=safe_error_class(failure.value), service='api')
    assert records(logger.path)[0]['error_class'] == 'validation'
    assert 'CANARY' not in logger.path.read_text()


def test_real_process_writers_produce_complete_unique_json_lines(tmp_path):
    context = multiprocessing.get_context('spawn')
    result = context.Queue()
    path = tmp_path / 'worker.jsonl'
    workers = [context.Process(target=_writer, args=(path, i, result)) for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert all(result.get(timeout=2) for _ in workers)
    rows = records(path)
    assert len(rows) == 200 and len({row['duration_ms'] for row in rows}) == 200
    assert path.read_bytes().endswith(b'\n')


def test_initial_creation_lookup_race_retries_without_removing_nofollow(tmp_path, monkeypatch):
    original, lock_calls = os.open, 0
    def open_file(path, flags, *args, **kwargs):
        nonlocal lock_calls
        if path == 'worker.jsonl.lock':
            lock_calls += 1
            assert flags & os.O_NOFOLLOW and kwargs.get('dir_fd') is not None
            if lock_calls == 1:
                raise FileNotFoundError(errno.ENOENT, 'Synthetic concurrent creation')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', open_file)
    logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
    assert logger.emit('worker_ready', service='worker')
    assert lock_calls == 2 and len(records(logger.path)) == 1


def test_rotation_has_a_fixed_storage_bound_and_keeps_parseable_private_files(tmp_path):
    logger = SafeJSONLLogger(tmp_path / 'graph.jsonl', max_bytes=4096, backups=2)
    for i in range(150):
        assert logger.emit('graph_node_completed', node='observe', run_id='run-' + 'a' * 150,
                           duration_ms=i, service='graph')
    files = [p for p in tmp_path.glob('graph.jsonl*') if p.suffix != '.lock']
    assert {p.name for p in files} == {'graph.jsonl', 'graph.jsonl.1', 'graph.jsonl.2'}
    assert sum(p.stat().st_size for p in files) <= 3 * 4096
    assert all(p.stat().st_size <= 4096 and stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    assert all(records(p) for p in files)
    assert records(logger.path)[-1]['duration_ms'] == 149


@pytest.mark.parametrize('target', ['file', 'backup', 'directory', 'lock', 'hardlink'])
def test_unsafe_paths_cannot_overwrite_another_file(tmp_path, target):
    victim = tmp_path / 'private-existing'
    victim.write_text('PRIVATE_EXISTING_CANARY')
    folder = tmp_path / 'logs'
    folder.mkdir(mode=0o700)
    path = folder / 'worker.jsonl'
    if target == 'directory':
        actual = tmp_path / 'actual'
        actual.mkdir()
        folder.rmdir()
        folder.symlink_to(actual, target_is_directory=True)
    elif target == 'hardlink':
        os.link(victim, path)
    else:
        name = path if target == 'file' else path.with_name(path.name + ('.1' if target == 'backup' else '.lock'))
        name.symlink_to(victim)
    logger = SafeJSONLLogger(path)
    assert not logger.emit('worker_ready', service='worker', pid=os.getpid())
    assert victim.read_text() == 'PRIVATE_EXISTING_CANARY'
    assert not logger.healthy and logger.last_write_succeeded is False


def test_write_failure_rolls_back_partial_line_and_does_not_print_private_error(tmp_path, monkeypatch, capsys):
    logger = SafeJSONLLogger(tmp_path / 'worker.jsonl')
    assert logger.emit('worker_ready', service='worker')
    original = logger.path.read_bytes()
    write, calls = os.write, 0
    def partial(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return write(fd, data[:7])
        raise OSError('PROVIDER_TOKEN_CANARY')
    monkeypatch.setattr(os, 'write', partial)
    assert not logger.emit('worker_stopped', service='worker')
    assert logger.path.read_bytes() == original and not logger.healthy
    assert 'CANARY' not in repr(capsys.readouterr())


def test_busy_writer_lock_wait_is_bounded(tmp_path):
    path = tmp_path / 'worker.jsonl'
    lock = os.open(path.with_name('worker.jsonl.lock'), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger = SafeJSONLLogger(path, lock_timeout=.03)
        started = time.monotonic()
        assert not logger.emit('worker_ready', service='worker')
        assert time.monotonic() - started < .3
        assert not path.exists()
    finally:
        os.close(lock)
