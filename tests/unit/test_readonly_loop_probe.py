"""Falsify real-loop scope/export/unknown-cost guards without live calls."""
from copy import deepcopy
import asyncio
import importlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/verification'))
probe = importlib.import_module('verify_readonly_loop')


def contract():
    return {'scenario': 'research', 'action_policy': {'mode': 'read_only'}, 'identity_ref': None,
        'memory_mode': 'disabled', 'snapshot_id': None, 'start_urls': [probe.SOURCE_URL],
        'sources': [{'source_id': probe.SOURCE_ID, 'site_id': 'arxiv', 'origin': 'https://arxiv.org',
                     'path_prefix': probe.SOURCE_PATH}],
        'parameters': {'queries': [probe.QUERY], 'topic_criteria': [probe.TOPIC],
                       'cutoff_at': probe.CUTOFF, 'max_items': 1},
        'original_instruction': probe.INSTRUCTION, 'provenance': [{'origin': 'user', 'authorizes_execution': True}]}


@pytest.mark.parametrize('mutation', ['write', 'identity', 'memory', 'snapshot', 'url', 'source_count',
    'source_origin', 'source_prefix', 'source_id', 'query', 'topic', 'limit', 'cutoff', 'instruction', 'evaluator', 'fixture'])
def test_contract_guard_rejects_scope_expansion_or_substituted_creation(mutation):
    value = contract()
    probe.assert_contract(value)
    if mutation == 'write': value['action_policy']['mode'] = 'repository_write'
    elif mutation == 'identity': value['identity_ref'] = 'account'
    elif mutation == 'memory': value['memory_mode'] = 'evaluation_snapshot'
    elif mutation == 'snapshot': value['snapshot_id'] = 'answers'
    elif mutation == 'url': value['start_urls'] = ['https://arxiv.org/search']
    elif mutation == 'source_count': value['sources'] += deepcopy(value['sources'])
    elif mutation == 'source_origin': value['sources'][0]['origin'] = 'http://127.0.0.1:8765'
    elif mutation == 'source_prefix': value['sources'][0]['path_prefix'] = '/'
    elif mutation == 'source_id': value['sources'][0]['source_id'] = 'fixture'
    elif mutation == 'query': value['parameters']['queries'] += ['other']
    elif mutation == 'topic': value['parameters']['topic_criteria'] = ['model invented broader topic']
    elif mutation == 'limit': value['parameters']['max_items'] = 25
    elif mutation == 'cutoff': value['parameters']['cutoff_at'] = '2026-10-02T00:00:00Z'
    elif mutation == 'instruction': value['original_instruction'] = 'seeded'
    elif mutation == 'evaluator': value['provenance'][0]['origin'] = 'evaluation_manifest'
    else: value['provenance'][0]['origin'] = 'explicit_test_configuration'
    with pytest.raises(probe.ProbeFailure): probe.assert_contract(value)


def success():
    paper = {field: 'test source-bound field' for field in probe.METADATA_FIELDS}
    paper.update(version='v1', source_url=probe.SOURCE_URL, claims=[], relations=[], authors=['test author'], revised_at=None)
    fields = [{'result_path': '/publications/0/' + field, 'verdict': 'PASS', 'evidence_ids': ['original']}
              for field in probe.METADATA_FIELDS if field != 'authors']
    fields.append({'result_path': '/publications/0/authors/0', 'verdict': 'PASS', 'evidence_ids': ['original']})
    return {'selected_run': {'run_id': 'same', 'state': 'SUCCEEDED'}, 'result_status': 'AVAILABLE',
        'display_complete_success': True, 'display_blockers': [], 'write_intents': [], 'pending_write_count': 0,
        'result': {'outcome': 'SUCCEEDED', 'coverage': {'complete': True}, 'unresolved': [], 'side_effects': [],
                   'checks': [{'verdict': 'PASS'}], 'items': {'scenario': 'research', 'publications': [paper]}},
        'field_checks': fields}


