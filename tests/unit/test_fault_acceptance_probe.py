"""Falsify M1-24 verdict guards without starting any service or browser."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import importlib
import json
from pathlib import Path
import signal
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/verification'))
probe = importlib.import_module('verify_fault_acceptance')


def valid_report(tmp_path):
    (tmp_path / 'current.json').write_text('{"current_execution":true}')
    return {'passed': True, 'checks': {'executed': True},
            'artifact_sha256': {'current.json': probe.digest(tmp_path / 'current.json')}}


@pytest.mark.parametrize('mutation', ['failed', 'no_checks', 'false_check', 'empty_hashes', 'changed_bytes', 'traversal', 'absolute'])
def test_current_subreport_requires_real_checks_and_matching_in_domain_artifacts(tmp_path, mutation):
    report = valid_report(tmp_path)
    if mutation == 'failed':
        report['passed'] = False
    elif mutation == 'no_checks':
        report['checks'] = {}
    elif mutation == 'false_check':
        report['checks']['executed'] = False
    elif mutation == 'empty_hashes':
        report['artifact_sha256'] = {}
    elif mutation == 'changed_bytes':
        (tmp_path / 'current.json').write_text('changed')
    elif mutation == 'traversal':
        report['artifact_sha256'] = {'../escaped.json': '0' * 64}
    elif mutation == 'absolute':
        report['artifact_sha256'] = {str(tmp_path / 'current.json'): probe.digest(tmp_path / 'current.json')}
    with pytest.raises(AssertionError):
        probe.validate_subreport(tmp_path, report)


def test_subreport_accepts_both_current_check_formats_and_fails_list_false(tmp_path):
    report = valid_report(tmp_path)
    assert probe.validate_subreport(tmp_path, report)['checked_files'] == 1
    report['checks'] = [{'name': 'executed', 'passed': True}]
    assert probe.validate_subreport(tmp_path, report)['check_names'] == ['executed']
    report['checks'][0]['passed'] = False
    with pytest.raises(AssertionError):
        probe.validate_subreport(tmp_path, report)


@pytest.mark.parametrize('parent_link', [False, True])
def test_subreport_rejects_symlink_even_when_it_resolves_inside_private_domain(tmp_path, parent_link):
    report = valid_report(tmp_path)
    if parent_link:
        real = tmp_path / 'real'
        real.mkdir()
        (real / 'current.json').write_text('owned')
        (tmp_path / 'linked').symlink_to(real, target_is_directory=True)
        name = 'linked/current.json'
    else:
        (tmp_path / 'linked.json').symlink_to(tmp_path / 'current.json')
        name = 'linked.json'
    report['artifact_sha256'] = {name: probe.digest(tmp_path / name)}
    with pytest.raises(AssertionError, match='symlink'):
        probe.validate_subreport(tmp_path, report)


def test_public_export_excludes_private_storage_and_credentials(tmp_path):
    for name in ('report.json', 'safe.json', '.private/raw.sqlite3', '.owned/raw.sqlite3', '.security/token'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    assert [path.name for path in probe.public_files(tmp_path)] == ['safe.json']


def state_fixture():
    return {'run': {'state': 'PAUSED', 'state_version': 2}, 'task_events': [
        {'event_type': 'state_changed', 'state_version': 1, 'payload_json': '{"current_state":"RUNNING"}'},
        {'event_type': 'state_changed', 'state_version': 2, 'payload_json': '{"current_state":"PAUSED"}'}],
        'run_results': [], 'integrity': 'ok', 'foreign_keys': []}


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_state', 'duplicate_result', 'integrity', 'foreign_keys'])
def test_atomic_state_event_guard_rejects_incomplete_or_duplicate_business_heads(mutation):
    value = state_fixture()
    probe.assert_state_events(value)
    if mutation == 'missing':
        value['task_events'].pop(0)
    elif mutation == 'duplicate':
        value['task_events'].append(deepcopy(value['task_events'][1]))
    elif mutation == 'wrong_state':
        value['run']['state'] = 'SUCCEEDED'
    elif mutation == 'duplicate_result':
        value['run_results'] = [{}]
        value['task_events'] += [{'event_type': 'result_ready'}, {'event_type': 'result_ready'}]
    elif mutation == 'integrity':
        value['integrity'] = 'corrupt'
    else:
        value['foreign_keys'] = [['broken']]
    with pytest.raises(AssertionError):
        probe.assert_state_events(value)


@pytest.mark.parametrize('point', probe.API_POINTS)
def test_api_fault_marker_wraps_exactly_one_real_commit_with_correct_durability(tmp_path, monkeypatch, point):
    db_path = tmp_path / 'owned.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE run_controls(idempotency_key TEXT)')
    stopped = []
    def stop(pid, sig):
        marker = json.loads((tmp_path / 'api-boundary.json').read_text())
        assert marker == {'stage': point, 'pid': pid}
        assert sig == signal.SIGSTOP
        # An independent reader sees neither row before COMMIT, and the row
        # after COMMIT; it does not use the writer's provisional snapshot.
        with sqlite3.connect(db_path) as reader:
            count = reader.execute('SELECT count(*) FROM run_controls').fetchone()[0]
        stopped.append(count)
    monkeypatch.setattr(probe.os, 'kill', stop)
    commits = []
    @contextmanager
    def real_transaction(db):
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
        commits.append('commit')
    fault = probe.ApiCommitFault(tmp_path, point, 'fixed-cancel')
    with sqlite3.connect(db_path) as db:
        with fault.wrap(real_transaction)(db):
            db.execute('INSERT INTO run_controls VALUES (?)', ('fixed-cancel',))
        with fault.wrap(real_transaction)(db):
            pass
    assert commits == ['commit', 'commit']
    assert stopped == [0 if point == 'api-before-commit' else 1]


def test_fault_constructor_rejects_arbitrary_points_and_never_hits_unrelated_request(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        probe.ApiCommitFault(tmp_path, 'arbitrary-code', 'owned')
    calls = []
    monkeypatch.setattr(probe.os, 'kill', lambda *args: calls.append(args))
    fault = probe.ApiCommitFault(tmp_path, 'api-before-commit', 'cancel-only')
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE run_controls(idempotency_key TEXT)')
        @contextmanager
        def real_transaction(db):
            yield db
            db.commit()
        with fault.wrap(real_transaction)(db):
            db.execute('INSERT INTO run_controls VALUES (?)', ('different-start',))
    assert not calls and not (tmp_path / 'api-boundary.json').exists()


def test_preserved_evidence_guard_rejects_reset_rewrite_or_disappearance():
    before = {'evidence-original': {'recorded_sha256': 'a' * 64, 'actual_sha256': 'a' * 64}}
    probe.assert_evidence_preserved(before, deepcopy(before))
    for after in ({}, {'evidence-original': {'recorded_sha256': 'b' * 64, 'actual_sha256': 'b' * 64}}):
        with pytest.raises(AssertionError):
            probe.assert_evidence_preserved(before, after)


def test_suite_stops_at_first_current_subprobe_failure_and_preserves_cross_summary(tmp_path, monkeypatch):
    called = []
    async def cross(output, private, report, **kwargs):
        report.update(passed=True, checks=[{'name': 'same-run', 'passed': True}])
    async def runner(output, private, group, **kwargs):
        called.append(group[0])
        raise AssertionError('first owned subprobe failure')
    monkeypatch.setattr(probe, 'cross_layer', cross)
    report = {'passed': False}
    with pytest.raises(AssertionError):
        asyncio.run(probe.run_suite(tmp_path, report, runner=runner))
    assert called == ['recovery']
    assert report['passed'] is False
    assert json.loads((tmp_path / 'same-run-summary.json').read_text())['passed'] is True
    assert (tmp_path / '.private').stat().st_mode & 0o777 == 0o700
    assert (tmp_path / '.owned').stat().st_mode & 0o777 == 0o700


def test_process_identity_guard_does_not_signal_reused_or_zombie_pid():
    inventory = {100: {'parent': 1, 'birth': 'owned-start', 'state': 'S'},
                 101: {'parent': 100, 'birth': 'child-start', 'state': 'R'},
                 102: {'parent': 101, 'birth': 'browser-start', 'state': 'Z'}}
    owned = probe.descendants(inventory, 100)
    assert probe.live_owned(inventory, owned) == [100, 101]
    inventory[101]['birth'] = 'unrelated-new-process'
    assert probe.live_owned(inventory, owned) == [100]


def test_fixed_scope_retains_partial_framework_requirements_and_never_recurses_check():
    assert all(script != 'check.sh' for _, script, _ in probe.GROUPS)
    assert probe.SCOPE['FR-04']['status'] == probe.SCOPE['FR-05']['status'] == 'M1_partial'
    assert probe.SCOPE['FR-04']['pending'] and probe.SCOPE['FR-05']['pending']
    assert 'disk-error' in probe.RECOVERY_CASES and 'unknown-write' in probe.RECOVERY_CASES


@pytest.mark.parametrize('field', ['actions_used', 'content_pages_used', 'observations_used', 'screenshots_used',
                                  'model_calls_used', 'active_ms', 'ci_wait_ms', 'remaining_actions'])
def test_workspace_budget_guard_rejects_projected_counter_or_remaining_reset(field):
    durable = {'actions_used': 2, 'content_pages_used': 1, 'observations_used': 3,
               'screenshots_used': 1, 'model_calls_used': 2, 'active_ms': 4500, 'ci_wait_ms': 0}
    limits = {'max_actions': 12, 'max_content_pages': 5, 'max_active_seconds': 120, 'max_ci_wait_seconds': 10}
    workspace = {'budget': {**durable, 'initialized': True, 'limits': limits, 'remaining_actions': 10,
                            'remaining_content_pages': 4, 'remaining_active_ms': 115500, 'remaining_ci_wait_ms': 10000}}
    probe.assert_workspace_budget(workspace, durable, limits)
    workspace['budget'][field] += 1
    with pytest.raises(AssertionError):
        probe.assert_workspace_budget(workspace, durable, limits)


def test_cross_layer_retains_primary_failure_when_fixture_cleanup_also_fails(tmp_path, monkeypatch):
    import verify_controls
    class FirstFailure(RuntimeError):
        pass
    class CleanupFailure(RuntimeError):
        pass
    async def start(self):
        raise FirstFailure('owned first failure')
    async def close(self):
        raise CleanupFailure('owned cleanup failure')
    monkeypatch.setattr(verify_controls.ControlsFixture, 'start', start)
    monkeypatch.setattr(verify_controls.ControlsFixture, 'close', close)
    report = {'passed': False, 'checks': []}
    with pytest.raises(FirstFailure):
        asyncio.run(probe.cross_layer(tmp_path, tmp_path / '.private-owned', report))
    assert report['primary_failure']['type'] == 'FirstFailure'
    assert 'CleanupFailure' in report['secondary_failures']
    assert report['passed'] is False
    assert (tmp_path / 'owned-lifecycle.json').is_file()


@pytest.mark.parametrize('missing_case', [False, True])
def test_fixed_write_group_requires_every_case_and_named_outcome(tmp_path, missing_case):
    cases = probe.WRITE_CASES
    matrix = [{'case': case, 'operation_status': 'CONFIRMED' if case in (
        'intent-not-applied', 'physical-applied', 'cancelled-write') else 'UNKNOWN'} for case in cases]
    names = {case + suffix for case in cases for suffix in (
        '_has_one_committed_intent', '_actual_crash_window', '_different_process_preserves_ledgers',
        '_each_actual_dispatch_has_an_independent_budget_debit')}
    for case in cases:
        names.add(case + ('_confirmed_without_duplicate_post' if case in (
            'intent-not-applied', 'physical-applied', 'cancelled-write') else '_unknown_never_replays_or_succeeds'))
    for case in ('intent-not-applied', 'physical-applied'):
        names |= {case + '_node_reentry_reuses_semantic_operation_without_write',
                  case + '_new_run_reuses_same_business_key_without_replay'}
    probe.write_json(tmp_path / 'matrix.json', matrix)
    report = {'checks': dict.fromkeys(names, True)}
    probe.validate_group_scope('writes', tmp_path, report)
    if missing_case:
        probe.write_json(tmp_path / 'matrix.json', matrix[:-1])
    else:
        report['checks'].pop('physical-unknown_unknown_never_replays_or_succeeds')
    with pytest.raises(AssertionError):
        probe.validate_group_scope('writes', tmp_path, report)


def test_explicit_summary_only_probe_still_requires_executed_checks(tmp_path):
    report = {'passed': True, 'checks': {'current_process_checks': True}, 'artifact_sha256': {}}
    assert probe.validate_subreport(tmp_path, report, summary_only=True)['checked_files'] == 0
    report['checks'] = {}
    with pytest.raises(AssertionError):
        probe.validate_subreport(tmp_path, report, summary_only=True)


@pytest.mark.parametrize('child_exited', [False, True])
def test_subprobe_exit_failure_propagates_even_when_orphan_is_no_longer_root_child(tmp_path, monkeypatch, child_exited):
    async def cross(output, private, report, **kwargs):
        report.update(passed=True, checks=[{'name': 'same-run', 'passed': True}], lifecycle={
            key: True for key in ('all_workers_exited', 'all_apis_exited', 'all_frontends_exited',
                                 'browser_disconnected', 'fixture_drained')})
    async def runner(output, private, group, **kwargs):
        probe.write_json(output / (group[0] + '-lifecycle.json'), {'all_observed_services_exited': child_exited})
        raise AssertionError('current subprobe failed')
    monkeypatch.setattr(probe, 'cross_layer', cross)
    report = {'passed': False}
    with pytest.raises(AssertionError):
        asyncio.run(probe.run_suite(tmp_path, report, runner=runner))
    assert report['all_owned_services_exited'] is child_exited


def test_main_preserves_failure_report_if_final_inventory_or_hashing_fails(tmp_path, monkeypatch):
    output = tmp_path / 'new-acceptance'
    async def suite(output, report, **kwargs):
        report.update(passed=True, all_owned_services_exited=True)
    def inventory():
        raise OSError('owned synthetic final inventory failure')
    monkeypatch.setattr(probe, 'run_suite', suite)
    monkeypatch.setattr(probe, 'process_inventory', inventory)
    monkeypatch.setattr(sys, 'argv', ['verify_fault_acceptance.py', '--output-dir', str(output)])
    assert probe.main() == 1
    report = json.loads((output / 'report.json').read_text())
    assert report['passed'] is False and report['finalization_failure']['type'] == 'OSError'
    assert report['artifact_sha256'] == {}


@pytest.mark.parametrize('killed,code,accepted', [(False, 0, True), (False, -signal.SIGTERM, True),
                                               (True, -signal.SIGKILL, True), (True, 0, False),
                                               (False, 3, False)])
def test_api_stop_awaits_normal_uvicorn_signal_exit_and_requires_real_fault_kill(killed, code, accepted):
    from types import SimpleNamespace
    class Child:
        returncode = None
        signaled = None
        awaited = False
        def terminate(self):
            self.signaled = signal.SIGTERM
        def kill(self):
            self.signaled = signal.SIGKILL
        async def wait(self):
            self.awaited = True
            self.returncode = code
    child = Child()
    if accepted:
        asyncio.run(probe.stop_api(SimpleNamespace(api=child), killed=killed))
    else:
        with pytest.raises(AssertionError):
            asyncio.run(probe.stop_api(SimpleNamespace(api=child), killed=killed))
    assert child.awaited and child.signaled == (signal.SIGKILL if killed else signal.SIGTERM)


@pytest.mark.parametrize('mutation', [None, 'cycle', 'foreign_run', 'missing'])
def test_original_evidence_resolves_actual_original_and_rejects_cycle_foreign_or_missing(mutation):
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE evidence(evidence_id TEXT,run_id TEXT,original_evidence_id TEXT,artifact_path TEXT,sha256 TEXT)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?,?,?)', [
            ('filtered', 'owned-run', 'raw', 'filtered.txt', 'f' * 64),
            ('raw', 'owned-run', None, 'raw.txt', 'a' * 64)])
        if mutation == 'cycle':
            db.execute("UPDATE evidence SET original_evidence_id='filtered' WHERE evidence_id='raw'")
        elif mutation == 'foreign_run':
            db.execute("UPDATE evidence SET run_id='different-run' WHERE evidence_id='raw'")
        elif mutation == 'missing':
            db.execute("DELETE FROM evidence WHERE evidence_id='raw'")
        if mutation:
            with pytest.raises(AssertionError):
                probe.original_evidence(db, 'filtered', 'owned-run')
        else:
            original, chain = probe.original_evidence(db, 'filtered', 'owned-run')
            assert original['evidence_id'] == 'raw' and original['original_evidence_id'] is None
            assert original['artifact_path'] == 'raw.txt' and original['sha256'] == 'a' * 64
            assert chain == ['filtered', 'raw']


@pytest.mark.parametrize('group', ['workbench', 'results'])
@pytest.mark.parametrize('missing', ['scan', 'files', 'hashes'])
def test_ui_group_scope_requires_nonempty_artifacts_and_an_executed_secret_scan(tmp_path, group, missing):
    report = {'checks': {'actual_ui': True}, 'secret_scan': {'passed': True, 'scanned_files': 1},
              'artifact_sha256': {'safe.txt': 'a' * 64}}
    probe.validate_group_scope(group, tmp_path, report)
    if missing == 'scan':
        report['secret_scan']['passed'] = False
    elif missing == 'files':
        report['secret_scan']['scanned_files'] = 0
    else:
        report['artifact_sha256'] = {}
    with pytest.raises(AssertionError):
        probe.validate_group_scope(group, tmp_path, report)


def test_executed_child_uses_owned_scope_so_production_export_and_secret_scan_do_real_work(tmp_path, monkeypatch):
    scripts = tmp_path / 'fixture-scripts'
    scripts.mkdir()
    child = scripts / 'owned_export_fixture.py'
    # This labelled unit child starts no app/browser. It uses the actual
    # existing export helpers to falsify the ancestor-directory regression.
    child.write_text("""import argparse, hashlib, json, sys
