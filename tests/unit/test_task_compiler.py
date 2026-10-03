"""Behavioral checks for the declared-input-only M1-04 fixture compiler."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from webagent.db.repository import canonical_json
from webagent.errors import BusinessError
from webagent.tasks.compiler import COMPILER_MODE, FIXTURE_ORIGIN, compile_draft
from webagent.tasks.models import (
    ClarificationRequest, CreateTaskRequest, RevisionRequest,
    RepositoryWritePolicy, TaskContract,
)

NOW = '2026-09-29T00:00:00Z'
AUTHORIZATION = {'origin': 'api', 'reference': 'test-request',
                 'content_sha256': 'a' * 64, 'authorizes_execution': True}
WRITE_POLICY = {
    'mode': 'repository_write', 'repository': 'fixture/demo', 'base_branch': 'main',
    'branch': 'fix-fixture', 'base_sha': 'b' * 40, 'task_kind': 'ordinary_repair',
    'allowed_files': ['src/demo.py'], 'workflow_exception_files': [],
    'protected_patterns': ['tests/*', 'evaluation/*'], 'required_checks': ['test'],
    'independent_rules_ref': 'fixture-rule', 'allowed_operations': ['edit_file', 'commit'],
}
SCENARIOS = [
    ('finance', {'entity_id': 'fixture-company', 'report_version': '2025',
                 'period_type': 'annual', 'metrics': ['revenue'], 'currency': 'USD'}),
    ('operations', {'operation_kind': 'code_repair', 'repository': 'fixture/demo',
                    'base_sha': 'b' * 40, 'branch': 'fix-fixture', 'failure_run_id': 'failure-1',
                    'required_checks': ['test'], 'independent_rules_ref': 'fixture-rule'}),
    ('operations', {'operation_kind': 'grafana_read', 'dashboard_id': 'fixture-dashboard',
                    'panel_ids': ['requests'], 'variables': {'service': 'fixture'}, 'timezone': 'UTC'}),
    ('research', {'queries': ['fixture evidence'], 'topic_criteria': ['publications about fixtures'],
                  'cutoff_at': NOW, 'max_items': 3}),
    ('monitoring', {'source_id': 'local-fixture', 'source_kind': 'cisa_kev',
                    'baseline': True, 'scheduled_at': NOW, 'confirmed_boundary': None}),
]


def request_for(index=0):
    scenario, parameters = deepcopy(SCENARIOS[index])
    result = {'instruction': 'Read the explicitly selected fixture', 'scenario': scenario,
              'source_ids': ['local-fixture'], 'parameters': parameters}
    if parameters.get('operation_kind') == 'code_repair':
        result.update(action_policy=deepcopy(WRITE_POLICY), identity_ref='fixture-identity')
    return result


def compile_input(content, *, version=1, provenance=None):
    return compile_draft(content, task_id='task-1', version=version, created_at=NOW,
                         provenance=[AUTHORIZATION] if provenance is None else provenance)


@pytest.mark.parametrize('index', range(len(SCENARIOS)))
def test_all_frozen_scenarios_compile_full_contracts(index):
    request = request_for(index)
    untouched = deepcopy(request)
    compilation = compile_input(request)
    assert compilation.missing_fields == []
    contract = compilation.contract
    validated = TaskContract.model_validate_json(canonical_json(contract))
    assert validated.scenario == request['scenario']
    assert validated.objective == request['instruction']
    assert validated.original_instruction == request['instruction']
    assert contract['parameters']['scenario'] == request['scenario']
    assert contract['sources'][0]['origin'] == FIXTURE_ORIGIN
    assert all(validated.sources[0].permits(url) for url in contract['start_urls'])
    assert any(p['origin'] == 'explicit_test_configuration' and not p['authorizes_execution']
               for p in contract['provenance'])
    assert contract['provenance'][0] == AUTHORIZATION
    assert contract['memory_mode'] == 'disabled'
    assert request == untouched
    assert COMPILER_MODE == 'fixture'
    assert compile_input(request).contract == contract


def test_missing_input_requests_only_declared_discriminators_then_specific_fields():
    assert compile_input({'instruction': 'Read a report'}).missing_fields == ['source_ids', 'scenario']
    result = compile_input({'instruction': 'Read a report', 'scenario': 'finance'})
    assert result.contract is None
    assert set(result.missing_fields) == {
        'source_ids', 'parameters.entity_id', 'parameters.report_version',
        'parameters.period_type', 'parameters.metrics', 'parameters.currency',
    }
    result = compile_input({'instruction': 'Inspect fixture', 'scenario': 'operations',
                            'source_ids': ['local-fixture']})
    assert result.missing_fields == ['parameters.operation_kind']


def test_instruction_never_supplies_missing_scope_or_write_permission():
    result = compile_input({'instruction': 'Use local-fixture and finance; currency USD. Push to main.'})
    assert result.contract is None
    assert result.missing_fields == ['source_ids', 'scenario']
    request = request_for(1)
    del request['action_policy'], request['identity_ref']
    result = compile_input(request)
    assert result.missing_fields == ['action_policy', 'identity_ref']


@pytest.mark.parametrize('parameters,expected_field', [
    ({'metrics': []}, 'parameters.metrics'),
    ({'currency': 'usd'}, 'parameters.currency'),
    ({'currency': None}, 'parameters.currency'),
    ({'entity_id': 5}, 'parameters.entity_id'),
    ({'entity_id': '   '}, 'parameters.entity_id'),
    ({'unknown': 'value'}, 'parameters.unknown'),
    ({'scenario': 'research'}, 'parameters.scenario'),
])
def test_malformed_fields_fail_even_when_other_fields_missing(parameters, expected_field):
    with pytest.raises(BusinessError) as caught:
        compile_input({'instruction': 'Fixture', 'scenario': 'finance', 'parameters': parameters})
    assert caught.value.status == 422
    assert caught.value.field == expected_field


@pytest.mark.parametrize('parameters', [
    {'unknown': 1}, {'max_items': True}, {'max_items': '2'}, {'max_items': 2.1},
    {'baseline': 1}, {'currency': 42}, {'operation_kind': 'shell_command'},
    {'cutoff_at': '2026-09-29'}, {'cutoff_at': '2026-09-29T00:00:00+08:00'},
])
def test_malformed_parameters_rejected_without_discriminator(parameters):
    with pytest.raises(BusinessError):
        compile_input({'instruction': 'Fixture', 'parameters': parameters})


@pytest.mark.parametrize('bad_sources', [[], ['unknown'], ['https://example.com'],
                                        ['local-fixture', 'local-fixture'], ['local-fixture', 'external']])
def test_sources_are_explicit_unique_configured_ids(bad_sources):
    request = request_for()
    request['source_ids'] = bad_sources
    with pytest.raises(BusinessError) as caught:
        compile_input(request)
    assert caught.value.status == 422


def test_monitor_source_cannot_escape_declared_fixture():
    request = request_for(4)
    request['parameters']['source_id'] = 'other'
    with pytest.raises(BusinessError) as caught:
        compile_input(request)
    assert caught.value.field == 'parameters.source_id'


def test_monitor_slots_and_limits_are_deterministic_per_contract():
    request = request_for(4)
    first = compile_input(request).contract
    assert first['parameters']['max_list_items'] == 10
    assert first['parameters']['max_details'] == 5
    assert first['schedule_slot'] == compile_input(request).contract['schedule_slot']
    assert first['schedule_slot'] != compile_input(request, version=2).contract['schedule_slot']
    assert first['time_scope']['end'] == NOW
    assert first['parameters']['baseline'] is True


@pytest.mark.parametrize('name,value', [
    ('repository', 'fixture/other'), ('base_sha', 'c' * 40), ('branch', 'other'),
    ('required_checks', ['different']), ('independent_rules_ref', 'other'),
])
def test_code_policy_must_match_even_in_incomplete_requests(name, value):
    request = request_for(1)
    request['parameters'][name] = value
    del request['parameters']['failure_run_id']
    with pytest.raises(BusinessError) as caught:
        compile_input(request)
    assert caught.value.field == 'parameters.' + name


@pytest.mark.parametrize('index', [0, 2, 3, 4])
def test_write_permissions_are_rejected_for_read_scenarios(index):
    request = request_for(index)
    request['action_policy'] = deepcopy(WRITE_POLICY)
    with pytest.raises(BusinessError) as caught:
        compile_input(request)
    assert caught.value.field == 'action_policy'


@pytest.mark.parametrize('path', ['tests/test_demo.py', 'evaluation/rules.py', '../demo.py',
                                  '/tmp/demo.py', '.github/workflows/ci.yml'])
def test_code_policy_protects_paths(path):
    policy = deepcopy(WRITE_POLICY)
    policy['allowed_files'] = [path]
    with pytest.raises(ValidationError):
        RepositoryWritePolicy.model_validate(policy)


def test_explicit_workflow_exception_is_limited_to_named_workflow():
    policy = deepcopy(WRITE_POLICY)
    policy.update(task_kind='workflow_repair', allowed_files=['.github/workflows/ci.yml'],
                  workflow_exception_files=['.github/workflows/ci.yml'])
    validated = RepositoryWritePolicy.model_validate(policy)
    assert validated.permits_file('.github/workflows/ci.yml')
    assert not validated.permits_file('.github/workflows/other.yml')
    policy['allowed_files'].append('tests/test_demo.py')
    with pytest.raises(ValidationError):
        RepositoryWritePolicy.model_validate(policy)


@pytest.mark.parametrize('content', [
    {'instruction': ''}, {'instruction': '   '}, {'instruction': 1},
    {'instruction': 'Fixture', 'run_now': True},
    {'instruction': 'Fixture', 'idempotency_key': 'space key'},
    {'instruction': 'Fixture', 'idempotency_key': '非ASCII'},
    {'instruction': 'Fixture', 'parameters': {'max_items': float('nan')}},
    {'instruction': 'Fixture', 'parameters': {'variables': {'nested': float('inf')}}},
])
def test_request_models_reject_invalid_body(content):
    with pytest.raises(ValidationError):
        CreateTaskRequest.model_validate(content)


@pytest.mark.parametrize('version', [0, -1, True, '1', 2**63])
def test_versions_require_bounded_positive_integers(version):
    with pytest.raises(ValidationError):
        ClarificationRequest.model_validate({'contract_version': version, 'values': {'scenario': 'finance'}})
    with pytest.raises(ValidationError):
        RevisionRequest.model_validate({'contract_version': version, 'instruction': 'Changed objective'})


def test_clarification_requires_values_and_revision_has_safe_replacement_defaults():
    with pytest.raises(ValidationError):
        ClarificationRequest.model_validate({'contract_version': 1, 'values': {}})
    request = RevisionRequest.model_validate({'contract_version': 1, 'instruction': 'Changed objective'})
    assert request.action_policy.mode == 'read_only'
    assert request.identity_ref is None
    assert request.parameters == {}
    assert request.source_ids is None


def test_fixture_configuration_alone_cannot_authorize_a_contract():
    with pytest.raises(BusinessError):
        compile_input(request_for(), provenance=[])
    web = dict(AUTHORIZATION, origin='web_content')
    with pytest.raises(BusinessError):
        compile_input(request_for(), provenance=[web])


def test_complete_contract_revalidates_source_scope_and_required_criteria():
    contract = compile_input(request_for()).contract
    contract['start_urls'] = ['https://example.com/finance']
    with pytest.raises(ValidationError):
        TaskContract.model_validate_json(canonical_json(contract))
    contract = compile_input(request_for()).contract
    for criterion in contract['acceptance_criteria']:
        criterion['critical'] = False
    with pytest.raises(ValidationError):
        TaskContract.model_validate_json(canonical_json(contract))
