"""Deterministic M1-04 compiler for explicitly selected synthetic local sources.

This does not parse natural language, contact a model/site, or start a Run. Its
fixed acceptance/output rules are test configuration, never inferred permission.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Annotated

from pydantic import TypeAdapter, ValidationError

from ..db.repository import canonical_json
from ..errors import BusinessError
from .models import (
    BudgetProfile, CodeParameters, CreateTaskRequest, FinanceParameters,
    GrafanaParameters, MonitoringParameters, ResearchParameters,
    RepositoryWritePolicy, Scenario, TaskContract,
)

COMPILER_MODE = 'fixture'
COMPILER_VERSION = 'm1-04-fixture-compiler-v1'
FIXTURE_SOURCE_ID = 'local-fixture'
FIXTURE_ORIGIN = 'http://127.0.0.1:8765'
FIXTURE_CONFIGURATION = {
    'compiler_mode': COMPILER_MODE,
    'compiler_version': COMPILER_VERSION,
    'source_id': FIXTURE_SOURCE_ID,
    'site_id': FIXTURE_SOURCE_ID,
    'origin': FIXTURE_ORIGIN,
    'path_prefix': '/',
}
FIXTURE_CONFIGURATION_SHA256 = hashlib.sha256(
    canonical_json(FIXTURE_CONFIGURATION).encode()).hexdigest()

PARAMETER_MODELS = (
    FinanceParameters, CodeParameters, GrafanaParameters,
    ResearchParameters, MonitoringParameters,
)


@dataclass(frozen=True)
class Compilation:
    contract: dict | None
    missing_fields: list[str]


def invalid(message: str, field: str) -> BusinessError:
    return BusinessError('INVALID_PARAMETER', message, field=field)


def validation_error(error: ValidationError, prefix: str = '') -> BusinessError:
    detail = error.errors(include_url=False)[0]
    path = '.'.join(str(part) for part in detail['loc'])
    return invalid(detail['msg'], '.'.join(x for x in (prefix, path) if x) or 'body')


def partial_parameters(model, parameters: dict) -> tuple[dict | None, list[str]]:
    """Missing fields request clarification; any malformed supplied value fails."""
    try:
        result = model.model_validate_json(canonical_json(parameters))
        return result.model_dump(mode='json'), []
    except ValidationError as error:
        errors = error.errors(include_url=False)
        for detail in errors:
            if detail['type'] != 'missing':
                field = '.'.join(('parameters', *(str(item) for item in detail['loc'])))
                raise invalid(detail['msg'], field) from error
        return None, ['parameters.' + '.'.join(str(item) for item in detail['loc']) for detail in errors]


def validate_unselected(parameters: dict, models: tuple) -> None:
    """Still reject malformed fields when a discriminator has not been supplied.

Only fields from a potentially applicable schema are admitted. Where schemas
share a field, at least one explicitly declared type must accept its value.
"""
    for name, value in parameters.items():
        declarations = [model.model_fields[name] for model in models if name in model.model_fields]
        if not declarations:
            raise invalid('Unknown parameter', 'parameters.' + name)
        for declaration in declarations:
            annotation = declaration.annotation
            if declaration.metadata:
                annotation = Annotated[annotation, *declaration.metadata]
            try:
                TypeAdapter(annotation).validate_json(canonical_json(value), strict=True)
                break
            except ValidationError:
                continue
        else:
            raise invalid('Invalid parameter value', 'parameters.' + name)


def compile_draft(content: dict, *, task_id: str, version: int,
                  created_at: str, provenance: list[dict]) -> Compilation:
    """Compile declared fixture fields or return a deterministic missing list.