from pathlib import Path
sys.path.insert(0, """ + repr(str(ROOT / 'scripts/verification')) + """)
from verify_task_entry import public_artifacts, scan_secrets
parser = argparse.ArgumentParser()
parser.add_argument('--output-dir', type=Path)
output = parser.parse_args().output_dir
output.mkdir(parents=True)
(output / 'safe.txt').write_text('Owned safe unit artifact')
(output / '.private').mkdir()
(output / '.private/raw.txt').write_text('OWNED_UNIT_PRIVATE_CANARY')
(output / '.security').mkdir()
(output / '.security/token').write_text('OWNED_UNIT_PRIVATE_CANARY')
report = {'passed': True, 'checks': {'executed_export_fixture': True},
          'secret_scan': scan_secrets(output, {'owned': 'OWNED_UNIT_PRIVATE_CANARY'}),
          'artifact_sha256': {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in public_artifacts(output)}}
(output / 'report.json').write_text(json.dumps(report))
""")
    output = tmp_path / 'fresh-acceptance'
    output.mkdir()
    private = output / '.private'
    private.mkdir(mode=0o700)
    (output / '.owned').mkdir(mode=0o700)
    monkeypatch.setattr(probe, 'SCRIPTS', scripts)
    # The unit child launches no descendants; isolate the OS process-inventory
    # seam (which the restricted unit sandbox denies) while still executing
    # and awaiting this real Python child and production export helpers.
    monkeypatch.setattr(probe, 'process_inventory', lambda: {})
    result = asyncio.run(probe.run_group(output, private, ('results', child.name, ())))
    assert result['passed'] is True and result['checked_files'] == 1
    assert result['private_report'] == '.owned/results/report.json'
    actual = json.loads((output / result['private_report']).read_text())
    assert actual['secret_scan']['passed'] is True and actual['secret_scan']['scanned_files'] == 1
    assert set(actual['artifact_sha256']) == {'safe.txt'}
    assert all('.owned' not in path.relative_to(output).parts for path in probe.public_files(output))