@pytest.mark.parametrize('mutation', ['run', 'state', 'unavailable', 'display', 'blockers', 'outcome', 'coverage',
    'unresolved', 'write', 'pending', 'effect', 'zero_items', 'extra_item', 'missing_metadata', 'wrong_version',
    'wrong_source', 'claim', 'relation', 'no_checks', 'field_fail', 'missing_binding', 'missing_authors', 'criterion'])
def test_success_requires_same_real_run_readable_original_bound_metadata(mutation):
    value = success()
    assert probe.assert_result(value, 'same')['revised_at'] is None
    final, item = value['result'], value['result']['items']['publications'][0]
    if mutation == 'run': value['selected_run']['run_id'] = 'seeded'
    elif mutation == 'state': value['selected_run']['state'] = 'PARTIAL'
    elif mutation == 'unavailable': value['result_status'] = 'UNAVAILABLE'
    elif mutation == 'display': value['display_complete_success'] = False
    elif mutation == 'blockers': value['display_blockers'] = ['evidence_not_readable']
    elif mutation == 'outcome': final['outcome'] = 'FAILED'
    elif mutation == 'coverage': final['coverage']['complete'] = False
    elif mutation == 'unresolved': final['unresolved'] = ['insufficient']
    elif mutation == 'write': value['write_intents'] = [{}]
    elif mutation == 'pending': value['pending_write_count'] = 1
    elif mutation == 'effect': final['side_effects'] = [{}]
    elif mutation == 'zero_items': final['items']['publications'] = []
    elif mutation == 'extra_item': final['items']['publications'].append(deepcopy(item))
    elif mutation == 'missing_metadata': del item['authors']
    elif mutation == 'wrong_version': item['version'] = 'v2'
    elif mutation == 'wrong_source': item['source_url'] = 'https://arxiv.org/abs/2307.13854'
    elif mutation == 'claim': item['claims'] = [{'statement': 'not requested'}]
    elif mutation == 'relation': item['relations'] = [{'target_id': 'not requested'}]
    elif mutation == 'no_checks': value['field_checks'] = []
    elif mutation == 'field_fail': value['field_checks'][0]['verdict'] = 'INSUFFICIENT'
    elif mutation == 'missing_binding': value['field_checks'][0]['evidence_ids'] = []
    elif mutation == 'missing_authors': value['field_checks'].pop()
    else: final['checks'][0]['verdict'] = 'CONFLICT'
    with pytest.raises(probe.ProbeFailure): probe.assert_result(value, 'same')


def test_unpriced_and_unknown_provider_calls_do_not_become_zero_cost():
    compilation = {'call_id': 'compile', 'status': 'SUCCEEDED', 'usage_json': '{"input_tokens":2}', 'duration_ms': 1}
    unfinished = {'call_id': 'generation', 'request_id': 'request', 'status': 'STARTED', 'attempt_number': 1,
                  'record_json': None}
    value = probe.accounting([compilation], [unfinished])
    assert value['provider_requests_reserved'] == 2
    assert value['unknown_usage_records'] == 1
    assert value['unknown_cost_records'] == 2
    assert value['estimated_subtotals'] == {} and value['total_cost_known'] is False
    assert all(row['estimated_cost'] is None for row in value['records'])


def test_priced_subtotals_keep_currency_and_exclude_unknown_preparation():
    compilation = {'call_id': 'compile', 'status': 'SUCCEEDED', 'usage_json': '{}'}
    attempts = []
    for index, (amount, currency) in enumerate([('0.5', 'CNY'), ('1.2', 'USD'), ('0.7', 'CNY')]):
        attempts.append({'call_id': str(index), 'request_id': str(index), 'status': 'VALID', 'attempt_number': 1,
            'record_json': json.dumps({'usage': {}, 'estimated_cost': amount, 'cost_currency': currency, 'price_version': 'v1'})})
    value = probe.accounting([compilation], attempts)
    assert value['estimated_subtotals'] == {'CNY': '1.2', 'USD': '1.2'}
    assert value['unknown_cost_records'] == 1 and value['total_cost_known'] is False