The caller owns persistence, provenance of each revision, and authorization for
starting execution. A complete result remains a prepared contract only.
"""
    try:
        request = CreateTaskRequest.model_validate(content)
    except ValidationError as error:
        raise validation_error(error) from error

    missing = []
    if request.source_ids is None:
        missing.append('source_ids')
    elif request.source_ids != [FIXTURE_SOURCE_ID]:
        raise invalid('Only the explicitly configured local-fixture source is available', 'source_ids')
    if request.scenario is None:
        missing.append('scenario')

    parameters = dict(request.parameters)
    supplied_scenario = parameters.get('scenario')
    if 'scenario' in parameters:
        try:
            TypeAdapter(Scenario).validate_python(supplied_scenario, strict=True)
        except ValidationError as error:
            raise invalid('Invalid parameter scenario', 'parameters.scenario') from error
        if request.scenario is not None and supplied_scenario != request.scenario:
            raise invalid('Scenario and parameters disagree', 'parameters.scenario')
    if request.scenario is not None:
        parameters['scenario'] = request.scenario

    chosen = None
    if request.scenario == 'finance':
        chosen = FinanceParameters
    elif request.scenario == 'research':
        chosen = ResearchParameters
    elif request.scenario == 'monitoring':
        chosen = MonitoringParameters
    elif request.scenario == 'operations':
        operation = parameters.get('operation_kind')
        if 'operation_kind' not in parameters:
            validate_unselected(parameters, (CodeParameters, GrafanaParameters))
            missing.append('parameters.operation_kind')
        elif operation == 'code_repair':
            chosen = CodeParameters
        elif operation == 'grafana_read':
            chosen = GrafanaParameters
        else:
            raise invalid('Unsupported operation kind', 'parameters.operation_kind')
    else:
        validate_unselected(parameters, PARAMETER_MODELS)

    parsed = None
    if chosen is not None:
        parsed, parameter_missing = partial_parameters(chosen, parameters)
        missing.extend(parameter_missing)

    policy = request.action_policy
    if chosen is CodeParameters:
        if not isinstance(policy, RepositoryWritePolicy):
            missing.append('action_policy')
        else:
            for name in ('repository', 'base_sha', 'branch', 'independent_rules_ref', 'required_checks'):
                if name not in parameters:
                    continue
                left, right = getattr(policy, name), parameters[name]
                if name == 'required_checks':
                    left, right = set(left), set(right)
                if left != right:
                    raise invalid('Code parameters and write policy disagree', 'parameters.' + name)
        if request.identity_ref is None:
            missing.append('identity_ref')
    elif chosen is not None and isinstance(policy, RepositoryWritePolicy):
        raise invalid('Only code_repair may authorize repository writes', 'action_policy')

    if chosen is MonitoringParameters and 'source_id' in parameters:
        if parameters['source_id'] != FIXTURE_SOURCE_ID:
            raise invalid('Monitoring source must be the configured local-fixture source', 'parameters.source_id')

    if missing:
        return Compilation(None, missing)

    # All discriminators and required fields have now passed schema validation.
    assert parsed is not None
    scenario = request.scenario
    target, output_fields, criteria, time_scope, start_path = fixture_rules(parsed)
    fixture_provenance = {
        'origin': 'explicit_test_configuration',
        'reference': COMPILER_VERSION,
        'content_sha256': FIXTURE_CONFIGURATION_SHA256,
        'authorizes_execution': False,
    }
    provenance = [dict(item) for item in provenance]
    if fixture_provenance not in provenance:
        provenance.append(fixture_provenance)
    schedule_slot = None
    if scenario == 'monitoring':
        slot_input = canonical_json([task_id, version, parsed['scheduled_at']])
        schedule_slot = 'fixture-' + hashlib.sha256(slot_input.encode()).hexdigest()[:40]
    contract = {
        'schema_version': 'm0-contract-v1',
        'task_id': task_id,
        'contract_version': version,
        'scenario': scenario,
        'objective': request.instruction,
        'original_instruction': request.instruction,
        'targets': [target],
        'sources': [{key: FIXTURE_CONFIGURATION[key]
                     for key in ('source_id', 'site_id', 'origin', 'path_prefix')}],
        'start_urls': [FIXTURE_ORIGIN + start_path],
        'parameters': parsed,
        'time_scope': time_scope,
        'output_schema': [{'field_id': name, 'required': True, 'description': description}
                          for name, description in output_fields],
        'acceptance_criteria': [
            {'criterion_id': name, 'expected_rule': rule, 'check_method': 'rule', 'critical': True}
            for name, rule in criteria],
        'action_policy': policy.model_dump(mode='json'),
        'identity_ref': request.identity_ref,
        'budget_profile': BudgetProfile().model_dump(mode='json'),
        'memory_mode': 'disabled',
        'snapshot_id': None,
        'batch_id': None,
        'schedule_slot': schedule_slot,
        'provenance': provenance,
        'created_at': created_at,
    }
    try:
        checked = TaskContract.model_validate_json(canonical_json(contract))
    except ValidationError as error:
        raise validation_error(error, 'contract') from error
    return Compilation(checked.model_dump(mode='json'), [])


def fixture_rules(parameters: dict) -> tuple:
    """Fixed synthetic test rules; values come exclusively from declared fields."""
    scenario = parameters['scenario']
    time_scope = {'start': None, 'end': None, 'basis': 'Explicit local fixture configuration'}
    if scenario == 'finance':
        target = {'object_id': parameters['entity_id'], 'kind': 'entity',
                  'canonical_name': parameters['entity_id']}
        fields = [('metrics', 'Values for each explicitly requested metric'),
                  ('report_version', 'Explicitly selected report version'),
                  ('currency', 'Explicitly selected currency'), ('period_type', 'Explicitly selected period type')]
        criteria = [('source_and_entity', 'Evidence identifies the declared fixture source and entity'),
                    ('report_scope', 'Report version, currency and period type match the declared parameters'),
                    ('requested_metrics', 'Every explicitly requested metric has supporting fixture evidence')]
        time_scope['basis'] = 'Declared report version: ' + parameters['report_version']
        path = '/finance'
    elif scenario == 'operations' and parameters['operation_kind'] == 'code_repair':
        target = {'object_id': parameters['repository'], 'kind': 'repository',
                  'canonical_name': parameters['repository']}
        fields = [('changes', 'Changes to explicitly allowed fixture files'),
                  ('checks', 'Results of all explicitly required checks')]
        criteria = [('write_scope', 'All changes remain inside the explicit repository write policy'),
                    ('required_checks', 'All declared required checks pass for the declared fixture revision'),
                    ('independent_rules', 'Declared independent fixture rules pass')]
        time_scope['basis'] = 'Declared base revision: ' + parameters['base_sha']
        path = '/operations/code-repair'
    elif scenario == 'operations':
        target = {'object_id': parameters['dashboard_id'], 'kind': 'dashboard',
                  'canonical_name': parameters['dashboard_id']}
        fields = [('panels', 'Visible data for each explicitly requested fixture panel'),
                  ('variables', 'Declared dashboard variable values'), ('timezone', 'Declared display timezone')]
        criteria = [('dashboard_scope', 'Fixture dashboard and panels match the declared identifiers'),
                    ('display_settings', 'Fixture evidence uses declared variables and timezone')]
        time_scope['basis'] = 'Fixture dashboard display timezone: ' + parameters['timezone']
        path = '/operations/grafana'
    elif scenario == 'research':
        identifier = 'queries-' + hashlib.sha256(canonical_json(parameters['queries']).encode()).hexdigest()[:40]
        target = {'object_id': identifier, 'kind': 'publication', 'canonical_name': 'Declared fixture research queries'}
        fields = [('publications', 'Fixture publications with source references, at most max_items')]
        criteria = [('topic_scope', 'Included fixture publications satisfy the declared topic criteria'),
                    ('cutoff', 'Publication dates are no later than the explicitly declared cutoff'),
                    ('item_limit', 'Results do not exceed the explicitly declared max_items')]
        time_scope = {'start': None, 'end': parameters['cutoff_at'], 'basis': 'Explicit research cutoff'}
        path = '/research'
    else:
        target = {'object_id': parameters['source_id'], 'kind': 'monitor_source',
                  'canonical_name': parameters['source_id']}
        fields = [('items', 'Fixture monitoring items within declared list/detail limits'),
                  ('boundary', 'Explicit fixture baseline or confirmed boundary')]
        criteria = [('monitor_scope', 'Fixture evidence matches the declared monitoring source and source kind'),
                    ('limits', 'List and detail counts stay within declared limits'),
                    ('baseline', 'Fixture result identifies baseline mode and declared confirmed boundary')]
        time_scope = {'start': None, 'end': parameters['scheduled_at'], 'basis': 'Explicit monitoring schedule time'}
        path = '/monitoring'
    return target, fields, criteria, time_scope, path
