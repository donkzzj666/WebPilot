"""Rules consume independently loaded facts, never a proposed PASS decision."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from webagent.db.repository import canonical_json
from webagent.models.schema import ProposeResult
from webagent.tasks.compiler import compile_draft
from webagent.tasks.models import TaskContract
from webagent.verification.models import Check, EvidenceDocument, FieldBinding, Result, SideEffect, Verdict
from webagent.verification.rules import _leaves, evaluate_rules

NOW = '2026-09-29T00:00:00Z'
WHEN = datetime(2026, 9, 29, tzinfo=timezone.utc)
POLICY = {'mode': 'repository_write', 'repository': 'fixture/demo', 'base_branch': 'main',
          'branch': 'fix', 'base_sha': 'b' * 40, 'task_kind': 'ordinary_repair',
          'allowed_files': ['src/demo.py'], 'workflow_exception_files': [],
          'protected_patterns': ['tests/*'], 'required_checks': ['test'],
          'independent_rules_ref': 'independent-v1', 'allowed_operations': ['edit_file', 'commit', 'create_pr']}


def setup(scenario='finance'):
    coverage = {'searched_sources': ['local-fixture'], 'queries': [], 'cutoff_at': None,
                'content_pages': 1, 'unread_candidates': [], 'gaps': [], 'complete': True}
    if scenario == 'finance':
        params = {'entity_id': 'company', 'report_version': '2025', 'period_type': 'annual', 'metrics': ['revenue'], 'currency': 'USD'}
        items = {'scenario': 'finance', 'values': [{'field_id': 'revenue', 'entity_id': 'company',
                 'report_version': '2025', 'period_start': NOW, 'period_end': NOW, 'period_type': 'annual',
                 'metric_definition': 'revenue', 'currency': 'USD', 'raw_value': '1.20',
                 'disclosed_unit': 'million', 'normalized_value': '1200000', 'value_origin': 'disclosed',
                 'formula': None, 'rounding_rule': 'exact', 'rounding_lower': '1200000',
                 'rounding_upper': '1200000', 'channel': 'original', 'evidence_ids': ['e1']}]}
    elif scenario == 'research':
        params = {'queries': ['independent research'], 'topic_criteria': ['evidence-based'], 'cutoff_at': NOW, 'max_items': 3}
        coverage.update(queries=params['queries'], cutoff_at=NOW)
        items = {'scenario': 'research', 'publications': [{'canonical_id': 'paper-1', 'version': 'v1',
                 'title': 'A study', 'authors': ['Author'], 'first_published_at': NOW, 'revised_at': None,
                 'source_url': 'http://127.0.0.1:8765/research/paper-1', 'topic_basis': 'Executor self-assessment is ignored',
                 'claims': [{'statement': 'Reported result', 'evidence_ids': ['e1']}], 'relations': [], 'evidence_ids': ['e1']}]}
    elif scenario == 'grafana':
        params = {'operation_kind': 'grafana_read', 'dashboard_id': 'dashboard', 'panel_ids': ['requests'], 'variables': {'service': 'demo'}, 'timezone': 'UTC'}
        items = {'scenario': 'operations', 'operation_kind': 'grafana_read', 'dashboard_id': 'dashboard',
                 'time_range': {'start': NOW, 'end': NOW, 'basis': 'Explicit period'},
                 'variables': {'service': 'demo'}, 'timezone': 'UTC', 'captured_at': NOW,
                 'panels': [{'panel_id': 'requests', 'unit': 'requests/s', 'points': [{'observed_at': NOW, 'value': '3.25', 'label': 'demo'}], 'evidence_ids': ['e1']}]}
    elif scenario == 'code':
        params = {'operation_kind': 'code_repair', 'repository': 'fixture/demo', 'base_sha': 'b' * 40,
                  'branch': 'fix', 'failure_run_id': 'failure-1', 'required_checks': ['test'], 'independent_rules_ref': 'independent-v1'}
        ci = {'name': 'test', 'test_run_id': 'ci-run', 'commit_sha': 'c' * 40, 'conclusion': 'success', 'evidence_ids': ['e1']}
        items = {'scenario': 'operations', 'operation_kind': 'code_repair', 'repository': 'fixture/demo',
                 'base_sha': 'b' * 40, 'branch': 'fix', 'pr_url': 'https://github.com/fixture/demo/pull/1',
                 'head_sha': 'c' * 40, 'head_before_verification': 'c' * 40, 'head_after_verification': 'c' * 40,
                 'changed_files': ['src/demo.py'], 'required_checks': [ci], 'independent_rules_ref': 'independent-v1',
                 'independent_test_result': {**ci, 'name': 'independent'}, 'evidence_ids': ['e1']}
    else:
        params = {'source_id': 'local-fixture', 'source_kind': 'cisa_kev', 'baseline': True, 'scheduled_at': NOW,
                  'confirmed_boundary': None, 'max_list_items': 10, 'max_details': 5}
        items = {'scenario': 'monitoring', 'source_id': 'local-fixture', 'baseline': True, 'scheduled_at': NOW,
                 'started_at': NOW, 'observed_at': NOW, 'discovered_boundary': 'head', 'verified_boundary': 'head',
                 'verified_contiguous': True, 'examined_ranges': ['head'], 'pending_items': [], 'gaps': [],
                 'events': [], 'notification_keys': []}
    base = {'instruction': 'Read declared original', 'scenario': items['scenario'], 'source_ids': ['local-fixture'], 'parameters': params}
    if scenario == 'code':
        base.update(action_policy=POLICY, identity_ref='test-identity')
    compiled = compile_draft(base, task_id='task-1', version=1, created_at=NOW,
                             provenance=[{'origin': 'api', 'reference': 'request', 'content_sha256': 'a' * 64, 'authorizes_execution': True}])
    body = compiled.contract
    if scenario in {'finance', 'grafana'}:
        body['time_scope'] = {'start': NOW, 'end': NOW, 'basis': 'Explicit period'}
    contract = TaskContract.model_validate_json(canonical_json(body))
    proposal = ProposeResult.model_validate_json(canonical_json({'type': 'ProposeResult', 'items': items, 'coverage': coverage,
                        'evidence_ids': ['e1'], 'unresolved': [], 'existing_operation_ids': []}))
    content = deepcopy(proposal.items.model_dump(mode='json'))
    content['coverage'] = proposal.coverage.model_dump(mode='json')
    if scenario == 'monitoring':
        content['verification_context'] = {'source_kind': params['source_kind'], 'confirmed_boundary': None,
                                           'list_items': 1, 'detail_pages': 1}
    doc = EvidenceDocument(evidence_id='e1', run_id='run-1', source_url=contract.start_urls[0],
                           captured_at=WHEN, artifact_kind='ci' if scenario == 'code' else 'text', sha256='a' * 64,
                           object_id=contract.targets[0].object_id, locator_or_page='json:original',
                           commit_sha='c' * 40 if scenario == 'code' else None,
                           test_run_id='ci-run' if scenario == 'code' else None, content=content)
    paths = list(_leaves(proposal.items.model_dump(mode='json')))
    if scenario in {'research', 'monitoring'}:
        paths += list(_leaves(proposal.coverage.model_dump(mode='json'), '/coverage'))
    bindings = [FieldBinding(result_path=path, evidence_id='e1', evidence_path=path) for path, value in paths]
    if scenario == 'monitoring':
        bindings += [FieldBinding(result_path='/context/' + name, evidence_id='e1', evidence_path='/verification_context/' + name)
                     for name in ('source_kind', 'confirmed_boundary', 'list_items', 'detail_pages')]
    return contract, proposal, [doc], bindings


def evaluate(case):
    return evaluate_rules(*case, checked_at=WHEN, run_id='run-1')


def replace_proposal(case, transform):
    contract, proposal, docs, bindings = case
    body = proposal.model_dump(mode='json')
    transform(body)
    return contract, ProposeResult.model_validate_json(canonical_json(body)), docs, bindings


@pytest.mark.parametrize('scenario', ['finance', 'code', 'grafana', 'research', 'monitoring'])
def test_original_artifacts_support_fields_and_program_rules(scenario):
    result = evaluate(setup(scenario))
    assert result.violations == []
    assert all(f.verdict == Verdict.PASS for f in result.fields)
    for check in result.checks:
        assert check.verdict == (Verdict.INSUFFICIENT if check.criterion_id == 'topic_scope' else Verdict.PASS)
    if scenario == 'research':
        assert result.semantic_criterion_ids == ['topic_scope']


def test_no_bindings_is_insufficient_even_with_complete_evidence_metadata():
    contract, proposal, docs, _ = setup()
    result = evaluate((contract, proposal, docs, []))
    assert all(f.verdict == Verdict.INSUFFICIENT for f in result.fields)
    assert not result.deliverable_paths


def test_model_value_cannot_override_original_and_diagnostics_are_fixed():
    case = replace_proposal(setup(), lambda body: body['items']['values'][0].update(raw_value='999.99'))
    result = evaluate(case)
    check = next(f for f in result.fields if f.result_path.endswith('/raw_value'))
    assert check.verdict == Verdict.FAIL
    assert '999.99' not in result.model_dump_json()


def test_conflicting_originals_cannot_be_averaged_into_pass():
    contract, proposal, docs, bindings = setup()
    body = deepcopy(docs[0].content)
    body['values'][0]['raw_value'] = '2.40'
    docs.append(docs[0].model_copy(update={'evidence_id': 'e2', 'content': body}))
    proposal = proposal.model_copy(update={'evidence_ids': ['e1', 'e2']})
    proposal = replace_proposal((contract, proposal, docs, bindings), lambda b: b['items']['values'][0]['evidence_ids'].append('e2'))[1]
    bindings.append(FieldBinding(result_path='/values/0/raw_value', evidence_id='e2', evidence_path='/values/0/raw_value'))
    result = evaluate((contract, proposal, docs, bindings))
    assert next(f for f in result.fields if f.result_path.endswith('/raw_value')).verdict == Verdict.CONFLICT


@pytest.mark.parametrize('change,expected', [({'run_id': 'other'}, 'evidence_run_mismatch'),
    ({'source_url': 'https://outside.invalid/page'}, 'evidence_source_outside_scope')])
def test_scope_and_run_apply_to_all_referenced_documents(change, expected):
    contract, proposal, docs, bindings = setup()
    extra = docs[0].model_copy(update={'evidence_id': 'extra', **change})
    proposal = proposal.model_copy(update={'evidence_ids': ['e1', 'extra']})
    result = evaluate((contract, proposal, docs + [extra], bindings))
    assert expected in result.violations
    assert result.deliverable_paths == []


@pytest.mark.parametrize('change', [{'readable': False}, {'content': None}])
def test_missing_or_corrupt_originals_never_support_pass(change):
    contract, proposal, docs, bindings = setup()
    result = evaluate((contract, proposal, [docs[0].model_copy(update=change)], bindings))
    assert all(f.verdict == Verdict.INSUFFICIENT for f in result.fields)


def test_arbitrary_frozen_rule_is_not_executed_or_accepted():
    contract, proposal, docs, bindings = setup()
    criterion = contract.acceptance_criteria[0].model_copy(update={'expected_rule': '__import__("os").system("true")'})
    contract = contract.model_copy(update={'acceptance_criteria': [criterion]})
    assert evaluate((contract, proposal, docs, bindings)).checks[0].verdict == Verdict.INSUFFICIENT


@pytest.mark.parametrize('pointer', ['x', '/bad~2', '/a~', 'a' * 5000])
def test_json_pointer_rejects_ambiguous_paths(pointer):
    with pytest.raises(ValidationError):
        FieldBinding(result_path=pointer, evidence_id='e', evidence_path='/x')


@pytest.mark.parametrize('pointer', ['/missing', '/values/-/raw_value', '/values/00/raw_value'])
def test_unresolved_pointer_is_not_keyword_matching(pointer):
    contract, proposal, docs, bindings = setup()
    bindings = [x.model_copy(update={'evidence_path': pointer}) if x.result_path.endswith('/raw_value') else x for x in bindings]
    result = evaluate((contract, proposal, docs, bindings))
    assert next(f for f in result.fields if f.result_path.endswith('/raw_value')).verdict == Verdict.INSUFFICIENT


def test_partial_entity_is_retained_without_claiming_whole_result_pass():
    contract, proposal, docs, bindings = setup()
    second = deepcopy(proposal.items.model_dump(mode='json')['values'][0])
    second['field_id'] = 'profit'
    proposal = replace_proposal((contract, proposal, docs, bindings), lambda b: b['items']['values'].append(second))[1]
    result = evaluate((contract, proposal, docs, bindings))
    assert result.deliverable_paths == ['/values/0']
    assert any(x.startswith('field_unverified:/values/1') for x in result.unresolved)


@pytest.mark.parametrize('field,value,code', [('entity_id', 'other', 'financial_entity_mismatch'),
    ('currency', 'EUR', 'financial_report_scope_mismatch'), ('report_version', '2024', 'financial_report_scope_mismatch')])
def test_wrong_finance_target_is_severe_even_if_original_agrees(field, value, code):
    contract, proposal, docs, bindings = setup()
    original = deepcopy(docs[0].content)
    original['values'][0][field] = value
    case = replace_proposal((contract, proposal, [docs[0].model_copy(update={'content': original})], bindings), lambda b: b['items']['values'][0].update({field: value}))
    result = evaluate(case)
    assert code in result.violations
    assert result.deliverable_paths == []


@pytest.mark.parametrize('updates,code', [({'normalized_value': '1200001', 'rounding_upper': '1200001'}, 'unit_normalization_mismatch'),
    ({'rounding_lower': '1100000', 'rounding_upper': '1300000'}, 'rounding_interval_mismatch'),
    ({'disclosed_unit': 'unknown'}, 'number_or_unit_not_supported'),
    ({'value_origin': 'derived', 'formula': 'eval(1)'}, 'derived_formula_not_supported')])
def test_decimal_validation_is_independent_of_candidate_and_no_percentage_tolerance(updates, code):
    contract, proposal, docs, bindings = setup()
    original = deepcopy(docs[0].content)
    original['values'][0].update(updates)
    case = replace_proposal((contract, proposal, [docs[0].model_copy(update={'content': original})], bindings), lambda b: b['items']['values'][0].update(updates))
    result = evaluate(case)
    assert code in result.unresolved
    assert next(x for x in result.checks if x.criterion_id == 'requested_metrics').verdict != Verdict.PASS
    assert result.deliverable_paths == []


def test_decimal_disclosed_precision_interval_is_exact():
    contract, proposal, docs, bindings = setup()
    updates = {'rounding_rule': 'disclosed_precision', 'rounding_lower': '1195000', 'rounding_upper': '1205000'}
    original = deepcopy(docs[0].content)
    original['values'][0].update(updates)
    case = replace_proposal((contract, proposal, [docs[0].model_copy(update={'content': original})], bindings), lambda b: b['items']['values'][0].update(updates))
    assert all(x.verdict == Verdict.PASS for x in evaluate(case).checks)


@pytest.mark.parametrize('field,value', [('head_after_verification', 'd' * 40), ('repository', 'other/repo'), ('changed_files', ['tests/test.py'])])
def test_code_requires_exact_stable_head_and_authorized_files(field, value):
    result = evaluate(replace_proposal(setup('code'), lambda b: b['items'].update({field: value})))
    assert result.violations
    assert not result.deliverable_paths


@pytest.mark.parametrize('updates', [{'commit_sha': 'd' * 40}, {'test_run_id': 'unrelated'}, {'artifact_kind': 'text'}])
def test_ci_metadata_must_bind_exact_test_and_head(updates):
    contract, proposal, docs, bindings = setup('code')
    result = evaluate((contract, proposal, [docs[0].model_copy(update=updates)], bindings))
    assert next(x for x in result.checks if x.criterion_id == 'required_checks').verdict == Verdict.FAIL


def test_research_self_evaluation_is_not_evidence_and_coverage_requires_original():
    contract, proposal, docs, bindings = setup('research')
    bindings = [b for b in bindings if not b.result_path.startswith('/coverage/')]
    result = evaluate((contract, proposal, docs, bindings))
    assert result.semantic_criterion_ids == ['topic_scope']
    assert not any(f.result_path.endswith('topic_basis') for f in result.fields)
    assert any(f.result_path == '/coverage/complete' and f.verdict == Verdict.INSUFFICIENT for f in result.fields)


def test_research_cutoff_and_dedup_are_not_semantic_overrides():
    case = replace_proposal(setup('research'), lambda b: b['items']['publications'][0].update(first_published_at='2026-09-30T00:00:00Z'))
    assert 'publication_after_cutoff' in evaluate(case).violations
    case = replace_proposal(setup('research'), lambda b: b['items']['publications'].append(deepcopy(b['items']['publications'][0])))
    assert 'duplicate_publication' in evaluate(case).violations


@pytest.mark.parametrize('updates', [{'timezone': 'Asia/Shanghai'}, {'variables': {'service': 'other'}}])
def test_grafana_query_state_cannot_drift(updates):
    assert 'grafana_query_state_mismatch' in evaluate(replace_proposal(setup('grafana'), lambda b: b['items'].update(updates))).violations


def test_monitor_requires_proven_contiguity_and_complete_boundary():
    case = replace_proposal(setup('monitoring'), lambda b: b['items'].update(verified_contiguous=False, verified_boundary=None))
    result = evaluate(case)
    assert 'monitor_continuity_missing' in result.unresolved
    assert 'monitor_verified_boundary_incomplete' in result.unresolved


def test_check_and_side_effect_cannot_claim_unbacked_success():
    with pytest.raises(ValidationError):
        Check(criterion_id='c', expected_rule='fixed', actual={}, verdict=Verdict.PASS, evidence_ids=[], checked_at=WHEN, checker_version='v1')
    with pytest.raises(ValidationError):
        SideEffect(operation_id='op', target='repo', effect_type='commit', status='CONFIRMED', receipt=None, evidence_ids=[], critical_violation=False)


def test_field_binding_cannot_borrow_an_unrelated_entity_reference():
    contract, proposal, docs, bindings = setup()
    docs.append(docs[0].model_copy(update={'evidence_id': 'other-entity'}))
    proposal = proposal.model_copy(update={'evidence_ids': ['e1', 'other-entity']})
    bindings[0] = bindings[0].model_copy(update={'evidence_id': 'other-entity'})
    assert 'binding_not_in_field_evidence' in evaluate((contract, proposal, docs, bindings)).violations


def test_evidence_metadata_object_must_match_frozen_target():
    contract, proposal, docs, bindings = setup()
    docs[0] = docs[0].model_copy(update={'object_id': 'wrong-entity'})
    assert 'evidence_object_outside_scope' in evaluate((contract, proposal, docs, bindings)).violations


def test_optional_financial_field_is_explicitly_unresolved():
    contract, proposal, docs, bindings = setup()
    optional = contract.output_schema[0].model_copy(update={'field_id': 'profit', 'required': False})
    contract = contract.model_copy(update={'output_schema': contract.output_schema + [optional]})
    result = evaluate((contract, proposal, docs, bindings))
    assert 'optional_financial_output_missing' in result.unresolved
    assert result.deliverable_paths == ['/values/0']


def test_model_keyword_mention_is_not_a_parsed_original_value():
    contract, proposal, docs, bindings = setup()
    docs[0] = docs[0].model_copy(update={'content': {'text': 'entity_id company raw_value 1.20 normalized_value 1200000'}})
    result = evaluate((contract, proposal, docs, bindings))
    assert all(field.verdict == Verdict.INSUFFICIENT for field in result.fields)


def test_bool_is_not_numeric_evidence_for_coverage_count():
    contract, proposal, docs, bindings = setup('research')
    original = deepcopy(docs[0].content)
    original['coverage']['content_pages'] = True
    docs[0] = docs[0].model_copy(update={'content': original})
    result = evaluate((contract, proposal, docs, bindings))
    assert next(f for f in result.fields if f.result_path == '/coverage/content_pages').verdict == Verdict.FAIL


def test_natural_compiler_rule_pairs_are_recognized_without_changing_frozen_text():
    from webagent.tasks.natural import _rules
    contract, proposal, docs, bindings = setup()
    criteria = [{'criterion_id': name, 'expected_rule': rule, 'check_method': method, 'critical': True}
                for name, rule, method in _rules(contract.parameters.model_dump(mode='json'))[2]]
    body = contract.model_dump(mode='json')
    body['acceptance_criteria'] = criteria
    contract = TaskContract.model_validate_json(canonical_json(body))
    result = evaluate((contract, proposal, docs, bindings))
    assert all(check.verdict == Verdict.PASS for check in result.checks)
    assert [check.expected_rule for check in result.checks] == [criterion['expected_rule'] for criterion in criteria]


def test_future_capture_is_not_current_verified_evidence():
    contract, proposal, docs, bindings = setup()
    docs[0] = docs[0].model_copy(update={'captured_at': datetime(2026, 9, 30, tzinfo=timezone.utc)})
    assert 'evidence_capture_after_verification' in evaluate((contract, proposal, docs, bindings)).violations


def test_evaluation_does_not_mutate_contract_proposal_or_originals():
    case = setup('research')
    before = deepcopy(case)
    evaluate(case)
    assert case == before


def test_partial_entity_can_retain_proven_identity_and_values_with_minor_gap():
    contract, proposal, docs, bindings = setup()
    bindings = [b for b in bindings if not b.result_path.endswith('/channel')]
    result = evaluate((contract, proposal, docs, bindings))
    assert result.deliverable_paths == ['/values/0']
    assert 'field_unverified:/values/0/channel' in result.unresolved


def test_monitor_cannot_use_events_count_as_actual_browser_read_count():
    contract, proposal, docs, bindings = setup('monitoring')
    bindings = [b for b in bindings if not b.result_path.startswith('/context/')]
    result = evaluate((contract, proposal, docs, bindings))
    assert next(c for c in result.checks if c.criterion_id == 'limits').verdict == Verdict.INSUFFICIENT


@pytest.mark.parametrize('name,value', [('list_items', 11), ('detail_pages', 6), ('list_items', True),
                                       ('list_items', -1), ('detail_pages', '1')])
def test_monitor_read_counters_require_original_integers_inside_frozen_limits(name, value):
    contract, proposal, docs, bindings = setup('monitoring')
    content = deepcopy(docs[0].content)
    content['verification_context'][name] = value
    docs[0] = docs[0].model_copy(update={'content': content})
    result = evaluate((contract, proposal, docs, bindings))
    assert next(f for f in result.fields if f.result_path == '/context/' + name).verdict == Verdict.FAIL


def test_monitor_context_cannot_bind_to_an_arbitrary_small_number_elsewhere():
    contract, proposal, docs, bindings = setup('monitoring')
    bindings = [b.model_copy(update={'evidence_path': '/coverage/content_pages'}) if b.result_path == '/context/list_items' else b for b in bindings]
    assert 'binding_context_path_invalid' in evaluate((contract, proposal, docs, bindings)).violations


def test_non_finite_original_json_is_rejected_before_evaluation():
    _contract, _proposal, docs, _bindings = setup()
    body = docs[0].model_dump()
    body['content'] = {'value': float('nan')}
    with pytest.raises(ValidationError):
        EvidenceDocument(**body)


def test_monitor_wrong_source_kind_cannot_leave_deliverables():
    contract, proposal, docs, bindings = setup('monitoring')
    content = deepcopy(docs[0].content)
    content['verification_context']['source_kind'] = 'security_community'
    result = evaluate((contract, proposal, [docs[0].model_copy(update={'content': content})], bindings))
    assert 'monitor_source_kind_mismatch' in result.violations
    assert not result.deliverable_paths


def test_grafana_null_points_do_not_count_as_deliverable_measurements():
    contract, proposal, docs, bindings = setup('grafana')
    original = deepcopy(docs[0].content)
    original['panels'][0]['points'][0]['value'] = None
    case = replace_proposal((contract, proposal, [docs[0].model_copy(update={'content': original})], bindings), lambda b: b['items']['panels'][0]['points'][0].update(value=None))
    assert not evaluate(case).deliverable_paths


@pytest.mark.parametrize('missing', ['start', 'end', 'both'])
def test_missing_frozen_financial_period_is_insufficient_not_wrong_scope(missing):
    contract, proposal, docs, bindings = setup()
    updates = {key: None for key in ('start', 'end') if missing in {key, 'both'}}
    contract = contract.model_copy(update={'time_scope': contract.time_scope.model_copy(update=updates)})
    result = evaluate((contract, proposal, docs, bindings))
    report = next(c for c in result.checks if c.criterion_id == 'report_scope')
    assert report.verdict == Verdict.INSUFFICIENT
    assert report.actual == {'code': 'frozen_financial_period_missing'}
    assert 'frozen_financial_period_missing' in result.unresolved
    assert 'financial_report_scope_mismatch' not in result.violations
    assert result.deliverable_paths == ['/values/0']


def test_explicit_financial_period_mismatch_remains_a_violation_with_other_bound_missing():
    contract, proposal, docs, bindings = setup()
    scope = contract.time_scope.model_copy(update={'start': None, 'end': datetime(2026, 9, 30, tzinfo=timezone.utc)})
    contract = contract.model_copy(update={'time_scope': scope})
    result = evaluate((contract, proposal, docs, bindings))
    assert 'financial_report_scope_mismatch' in result.violations
    assert 'frozen_financial_period_missing' not in result.unresolved
    assert not result.deliverable_paths


def test_decimal_overflow_is_insufficient_and_does_not_escape_verifier(monkeypatch):
    from webagent.verification import rules
    case = setup()
    monkeypatch.setitem(rules._UNITS, 'million', Decimal('1e1000000'))
    result = evaluate(case)
    assert 'number_not_supported' in result.unresolved
    assert next(c for c in result.checks if c.criterion_id == 'requested_metrics').verdict == Verdict.INSUFFICIENT
    assert not result.deliverable_paths


@pytest.mark.parametrize('rounded', [False, True])
def test_decimal_normalization_preserves_more_than_default_precision_digits(rounded):
    contract, proposal, docs, bindings = setup()
    raw = '1234567890123456789012345678912345678912345'
    normalized = '1234567890123456789012345679000000000000000' if rounded else raw
    updates = {'raw_value': raw, 'disclosed_unit': '1', 'normalized_value': normalized,
               'rounding_lower': normalized, 'rounding_upper': normalized}
    original = deepcopy(docs[0].content)
    original['values'][0].update(updates)
    case = replace_proposal((contract, proposal, [docs[0].model_copy(update={'content': original})], bindings), lambda b: b['items']['values'][0].update(updates))
    result = evaluate(case)
    verdict = next(c for c in result.checks if c.criterion_id == 'requested_metrics').verdict
    assert verdict == (Verdict.FAIL if rounded else Verdict.PASS)


def test_ambient_decimal_precision_and_exponent_limits_do_not_change_verification():
    case = setup()
    with localcontext() as context:
        context.Emax = 2
        context.prec = 2
        result = evaluate(case)
    assert all(c.verdict == Verdict.PASS for c in result.checks)


def test_numbers_beyond_bounded_parser_support_are_insufficient():
    contract, proposal, docs, bindings = setup()
    value = '1' * 1001
    updates = {'raw_value': value, 'disclosed_unit': '1', 'normalized_value': value,
               'rounding_lower': value, 'rounding_upper': value}
    original = deepcopy(docs[0].content)
    original['values'][0].update(updates)
    case = replace_proposal((contract, proposal, [docs[0].model_copy(update={'content': original})], bindings), lambda b: b['items']['values'][0].update(updates))
    result = evaluate(case)
    assert 'number_not_supported' in result.unresolved
    assert next(c for c in result.checks if c.criterion_id == 'requested_metrics').verdict == Verdict.INSUFFICIENT


def test_known_rule_marked_semantic_requires_an_independent_call():
    contract, proposal, docs, bindings = setup()
    criterion = contract.acceptance_criteria[0].model_copy(update={'check_method': 'semantic'})
    contract = contract.model_copy(update={'acceptance_criteria': [criterion, *contract.acceptance_criteria[1:]]})
    result = evaluate((contract, proposal, docs, bindings))
    check = next(c for c in result.checks if c.criterion_id == criterion.criterion_id)
    assert result.semantic_criterion_ids == [criterion.criterion_id]
    assert check.verdict == Verdict.INSUFFICIENT
    assert check.actual == {'code': 'independent_semantic_review_required'}


@pytest.mark.parametrize('missing', [False, True])
def test_semantic_method_cannot_erase_failed_or_missing_original_facts(missing):
    contract, proposal, docs, bindings = setup()
    criterion = contract.acceptance_criteria[0].model_copy(update={'check_method': 'semantic'})
    contract = contract.model_copy(update={'acceptance_criteria': [criterion]})
    if missing:
        bindings = []
    else:
        content = deepcopy(docs[0].content)
        content['values'][0]['raw_value'] = '99.99'
        docs[0] = docs[0].model_copy(update={'content': content})
    result = evaluate((contract, proposal, docs, bindings))
    check = result.checks[0]
    assert check.verdict == (Verdict.INSUFFICIENT if missing else Verdict.FAIL)
    assert check.actual != {'code': 'independent_semantic_review_required'}
