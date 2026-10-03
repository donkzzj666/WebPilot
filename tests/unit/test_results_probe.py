"""M1-22 acceptance fixtures must derive results and never export secrets."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'verification'))
probe = importlib.import_module('verify_results')


@pytest.mark.parametrize('method,path,status,route', [
    ('GET', '/v1/tasks/PRIVATE/results?api_key=PRIVATE', 200, 'task_results'),
    ('GET', '/v1/evidence/PRIVATE/content?path=PRIVATE', 409, 'evidence_content'),
    ('GET', '/v1/evidence/PRIVATE', 200, 'evidence_metadata'),
    ('GET', '/v1/runs/PRIVATE/result', 404, 'run_result'),
    ('DELETE', '/other/PRIVATE', 'PRIVATE', 'other'),
])
def test_summary_never_exports_identifiers_queries_unknown_methods_or_status(method, path, status, route):
    value = probe.request_summary(method, path, status)
    assert value['route'] == route
    assert 'PRIVATE' not in json.dumps(value)
    assert value['status'] == (status if type(status) is int else 0)


def test_scoped_source_preserves_disclosed_fields_but_isolates_fault_files():
    raw = b'{"values":[{"field_id":"revenue","raw_value":"10.00"}]}'
    first = probe.scoped_document(raw, 'first')
    second = probe.scoped_document(raw, 'second')
    assert first != second
    assert json.loads(first)['values'] == json.loads(second)['values'] == json.loads(raw)['values']
    assert raw == b'{"values":[{"field_id":"revenue","raw_value":"10.00"}]}'


@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp('owned-results-probe')
    fixture = probe.OwnedHTTPFixture()
    fixture.origin = 'http://127.0.0.1:18089'
    canaries = {'password': 'OWNED_PRIVATE_PASSWORD', 'provider': 'OWNED_PRIVATE_PROVIDER'}
    raw_document = json.loads(fixture.documents['/disclosure'])
    raw_document['private_debug'] = canaries
    raw = probe.canonical_json(raw_document).encode()
    cases = asyncio.run(probe.prepare_cases(directory, fixture, raw, fixture.documents['/conflict'], canaries))
    return directory, cases, canaries


def test_prepared_results_are_production_aggregates_with_four_real_verdicts(prepared):
    directory, cases, _ = prepared
    assert cases['positive']['outcome'] == cases['assisted']['outcome'] == 'SUCCEEDED'
    assert cases['partial']['outcome'] == 'PARTIAL'
    assert cases['failed']['outcome'] == 'FAILED'
    assert cases['unknown']['outcome'] != 'SUCCEEDED'
    assert {verdict for case in cases.values() for verdict in case.get('verdicts', [])} == {
        'PASS', 'FAIL', 'INSUFFICIENT', 'CONFLICT'}
    with probe.connect(directory / 'business.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM run_results').fetchone()[0] == 13
        assert db.execute('SELECT count(*) FROM run_verifications').fetchone()[0] == 13
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='result_ready'").fetchone()[0] == 13


def test_retry_history_retains_failed_first_result_and_links_second_success(prepared):
    directory, cases, _ = prepared
    retry = cases['retry']
    service = probe.VerificationService(directory)
    assert service.read(retry['run_id'])['result']['outcome'] == 'FAILED'
    assert service.read(retry['second_run_id'])['result']['outcome'] == 'SUCCEEDED'
    with probe.connect(directory / 'business.sqlite3') as db:
        row = db.execute('SELECT parent_run_id FROM runs WHERE run_id=?', (retry['second_run_id'],)).fetchone()
        assert row['parent_run_id'] == retry['run_id']


def test_history_fixture_forces_actual_pagination_with_only_one_current_aggregation(prepared):
    from webagent.results.store import results
    directory, cases, _ = prepared
    paging = cases['paging']
    first = results(directory, paging['task_id'])
    assert first['display_complete_success'] is True
    assert len(first['runs']) == 20 and first['next_cursor'] is not None
    second = results(directory, paging['task_id'], before=int(first['next_cursor']), run_id=paging['run_id'])
    assert len(second['runs']) == 2 and second['next_cursor'] is None
    assert second['selected_run']['run_id'] == first['selected_run']['run_id']
    assert len({row['run_id'] for row in first['runs'] + second['runs']}) == 22
    assert sum(row['has_result'] for row in first['runs'] + second['runs']) == 1


def test_corruption_and_missing_cases_cannot_damage_positive_original(prepared):
    directory, cases, _ = prepared
    store = probe.EvidenceStore(directory)
    positive = cases['positive']
    metadata, raw = store.read(positive['original_id'], run_id=positive['run_id'], allow_restricted=True)
    assert json.loads(raw)['owned_run_fixture'] == positive['run_id']
    assert (directory / cases['corrupt']['original_path']).read_bytes() == b'owned post-aggregation damaged artifact'
    assert not (directory / cases['insufficient']['original_path']).exists()
    assert metadata['artifact_path'] != cases['corrupt']['original_path']


def test_display_derivative_redacts_actual_original_secrets_without_changing_values(prepared):
    directory, cases, canaries = prepared
    store = probe.EvidenceStore(directory)
    positive = cases['positive']
    original, raw = store.read(positive['original_id'], run_id=positive['run_id'], allow_restricted=True)
    derivative, safe = store.read(positive['display_id'], run_id=positive['run_id'])
    assert all(value.encode() in raw and value.encode() not in safe for value in canaries.values())
    assert derivative['original_evidence_id'] == original['evidence_id']
    assert derivative['source_url'] == original['source_url']
    assert derivative['captured_at'] == original['captured_at']
    assert json.loads(raw)['values'] == json.loads(safe)['values']


def test_ledger_digest_covers_history_and_pending_write_fact(prepared):
    directory, cases, _ = prepared
    path = directory / 'business.sqlite3'
    before = probe.ledger_digest(path)
    assert before == probe.ledger_digest(path)
    with probe.connect(path) as db, probe.transaction(db):
        db.execute('UPDATE runs SET assistance_count=assistance_count+1 WHERE run_id=?',
                   (cases['not_ready']['run_id'],))
    assert probe.ledger_digest(path) != before
