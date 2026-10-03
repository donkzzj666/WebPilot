"""Actual service, immutable artifacts, model journal and terminal aggregation."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from webagent.db import connect, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task
from webagent.errors import BusinessError
from webagent.models.journal import list_attempts
from webagent.models.schema import ProposeResult
from webagent.models.transport import ModelConfig, ProviderReply
from webagent.state import transition
from webagent.tasks.compiler import compile_draft
from webagent.tasks.models import TaskContract
from webagent.verification.models import FieldBinding
from webagent.verification.service import VerificationService

NOW = '2026-09-29T00:00:00Z'


class SemanticProvider:
    config = ModelConfig()
    sensitive_literals = ('integration-verifier-private-key',)

    def __init__(self, during_call=None, verdict='PASS'):
        self.calls = []
        self.during_call = during_call
        self.verdict = verdict

    async def complete_verification(self, payload, schema, repair_errors=None):
        self.calls.append(deepcopy(payload))
        if self.during_call is not None:
            await self.during_call()
        diagnostic = {'PASS': 'supported', 'FAIL': 'contradicted', 'INSUFFICIENT': 'missing_support', 'CONFLICT': 'conflicting_sources'}[self.verdict]
        return ProviderReply(canonical_json({'checks': [{'criterion_id': c['criterion_id'], 'verdict': self.verdict,
            'evidence_ids': [e['evidence_id'] for e in payload['untrusted_evidence']], 'actual': {'code': diagnostic}}
            for c in payload['frozen_requirements']['criteria']]}))


def leaves(value, path=''):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            if key not in {'evidence_ids', 'topic_basis', 'scenario'}:
                yield from leaves(child, path + '/' + key.replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list) and value:
        for i, child in enumerate(value):
            yield from leaves(child, path + '/' + str(i))
    else:
        yield path


@pytest.fixture
def prepared(database):
    contract_body = compile_draft({'instruction': 'Find publications on fixture evidence', 'scenario': 'research',
        'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixture evidence'],
            'topic_criteria': ['fixture evidence'], 'cutoff_at': NOW, 'max_items': 3}},
        task_id='task-1', version=1, created_at=NOW, provenance=[{'origin': 'api', 'reference': 'test-request',
            'content_sha256': 'a'*64, 'authorizes_execution': True}]).contract
    contract_body['budget_profile']['max_model_format_repairs'] = 0
    contract = TaskContract.model_validate_json(canonical_json(contract_body))
    with connect(database) as db, transaction(db):
        create_task(db, task_id='task-1', instruction=contract.original_instruction, requested_fields=['contract'])
        add_contract(db, contract_body)
        create_run(db, run_id='run-1', task_id='task-1', contract_version=1,
                   graph_version='graph-v1', graph_state_schema_version='state-v1',
                   model_config_sha256=ModelConfig().config_sha256, runtime_config_sha256='b'*64)
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES (?,?)', ('budget-1', 'run-1'))
    transition(database, run_id='run-1', expected_state_version=0, target='RUNNING')
    service = VerificationService(database.parent)
    coverage = {'searched_sources': ['local-fixture'], 'queries': ['fixture evidence'], 'cutoff_at': NOW,
                'content_pages': 1, 'unread_candidates': [], 'gaps': [], 'complete': True}
    original = {'scenario': 'research', 'publications': [{'canonical_id': 'paper-1', 'version': 'v1',
        'title': 'A study of fixture evidence', 'authors': ['Example Author'], 'first_published_at': NOW,
        'revised_at': None, 'source_url': contract.start_urls[0],
        'claims': [{'statement': 'The study measures fixture evidence.'}], 'relations': []}], 'coverage': coverage}
    item = service.evidence.publish('run-1', canonical_json(original).encode(), evidence_id='evidence-1',
        source_url=contract.start_urls[0], captured_at=NOW, object_id=contract.targets[0].object_id,
        query_scope='fixture research', locator_or_page='original structured publication',
        sensitivity='public', redaction_status='FILTERED', policy_version='fixture-v1')
    candidate = deepcopy(original)
    candidate.pop('coverage')
    candidate['publications'][0].update(evidence_ids=['evidence-1'], topic_basis='EXECUTOR_SELF_ASSESSMENT')
    candidate['publications'][0]['claims'][0]['evidence_ids'] = ['evidence-1']
    proposal = ProposeResult.model_validate_json(canonical_json({'type': 'ProposeResult', 'items': candidate,
        'coverage': coverage, 'evidence_ids': ['evidence-1'], 'unresolved': [], 'existing_operation_ids': []}))
    bindings = [FieldBinding(result_path=path, evidence_id='evidence-1', evidence_path=path)
                for path in list(leaves(candidate)) + list(leaves(coverage, '/coverage'))]
    service.begin('run-1', 1)
    return service, contract, proposal, bindings, item, original


def verify(case, provider):
    service, _, proposal, bindings, *_ = case
    return asyncio.run(service.verify('run-1', proposal, bindings, expected_state_version=2, provider=provider))


def state(service):
    with connect(service.database) as db:
        return db.execute('SELECT state FROM runs WHERE run_id=?', ('run-1',)).fetchone()[0]


def counts(service):
    with connect(service.database) as db:
        return tuple(db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('run_verifications', 'run_results'))


def test_independent_semantic_and_rules_produce_atomic_success_without_self_evaluation(prepared):
    service, _, _, _, _, _ = prepared
    provider = SemanticProvider()
    record = verify(prepared, provider)
    result = service.finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == state(service) == 'SUCCEEDED'
    assert result.assistance_count == 0 and counts(service) == (1, 1)
    assert all(check.verdict.value == 'PASS' for check in result.checks)
    assert 'EXECUTOR_SELF_ASSESSMENT' not in canonical_json(provider.calls)
    assert provider.calls[0]['untrusted_evidence'][0]['content']['publications'][0]['title'] == 'A study of fixture evidence'
    assert len(list_attempts(service.database, 'run-1')) == 1
    with connect(service.database) as db:
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='result_ready'").fetchone()[0] == 1
    assert service.finalize(record['verification_id'], expected_state_version=2) == result


def test_semantic_pass_cannot_cover_missing_field_binding(prepared):
    service, contract, proposal, bindings, item, original = prepared
    bindings = [b for b in bindings if b.result_path != '/publications/0/authors/0']
    record = verify((service, contract, proposal, bindings, item, original), SemanticProvider())
    result = service.finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'FAILED'
    assert any('authors/0' in item for item in result.unresolved)
    assert next(c for c in result.checks if c.criterion_id == 'topic_scope').verdict.value == 'INSUFFICIENT'
    fields = service.read('run-1')['field_checks']
    assert next(f for f in fields if f['result_path'].endswith('/authors/0'))['verdict'] == 'INSUFFICIENT'


def test_semantic_pass_cannot_overwrite_rule_fail_on_original_value(prepared):
    service, contract, proposal, bindings, item, original = prepared
    changed = proposal.model_dump(mode='json')
    changed['items']['publications'][0]['title'] = 'Invented publication title'
    proposal = ProposeResult.model_validate_json(canonical_json(changed))
    provider = SemanticProvider()
    record = verify((service, contract, proposal, bindings, item, original), provider)
    result = service.finalize(record['verification_id'], expected_state_version=2)
    assert provider.calls and result.outcome == 'FAILED'
    assert next(c for c in result.checks if c.criterion_id == 'topic_scope').verdict.value == 'FAIL'
    assert any('field_fail:/publications/0/title' == item for item in result.unresolved)


@pytest.mark.parametrize('target', ['PAUSED', 'CANCELLED'])
def test_pause_or_cancel_during_semantic_call_discards_verification(prepared, target):
    service = prepared[0]
    async def interrupt():
        if target == 'PAUSED':
            transition(service.database, run_id='run-1', expected_state_version=2, target='RUNNING')
            transition(service.database, run_id='run-1', expected_state_version=3, target='PAUSED')
        else:
            transition(service.database, run_id='run-1', expected_state_version=2, target=target)
    with pytest.raises(BusinessError) as caught:
        verify(prepared, SemanticProvider(interrupt))
    assert caught.value.code == 'STATE_CONFLICT'
    assert counts(service) == (0, 0) and state(service) == target
    assert list_attempts(service.database, 'run-1')[0]['status'] == 'CANCELLED'


@pytest.mark.parametrize('mode', ['tamper', 'remove', 'expire'])
def test_original_changed_during_semantic_io_is_rechecked_and_never_persisted(prepared, mode):
    service, _, _, _, item, _ = prepared
    async def change_original():
        path = service.data_dir / item['artifact_path']
        if mode == 'tamper':
            path.write_bytes(b'X' * item['size_bytes'])
        elif mode == 'remove':
            path.unlink()
        else:
            service.evidence.expire(item['evidence_id'])
    with pytest.raises(BusinessError) as caught:
        verify(prepared, SemanticProvider(change_original))
    assert caught.value.code == 'STATE_CONFLICT'
    assert counts(service) == (0, 0) and state(service) == 'VERIFYING'
    assert list_attempts(service.database, 'run-1')[0]['status'] == 'ERROR'


def test_original_changed_after_verification_is_rechecked_at_final_aggregation(prepared):
    service, _, _, _, item, _ = prepared
    record = verify(prepared, SemanticProvider())
    (service.data_dir / item['artifact_path']).write_bytes(b'X' * item['size_bytes'])
    with pytest.raises(BusinessError) as caught:
        service.finalize(record['verification_id'], expected_state_version=2)
    assert caught.value.code == 'STATE_CONFLICT'
    assert counts(service) == (1, 0) and state(service) == 'VERIFYING'


def test_semantic_provider_failure_retains_verified_fields_but_never_success(prepared):
    service = prepared[0]
    async def fail():
        raise RuntimeError('integration-verifier-private-key')
    record = verify(prepared, SemanticProvider(fail))
    result = service.finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'FAILED' and result.checks[0].verdict.value == 'INSUFFICIENT'
    assert 'integration-verifier-private-key' not in result.model_dump_json()


def test_caller_cancellation_leaves_no_verification_result(prepared):
    service, _, proposal, bindings, _, _ = prepared
    entered = asyncio.Event()
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
    provider = SemanticProvider(blocked)
    async def exercise():
        task = asyncio.create_task(service.verify('run-1', proposal, bindings, expected_state_version=2, provider=provider))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert counts(service) == (0, 0)
    assert list_attempts(service.database, 'run-1')[0]['status'] == 'CANCELLED'


def test_human_assistance_is_read_from_run_at_aggregation(prepared):
    service = prepared[0]
    with connect(service.database) as db, transaction(db):
        db.execute("UPDATE runs SET assistance_count=2 WHERE run_id='run-1'")
    record = verify(prepared, SemanticProvider())
    result = service.finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'SUCCEEDED' and result.assistance_count == 2
    assert service.read('run-1')['assistance'] == 'assisted'


def alternate_reference(case, evidence_id, *, title=None):
    service, contract, proposal, bindings, item, original = case
    value = proposal.model_dump_json().replace('"evidence-1"', '"' + evidence_id + '"')
    body = json.loads(value)
    if title is not None:
        body['items']['publications'][0]['title'] = title
    changed = ProposeResult.model_validate_json(canonical_json(body))
    remapped = [binding.model_copy(update={'evidence_id': evidence_id}) for binding in bindings]
    return service, contract, changed, remapped, item, original


def publish_additional(case, evidence_id, raw, **options):
    service, contract, *_ = case
    return service.evidence.publish('run-1', raw, evidence_id=evidence_id,
        source_url=contract.start_urls[0], captured_at=NOW, object_id=contract.targets[0].object_id,
        query_scope='fixture research', locator_or_page='structured original publication',
        sensitivity=options.pop('sensitivity', 'public'), redaction_status='FILTERED', policy_version='fixture-v1',
        expected_state_version=2, **options)


@pytest.mark.parametrize('bad_kind', ['duplicate_property', 'nonfinite', 'truncated'])
def test_ambiguous_original_json_cannot_support_semantic_or_rule_pass(prepared, bad_kind):
    raw = canonical_json(prepared[-1])
    if bad_kind == 'duplicate_property':
        raw = raw.replace('"title":', '"title":"Contradicting duplicate", "title":', 1)
    elif bad_kind == 'nonfinite':
        raw = raw.replace('"content_pages":1', '"content_pages":NaN', 1)
    else:
        raw = raw[:-1]
    publish_additional(prepared, 'evidence-2', raw.encode())
    provider = SemanticProvider()
    record = verify(alternate_reference(prepared, 'evidence-2'), provider)
    result = prepared[0].finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'FAILED' and not provider.calls
    assert all(c.verdict.value == 'INSUFFICIENT' for c in result.checks)


def test_display_derivative_cannot_replace_original_business_facts(prepared):
    display = deepcopy(prepared[-1])
    display['publications'][0]['title'] = 'Forged derivative title'
    publish_additional(prepared, 'display-1', canonical_json(display).encode(),
                       original_evidence_id='evidence-1', sensitivity='redacted')
    provider = SemanticProvider()
    record = verify(alternate_reference(prepared, 'display-1', title='Forged derivative title'), provider)
    result = prepared[0].finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'FAILED'
    assert next(c for c in result.checks if c.criterion_id == 'topic_scope').verdict.value == 'FAIL'
    assert provider.calls[0]['untrusted_evidence'][0]['content']['publications'][0]['title'] == 'A study of fixture evidence'


def test_semantic_pass_cannot_replace_conflicting_original_field(prepared):
    service, contract, proposal, bindings, item, original = prepared
    conflicting = deepcopy(original)
    conflicting['publications'][0]['title'] = 'Conflicting published title'
    publish_additional(prepared, 'evidence-2', canonical_json(conflicting).encode())
    body = proposal.model_dump(mode='json')
    body['evidence_ids'].append('evidence-2')
    body['items']['publications'][0]['evidence_ids'].append('evidence-2')
    proposal = ProposeResult.model_validate_json(canonical_json(body))
    bindings += [FieldBinding(result_path='/publications/0/title', evidence_id='evidence-2',
                              evidence_path='/publications/0/title')]
    record = verify((service, contract, proposal, bindings, item, original), SemanticProvider())
    result = service.finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'FAILED'
    assert next(c for c in result.checks if c.criterion_id == 'topic_scope').verdict.value == 'CONFLICT'


def test_frozen_cutoff_cannot_be_relaxed_by_semantic_pass_even_when_source_matches(prepared):
    service, contract, proposal, bindings, item, original = prepared
    changed_original = deepcopy(original)
    changed_original['publications'][0]['first_published_at'] = '2026-09-30T00:00:00Z'
    publish_additional(prepared, 'evidence-2', canonical_json(changed_original).encode())
    case = alternate_reference(prepared, 'evidence-2')
    body = case[2].model_dump(mode='json')
    body['items']['publications'][0]['first_published_at'] = '2026-09-30T00:00:00Z'
    proposal = ProposeResult.model_validate_json(canonical_json(body))
    record = verify((service, contract, proposal, case[3], item, original), SemanticProvider())
    result = service.finalize(record['verification_id'], expected_state_version=2)
    assert result.outcome == 'FAILED' and 'publication_after_cutoff' in result.unresolved
    assert all(c.verdict.value == 'FAIL' for c in result.checks)