def test_public_export_excludes_private_logs_database_and_tokens(tmp_path):
    for name in ['safe.json', 'report.json', '.private/worker.log', '.owned/data.sqlite3', '.security/token']:
        path = tmp_path / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('secret')
    assert [path.name for path in probe.public_files(tmp_path)] == ['safe.json']
    scan = probe.scan_exports(tmp_path, {'provider_key': 'secret'}, report={'public': 'secret'})
    assert scan['passed'] is False and scan['scanned_files'] == 2
    assert scan['matches'] == [{'artifact': 'safe.json', 'secret_names': ['provider_key']},
                               {'artifact': 'report.json', 'secret_names': ['provider_key']}]
    assert 'secret' not in json.dumps(scan).replace('secret_names', '').replace('secret_canaries', '')


@pytest.mark.parametrize('mutation', ['bytes', 'missing', 'traversal', 'absolute', 'symlink'])
def test_original_artifact_hash_guard_rejects_replacement_escape_or_missing_bytes(tmp_path, mutation):
    original = tmp_path / 'artifacts/text.json'
    original.parent.mkdir(); original.write_text('{"public_source":true}')
    row = {'artifact_path': 'artifacts/text.json', 'sha256': probe.digest(original), 'evidence_id': 'original',
           'source_url': probe.SOURCE_URL, 'original_evidence_id': None}
    assert probe.evidence_manifest(tmp_path, [row])[0]['actual_sha256'] == row['sha256']
    if mutation == 'bytes': original.write_text('damaged')
    elif mutation == 'missing': original.unlink()
    elif mutation == 'traversal': row['artifact_path'] = '../escape'
    elif mutation == 'absolute': row['artifact_path'] = str(original)
    else:
        link = original.parent / 'linked.json'; link.symlink_to(original); row['artifact_path'] = 'artifacts/linked.json'
    with pytest.raises(probe.ProbeFailure): probe.evidence_manifest(tmp_path, [row])


def prepared(tmp_path):
    data = tmp_path / '.private/data'; data.mkdir(parents=True)
    model = {'provider': 'deepseek', 'model_id': 'deepseek-flash', 'base_url': 'https://api.deepseek.com'}
    raw_model = json.dumps(model)
    raw_runtime = '{"settings_version":1}'
    with sqlite3.connect(data / 'business.sqlite3') as db:
        for table in ('tasks', 'contracts', 'runs', 'task_compilations', 'model_generations', 'model_attempts'):
            db.execute('CREATE TABLE ' + table + '(id TEXT)')
        db.execute('CREATE TABLE scheduler_workers(state TEXT)')
        db.execute('CREATE TABLE model_settings_versions(version INTEGER,model_json TEXT,runtime_json TEXT,'
            'model_config_sha256 TEXT,runtime_config_sha256 TEXT,credential_ref TEXT,disclosure_version TEXT)')
        import hashlib
        db.execute('INSERT INTO model_settings_versions VALUES(1,?,?,?,?,?,?)', (raw_model, raw_runtime,
            hashlib.sha256(raw_model.encode()).hexdigest(), hashlib.sha256(raw_runtime.encode()).hexdigest(),
            'opaque-reference', 'model-data-v1'))
    return data


