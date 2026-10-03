"""Model proposals stay typed, bound to input, and separate from final outcomes."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from webagent.models.schema import (
    InvalidModelOutput, ModelInput, output_json_schema, parse_model_output,
    validate_output_for_input,
)
from webagent.tasks.compiler import compile_draft

NOW = '2026-09-29T00:00:00Z'
BASE_SHA = 'b' * 40
SCENARIOS = {
    'finance': {'entity_id': 'fixture-company', 'report_version': '2025',
                'period_type': 'annual', 'metrics': ['revenue'], 'currency': 'USD'},
    'code': {'operation_kind': 'code_repair', 'repository': 'fixture/demo',
             'base_sha': BASE_SHA, 'branch': 'fix-fixture', 'failure_run_id': 'failure-1',
             'required_checks': ['test'], 'independent_rules_ref': 'fixture-rule'},
    'grafana': {'operation_kind': 'grafana_read', 'dashboard_id': 'fixture-dashboard',
                'panel_ids': ['requests'], 'variables': {'service': 'fixture'}, 'timezone': 'UTC'},
    'research': {'queries': ['fixture evidence'], 'topic_criteria': ['fixtures'],
                 'cutoff_at': NOW, 'max_items': 3},
    'monitoring': {'source_id': 'local-fixture', 'source_kind': 'cisa_kev',
                   'baseline': True, 'scheduled_at': NOW, 'confirmed_boundary': None},
}


def input_body(scenario='finance'):
    request = {'instruction': 'Read the explicitly selected fixture',
               'scenario': 'operations' if scenario in ('code', 'grafana') else scenario,
               'source_ids': ['local-fixture'], 'parameters': deepcopy(SCENARIOS[scenario])}
    if scenario == 'code':
        request['identity_ref'] = 'fixture-identity'
        request['action_policy'] = {
            'mode': 'repository_write', 'repository': 'fixture/demo', 'base_branch': 'main',
            'branch': 'fix-fixture', 'base_sha': BASE_SHA, 'task_kind': 'ordinary_repair',
            'allowed_files': ['src/demo.py'], 'workflow_exception_files': [],
            'protected_patterns': ['tests/*'], 'required_checks': ['test'],
            'independent_rules_ref': 'fixture-rule', 'allowed_operations': ['edit_file', 'commit'],
        }
    contract = compile_draft(
        request, task_id='task-1', version=1, created_at=NOW,
        provenance=[{'origin': 'api', 'reference': 'test-request', 'content_sha256': 'a' * 64,
                     'authorizes_execution': True}],
    ).contract
    return {
        'run_id': 'run-1', 'contract': contract,
        'observation': {
            'snapshot_id': 'snapshot-1', 'run_id': 'run-1', 'captured_at': NOW,
            'source_url': contract['start_urls'][0], 'title': 'Synthetic page',
            'tab_id': 'tab-1', 'frame_id': 'frame-1', 'page_version': 'page-1',
            'width': 100, 'height': 100, 'visible_excerpt': 'Fixture content',
            'evidence_ids': ['image-1', 'text-1'], 'redaction_status': 'FILTERED',
        },
        'verified_checkpoint': {
            'checkpoint_id': 'checkpoint-1', 'task_id': 'task-1', 'run_id': 'run-1',
            'contract_version': 1, 'current_subgoal': 'inspect', 'verified_item_ids': [],
            'pending_item_ids': ['item-1'], 'current_object_id': 'object-1',
            'current_object_version': None, 'current_snapshot_id': 'snapshot-1',
            'flow_version': None, 'action_sequence': 0, 'business_event_id': 0,
            'budget_record_ref': 'budget-1', 'identity_ref': contract['identity_ref'],
            'pending_operation_ids': [], 'epoch': 1, 'evidence_ids': ['past-evidence'], 'saved_at': NOW,
        },
        'image_evidence_ids': ['image-1'],
        'allowed_action_schema_ref': 'urn:webagent:m0-contract-v1:Action',
        'selected_flow_versions': [],
    }


def model_input(scenario='finance'):
    return ModelInput.model_validate_json(json.dumps(input_body(scenario)))


def action_body(kind='read_visible', *, scenario='finance'):
    body = input_body(scenario)
    arguments = {
        'navigate': {'url': 'http://127.0.0.1:8765/next'},
        'click': {}, 'input': {'text': 'fixture'}, 'keypress': {'key': 'Enter'},
        'select': {'option_label': 'fixture'}, 'scroll': {'direction': 'down', 'pixels': 500},
        'switch_tab': {'tab_id': 'tab-2'}, 'read_visible': {}, 'screenshot': {},
        'download_attachment': {'attachment_url': 'http://127.0.0.1:8765/file.pdf',
                                'link_evidence_id': 'text-1'},
    }
    return {'type': 'Action', 'action': {
        'run_id': 'run-1', 'step_id': 'step-1', 'epoch': 1, 'snapshot_id': 'snapshot-1',
        'target': {'page_url': body['observation']['source_url'], 'tab_id': 'tab-1',
                   'frame_id': 'frame-1', 'locator': {'strategy': 'dom', 'attribute': 'id', 'value': 'fixture'},
                   'write_scope': None},
        'expected_effect': 'read', 'action_type': kind, 'args': arguments[kind],
    }}


def proposal_body(scenario='finance'):
    body = input_body(scenario)
    items = {
        'finance': {'scenario': 'finance', 'values': []},
        'code': {'scenario': 'operations', 'operation_kind': 'code_repair',
                 'repository': 'fixture/demo', 'base_sha': BASE_SHA, 'branch': 'fix-fixture',
                 'pr_url': None, 'head_sha': None, 'head_before_verification': None,
                 'head_after_verification': None, 'changed_files': [], 'required_checks': [],
                 'independent_rules_ref': 'fixture-rule', 'independent_test_result': None, 'evidence_ids': []},
        'grafana': {'scenario': 'operations', 'operation_kind': 'grafana_read',
                    'dashboard_id': 'fixture-dashboard', 'time_range': body['contract']['time_scope'],
                    'variables': {'service': 'fixture'}, 'timezone': 'UTC', 'captured_at': NOW, 'panels': []},
        'research': {'scenario': 'research', 'publications': []},
        'monitoring': {'scenario': 'monitoring', 'source_id': 'local-fixture', 'baseline': True,
                       'scheduled_at': NOW, 'started_at': NOW, 'observed_at': NOW,
                       'discovered_boundary': None, 'verified_boundary': None, 'verified_contiguous': False,
                       'examined_ranges': [], 'pending_items': [], 'gaps': [], 'events': [], 'notification_keys': []},
    }[scenario]
    return {'type': 'ProposeResult', 'items': items,
            'coverage': {'searched_sources': ['local-fixture'], 'queries': [],
                         'cutoff_at': NOW if scenario == 'research' else None,
                         'content_pages': 1, 'unread_candidates': [], 'gaps': ['unfinished'], 'complete': False},
            'evidence_ids': [], 'unresolved': ['Needs evidence'], 'existing_operation_ids': []}


def parse(body):
    return parse_model_output(json.dumps(body))


@pytest.mark.parametrize('kind', [
    'navigate', 'click', 'input', 'keypress', 'select', 'scroll', 'switch_tab',
    'read_visible', 'screenshot', 'download_attachment',
])
def test_all_allowed_action_variants_preserve_typed_arguments(kind):
    output = parse(action_body(kind))
    assert output.action.action_type == kind
    assert validate_output_for_input(output, model_input()) is output


@pytest.mark.parametrize('scenario', list(SCENARIOS))
def test_partial_result_proposals_are_typed_without_claiming_success(scenario):
    output = parse(proposal_body(scenario))
    assert validate_output_for_input(output, model_input(scenario)) is output
    assert 'outcome' not in output.model_dump()


def test_evidence_and_input_requests_complete_four_output_choices():
    prepared = model_input()
    body = {'type': 'RequestEvidence', 'criterion_ids': [prepared.contract.acceptance_criteria[0].criterion_id],
            'source_ids': ['local-fixture'], 'needed': 'Current visible evidence'}
    assert validate_output_for_input(parse(body), prepared).type == 'RequestEvidence'
    body = {'type': 'RequestInput', 'requested_fields': ['period'], 'reason': 'User must select the period'}
    assert validate_output_for_input(parse(body), prepared).type == 'RequestInput'


@pytest.mark.parametrize('raw', [
    '', 'null', '[]', '[{"type":"RequestInput"}]', '{}', '"Action"', '{broken',
    '```json\n{"type":"RequestInput","requested_fields":["x"],"reason":"missing"}\n```',
    '{"type":"RequestInput","type":"RequestEvidence"}',
    '{"type":"RequestInput","requested_fields":["x"],"reason":"a","reason":"b"}',
    '{"type":"RequestInput","requested_fields":["x"],"reason":NaN}',
    '{"type":"RequestInput","requested_fields":["x"],"reason":Infinity}',
    '{"type":"RequestInput","requested_fields":["x"],"reason":1e999}',
    '{"type":"RequestInput","requested_fields":["x"],"reason":"why"} {}',
    '{"type":"SUCCEEDED"}', '{"success":true}',
])
def test_malformed_ambiguous_and_direct_success_documents_are_rejected(raw):
    with pytest.raises(InvalidModelOutput):
        parse_model_output(raw)


@pytest.mark.parametrize('bad_kind', ['evaluate', 'execute_script', 'shell', 'python', 'exec', 'open_url'])
def test_unknown_code_and_tool_actions_are_never_accepted(bad_kind):
    body = action_body()
    body['action']['action_type'] = bad_kind
    with pytest.raises(InvalidModelOutput):
        parse(body)


@pytest.mark.parametrize('field,value', [('epoch', True), ('epoch', '1'), ('epoch', 1.0),
                                        ('epoch', 0), ('run_id', 123), ('snapshot_id', ' ')])
def test_action_ids_and_integers_are_strict(field, value):
    body = action_body()
    body['action'][field] = value
    with pytest.raises(InvalidModelOutput):
        parse(body)


@pytest.mark.parametrize('field,value', [('run_id', 'other'), ('epoch', 2), ('snapshot_id', 'other')])
def test_action_must_bind_current_run_epoch_and_observation(field, value):
    body = action_body()
    body['action'][field] = value
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())


@pytest.mark.parametrize('field,value', [('tab_id', 'other'), ('frame_id', 'other'),
                                        ('page_url', 'http://127.0.0.1:8765/other')])
def test_action_target_cannot_change_observation_reference(field, value):
    body = action_body()
    body['action']['target'][field] = value
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())


def test_action_cannot_navigate_outside_source_or_download_unseen_link():
    body = action_body('navigate')
    body['action']['args']['url'] = 'https://unknown.example/escape'
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())
    body = action_body('download_attachment')
    body['action']['args']['link_evidence_id'] = 'invented-link'
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())


def test_coordinate_action_uses_supplied_screenshot_and_exact_dimensions():
    body = action_body('click')
    loc = {'strategy': 'coordinate', 'screenshot_evidence_id': 'image-1', 'snapshot_id': 'snapshot-1',
           'tab_id': 'tab-1', 'frame_id': 'frame-1', 'width': 100, 'height': 100, 'x': 10, 'y': 20}
    body['action']['target']['locator'] = loc
    assert validate_output_for_input(parse(body), model_input()).type == 'Action'
    for field, value in [('x', 100), ('width', 101), ('screenshot_evidence_id', 'past-evidence'),
                         ('snapshot_id', 'other'), ('frame_id', 'other')]:
        changed = deepcopy(body)
        changed['action']['target']['locator'][field] = value
        with pytest.raises(InvalidModelOutput):
            validate_output_for_input(parse(changed), model_input())


@pytest.mark.parametrize('kind', ['click', 'input', 'select'])
def test_targeted_interaction_requires_locator(kind):
    body = action_body(kind)
    body['action']['target']['locator'] = None
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())


def test_write_scope_stays_inside_frozen_authorization():
    body = action_body('input', scenario='code')
    body['action']['expected_effect'] = 'write'
    body['action']['target']['write_scope'] = {
        'repository': 'fixture/demo', 'branch': 'fix-fixture', 'base_sha': BASE_SHA,
        'operation': 'edit_file', 'files': ['src/demo.py'], 'operation_id': 'operation-1',
        'identity_ref': 'fixture-identity', 'target_rechecked_at': NOW,
    }
    assert validate_output_for_input(parse(body), model_input('code')).type == 'Action'
    for field, value in [('repository', 'other/repo'), ('branch', 'main'), ('files', ['tests/test.py']),
                         ('operation', 'create_pr'), ('identity_ref', 'other'), ('files', [])]:
        changed = deepcopy(body)
        changed['action']['target']['write_scope'][field] = value
        with pytest.raises(InvalidModelOutput):
            validate_output_for_input(parse(changed), model_input('code'))
    body['action']['target']['page_url'] = model_input().observation.source_url
    with pytest.raises(InvalidModelOutput) as caught:
        validate_output_for_input(parse(body), model_input())
    assert caught.value.errors[0]['reason'] == 'read_only_contract'


@pytest.mark.parametrize('field,value', [('criterion_ids', ['invented']), ('source_ids', ['unknown'])])
def test_evidence_request_cannot_invent_scope(field, value):
    prepared = model_input()
    body = {'type': 'RequestEvidence', 'criterion_ids': [prepared.contract.acceptance_criteria[0].criterion_id],
            'source_ids': ['local-fixture'], 'needed': 'Current evidence'}
    body[field] = value
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), prepared)


def test_proposal_needs_known_evidence_and_matching_scenario():
    body = proposal_body()
    body['evidence_ids'] = ['invented']
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())
    body['evidence_ids'] = ['past-evidence']
    assert validate_output_for_input(parse(body), model_input()).type == 'ProposeResult'
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(proposal_body('research')), model_input())
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(proposal_body('code')), model_input('grafana'))


def test_proposal_does_not_allow_outcome_or_untyped_items():
    for change in ({'outcome': 'SUCCEEDED'}, {'success': True}, {'items': []},
                   {'items': {'scenario': 'finance', 'values': [], 'outcome': 'SUCCEEDED'}}):
        body = proposal_body()
        body.update(change)
        with pytest.raises(InvalidModelOutput):
            parse(body)


def finance_item():
    return {'field_id': 'metrics', 'entity_id': 'fixture-company', 'report_version': '2025',
            'period_start': '2025-01-01T00:00:00Z', 'period_end': '2025-12-31T00:00:00Z',
            'period_type': 'annual', 'metric_definition': 'Revenue', 'currency': 'USD',
            'raw_value': '100', 'disclosed_unit': 'USD', 'normalized_value': '100',
            'value_origin': 'disclosed', 'formula': None, 'rounding_rule': 'exact',
            'rounding_lower': '100', 'rounding_upper': '100', 'channel': 'local-fixture',
            'evidence_ids': ['text-1']}


def test_financial_proposal_preserves_values_and_indexes_field_level_evidence():
    body = proposal_body()
    body['items']['values'] = [finance_item()]
    body['evidence_ids'] = ['text-1']
    output = validate_output_for_input(parse(body), model_input())
    assert output.items.values[0].normalized_value == '100'
    body['evidence_ids'] = []
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())


@pytest.mark.parametrize('field,value', [
    ('normalized_value', 100), ('normalized_value', 'NaN'), ('normalized_value', '101'),
    ('value_origin', 'derived'), ('period_start', '2026-01-01T00:00:00Z'),
    ('period_start', '2025-01-01T00:00:00'), ('currency', 'usd'),
    ('entity_id', 'other-entity'), ('report_version', '2024'), ('evidence_ids', ['invented']),
])
def test_financial_proposal_rejects_invalid_and_out_of_scope_values(field, value):
    body = proposal_body()
    item = finance_item()
    item[field] = value
    body['items']['values'] = [item]
    body['evidence_ids'] = ['text-1']
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input())


def test_result_coverage_cannot_claim_complete_with_known_gaps():
    body = proposal_body()
    body['coverage']['complete'] = True
    with pytest.raises(InvalidModelOutput):
        parse(body)
    body['coverage']['gaps'] = []
    body['coverage']['unread_candidates'] = ['unread']
    with pytest.raises(InvalidModelOutput):
        parse(body)


def test_research_cutoff_and_monitor_schedule_remain_frozen():
    body = proposal_body('research')
    body['coverage']['cutoff_at'] = '2026-09-30T00:00:00Z'
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input('research'))
    body = proposal_body('monitoring')
    body['items']['scheduled_at'] = '2026-09-30T00:00:00Z'
    with pytest.raises(InvalidModelOutput):
        validate_output_for_input(parse(body), model_input('monitoring'))


@pytest.mark.parametrize('raw', [None, b'{}', '\ud800', '[' * 1200 + ']' * 1200, ' ' * (1024 * 1024 + 1)])
def test_invalid_encoding_type_depth_and_oversized_output_fail_safely(raw):
    with pytest.raises(InvalidModelOutput):
        parse_model_output(raw)


def test_nested_extra_fields_and_secret_values_are_redacted_from_diagnostics():
    secret = 'SYNTHETIC-secret-should-never-be-echoed'
    body = action_body()
    body['action']['args'][secret] = secret
    with pytest.raises(InvalidModelOutput) as caught:
        parse(body)
    assert secret not in str(caught.value)
    assert secret not in json.dumps(caught.value.errors)
    assert '[unknown]' in caught.value.errors[0]['field']
    body['action']['target']['page_url'] = 'https://user:' + secret + '@example.com'
    with pytest.raises(InvalidModelOutput) as caught:
        parse(body)
    assert secret not in repr(caught.value.errors)


@pytest.mark.parametrize('section,field,value', [
    ('observation', 'run_id', 'other'), ('observation', 'redaction_status', 'BLOCKED'),
    ('observation', 'source_url', 'https://outside.example/'),
    ('verified_checkpoint', 'run_id', 'other'), ('verified_checkpoint', 'task_id', 'other'),
    ('verified_checkpoint', 'contract_version', 2), ('verified_checkpoint', 'current_snapshot_id', 'other'),
    ('verified_checkpoint', 'identity_ref', 'other'),
])
def test_model_input_rejects_unfiltered_and_cross_bound_context(section, field, value):
    body = input_body()
    body[section][field] = value
    with pytest.raises(ValidationError):
        ModelInput.model_validate_json(json.dumps(body))


def test_images_and_memory_must_be_part_of_current_input_contract():
    body = input_body()
    body['image_evidence_ids'] = ['past-evidence']
    with pytest.raises(ValidationError):
        ModelInput.model_validate_json(json.dumps(body))
    body = input_body()
    body['selected_flow_versions'] = ['historical-flow']
    with pytest.raises(ValidationError):
        ModelInput.model_validate_json(json.dumps(body))


def test_schema_has_exact_four_variants_and_no_open_extra_object_fields():
    schema = output_json_schema()
    assert set(schema['discriminator']['mapping']) == {'Action', 'RequestEvidence', 'ProposeResult', 'RequestInput'}
    assert set(schema['$defs']['ModelAction']['properties']['action']['discriminator']['mapping']) == {
        'navigate', 'click', 'input', 'keypress', 'select', 'scroll', 'switch_tab',
        'read_visible', 'screenshot', 'download_attachment',
    }
    assert all(definition.get('additionalProperties') is False
               for definition in schema['$defs'].values() if definition.get('type') == 'object')
    schema['$defs'].clear()
    assert output_json_schema()['$defs']
