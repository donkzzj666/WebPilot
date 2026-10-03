"""Pure verification of frozen contracts against parsed original artifacts.

No rule text is executed. Only compiler-owned (identifier, text) pairs are
recognized, and a proposed value is never its own evidence.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from decimal import Context, Decimal, DecimalException, localcontext
from urllib.parse import urlsplit

from ..models.schema import CodeResult, FinanceResult, GrafanaResult, MonitorResult, ProposeResult, ResearchResult
from ..tasks.compiler import fixture_rules
from ..tasks.models import CodeParameters, FinanceParameters, GrafanaParameters, MonitoringParameters, ResearchParameters, TaskContract
from ..tasks.natural import _rules as natural_rules
from .models import Check, EvidenceDocument, FieldBinding, FieldCheck, RuleEvaluation, Verdict

CHECKER_VERSION = 'runtime-rules-v1'
_MISSING = object()
_SKIP = {'evidence_ids', 'topic_basis', 'scenario', 'operation_kind'}


def _escape(value: str) -> str:
    return value.replace('~', '~0').replace('/', '~1')


def _leaves(value, path=''):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            if key not in _SKIP:
                yield from _leaves(child, path + '/' + _escape(key))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _leaves(child, path + '/' + str(index))
    else:
        yield path, value


def _resolve(value, pointer):
    if pointer == '':
        return value
    for escaped in pointer[1:].split('/'):
        key = escaped.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            value = value.get(key, _MISSING)
        elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', key):
            index = int(key)
            value = value[index] if index < len(value) else _MISSING
        else:
            return _MISSING
    return value


def _canonical(value):
    # JSON representations preserve types: True is not evidence for numeric 1.
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'evidence_ids':
                yield from child
            else:
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _worst(verdicts):
    values = set(verdicts)
    for verdict in (Verdict.CONFLICT, Verdict.FAIL, Verdict.INSUFFICIENT):
        if verdict in values:
            return verdict
    return Verdict.PASS if values else Verdict.INSUFFICIENT


def _known_rules(contract):
    parameters = contract.parameters.model_dump(mode='json')
    pairs = {(name, rule) for name, rule in fixture_rules(parameters)[2]}
    pairs.update((name, rule) for name, rule, _method in natural_rules(parameters)[2])
    return pairs


def _bound_reference_ids(items, path, fallback):
    """The nearest entity/claim evidence list limits admissible field bindings."""
    scope = fallback
    current = items
    for part in path[1:].split('/'):
        if isinstance(current, dict) and 'evidence_ids' in current:
            scope = set(current['evidence_ids'])
        current = _resolve(current, '/' + part)
        if current is _MISSING:
            break
    return scope


def _field_checks(contract, proposal, documents, bindings, run_id):
    values = dict(_leaves(proposal.items.model_dump(mode='json')))
    counters = {}
    if isinstance(proposal.items, (ResearchResult, MonitorResult)):
        values.update(_leaves(proposal.coverage.model_dump(mode='json'), '/coverage'))
    if isinstance(contract.parameters, MonitoringParameters):
        values.update({'/context/source_kind': contract.parameters.source_kind,
                       '/context/confirmed_boundary': contract.parameters.confirmed_boundary,
                       '/context/list_items': None, '/context/detail_pages': None})
        counters = {'/context/list_items': contract.parameters.max_list_items,
                    '/context/detail_pages': contract.parameters.max_details}
    grouped = defaultdict(list)
    violations, unresolved = [], []
    by_id = {doc.evidence_id: doc for doc in documents}
    if len(by_id) != len(documents):
        violations.append('duplicate_evidence_ids')
    if len({doc.run_id for doc in documents}) > 1 or (run_id is not None and any(doc.run_id != run_id for doc in documents)):
        violations.append('evidence_run_mismatch')
    declared = set(proposal.evidence_ids)
    references = set(_references(proposal.items.model_dump(mode='json')))
    if not references <= declared:
        violations.append('undeclared_field_evidence')
    if len(proposal.evidence_ids) != len(declared):
        violations.append('duplicate_proposal_evidence')
    for eid in references | declared:
        doc = by_id.get(eid)
        if doc is None or not doc.readable or doc.content is None:
            unresolved.append('unavailable_evidence')
        elif not any(source.permits(doc.source_url) for source in contract.sources):
            violations.append('evidence_source_outside_scope')
        elif run_id is not None and doc.run_id != run_id:
            violations.append('evidence_run_mismatch')
        elif (not isinstance(contract.parameters, ResearchParameters)
              and doc.object_id not in {t.object_id for t in contract.targets}
              and not (doc.snapshot_id is not None and doc.object_id == doc.snapshot_id)):
            violations.append('evidence_object_outside_scope')
    for binding in bindings:
        if binding.result_path not in values:
            violations.append('binding_result_path_unknown')
        elif binding.evidence_id not in declared:
            violations.append('binding_evidence_undeclared')
        elif binding.evidence_id not in _bound_reference_ids(proposal.items.model_dump(mode='json'), binding.result_path, declared):
            violations.append('binding_not_in_field_evidence')
        elif (binding.result_path.startswith('/context/')
              and binding.evidence_path != binding.result_path.replace('/context/', '/verification_context/', 1)
              and not (by_id.get(binding.evidence_id) is not None
                       and by_id[binding.evidence_id].snapshot_id is not None
                       and binding.evidence_path == binding.result_path.replace('/context/', '/parsed_text/verification_context/', 1))):
            violations.append('binding_context_path_invalid')
        else:
            grouped[binding.result_path].append(binding)
    fields = []
    for path, expected in values.items():
        selected = grouped[path]
        observed, used, insufficient, invalid_counter = [], [], not selected, False
        for binding in selected:
            doc = by_id.get(binding.evidence_id)
            if (doc is None or not doc.readable or doc.content is None
                    or (run_id is not None and doc.run_id != run_id)
                    or not any(source.permits(doc.source_url) for source in contract.sources)):
                insufficient = True
                continue
            actual = _resolve(doc.content, binding.evidence_path)
            if actual is _MISSING:
                insufficient = True
                continue
            observed.append(_canonical(actual))
            used.append(doc.evidence_id)
            if path in counters and (type(actual) is not int or actual < 0 or actual > counters[path]):
                invalid_counter = True
        if len(set(observed)) > 1:
            verdict, code = Verdict.CONFLICT, 'original_values_conflict'
        elif invalid_counter:
            verdict, code = Verdict.FAIL, 'observed_read_count_outside_limit'
        elif path not in counters and observed and observed[0] != _canonical(expected):
            verdict, code = Verdict.FAIL, 'original_value_mismatch'
        elif insufficient or not observed:
            verdict, code = Verdict.INSUFFICIENT, 'original_field_unavailable'
        else:
            verdict, code = Verdict.PASS, 'original_value_matches'
        fields.append(FieldCheck(result_path=path, verdict=verdict,
                                 evidence_ids=sorted(set(used)), actual={'code': code}))
    return fields, violations, unresolved


_UNITS = {'1': Decimal(1), 'unit': Decimal(1), 'units': Decimal(1), '元': Decimal(1),
          'thousand': Decimal(1000), 'thousands': Decimal(1000), '千': Decimal(1000),
          'million': Decimal(1000000), 'millions': Decimal(1000000), '百万': Decimal(1000000),
          'billion': Decimal(1000000000), 'billions': Decimal(1000000000), '十亿': Decimal(1000000000),
          '万': Decimal(10000), '万元': Decimal(10000), '亿元': Decimal(100000000)}


def _finance_number(item):
    if item.value_origin != 'disclosed':
        # Formula execution requires a separate versioned parser and independently
        # bound operands. Arbitrary formula text is never evaluated here.
        return Verdict.INSUFFICIENT, 'derived_formula_not_supported'
    multiplier = _UNITS.get(item.disclosed_unit.lower().strip())
    raw = item.raw_value.strip()
    if not multiplier or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', raw):
        return Verdict.INSUFFICIENT, 'number_or_unit_not_supported'
    numbers = (raw, item.normalized_value, item.rounding_lower, item.rounding_upper)
    if any(len(number) > 1000 for number in numbers):
        return Verdict.INSUFFICIENT, 'number_not_supported'
    try:
        with localcontext(Context(prec=max(28, max(map(len, numbers)) + 20))):
            # Keep all disclosed digits through unit scaling and interval
            # arithmetic; the process-global precision must not round a wrong
            # candidate into agreement. The supported unit scale is <= 10^9.
            value = Decimal(raw) * multiplier
            normalized = Decimal(item.normalized_value)
            lower, upper = Decimal(item.rounding_lower), Decimal(item.rounding_upper)
            if value != normalized:
                return Verdict.FAIL, 'unit_normalization_mismatch'
            if item.rounding_rule in {'exact', '精确', '无取整'}:
                expected_lower = expected_upper = value
            elif item.rounding_rule in {'disclosed_precision', 'nearest', '按披露精度取整'}:
                half_step = Decimal(1).scaleb(Decimal(raw).as_tuple().exponent) * multiplier / 2
                expected_lower, expected_upper = value - half_step, value + half_step
            else:
                return Verdict.INSUFFICIENT, 'rounding_rule_not_supported'
            if (lower, upper) != (expected_lower, expected_upper):
                return Verdict.FAIL, 'rounding_interval_mismatch'
    except (DecimalException, OverflowError):
        return Verdict.INSUFFICIENT, 'number_not_supported'
    return Verdict.PASS, 'decimal_normalization_verified'


def _scenario(contract, proposal, docs):
    """Return program checks and severe scope violations, never caller verdicts."""
    items, p = proposal.items, contract.parameters
    facts, violations, unresolved = {}, [], []
    def fact(names, condition, code, *, severe=False, insufficient=False):
        verdict = Verdict.PASS if condition else (Verdict.INSUFFICIENT if insufficient else Verdict.FAIL)
        for name in names:
            facts[name] = (verdict, code)
        if not condition:
            (violations if severe else unresolved).append(code)
    if items.scenario != contract.scenario:
        return {}, ['result_scenario_mismatch'], []
    if isinstance(items, FinanceResult) and isinstance(p, FinanceParameters):
        scope = all((v.entity_id, v.report_version, v.period_type, v.currency) ==
                    (p.entity_id, p.report_version, p.period_type, p.currency) for v in items.values)
        fact(('source_and_entity', 'entity'), bool(items.values) and all(v.entity_id == p.entity_id for v in items.values), 'financial_entity_mismatch', severe=bool(items.values))
        period_mismatch = any(
            (contract.time_scope.start is not None and v.period_start != contract.time_scope.start)
            or (contract.time_scope.end is not None and v.period_end != contract.time_scope.end)
            for v in items.values)
        if not scope or period_mismatch:
            fact(('report_scope', 'report'), False, 'financial_report_scope_mismatch', severe=True)
        elif contract.time_scope.start is None or contract.time_scope.end is None:
            fact(('report_scope', 'report'), False, 'frozen_financial_period_missing', insufficient=True)
        else:
            fact(('report_scope', 'report'), True, 'financial_report_scope_verified')
        ids = [v.field_id for v in items.values]
        if len(ids) != len(set(ids)):
            violations.append('duplicate_financial_fields')
        available_metrics = {v.metric_definition for v in items.values} | set(ids)
        fact(('requested_metrics', 'metrics'), set(p.metrics) <= available_metrics, 'requested_financial_metrics_missing', insufficient=True)
        semantic_groups = {'metrics', 'report_version', 'currency', 'period_type'}
        if not {f.field_id for f in contract.output_schema if f.required and f.field_id not in semantic_groups} <= set(ids):
            unresolved.append('required_financial_output_missing')
        if not {f.field_id for f in contract.output_schema if not f.required and f.field_id not in semantic_groups} <= set(ids):
            unresolved.append('optional_financial_output_missing')
        for v in items.values:
            verdict, code = _finance_number(v)
            if verdict != Verdict.PASS:
                unresolved.append(code)
                for name in ('requested_metrics', 'metrics'):
                    old = facts[name]
                    facts[name] = (_worst([old[0], verdict]), code)
    elif isinstance(items, CodeResult) and isinstance(p, CodeParameters):
        target = (items.repository, items.base_sha, items.branch, items.independent_rules_ref) == (p.repository, p.base_sha, p.branch, p.independent_rules_ref)
        permitted = all(contract.action_policy.permits_file(path) for path in items.changed_files)
        fact(('write_scope',), target and permitted, 'code_write_scope_mismatch', severe=True)
        if not items.pr_url or not items.head_sha or not items.changed_files:
            unresolved.append('code_delivery_incomplete')
        if items.pr_url:
            pr = urlsplit(items.pr_url)
            if pr.scheme != 'https' or pr.hostname != 'github.com' or not re.fullmatch('/' + re.escape(p.repository) + r'/pull/[1-9][0-9]*', pr.path) or pr.query or pr.fragment:
                violations.append('code_pr_target_mismatch')
        head_stable = items.head_sha is not None and items.head_before_verification == items.head_sha == items.head_after_verification
        if not head_stable:
            violations.append('code_verified_head_mismatch')
        def test_pass(test):
            return (test is not None and head_stable and test.commit_sha == items.head_sha
                    and test.conclusion == 'success' and bool(test.evidence_ids)
                    and any((d := docs.get(eid)) is not None and d.readable and d.artifact_kind == 'ci'
                            and d.commit_sha == test.commit_sha and d.test_run_id == test.test_run_id
                            for eid in test.evidence_ids))
        names = [t.name for t in items.required_checks]
        fact(('required_checks',), len(names) == len(set(names)) and set(names) == set(p.required_checks) and all(test_pass(t) for t in items.required_checks), 'required_ci_not_verified')
        fact(('independent_rules',), test_pass(items.independent_test_result), 'independent_ci_not_verified')
    elif isinstance(items, GrafanaResult) and isinstance(p, GrafanaParameters):
        panels = [x.panel_id for x in items.panels]
        fact(('dashboard_scope', 'dashboard'), items.dashboard_id == p.dashboard_id and len(panels) == len(set(panels)) and set(panels) == set(p.panel_ids), 'grafana_target_or_panels_mismatch', severe=items.dashboard_id != p.dashboard_id)
        fact(('display_settings', 'window'), (items.variables, items.timezone, items.time_range) == (p.variables, p.timezone, contract.time_scope), 'grafana_query_state_mismatch', severe=True)
        if any(not panel.points for panel in items.panels):
            unresolved.append('grafana_panel_data_missing')
        if any(point.value is None for panel in items.panels for point in panel.points):
            unresolved.append('grafana_value_missing')
        if contract.time_scope.start and any(point.observed_at < contract.time_scope.start for panel in items.panels for point in panel.points):
            violations.append('grafana_point_outside_window')
        if contract.time_scope.end and any(point.observed_at > contract.time_scope.end for panel in items.panels for point in panel.points):
            violations.append('grafana_point_outside_window')
    elif isinstance(items, ResearchResult) and isinstance(p, ResearchParameters):
        ids = [x.canonical_id for x in items.publications]
        if len(ids) != len(set(ids)):
            violations.append('duplicate_publication')
        if any(not any(s.permits(x.source_url) for s in contract.sources) for x in items.publications):
            violations.append('publication_source_outside_scope')
        fact(('cutoff',), all(x.first_published_at <= p.cutoff_at and (x.revised_at is None or x.revised_at <= p.cutoff_at) for x in items.publications), 'publication_after_cutoff', severe=True)
        fact(('item_limit', 'count'), len(items.publications) <= p.max_items, 'publication_limit_exceeded')
        if not items.publications:
            unresolved.append('research_no_deliverable')
        if proposal.coverage.cutoff_at != p.cutoff_at or set(proposal.coverage.queries) != set(p.queries):
            violations.append('research_query_or_cutoff_mismatch')
        if set(proposal.coverage.searched_sources) != {s.source_id for s in contract.sources}:
            unresolved.append('research_source_coverage_missing')
    elif isinstance(items, MonitorResult) and isinstance(p, MonitoringParameters):
        fact(('monitor_scope', 'source'), (items.source_id, items.scheduled_at) == (p.source_id, p.scheduled_at), 'monitor_source_or_slot_mismatch', severe=True)
        fact(('baseline', 'boundary'), items.baseline == p.baseline and items.verified_contiguous and (p.confirmed_boundary is None or p.confirmed_boundary in items.examined_ranges), 'monitor_boundary_not_verified')
        # Actual list/detail counts are independently parsed through the
        # /context bindings below. Event count and claimed coverage are not
        # measurements of browser reads.
        fact(('limits',), True, 'monitor_read_counts_verified')
        if items.pending_items or items.gaps or not proposal.coverage.complete:
            unresolved.append('monitor_coverage_incomplete')
        if not items.examined_ranges or not items.verified_contiguous:
            unresolved.append('monitor_continuity_missing')
        if items.verified_boundary != items.discovered_boundary:
            unresolved.append('monitor_verified_boundary_incomplete')
        if not items.baseline and items.verified_boundary is None:
            unresolved.append('monitor_verified_boundary_missing')
    else:
        violations.append('result_operation_subtype_mismatch')
    if proposal.coverage.gaps or proposal.coverage.unread_candidates or not proposal.coverage.complete:
        unresolved.append('coverage_incomplete')
    return facts, violations, unresolved


def evaluate_rules(contract: TaskContract, proposal: ProposeResult,
                   documents: list[EvidenceDocument], bindings: list[FieldBinding], *,
                   checked_at: datetime, run_id: str | None = None) -> RuleEvaluation:
    """Compare every business leaf with explicitly bound, independently loaded facts.

    A binding names a path within ``proposal.items`` (or ``/coverage/...`` for
    research and monitoring coverage) and a path within an
    original artifact's parsed JSON. It supplies no verdict, value or authority.
    Monitoring additionally requires /context/{source_kind,confirmed_boundary,
    list_items,detail_pages}, bound to /verification_context/* in the original.
    The two counts are read from the artifact; the proposal supplies no count.
    The service must load all referenced evidence for the current Run.
    """
    fields, violations, unresolved = _field_checks(contract, proposal, documents, bindings, run_id)
    if any(doc.captured_at > checked_at for doc in documents):
        violations.append('evidence_capture_after_verification')
    docs = {doc.evidence_id: doc for doc in documents}
    facts, scenario_violations, scenario_unresolved = _scenario(contract, proposal, docs)
    violations.extend(scenario_violations)
    unresolved.extend(scenario_unresolved)
    if isinstance(proposal.items, MonitorResult):
        if any(field.result_path == '/context/source_kind' and field.verdict in {Verdict.FAIL, Verdict.CONFLICT} for field in fields):
            violations.append('monitor_source_kind_mismatch')
    field_verdict = _worst(field.verdict for field in fields)
    refs = sorted({eid for field in fields for eid in field.evidence_ids})
    known = _known_rules(contract)
    checks, semantic = [], []
    for criterion in contract.acceptance_criteria:
        if (criterion.criterion_id, criterion.expected_rule) not in known:
            verdict, code = Verdict.INSUFFICIENT, 'unsupported_frozen_rule'
        else:
            topic = criterion.criterion_id in {'topic', 'topic_scope'} and isinstance(proposal.items, ResearchResult)
            verdict, code = ((Verdict.PASS, 'original_fields_verified') if topic else
                             facts.get(criterion.criterion_id, (Verdict.INSUFFICIENT, 'unsupported_frozen_rule')))
            verdict = _worst([verdict, field_verdict])
            if topic or criterion.check_method == 'semantic':
                semantic.append(criterion.criterion_id)
                # Only independently supported rule facts may advance to the
                # semantic layer. Missing or contradictory facts retain a
                # different diagnostic so a semantic PASS cannot replace them.
                if verdict == Verdict.PASS:
                    verdict, code = Verdict.INSUFFICIENT, 'independent_semantic_review_required'
                elif field_verdict != Verdict.PASS:
                    code = 'original_fields_unverified'
        if violations:
            verdict, code = Verdict.FAIL, 'verification_scope_violation'
        if verdict == Verdict.PASS and not refs:
            verdict, code = Verdict.INSUFFICIENT, 'original_evidence_missing'
        checks.append(Check(criterion_id=criterion.criterion_id, expected_rule=criterion.expected_rule,
                            actual={'code': code}, verdict=verdict, evidence_ids=refs,
                            checked_at=checked_at, checker_version=CHECKER_VERSION))
    unresolved.extend('field_unverified:' + field.result_path for field in fields if field.verdict != Verdict.PASS)
    # Individual supported entities remain deliverable when another entity has a
    # gap. Scope violations suppress delivery rather than laundering a wrong target.
    groups = defaultdict(list)
    for field in fields:
        parts = field.result_path.split('/')
        prefix = '/'.join(parts[:3]) if len(parts) > 2 and parts[1] in {'values', 'publications', 'panels', 'events'} else ''
        groups[prefix].append(field)
    def deliverable_group(path, group):
        if not path or any(f.verdict in {Verdict.FAIL, Verdict.CONFLICT} for f in group):
            return False
        passed = {f.result_path[len(path):] for f in group if f.verdict == Verdict.PASS}
        kind = path.split('/')[1]
        required = {
            'values': {'/field_id', '/entity_id', '/report_version', '/period_start', '/period_end',
                       '/period_type', '/currency', '/raw_value', '/normalized_value', '/disclosed_unit'},
            'publications': {'/canonical_id', '/version', '/source_url', '/first_published_at', '/title'},
            'panels': {'/panel_id', '/unit'},
            'events': {'/source_id', '/object_id', '/semantic_version', '/event_type', '/observed_at', '/content_fingerprint'},
        }.get(kind, set())
        if kind == 'values' and _finance_number(proposal.items.values[int(path.split('/')[2])])[0] != Verdict.PASS:
            return False
        if kind == 'panels':
            payload = proposal.items.model_dump(mode='json')
            if not any(p.startswith('/points/') and p.endswith('/value') and _resolve(payload, path + p) is not None for p in passed):
                return False
        return bool(required) and required <= passed
    deliverable = [path for path, group in groups.items() if deliverable_group(path, group)]
    if isinstance(proposal.items, CodeResult) and fields and field_verdict == Verdict.PASS:
        deliverable = ['']
    if violations:
        deliverable = []
    return RuleEvaluation(checks=checks, fields=fields, semantic_criterion_ids=semantic,
                          violations=sorted(set(violations)), deliverable_paths=sorted(deliverable),
                          unresolved=sorted(set(unresolved)))