@pytest.mark.parametrize('mutation', ['task', 'run', 'compilation', 'worker', 'provider', 'model', 'url', 'hash', 'credential'])
def test_preflight_rejects_seeded_or_already_running_domains_and_fixture_providers(tmp_path, mutation):
    data = prepared(tmp_path)
    assert probe.preflight(data)['settings_version'] == 1
    with sqlite3.connect(data / 'business.sqlite3') as db:
        if mutation in ('task', 'run', 'compilation'):
            table = {'task': 'tasks', 'run': 'runs', 'compilation': 'task_compilations'}[mutation]
            db.execute('INSERT INTO ' + table + ' VALUES(?)', ('seeded',))
        elif mutation == 'worker': db.execute("INSERT INTO scheduler_workers VALUES('ACTIVE')")
        elif mutation == 'hash': db.execute("UPDATE model_settings_versions SET model_config_sha256='wrong'")
        elif mutation == 'credential': db.execute('UPDATE model_settings_versions SET credential_ref=NULL')
        else:
            row = db.execute('SELECT model_json FROM model_settings_versions').fetchone()
            value = json.loads(row[0]); value[{'provider':'provider','model':'model_id','url':'base_url'}[mutation]] = 'fixture'
            db.execute('UPDATE model_settings_versions SET model_json=?', (json.dumps(value),))
    with pytest.raises(probe.ProbeFailure): probe.preflight(data)


@pytest.mark.parametrize('mutation', ['default', 'nested_default', 'symlink', 'existing_output', 'overlap'])
def test_default_data_symlinks_or_reused_output_are_rejected(tmp_path, monkeypatch, mutation):
    fake_root = tmp_path / 'repo'; fake_root.mkdir(); monkeypatch.setattr(probe, 'ROOT', fake_root)
    data = prepared(tmp_path); output = tmp_path / 'public-new'
    if mutation in ('default', 'nested_default'):
        data = fake_root / ('data/subdir' if mutation == 'nested_default' else 'data')
        data.mkdir(parents=True)
    elif mutation == 'symlink':
        linked = tmp_path / 'linked'; linked.symlink_to(data, target_is_directory=True); data = linked
    elif mutation == 'existing_output': output.mkdir()
    else: output = data / 'new'
    with pytest.raises(probe.ProbeFailure): probe.validate_paths(data, output)


def test_sandbox_profile_excludes_all_preparation_and_old_acceptance_without_reading_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    for name in ['M1-01', 'M1-24', 'M1-25', 'M1-foo']:
        (tmp_path / 'artifacts/verification' / name).mkdir(parents=True)
    profile, excluded = probe.sandbox_profile()
    assert excluded == [tmp_path / 'docs/m0', tmp_path / '.git',
        tmp_path / 'artifacts/verification/M1-01', tmp_path / 'artifacts/verification/M1-24']
    assert profile.count('(deny file-read*') == 4
    assert str(tmp_path / 'artifacts/verification/M1-25') not in profile
    assert '(allow default)' in profile


def test_child_environment_removes_arbitrary_hooks_proxy_and_disables_all_tracing(tmp_path, monkeypatch):
    for name in ('NODE_OPTIONS', 'PYTHONSTARTUP', 'HTTPS_PROXY', 'http_proxy'):
        monkeypatch.setenv(name, 'untrusted-hook')
    for flag in probe.TRACE_FLAGS: monkeypatch.setenv(flag, 'true')
    value = probe.child_environment(tmp_path, 12345, 12346)
    assert not any(name in value for name in ('NODE_OPTIONS', 'PYTHONSTARTUP', 'HTTPS_PROXY', 'http_proxy'))
    assert all(value[flag] == 'false' for flag in probe.TRACE_FLAGS)
    assert value['WEBAGENT_DATA_DIR'] == str(tmp_path)


def test_later_secret_scan_or_cleanup_failure_never_replaces_first_production_failure():
    value = {'secondary_errors': []}
    probe.preserve_failure(value, probe.ProbeFailure('ui_natural_compilation_failed'), 'production-ui-loop')
    first = deepcopy(value['error'])
    probe.preserve_failure(value, probe.ProbeFailure('public_export_secret_scan_failed'), 'secret-scan')
    probe.preserve_failure(value, probe.ProbeFailure('owned_descendant_cleanup_leak'), 'descendant-cleanup')
    assert value['error'] == first
    assert [row['reason'] for row in value['secondary_errors']] == ['public_export_secret_scan_failed', 'owned_descendant_cleanup_leak']


