"""Bounded local JSONL diagnostics with an explicit, content-free schema.

Callers supply program-owned identifiers, never request content or exceptions'
messages. Graph identifiers are additionally resolved against committed SQLite
facts. These records cannot change execution authority or business events.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID
from pydantic import ValidationError

from ..errors import BusinessError
from ..db.connection import StorageBusyError
from ..models.transport import ProviderFailure
from ..evidence.redaction import TextRedactor

EVENTS = frozenset(('api_request', 'api_error', 'service_started', 'service_stopped',
    'service_failed', 'worker_ready', 'worker_stopped', 'graph_node_started',
    'graph_node_completed', 'graph_node_failed', 'graph_node_interrupted',
    'graph_checkpoint_saved', 'graph_executor_failed'))
NODES = frozenset(('reconcile', 'observe', 'decide', 'dispatch', 'confirm', 'verify',
    'aggregate', 'recover', 'prepare_wait', 'wait', 'stopped'))
ERROR_CLASSES = frozenset(('business', 'internal', 'cancelled', 'budget', 'storage',
    'provider', 'validation', 'configuration', 'resource', 'evidence', 'timeout'))
ERROR_CODES = frozenset(('INTERNAL_ERROR', 'CANCELLED', 'STATE_CONFLICT', 'CONTROL_PENDING',
    'BUDGET_EXCEEDED', 'MODEL_FAILED', 'MODEL_OUTPUT_INVALID', 'VERIFICATION_FAILED',
    'RECOVERY_REQUIRED', 'FORBIDDEN', 'NOT_FOUND', 'INVALID_PARAMETER', 'RESOURCE_CONFLICT',
    'SERVICE_UNAVAILABLE', 'EVIDENCE_MISSING', 'EVIDENCE_CORRUPT', 'IDENTITY_RECHECK_REQUIRED',
    'CONFIG_NOT_READY', 'CONFIG_SNAPSHOT_MISSING', 'CONFIG_SNAPSHOT_INVALID',
    'IDEMPOTENCY_CONFLICT', 'SITE_THROTTLED', 'STORAGE_UNAVAILABLE',
    'TIMEOUT', 'UPSTREAM_ERROR', 'MODEL_RATE_LIMIT'))
_IDS = frozenset(('task_id', 'run_id', 'step_id', 'operation_id', 'thread_id',
    'checkpoint_id', 'business_checkpoint_id', 'worker_id', 'verification_id'))
_NUMBERS = frozenset(('duration_ms', 'pid', 'worker_generation', 'event_id',
    'progress_id', 'state_version', 'epoch'))
_FIELDS = _IDS | _NUMBERS | frozenset(('request_id', 'transport_request_id', 'service', 'http_status', 'method',
    'error_class', 'error_code', 'node', 'graph_version', 'state_schema_version'))
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', re.ASCII)
_METHODS = frozenset(('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'))
_MAX_INT = 2**63 - 1
_ID_REDACTOR = TextRedactor()
_DIAGNOSTIC_CODES = {'model_failed': 'MODEL_FAILED', 'invalid_model_output': 'MODEL_OUTPUT_INVALID',
    'verification_incomplete': 'VERIFICATION_FAILED', 'recovery_required': 'RECOVERY_REQUIRED',
    'page_changed': 'STATE_CONFLICT', 'budget_exceeded': 'BUDGET_EXCEEDED',
    'configuration_required': 'CONFIG_NOT_READY', 'identity_recheck_required': 'IDENTITY_RECHECK_REQUIRED',
    'graph_preparation_failed': 'SERVICE_UNAVAILABLE'}


def safe_error_code(error):
    if isinstance(error, asyncio.CancelledError):
        return 'CANCELLED'
    if isinstance(error, TimeoutError):
        return 'TIMEOUT'
    if isinstance(error, ValidationError):
        return 'INVALID_PARAMETER'
    if isinstance(error, ProviderFailure):
        category = error.error_class
        if type(category) is not str:
            return 'INTERNAL_ERROR'
        return {'timeout': 'TIMEOUT', 'rate_limit': 'MODEL_RATE_LIMIT',
            'invalid_credentials': 'UPSTREAM_ERROR', 'invalid_output': 'MODEL_OUTPUT_INVALID',
            'provider_error': 'UPSTREAM_ERROR'}.get(category, 'INTERNAL_ERROR')
    if isinstance(error, BusinessError) and type(error.code) is str and error.code in ERROR_CODES:
        return error.code
    if (isinstance(error, (sqlite3.Error, StorageBusyError))
            or isinstance(error, OSError) and error.errno in
                (errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EPERM, errno.EROFS,
                 errno.EIO, errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EMFILE, errno.ENFILE)):
        return 'STORAGE_UNAVAILABLE'
    return 'INTERNAL_ERROR'


def safe_error_class(error):
    code = safe_error_code(error)
    if code == 'CANCELLED':
        return 'cancelled'
    if code == 'TIMEOUT':
        return 'timeout'
    if code == 'BUDGET_EXCEEDED':
        return 'budget'
    if code.startswith('EVIDENCE_'):
        return 'evidence'
    if code.startswith('CONFIG_'):
        return 'configuration'
    if code in ('MODEL_FAILED', 'MODEL_OUTPUT_INVALID', 'MODEL_RATE_LIMIT', 'UPSTREAM_ERROR'):
        return 'provider'
    if code in ('RESOURCE_CONFLICT', 'IDENTITY_RECHECK_REQUIRED'):
        return 'resource'
    if code == 'INVALID_PARAMETER':
        return 'validation'
    if code == 'STORAGE_UNAVAILABLE':
        return 'storage'
    return 'business' if isinstance(error, BusinessError) else 'internal'


def _valid_fields(fields):
    if any(type(key) is not str or key not in _FIELDS for key in fields):
        return False
    for key, value in fields.items():
        if key in _IDS:
            if (type(value) is not str or _ID.fullmatch(value) is None
                    or _ID_REDACTOR.contains_sensitive(value)):
                return False
        elif key in _NUMBERS:
            if type(value) is not int or not 0 <= value <= _MAX_INT:
                return False
            if key in ('pid', 'worker_generation', 'event_id', 'progress_id', 'epoch') and value == 0:
                return False
        elif key in ('request_id', 'transport_request_id'):
            if type(value) is not str or not re.fullmatch(r'[0-9a-f]{32}|[0-9a-f-]{36}', value):
                return False
            try:
                if UUID(value).version != 4:
                    return False
            except ValueError:
                return False
        elif key == 'http_status':
            if type(value) is not int or not 100 <= value <= 599:
                return False
        else:
            allowed = {'service': ('api', 'worker', 'graph'), 'method': _METHODS,
                'node': NODES, 'error_class': ERROR_CLASSES, 'error_code': ERROR_CODES,
                'graph_version': ('browser-loop-v1',), 'state_schema_version': ('browser-loop-state-v1',)}[key]
            if type(value) is not str or value not in allowed:
                return False
    return True


class SafeJSONLLogger:
    """At most backups+1 bounded files; one flock-protected append at a time.

    Writers coordinate across threads and processes using a private sidecar.
    Lock contention is bounded; diagnostic failure returns False and does not
    propagate into the application's success/failure decision.
    """
    def __init__(self, path, *, max_bytes=1024 * 1024, backups=3, lock_timeout=.1):
        if (type(max_bytes) is not int or not 4096 <= max_bytes <= 64 * 1024 * 1024
                or type(backups) is not int or not 0 <= backups <= 8
                or type(lock_timeout) not in (int, float) or not 0 < lock_timeout <= 1):
            raise ValueError('Invalid bounded diagnostic storage configuration')
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.lock_timeout, self.dropped = lock_timeout, 0
        self.last_write_succeeded = None

    @property
    def healthy(self):
        return self.last_write_succeeded is not False

    @staticmethod
    def _regular(info):
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()

    def _open(self, directory, name):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        # Concurrent first creation can return a transient ENOENT on macOS.
        # Retry only that lookup, against the same already-open directory and
        # with NOFOLLOW intact; other errors still fail closed immediately.
        for attempt in range(3):
            try:
                fd = os.open(name, flags, 0o600, dir_fd=directory)
                break
            except FileNotFoundError:
                if attempt == 2:
                    raise
        try:
            if not self._regular(os.fstat(fd)):
                raise OSError('Unsafe diagnostic file')
            os.fchmod(fd, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _check_files(self, directory):
        for name in [self.path.name, *(self.path.name + '.' + str(i) for i in range(1, self.backups + 1))]:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not self._regular(info) or stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError('Unsafe diagnostic file')

    def _rotate(self, directory):
        if self.backups:
            oldest = self.path.name + '.' + str(self.backups)
            try:
                os.unlink(oldest, dir_fd=directory)
            except FileNotFoundError:
                pass
            for index in range(self.backups - 1, 0, -1):
                try:
                    os.rename(self.path.name + '.' + str(index), self.path.name + '.' + str(index + 1),
                              src_dir_fd=directory, dst_dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.rename(self.path.name, self.path.name + '.1', src_dir_fd=directory, dst_dir_fd=directory)
        else:
            os.unlink(self.path.name, dir_fd=directory)

    def emit(self, event, **fields):
        if type(event) is not str or event not in EVENTS or not _valid_fields(fields):
            self.dropped += 1
            return False
        record = {'schema_version': 1,
            'timestamp_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'event': event, **fields}
        data = (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n').encode()
        if len(data) > 4096 or len(data) > self.max_bytes:
            self.dropped += 1
            return False
        directory = lock = fd = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise OSError('Unsafe diagnostic directory')
            lock = self._open(directory, self.path.name + '.lock')
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError('Diagnostic writer is busy')
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
            self._check_files(directory)
            try:
                size = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False).st_size
            except FileNotFoundError:
                size = 0
            if size + len(data) > self.max_bytes:
                self._rotate(directory)
            fd = self._open(directory, self.path.name)
            previous = os.fstat(fd).st_size
            try:
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        raise OSError('Diagnostic append was incomplete')
                    offset += written
            except OSError:
                os.ftruncate(fd, previous)
                raise
            self.last_write_succeeded = True
            return True
        except (OSError, ValueError):
            self.dropped += 1
            self.last_write_succeeded = False
            return False
        finally:
            for handle in (fd, lock, directory):
                if handle is not None:
                    try:
                        os.close(handle)
                    except OSError:
                        pass


class TrustedGraphDiagnostics:
    """Translate only verified framework references into the logger schema."""
    def __init__(self, data_dir, logger, *, store=None, checkpointer=None):
        from ..graph.store import GraphStore
        self.store = store or GraphStore(Path(data_dir) / 'business.sqlite3')
        self.logger, self.checkpointer = logger, checkpointer

    @contextmanager
    def _snapshot(self):
        # Diagnostics must never recreate a missing business database. Keep
        # lock waits and query work bounded even after an async hook times out.
        db = sqlite3.connect(self.store.path.resolve().as_uri() + '?mode=ro',
                             uri=True, isolation_level=None, timeout=.1)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            remaining = [5000]
            def progress():
                remaining[0] -= 1
                return int(remaining[0] < 0)
            db.set_progress_handler(progress, 1000)
            db.execute('BEGIN')
            yield db
        finally:
            db.close()

    def _binding(self, db, run_id, state=None):
        from ..graph.models import validate_graph_state
        run = self.store._run(db, run_id)
        state = validate_graph_state(state) if state is not None else self.store._state(db, run)
        if (state['run_id'] != run_id or state['contract_version'] != run['contract_version']
                or state['state_version'] > run['state_version']):
            return None
        if state['state_version'] == run['state_version']:
            from ..state import TERMINAL_STATES
            if state['completed'] != (run['state'] in TERMINAL_STATES):
                return None
        event = db.execute('SELECT * FROM task_events WHERE run_id=? AND event_id=?',
                           (run_id, state['business_event_id'])).fetchone()
        if event is None or event['state_version'] != state['state_version']:
            return None
        progress = None
        if state['progress_id']:
            progress = db.execute('SELECT * FROM graph_progress WHERE run_id=? AND progress_id=?',
                                  (run_id, state['progress_id'])).fetchone()
            if (progress is None or progress['business_event_id'] != event['event_id']
                    or progress['state_version'] != state['state_version']
                    or progress['checkpoint_id'] != state['business_checkpoint_id']
                    or progress['wait_id'] != state['wait_id']
                    or progress['contract_sha256'] != run['contract_sha256']
                    or progress['graph_version'] != state['graph_version']
                    or progress['state_schema_version'] != state['state_schema_version']):
                return None
            if progress['snapshot_id'] is not None and progress['snapshot_id'] != state['snapshot_id']:
                return None
        if state['wait_id'] and (progress is None or progress['phase'] != 'wait'
                or event['event_type'] != 'wait_registered'
                or json.loads(event['payload_json']).get('wait_id') != state['wait_id']
                or state['route'] != 'wait' or state['completed']):
            return None
        if state['business_checkpoint_id']:
            checkpoint = self.store._checkpoint(db, run_id, state['business_checkpoint_id'])
            if (checkpoint is None or checkpoint.contract_version != run['contract_version']
                    or checkpoint.business_event_id > event['event_id']):
                return None
        if state['snapshot_id'] and not db.execute(
                'SELECT 1 FROM observations WHERE run_id=? AND snapshot_id=?', (run_id, state['snapshot_id'])).fetchone():
            return None
        fields = {'service': 'graph', 'task_id': run['task_id'], 'run_id': run_id, 'thread_id': run_id,
            'graph_version': run['graph_version'], 'state_schema_version': run['graph_state_schema_version'],
            'event_id': event['event_id'], 'state_version': state['state_version']}
        for source, target in (('progress_id', 'progress_id'), ('business_checkpoint_id', 'business_checkpoint_id')):
            if state[source] is not None:
                fields[target] = state[source]
        if progress is not None and progress['verification_id'] is not None:
            verification = db.execute('''SELECT verification_id,state_version FROM run_verifications
                WHERE run_id=? AND verification_id=? AND contract_sha256=?''',
                (run_id, progress['verification_id'], run['contract_sha256'])).fetchone()
            if verification is None:
                return None
            if verification['state_version'] != progress['state_version']:
                # Finalization advances VERIFYING to a terminal state. The
                # immutable result event, rather than a framework field, binds
                # the preceding verifier capsule to the terminal progress.
                if (progress['phase'] != 'aggregate' or event['event_type'] != 'result_ready'
                        or verification['state_version'] != progress['state_version'] - 1
                        or json.loads(event['payload_json']).get('result_ref') != verification['verification_id']):
                    return None
            fields['verification_id'] = verification['verification_id']
        payload = json.loads(event['payload_json'])
        if event['event_type'] == 'action_recorded':
            step = db.execute('SELECT step_id FROM steps WHERE run_id=? AND step_id=?',
                              (run_id, payload.get('step_id'))).fetchone()
            if step:
                fields['step_id'] = step['step_id']
                intent = db.execute('SELECT operation_id FROM gateway_attempts WHERE run_id=? AND step_id=?',
                                    (run_id, step['step_id'])).fetchone()
                if intent and intent['operation_id']:
                    fields['operation_id'] = intent['operation_id']
        elif event['event_type'] in ('operation_requested', 'operation_completed'):
            operation = db.execute('SELECT operation_id FROM run_controls WHERE run_id=? AND operation_id=?',
                                   (run_id, payload.get('operation_id'))).fetchone()
            if operation:
                fields['operation_id'] = operation['operation_id']
        return fields, state

    def node(self, run_id, node, phase, *, error=None):
        if (type(node) is not str or node not in NODES or type(phase) is not str
                or phase not in ('started', 'completed', 'failed', 'interrupted')):
            return False
        return self._record('graph_node_' + phase, run_id, node=node, error=error)

    def executor_error(self, run_id, error):
        return self._record('graph_executor_failed', run_id, error=error)

    def _record(self, event, run_id, *, node=None, error=None):
        try:
            with self._snapshot() as db:
                bound = self._binding(db, run_id)
            if bound is None:
                return False
            fields, state = bound
            if node is not None:
                fields['node'] = node
            if error is not None:
                fields.update(error_code=safe_error_code(error), error_class=safe_error_class(error))
            elif state['diagnostic'] in _DIAGNOSTIC_CODES:
                code = _DIAGNOSTIC_CODES[state['diagnostic']]
                fields.update(error_code=code, error_class=safe_error_class(BusinessError(code, 'Diagnostic')))
            return self.logger.emit(event, **fields)
        except Exception:
            return False

    def _read_binding(self, run_id, state):
        with self._snapshot() as db:
            return self._binding(db, run_id, state)

    async def checkpoint(self, run_id, *, checkpointer=None):
        from ..graph.models import GraphSnapshot
        try:
            saver = checkpointer if checkpointer is not None else self.checkpointer
            if saver is None:
                return False
            saved = await saver.aget_tuple({'configurable': {'thread_id': run_id}})
            if saved is None or type(saved.checkpoint) is not dict or type(saved.config) is not dict:
                return False
            config, checkpoint = saved.config.get('configurable'), saved.checkpoint
            if (type(config) is not dict or config.get('thread_id') != run_id
                    or config.get('checkpoint_ns', '') != '' or type(checkpoint.get('id')) is not str
                    or config.get('checkpoint_id') != checkpoint['id']):
                return False
            UUID(checkpoint['id'])
            metadata = saved.metadata
            if (type(metadata) is not dict or set(metadata) - {'source', 'step', 'parents', 'thread_id'}
                    or metadata.get('source') not in ('input', 'loop', 'update', 'fork')
                    or type(metadata.get('step')) is not int
                    or metadata.get('thread_id', run_id) != run_id):
                return False
            channels = checkpoint.get('channel_values')
            if type(channels) is not dict:
                return False
            state, initial = {}, None
            for key, value in channels.items():
                if key in GraphSnapshot.model_fields:
                    state[key] = value
                elif key == '__start__':
                    initial = GraphSnapshot.model_validate(value).state()
                elif (type(key) is str and key.startswith('branch:to:')
                      and key.removeprefix('branch:to:') in NODES and value is None):
                    continue
                else:
                    return False
            state = GraphSnapshot.model_validate(state or initial).state()
            bound = await asyncio.to_thread(self._read_binding, run_id, state)
            if bound is None:
                return False
            fields, _ = bound
            return await asyncio.to_thread(self.logger.emit, 'graph_checkpoint_saved',
                                           **fields, checkpoint_id=checkpoint['id'])
        except Exception:
            return False
