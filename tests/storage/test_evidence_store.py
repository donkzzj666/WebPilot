"""Real file/SQLite failures must never manufacture a complete evidence index."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import errno
import os
import sqlite3
import threading

import pytest

from webagent.db import connect, migrate, transaction
from webagent.errors import BusinessError
from webagent.evidence.store import EvidenceStore
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from conftest import seed


@pytest.fixture
def store(database):
    with connect(database) as db, transaction(db):
        seed(db)
    return EvidenceStore(database.parent)


def publish(store, data=b'clean synthetic artifact', **overrides):
    options = dict(source_url='https://fixture.invalid/object', captured_at=datetime.now(timezone.utc),
                   object_id='fixture-object', query_scope='synthetic', locator_or_page='page 1',
                   sensitivity='public', redaction_status='FILTERED', policy_version='fixture-v1')
    options.update(overrides)
    return store.publish('run-1', data, **options)


def assert_error(fn, code):
    with pytest.raises(BusinessError) as caught:
        fn()
    assert caught.value.code == code


def test_publication_index_and_audit_share_commit_and_restart(store):
    item = publish(store)
    assert item['sha256'] and item['size_bytes'] == 24 and item['capture_status'] == 'COMPLETE'
    assert item['mime_type'] == 'text/plain; charset=utf-8'
    assert EvidenceStore(store.data_dir).read(item['evidence_id'])[1] == b'clean synthetic artifact'
    with connect(store.database) as db:
        assert db.execute('SELECT count(*) FROM evidence_events').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM evidence_run_guards').fetchone()[0] == 1
    assert store.scan_orphans(grace_seconds=0, cleanup=True) == []


@pytest.mark.parametrize('stage,extension', [('mid_write', '.tmp'), ('before_rename', '.tmp'),
    ('after_rename', '.blob'), ('before_index', '.blob'), ('before_commit', '.blob')])
def test_failure_leaves_no_index_only_recoverable_orphan(store, stage, extension):
    def fault(current):
        if current == stage:
            raise RuntimeError('synthetic interrupted publication')
    store.fault_hook = store.files.fault_hook = fault
    with pytest.raises(RuntimeError):
        publish(store)
    with connect(store.database) as db:
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM evidence_events').fetchone()[0] == 0
    store.fault_hook = store.files.fault_hook = None
    assert store.scan_orphans(grace_seconds=3600) == []
    orphans = store.scan_orphans(grace_seconds=0)
    assert len(orphans) == 1 and orphans[0]['artifact_path'].endswith(extension)
    assert orphans[0]['status'] == 'REGISTERED'
    assert store.scan_orphans(grace_seconds=0, cleanup=True)[0]['status'] == 'CLEANED'
    assert store.files.candidates() == []


def test_duplicate_identity_never_overwrites_artifact(store):
    original = publish(store, evidence_id='fixed-id')
    assert_error(lambda: publish(store, b'new content', evidence_id='fixed-id'), 'STATE_CONFLICT')
    assert store.read('fixed-id')[1] == b'clean synthetic artifact'
    assert store.metadata('fixed-id')['sha256'] == original['sha256']
    assert store.scan_orphans(grace_seconds=0) == []


@pytest.mark.parametrize('table', ['evidence', 'evidence_artifacts', 'evidence_events'])
@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE', 'REPLACE'])
def test_immutable_history_is_enforced_by_sql(store, table, operation):
    publish(store)
    with connect(store.database) as db:
        if operation == 'UPDATE':
            sql = f'UPDATE {table} SET ' + ('event_type=event_type' if table == 'evidence_events' else 'evidence_id=evidence_id')
        elif operation == 'DELETE':
            sql = f'DELETE FROM {table}'
        else:
            sql = f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)


def test_sensitive_original_blocked_and_display_copy_bound_same_run(store):
    original = publish(store, b'secret synthetic original', sensitivity='restricted', redaction_status='BLOCKED', policy_version=None)
    assert_error(lambda: store.read(original['evidence_id']), 'FORBIDDEN')
    assert store.read(original['evidence_id'], allow_restricted=True)[1] == b'secret synthetic original'
    child = publish(store, b'[REDACTED]', sensitivity='redacted', original_evidence_id=original['evidence_id'])
    assert child['original_evidence_id'] == original['evidence_id']
    assert store.read(child['evidence_id'])[1] == b'[REDACTED]'
    assert_error(lambda: store.read(child['evidence_id'], run_id='another-run'), 'NOT_FOUND')
    with connect(store.database) as db, transaction(db):
        seed(db, task_id='task-2', run_id='run-2')
    assert_error(lambda: store.publish('run-2', b'clean', source_url='https://fixture.invalid',
        captured_at=datetime.now(timezone.utc), object_id='o', query_scope='', locator_or_page='',
        sensitivity='redacted', original_evidence_id=original['evidence_id'], redaction_status='FILTERED',
        policy_version='v1'), 'INVALID_PARAMETER')


@pytest.mark.parametrize('mode', ['overwrite', 'truncate', 'missing', 'symlink', 'hardlink'])
def test_verified_reads_reject_corrupt_missing_and_linked_files(store, mode):
    item = publish(store)
    path = store.data_dir / item['artifact_path']
    if mode == 'overwrite':
        path.write_bytes(b'X' * item['size_bytes'])
    elif mode == 'truncate':
        path.write_bytes(b'X')
    elif mode == 'missing':
        path.unlink()
    elif mode == 'symlink':
        path.unlink()
        path.symlink_to(store.database)
    else:
        os.link(path, store.data_dir / 'external-copy')
    code = 'EVIDENCE_MISSING' if mode == 'missing' else 'EVIDENCE_CORRUPT'
    assert_error(lambda: store.read(item['evidence_id']), code)
    assert store.metadata(item['evidence_id'])['availability'] == code.removeprefix('EVIDENCE_')
    with connect(store.database) as db:
        assert db.execute('SELECT capture_status FROM evidence').fetchone()[0] == 'COMPLETE'
        assert db.execute('SELECT count(*) FROM evidence_events').fetchone()[0] == 2


def test_expiry_marks_availability_without_erasing_history(store):
    item = publish(store)
    path = store.data_dir / item['artifact_path']
    store.expire(item['evidence_id'])
    assert_error(lambda: store.read(item['evidence_id']), 'EVIDENCE_EXPIRED')
    assert path.exists() and store.metadata(item['evidence_id'])['capture_status'] == 'COMPLETE'
    store.expire(item['evidence_id'])
    with connect(store.database) as db:
        assert db.execute('SELECT count(*) FROM evidence_events').fetchone()[0] == 2


@pytest.mark.parametrize('stage', ['mid_write', 'after_rename', 'before_index', 'before_commit'])
@pytest.mark.parametrize('kind', ['ENOSPC', 'EDQUOT', 'SQLITE_FULL'])
def test_disk_full_latches_private_marker_across_restart(store, stage, kind):
    def fault(current):
        if current != stage:
            return
        if kind == 'SQLITE_FULL':
            error = sqlite3.OperationalError('database or disk is full')
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error
        raise OSError(getattr(errno, kind), 'synthetic storage full')
    store.fault_hook = store.files.fault_hook = fault
    assert_error(lambda: publish(store), 'EVIDENCE_STORAGE_UNAVAILABLE')
    assert_error(store.assert_dispatch_allowed, 'EVIDENCE_STORAGE_UNAVAILABLE')
    assert (store.data_dir / 'evidence/control/fault.marker').read_bytes()[0:1] == b'1'
    assert_error(lambda: EvidenceStore(store.data_dir), 'EVIDENCE_STORAGE_UNAVAILABLE')
    with connect(store.database) as db:
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0
        assert db.execute('SELECT faulted FROM evidence_domain').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['truncate', 'delete', 'symlink', 'hardlink', 'arbitrary'])
def test_invalid_fault_control_never_reinitializes_on_restart(store, mode):
    marker = store.data_dir / 'evidence/control/fault.marker'
    if mode == 'truncate':
        marker.write_bytes(b'0')
    elif mode == 'delete':
        marker.unlink()
    elif mode == 'symlink':
        marker.unlink()
        marker.symlink_to(store.database)
    elif mode == 'hardlink':
        os.link(marker, store.data_dir / 'marker-link')
    else:
        marker.write_bytes(b'x' + b'\0' * 4095)
    assert_error(store.assert_dispatch_allowed, 'EVIDENCE_STORAGE_UNAVAILABLE')
    assert_error(lambda: EvidenceStore(store.data_dir), 'EVIDENCE_STORAGE_UNAVAILABLE')


def test_concurrent_publishers_and_scan_preserve_inflight_then_reference(store):
    reached, release = threading.Event(), threading.Event()
    def fault(stage):
        if stage == 'after_rename':
            reached.set()
            assert release.wait(3)
    writer = EvidenceStore(store.data_dir, fault_hook=fault)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(publish, writer)
        assert reached.wait(3)
        scanning = pool.submit(store.scan_orphans, grace_seconds=0, cleanup=True)
        assert not scanning.done()
        release.set()
        item = pending.result()
        assert scanning.result() == []
    assert store.read(item['evidence_id'])[1] == b'clean synthetic artifact'


def test_concurrent_same_id_only_one_publication_commits(store):
    writers = [EvidenceStore(store.data_dir), EvidenceStore(store.data_dir)]
    def once(writer):
        try:
            return publish(writer, evidence_id='same-id')['evidence_id']
        except BusinessError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(once, writers)) == ['STATE_CONFLICT', 'same-id']
    assert store.scan_orphans(grace_seconds=0) == []


def test_scheduled_publish_requires_fresh_qualification_after_file_write(store):
    scheduler = SchedulerStore(store.database)
    scheduler.enqueue('run-1', [Resource.site_identity('fixture', None), Resource.browser_context('run-1')],
                      expected_state_version=0)
    generation = scheduler.start_worker('evidence-worker')
    token = scheduler.claim('evidence-worker', generation)
    assert_error(lambda: publish(store), 'RESOURCE_CONFLICT')
    def expire(stage):
        if stage == 'before_index':
            with connect(store.database) as db:
                db.execute('UPDATE scheduler_workers SET expires_at=heartbeat_at')
    store.fault_hook = expire
    assert_error(lambda: publish(store, execution_token=token), 'RESOURCE_CONFLICT')
    with connect(store.database) as db:
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0
    assert len(store.scan_orphans(grace_seconds=0)) == 1


@pytest.mark.parametrize('options', [dict(captured_at='bad'), dict(sensitivity='redacted'),
    dict(sensitivity='restricted'), dict(policy_version=None), dict(artifact_kind='ci'),
    dict(artifact_kind='html'), dict(evidence_id='../path'), dict(commit_sha='bad')])
def test_invalid_metadata_does_not_index(store, options):
    # Ids are opaque DB identifiers; traversal-looking evidence IDs are accepted
    # because only generated UUID artifact paths are used. Exercise true invalid ids.
    if options == {'evidence_id': '../path'}:
        options = {'evidence_id': 'bad\0id'}
    assert_error(lambda: publish(store, **options), 'INVALID_PARAMETER')
    assert store.files.candidates() == []


def test_migration_v12_keeps_legacy_evidence_and_never_fabricates_detail(tmp_path):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=12)
    with connect(path) as db, transaction(db):
        seed(db)
        db.execute('''INSERT INTO evidence(evidence_id,run_id,source_url,captured_at,object_id,
            query_scope,artifact_path,sha256,locator_or_page,excerpt,sensitivity,capture_status,artifact_kind)
            VALUES('legacy','run-1','https://fixture.invalid',?,'o','','legacy.blob',?,'','','public','COMPLETE','text')''',
            ('2026-10-01T00:00:00.000000Z', 'a' * 64))
    migrate(path)
    with connect(path) as db:
        assert db.execute('SELECT evidence_id FROM evidence').fetchone()[0] == 'legacy'
    assert_error(lambda: EvidenceStore(tmp_path).read('legacy'), 'NOT_FOUND')


def observation(store):
    with connect(store.database) as db, transaction(db):
        db.execute('''INSERT INTO observations VALUES('snapshot-1','run-1',?,
            'https://fixture.invalid/object','Captured page','tab','frame','v1',800,600,'','BLOCKED')''',
            ('2026-10-01T00:00:00.000000Z',))
    return dict(snapshot_id='snapshot-1', run_id='run-1', source_url='https://fixture.invalid/object',
                redaction_status='FILTERED', visible_excerpt='safe excerpt', evidence_ids=[])


def test_filtered_observation_append_only_binding_and_integrity(store):
    dto = observation(store)
    original = publish(store, sensitivity='restricted', redaction_status='BLOCKED', policy_version=None, snapshot_id='snapshot-1')
    child = publish(store, b'safe', sensitivity='redacted', original_evidence_id=original['evidence_id'], snapshot_id='snapshot-1')
    dto['evidence_ids'] = [child['evidence_id']]
    filtered = store.record_filtered_observation('snapshot-1', dto, policy_version='fixture-v1',
                                                 evidence_ids=dto['evidence_ids'])
    assert filtered['content'] == dto
    assert store.filtered_observation('snapshot-1', run_id='run-1')['sha256'] == filtered['sha256']
    assert_error(lambda: store.filtered_observation('snapshot-1', run_id='run-2'), 'NOT_FOUND')
    assert_error(lambda: store.record_filtered_observation('snapshot-1', dto, policy_version='fixture-v1',
                 evidence_ids=dto['evidence_ids']), 'STATE_CONFLICT')
    with connect(store.database) as db:
        assert db.execute('SELECT visible_excerpt,redaction_status FROM observations').fetchone()[:] == ('', 'BLOCKED')
        for sql in ("UPDATE observations SET title='tampered'", 'DELETE FROM observations',
                    'INSERT OR REPLACE INTO observations SELECT * FROM observations',
                    'UPDATE filtered_observations SET content_json=content_json', 'DELETE FROM filtered_observations'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)


@pytest.mark.parametrize('field,value', [('snapshot_id', 'other'), ('run_id', 'other'),
    ('redaction_status', 'BLOCKED'), ('evidence_ids', ['unknown'])])
def test_filtered_dto_cannot_forge_snapshot_run_or_evidence_binding(store, field, value):
    dto = observation(store)
    dto[field] = value
    assert_error(lambda: store.record_filtered_observation('snapshot-1', dto, policy_version='fixture-v1'),
                 'INVALID_PARAMETER')


@pytest.mark.parametrize('kind', ['blocked', 'expired', 'other-run', 'wrong-policy'])
def test_filtered_observation_cannot_depend_on_unapproved_artifacts(store, kind):
    dto = observation(store)
    if kind == 'blocked':
        item = publish(store, sensitivity='restricted', redaction_status='BLOCKED', policy_version=None)
    elif kind == 'other-run':
        with connect(store.database) as db, transaction(db):
            seed(db, task_id='task-2', run_id='run-2')
        item = store.publish('run-2', b'safe', source_url='https://fixture.invalid', captured_at=datetime.now(timezone.utc),
            object_id='o', query_scope='', locator_or_page='', sensitivity='public', redaction_status='FILTERED', policy_version='fixture-v1')
    else:
        item = publish(store)
        if kind == 'expired':
            store.expire(item['evidence_id'])
    dto['evidence_ids'] = [item['evidence_id']]
    code = 'NOT_FOUND' if kind == 'other-run' else 'FORBIDDEN'
    assert_error(lambda: store.record_filtered_observation('snapshot-1', dto,
        policy_version='different' if kind == 'wrong-policy' else 'fixture-v1', evidence_ids=dto['evidence_ids']), code)


@pytest.mark.parametrize('mode', ['valid', 'missing', 'corrupt', 'expired', 'empty-tracked'])
def test_success_guard_uses_current_transaction_and_actual_artifacts(store, mode):
    if mode == 'empty-tracked':
        with connect(store.database) as db:
            db.execute("INSERT INTO evidence_run_guards VALUES('run-1','2026-10-01T00:00:00.000000Z')")
    else:
        item = publish(store)
        path = store.data_dir / item['artifact_path']
        if mode == 'missing':
            path.unlink()
        elif mode == 'corrupt':
            path.write_bytes(b'broken')
        elif mode == 'expired':
            store.expire(item['evidence_id'])
    with connect(store.database) as db, transaction(db):
        if mode == 'valid':
            store.assert_run_ready('run-1', db)
        else:
            assert_error(lambda: store.assert_run_ready('run-1', db),
                         'EVIDENCE_CORRUPT' if mode == 'corrupt' else 'EVIDENCE_MISSING')


def test_success_guard_verifies_sensitive_original_even_with_good_derivative(store):
    original = publish(store, b'original', sensitivity='restricted', redaction_status='BLOCKED', policy_version=None)
    publish(store, b'safe', sensitivity='redacted', original_evidence_id=original['evidence_id'])
    (store.data_dir / original['artifact_path']).unlink()
    assert_error(lambda: store.assert_run_ready('run-1'), 'EVIDENCE_MISSING')


def test_missing_publication_details_reject_legacy_success(tmp_path):
    path = tmp_path / 'business.sqlite3'
    migrate(path, target=12)
    with connect(path) as db, transaction(db):
        seed(db)
        db.execute('''INSERT INTO evidence(evidence_id,run_id,source_url,captured_at,object_id,
            query_scope,artifact_path,sha256,locator_or_page,excerpt,sensitivity,capture_status,artifact_kind)
            VALUES('legacy','run-1','https://fixture.invalid',?,'o','','legacy.blob',?,'','','public','COMPLETE','text')''',
            ('2026-10-01T00:00:00.000000Z', 'a' * 64))
    migrate(path)
    assert_error(lambda: EvidenceStore(tmp_path).assert_run_ready('run-1'), 'EVIDENCE_MISSING')


def test_default_30_day_retention_marks_before_any_file_deletion(store):
    from datetime import timedelta
    item = publish(store)
    published = datetime.fromisoformat(item['published_at'].replace('Z', '+00:00'))
    assert datetime.fromisoformat(item['expires_at'].replace('Z', '+00:00')) - published == timedelta(days=30)
    assert store.expire_due(now=published + timedelta(days=29)) == []
    assert store.expire_due(now=published + timedelta(days=30)) == [item['evidence_id']]
    assert store.expire_due(now=published + timedelta(days=31)) == []
    assert_error(lambda: store.read(item['evidence_id']), 'EVIDENCE_EXPIRED')
    assert (store.data_dir / item['artifact_path']).is_file()
    assert store.scan_orphans(grace_seconds=0, cleanup=True) == []


def test_formal_evaluation_retained_until_explicit_cleanup(store):
    item = publish(store, retain=True)
    assert item['expires_at'] is None and item['keep_until_explicit_cleanup'] == 1
    assert store.expire_due(now='2099-10-01T00:00:00.000000Z') == []
    assert store.read(item['evidence_id'])[1] == b'clean synthetic artifact'


@pytest.mark.parametrize('value', ['bad', datetime(2026, 10, 1), True])
def test_expiry_requires_aware_timestamp(store, value):
    assert_error(lambda: store.expire_due(now=value), 'INVALID_PARAMETER')


def test_retention_facts_and_expired_state_cannot_be_silently_rewritten(store):
    item = publish(store)
    store.expire(item['evidence_id'])
    with connect(store.database) as db:
        for sql in ('UPDATE evidence_retention SET expires_at=NULL', 'DELETE FROM evidence_retention',
                    'INSERT OR REPLACE INTO evidence_retention SELECT * FROM evidence_retention',
                    "UPDATE evidence_availability SET status='AVAILABLE'"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)


def test_publication_reuses_business_observation_and_step_evidence_bindings(store):
    observation(store)
    with connect(store.database) as db, transaction(db):
        db.execute('''INSERT INTO steps(step_id,run_id,sequence,step_kind,epoch,input_snapshot_id,started_at)
            VALUES('step-1','run-1',1,'decision',1,'snapshot-1','2026-10-01T00:00:00.000000Z')''')
    item = publish(store, snapshot_id='snapshot-1', step_id='step-1')
    with connect(store.database) as db:
        assert db.execute('SELECT * FROM observations_evidence').fetchone()[:] == ('run-1', 'snapshot-1', item['evidence_id'])
        assert db.execute('SELECT * FROM steps_evidence').fetchone()[:] == ('run-1', 'step-1', item['evidence_id'])


def test_terminal_read_guard_never_takes_publication_lock_behind_db_write_lock(store):
    publish(store)
    locked, release = threading.Event(), threading.Event()
    def writer():
        with store.files.locked():
            locked.set()
            assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        assert locked.wait(3)
        try:
            with connect(store.database) as db, transaction(db):
                reader = EvidenceStore(store.data_dir, initialize=False)
                reader.assert_run_ready('run-1', db)
        finally:
            release.set()
        future.result()


def test_read_only_initialization_fails_closed_without_creating_artifact_tree(database):
    with pytest.raises(BusinessError) as caught:
        EvidenceStore(database.parent, initialize=False)
    assert caught.value.code == 'EVIDENCE_STORAGE_UNAVAILABLE'
    assert not (database.parent / 'evidence').exists()


def test_interrupted_capture_cannot_succeed_even_if_some_artifacts_are_complete(store):
    observation(store)
    item = publish(store, snapshot_id='snapshot-1')
    assert_error(lambda: store.assert_run_ready('run-1'), 'EVIDENCE_MISSING')
    with connect(store.database) as db:
        dto = dict(db.execute('SELECT * FROM observations WHERE snapshot_id=\'snapshot-1\'').fetchone())
    dto.update(redaction_status='FILTERED', evidence_ids=[item['evidence_id']])
    store.record_filtered_observation('snapshot-1', dto, policy_version='fixture-v1', evidence_ids=dto['evidence_ids'])
    store.assert_run_ready('run-1')


def test_filtered_capture_reference_must_match_snapshot_not_only_run(store):
    observation(store)
    publish(store, snapshot_id='snapshot-1')
    unrelated = publish(store)
    with connect(store.database) as db:
        dto = dict(db.execute('SELECT * FROM observations').fetchone())
    dto.update(redaction_status='FILTERED', evidence_ids=[unrelated['evidence_id']])
    assert_error(lambda: store.record_filtered_observation('snapshot-1', dto, policy_version='fixture-v1',
                 evidence_ids=dto['evidence_ids']), 'FORBIDDEN')


@pytest.mark.parametrize('operation', ['expire', 'expire_due', 'orphan_cleanup'])
def test_maintenance_storage_full_also_revokes_dispatch_and_survives_restart(store, operation, monkeypatch):
    item = publish(store)
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'synthetic maintenance storage full')
    if operation == 'orphan_cleanup':
        with store.files.locked():
            store.files.publish(b'orphan')
        monkeypatch.setattr(store.files, 'unlink', full)
        call = lambda: store.scan_orphans(grace_seconds=0, cleanup=True)
    else:
        monkeypatch.setattr(store, '_event', full)
        call = lambda: store.expire(item['evidence_id']) if operation == 'expire' else store.expire_due(now='2099-10-01T00:00:00.000000Z')
    assert_error(call, 'EVIDENCE_STORAGE_UNAVAILABLE')
    assert_error(store.assert_dispatch_allowed, 'EVIDENCE_STORAGE_UNAVAILABLE')
    assert_error(lambda: EvidenceStore(store.data_dir), 'EVIDENCE_STORAGE_UNAVAILABLE')
    assert store.metadata(item['evidence_id'])['availability'] == 'AVAILABLE'