def test_cleanup_signals_only_witnessed_same_birth_descendants_and_proves_their_exit(monkeypatch):
    inventory = {101: {'parent': 1, 'birth': 'owned', 'state': 'S'},
                 102: {'parent': 1, 'birth': 'reused-new', 'state': 'S'},
                 103: {'parent': 1, 'birth': 'unrelated', 'state': 'S'},
                 104: {'parent': 1, 'birth': 'zombie', 'state': 'Z'}}
    monkeypatch.setattr(probe, 'process_inventory', lambda: deepcopy(inventory))
    calls = []
    def kill(pid, sig):
        calls.append((pid, sig)); inventory.pop(pid)
    monkeypatch.setattr(probe.os, 'kill', kill)
    result = asyncio.run(probe.settle_witnessed_children({101:'owned', 102:'reused-old', 104:'zombie'}))
    assert [pid for pid, _ in calls] == [101]
    assert result == {'observed_descendants': 3, 'left_alive_after_normal_cleanup': 1,
                      'all_observed_descendants_exited': True}
    assert 102 in inventory and 103 in inventory


def test_live_descendant_after_scoped_cleanup_blocks_artifact_hash_eligibility(monkeypatch):
    inventory = {101: {'parent': 1, 'birth': 'owned', 'state': 'S'}}
    monkeypatch.setattr(probe, 'process_inventory', lambda: inventory)
    calls = []
    monkeypatch.setattr(probe.os, 'kill', lambda *args: calls.append(args))
    counter = iter(range(0, 100, 4))
    monkeypatch.setattr(probe.time, 'monotonic', lambda: next(counter))
    with pytest.raises(probe.ProbeFailure, match='owned_descendant_process_still_live'):
        asyncio.run(probe.settle_witnessed_children({101:'owned'}))
    assert len(calls) == 2 and all(pid == 101 for pid, _ in calls)


@pytest.mark.parametrize('mutation', ['file_not_denied', 'git_not_denied', 'history_not_denied', 'control_denied', 'child_error'])
def test_actual_os_boundary_probe_must_deny_files_git_and_history_and_allow_runtime_source(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    for path in ['docs/m0/evaluation/task-cards.json', 'docs/m0/evaluation/evaluator-only/README.md',
                 'backend/webagent/config.py']:
        file = tmp_path / path; file.parent.mkdir(parents=True, exist_ok=True); file.touch()
    (tmp_path / '.git').mkdir()
    (tmp_path / 'artifacts/verification/M1-24').mkdir(parents=True)
    profile = tmp_path / 'boundary.sb'; profile.write_text(probe.sandbox_profile()[0])
    class Result:
        returncode = 0
        stdout = json.dumps({'denied': [True, True, True, True], 'runtime_source_readable': True})
    def run(command, **kwargs):
        assert command[:3] == ['/usr/bin/sandbox-exec', '-f', str(profile)]
        assert '-c' in command and 'handle.read(1)' in command[5]
        assert 'handle.read(1)' not in command[-1]
        return Result()
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.sandbox_read_guard(profile, {})['git_directory_read_denied'] is True
    data = {'denied': [True, True, True, True], 'runtime_source_readable': True}
    if mutation == 'file_not_denied': data['denied'][0] = False
    elif mutation == 'git_not_denied': data['denied'][2] = False
    elif mutation == 'history_not_denied': data['denied'][3] = False
    elif mutation == 'control_denied': data['runtime_source_readable'] = False
    else: Result.returncode = 1
    Result.stdout = json.dumps(data)
    with pytest.raises(probe.ProbeFailure): probe.sandbox_read_guard(profile, {})
