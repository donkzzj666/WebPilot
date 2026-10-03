"""Ground model proposals in user input, then build program-validated contracts.

The model may suggest a scenario and typed business parameters. Sources, start
URLs, identity, write permissions, budgets and acceptance rules never come from
model output or web content. No network, model call or execution happens here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, field_validator

from ..db.repository import canonical_json
from ..errors import BusinessError
from .compiler import Compilation
from .models import (
    BudgetProfile, CodeParameters, CreateTaskRequest, FinanceParameters, GrafanaParameters,
    Id, MonitoringParameters, RepositoryWritePolicy, ResearchParameters, Scenario,
    StrictModel, TaskContract, finite_json,
)

COMPILER_VERSION = 'm1-06-natural-compiler-v1'
PARAMETER_MODELS = (FinanceParameters, CodeParameters, GrafanaParameters, ResearchParameters, MonitoringParameters)
PARAMETER_NAMES = frozenset(name for model in PARAMETER_MODELS for name in model.model_fields)
AMBIGUOUS_PATHS = frozenset({'scenario', 'parameters'} | set(PARAMETER_NAMES)
                            | {'parameters.' + name for name in PARAMETER_NAMES})
AmbiguousPath = Literal[tuple(sorted(AMBIGUOUS_PATHS))]
MAX_PROPOSAL_BYTES = 65536


class InvalidNaturalProposal(ValueError):
    """Static safe diagnostics: no model output, unknown field or input values."""
    def __init__(self):
        super().__init__('Natural-language proposal failed local validation')
        self.errors = [{'field': '$', 'reason': 'invalid_proposal'}]


class NaturalProposal(StrictModel):
    scenario: Scenario | None
    parameters: Annotated[dict[Id, JsonValue], Field(max_length=40)]
    ambiguous_fields: Annotated[list[AmbiguousPath], Field(max_length=80)]

    @field_validator('parameters')
    @classmethod
    def allowed_parameters(cls, value):
        finite_json(value)
        if not set(value) <= PARAMETER_NAMES:
            raise ValueError('only declared business parameters may be proposed')
        try:
            _validate_unselected(value)
        except BusinessError:
            raise ValueError('proposed business parameter has an invalid type or value') from None
        return value


def extract_schema() -> dict:
    schema = NaturalProposal.model_json_schema()
    declarations: dict[str, list[dict]] = {}
    applicable: dict[str, list[str]] = {}
    for model in PARAMETER_MODELS:
        model_schema = model.model_json_schema()
        scenario = model.model_fields['scenario'].annotation.__args__[0]
        operation = model.model_fields.get('operation_kind')
        label = scenario + ('/' + operation.annotation.__args__[0] if operation else '')
        for name, value in model_schema['properties'].items():
            declaration = deepcopy(value)
            declaration.pop('title', None)
            if name in ('cutoff_at', 'scheduled_at'):
                declaration['pattern'] = r'(?:Z|\+00:00)$'
            if declaration not in declarations.setdefault(name, []):
                declarations[name].append(declaration)
            applicable.setdefault(name, []).append(label)
    properties = {}
    for name, alternatives in declarations.items():
        property_schema = deepcopy(alternatives[0]) if len(alternatives) == 1 else {'anyOf': alternatives}
        property_schema['description'] = 'Applicable to: ' + ', '.join(applicable[name]) + '. Omit when missing, ambiguous, or unsupported by trusted user instruction.'
        properties[name] = property_schema
    schema['properties']['parameters'] = {
        'type': 'object', 'properties': properties, 'additionalProperties': False,
        'description': 'Partial business parameters only. Every supplied value must match its scenario schema; do not invent missing fields.',
    }
    return schema


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError('nonfinite number')


def parse_proposal(raw: str) -> NaturalProposal:
    try:
        if type(raw) is not str or len(raw.encode('utf-8')) > MAX_PROPOSAL_BYTES:
            raise ValueError('invalid size or type')
        data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        finite_json(data)  # Also rejects 1e999, not just nonstandard NaN literals.
        return NaturalProposal.model_validate(data)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise InvalidNaturalProposal() from None


def _invalid(field='body') -> BusinessError:
    return BusinessError('INVALID_PARAMETER', '任务字段未通过程序校验。', field=field)


def _request(content: dict) -> CreateTaskRequest:
    try:
        request = CreateTaskRequest.model_validate_json(canonical_json(content))
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise _invalid() from None
    if request.compiler_mode != 'natural_language':
        raise _invalid('compiler_mode')
    return request


def trusted_instruction(text: str) -> str:
    """Discard explicitly quoted/web-derived blocks before extracting facts.

Unclosed blocks and an inline webpage marker consume the remaining text;
guessing where trusted instructions resume would promote quoted content.
The complete original instruction is retained in the persisted task/contract.
"""
    # Fenced blocks and explicit web-content containers may span many lines.
    value = re.sub(r'(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)', '', text)
    # Malformed/inline fences cannot safely mark a return to trusted prose.
    value = re.split(r'`{3,}|~{3,}', value, maxsplit=1)[0]
    value = re.sub(r'(?is)<(?:web_content|page_content|web_context|blockquote)\b[^>]*>.*?(?:</(?:web_content|page_content|web_context|blockquote)\s*>|\Z)', '', value)
    retained = []
    quoting = False
    for line in value.splitlines():
        if re.match(r'^\s*>', line):
            quoting = True
            continue
        if quoting:
            if not line.strip():
                quoting = False
            continue
        retained.append(line)
    value = '\n'.join(retained)
    marker = re.search(
        r'(?i)(?:网页(?:内容|正文|原文|摘录|返回)|页面(?:内容|正文|原文)|引用网页|以下是网页内容)\s*[:：]'
        r'|(?:网页|页面|网站|站点)(?:说|写道|要求|指示|建议|提示)'
        r'|(?:web|page)[ _-]*(?:content|context)\s*[:：]'
        r'|(?:按|按照|遵循)网页(?:中|里)?的指令', value)
    return (value[:marker.start()] if marker else value).strip()


# Enumerated translations only; never use these words to invent IDs or grants.
ENUM_WORDS = {
    'scenario': {
        'finance': ('财报', '财务', '年报', '季报', 'finance'),
        'operations': ('Grafana', '仪表盘', '看板', '代码修复', '修复代码', '修复仓库', 'operations'),
        'research': ('论文', '文献', '研究', 'research'),
        'monitoring': ('监控', '监测', 'monitoring'),
    },
    'period_type': {
        'annual': ('年度', '年报', 'annual'),
        'quarterly': ('季度', '季报', 'quarterly'),
        'year_to_date': ('年初至今', 'year_to_date'),
        'point_in_time': ('时点', 'point_in_time'),
    },
    'operation_kind': {
        'code_repair': ('代码修复', '修复代码', '修复仓库', 'code_repair'),
        'grafana_read': ('Grafana', '仪表盘', '看板', 'grafana_read'),
    },
    'currency': {'USD': ('美元', 'USD'), 'CNY': ('人民币', 'CNY'), 'EUR': ('欧元', 'EUR'),
                 'GBP': ('英镑', 'GBP'), 'JPY': ('日元', 'JPY')},
    'metrics': {'revenue': ('营收', '营业收入', 'revenue'), 'net_income': ('净利润', 'net_income'),
                'operating_cash_flow': ('经营现金流', 'operating_cash_flow')},
    'source_kind': {'cisa_kev': ('CISA KEV', 'cisa_kev'), 'security_community': ('安全社区', 'security_community')},
}
_ALTERNATIVE = re.compile(r'或者|还是|或|不确定|尚未确定|未确定|\bor\b', re.I)
_UNNAMED = re.compile(r'某个|某家|哪个公司|哪家公司|哪个|哪家|未指定|对象待定')
_RELATIVE_TIME = re.compile(r'最近|最新|上期|上一期|去年|今年|本年|上个月|本月|上个季度|本季度|过去\s*\d+\s*[天月年]')
_OBJECT_FIELDS = frozenset({'entity_id', 'repository', 'dashboard_id', 'source_id'})
_TIME_FIELDS = frozenset({'report_version', 'cutoff_at', 'scheduled_at'})


def _has_alternatives(instruction: str) -> bool:
    # A clear ban on several write operations is not an unresolved target choice.
    operation = r'(?:修改|写入|删除|提交|合并)'
    permission_ban = r'(?:不要|不得|不允许|禁止|不)\s*' + operation + r'(?:(?:\s*[、或和]\s*|\s*或者\s*)' + operation + r')*'
    remaining = re.sub(permission_ban, '', instruction)
    # Conservative boundary: selecting the correct target under exclusions or
    # negated commands needs an explicit structured clarification in this phase.
    exclusions = r'不要|不用|不得|不允许|不需要|无需读取|不读取|不查看|不查询|不选择|禁止|排除|除了|除外|而不是|不是|(?<!分)别(?:读取|查看|选)|仅供参考|只是(?:例子|示例|参考)'
    return bool(_ALTERNATIVE.search(remaining) or re.search(exclusions, remaining))


def _multiple_objects(name: str, value: str, instruction: str) -> bool:
    if name not in _OBJECT_FIELDS:
        return False
    if re.search(r'分别|对比|比较|多家|两家|各家|多个(?:对象|公司|仓库|看板)|两个(?:对象|公司|仓库|看板)', instruction):
        return True
    adjacent = r'(?:和|与|及|以及|、|\band\b)'
    escaped = re.escape(value)
    return bool(re.search(escaped + r'\s*' + adjacent, instruction)
                or re.search(adjacent + r'\s*' + escaped, instruction))


def _literal(text: str, value: str) -> bool:
    if not value or not value.strip():
        return False
    # Avoid matching A inside BETA, 2025 inside 20250, or IDs inside another ID.
    pattern = re.escape(value)
    if value[0].isascii() and (value[0].isalnum() or value[0] in '_-'):
        pattern = r'(?<![A-Za-z0-9_-])' + pattern
    if value[-1].isascii() and (value[-1].isalnum() or value[-1] in '_-'):
        pattern += r'(?![A-Za-z0-9_-])'
    return re.search(pattern, text) is not None


def _enum_supported(name: str, value: str, instruction: str) -> bool:
    choices = ENUM_WORDS.get(name, {})
    matches = {candidate for candidate, words in choices.items()
               if any(_literal(instruction, word) for word in words)}
    # Scalar enum conflicts request clarification. Metric lists may name several.
    if name != 'metrics' and matches and matches != {value}:
        return False
    return value in matches or _literal(instruction, value)


def _grounded(name: str, value, instruction: str) -> bool:
    if name in _OBJECT_FIELDS and _UNNAMED.search(instruction):
        return False
    if name in _TIME_FIELDS and _RELATIVE_TIME.search(instruction):
        return False
    if name == 'report_version' and len(set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', instruction))) > 1:
        return False
    if isinstance(value, str):
        if _multiple_objects(name, value, instruction):
            return False
        if name == 'report_version':
            quarter = re.fullmatch(r'((?:19|20)\d{2})-?[Qq]([1-4])', value)
            if quarter:
                year, number = quarter.groups()
                chinese = '一二三四'[int(number) - 1]
                markers = (f'Q{number}', f'第{number}季度', f'{number}季度', f'第{chinese}季度', f'{chinese}季度')
                found = {n for n in range(1, 5) if any(word in instruction for word in
                         (f'Q{n}', f'第{n}季度', f'{n}季度', f'第{"一二三四"[n-1]}季度', f'{"一二三四"[n-1]}季度'))}
                return _literal(instruction, year) and found == {int(number)} and any(word in instruction for word in markers)
        return _enum_supported(name, value, instruction)
    if type(value) is bool:
        if name != 'baseline':
            return False
        if re.search(r'不(?:需要)?(?:建立|创建|初始化)基线|无需基线|增量监控|非基线', instruction):
            return not value
        return value and any(word in instruction for word in ('建立基线', '创建基线', '初始化基线', '首次基线'))
    if value is None:
        return name == 'confirmed_boundary' and any(word in instruction for word in ('无已确认边界', '没有已确认边界', '首次基线'))
    if type(value) in (int, float):
        if not math.isfinite(value):
            return False
        labels = {'max_items': ('最多', '上限', '返回', '收集', '列出'),
                  'max_list_items': ('列表最多', '列表上限'),
                  'max_details': ('详情最多', '详情上限')}.get(name, ())
        number = re.escape(str(value))
        return bool(re.search(r'\b' + re.escape(name) + r'\s*[:=：]\s*' + number + r'(?![\d.])', instruction)
                    or any(re.search(re.escape(label) + r'\s*' + number + r'\s*[篇项条个](?![\d.])', instruction) for label in labels))
    if isinstance(value, list):
        return bool(value) and all(_grounded(name, item, instruction) for item in value)
    if isinstance(value, dict):
        if name != 'variables':
            return False
        if not value:
            return any(word in instruction for word in ('无变量', '不设置变量'))
        return all(isinstance(child, str) and any(_literal(instruction, expression)
                   for expression in (f'{key}={child}', f'{key}:{child}', f'{key}：{child}'))
                   for key, child in value.items())
    return False


def _validate_unselected(parameters: dict, candidates=PARAMETER_MODELS) -> None:
    for name, value in parameters.items():
        declarations = [model.model_fields[name] for model in candidates if name in model.model_fields]
        if not declarations:
            raise _invalid('parameters')
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
            raise _invalid('parameters.' + name)


def prepare_content(content: dict, proposal: NaturalProposal) -> dict:
    """Accept supported suggestions; explicit API values always retain priority."""
    request = _request(content)
    try:
        proposal = NaturalProposal.model_validate(proposal.model_dump())
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise InvalidNaturalProposal() from None
    _validate_unselected(request.parameters)
    result = request.model_dump(mode='json', exclude={'idempotency_key'})
    instruction = trusted_instruction(request.instruction)
    ambiguous = set(proposal.ambiguous_fields)
    # Instructions containing unresolved alternatives are deliberately not
    # resolved by the model. Explicit structured API input may resolve them.
    alternatives = _has_alternatives(instruction)
    scenario_conflict = (proposal.parameters.get('scenario') is not None
                         and proposal.parameters['scenario'] != proposal.scenario)
    if request.scenario is None and proposal.scenario is not None and 'scenario' not in ambiguous and not scenario_conflict:
        if not alternatives and _enum_supported('scenario', proposal.scenario, instruction):
            result['scenario'] = proposal.scenario
    for name, value in proposal.parameters.items():
        if name in result['parameters']:
            continue
        if (alternatives or 'parameters' in ambiguous or name in ambiguous
                or 'parameters.' + name in ambiguous):
            continue
        if name == 'scenario':
            if value == result['scenario']:
                result['parameters'][name] = value
            continue
        if _grounded(name, value, instruction):
            try:
                candidates = {
                    'finance': (FinanceParameters,), 'operations': (CodeParameters, GrafanaParameters),
                    'research': (ResearchParameters,), 'monitoring': (MonitoringParameters,),
                }.get(result['scenario'], PARAMETER_MODELS)
                if result['scenario'] == 'operations':
                    operation = result['parameters'].get('operation_kind')
                    if operation is None and not ({'operation_kind', 'parameters.operation_kind'} & ambiguous):
                        proposed = proposal.parameters.get('operation_kind')
                        if isinstance(proposed, str) and _grounded('operation_kind', proposed, instruction):
                            operation = proposed
                    if operation in ('code_repair', 'grafana_read'):
                        candidates = (CodeParameters,) if operation == 'code_repair' else (GrafanaParameters,)
                _validate_unselected({name: value}, candidates)
            except BusinessError:
                # A model suggestion is not a malformed user API parameter.
                continue
            result['parameters'][name] = deepcopy(value)
    return result


def _parameters(request: CreateTaskRequest) -> tuple[type | None, dict | None, list[str]]:
    parameters = dict(request.parameters)
    _validate_unselected(parameters)
    if 'scenario' in parameters:
        _validate_unselected({'scenario': parameters['scenario']})
        if request.scenario is not None and parameters['scenario'] != request.scenario:
            raise _invalid('parameters.scenario')
    if request.scenario is None:
        _validate_unselected(parameters)
        return None, None, ['scenario']
    parameters['scenario'] = request.scenario
    model = {'finance': FinanceParameters, 'research': ResearchParameters, 'monitoring': MonitoringParameters}.get(request.scenario)
    if request.scenario == 'operations':
        operation = parameters.get('operation_kind')
        if 'operation_kind' not in parameters:
            _validate_unselected(parameters, (CodeParameters, GrafanaParameters))
            return None, None, ['parameters.operation_kind']
        model = {'code_repair': CodeParameters, 'grafana_read': GrafanaParameters}.get(operation)
        if model is None:
            raise _invalid('parameters.operation_kind')
    try:
        parsed = model.model_validate_json(canonical_json(parameters))
        return model, parsed.model_dump(mode='json'), []
    except ValidationError as error:
        details = error.errors(include_input=False, include_context=False, include_url=False)
        if any(detail['type'] != 'missing' for detail in details):
            raise _invalid('parameters') from None
        return model, None, ['parameters.' + str(detail['loc'][0]) for detail in details]


def compile_natural(content: dict, *, task_id: str, version: int,
                    created_at: str, provenance: list[dict]) -> Compilation:
    request = _request(content)
    missing = []
    if request.sources is None:
        missing.append('sources')
    if request.start_urls is None:
        missing.append('start_urls')
    if request.sources and request.start_urls:
        for url in request.start_urls:
            if not any(source.permits(url) for source in request.sources):
                raise _invalid('start_urls')
    model, parameters, parameter_missing = _parameters(request)
    missing.extend(parameter_missing)
    policy = request.action_policy
    if model is CodeParameters:
        if not isinstance(policy, RepositoryWritePolicy):
            missing.append('action_policy')
        else:
            for name in ('repository', 'base_sha', 'branch', 'independent_rules_ref', 'required_checks'):
                if name not in request.parameters:
                    continue
                expected, supplied = getattr(policy, name), request.parameters[name]
                if name == 'required_checks':
                    expected, supplied = set(expected), set(supplied)
                if expected != supplied:
                    raise _invalid('parameters.' + name)
        if request.identity_ref is None:
            missing.append('identity_ref')
    elif model is not None and isinstance(policy, RepositoryWritePolicy):
        raise _invalid('action_policy')
    if model is GrafanaParameters:
        if request.time_scope is None:
            missing.append('time_scope')
        elif request.time_scope.start is None or request.time_scope.end is None:
            missing.append('time_scope')
    if model is FinanceParameters and 'report_version' in request.parameters:
        if _RELATIVE_TIME.search(request.parameters['report_version']):
            missing.append('parameters.report_version')
        if request.parameters.get('period_type') == 'quarterly':
            report = request.parameters['report_version']
            if (not re.search(r'(?:19|20)\d{2}', report)
                    or not re.search(r'[Qq][1-4](?!\d)|第?[一二三四1-4]季度', report)):
                missing.append('parameters.report_version')
    if model is MonitoringParameters and 'source_id' in request.parameters and request.sources:
        if request.parameters['source_id'] not in {source.source_id for source in request.sources}:
            raise _invalid('parameters.source_id')
    if missing:
        return Compilation(None, list(dict.fromkeys(missing)))
    assert parameters is not None and request.sources is not None and request.start_urls is not None
    target, fields, criteria, time_scope = _rules(parameters)
    if request.time_scope is not None:
        time_scope = request.time_scope.model_dump(mode='json')
        if model is ResearchParameters and time_scope['end'] != parameters['cutoff_at']:
            raise _invalid('time_scope')
        if model is MonitoringParameters and time_scope['end'] != parameters['scheduled_at']:
            raise _invalid('time_scope')
    schedule_slot = None
    if model is MonitoringParameters:
        digest = hashlib.sha256(canonical_json([task_id, version, parameters['scheduled_at']]).encode()).hexdigest()
        schedule_slot = 'monitor-' + digest[:40]
    contract = {
        'schema_version': 'm0-contract-v1', 'task_id': task_id, 'contract_version': version,
        'scenario': request.scenario, 'objective': request.instruction, 'original_instruction': request.instruction,
        'targets': [target], 'sources': [source.model_dump(mode='json') for source in request.sources],
        'start_urls': request.start_urls, 'parameters': parameters, 'time_scope': time_scope,
        'output_schema': [{'field_id': name, 'required': True, 'description': description} for name, description in fields],
        'acceptance_criteria': [{'criterion_id': name, 'expected_rule': rule, 'check_method': method, 'critical': True}
                                for name, rule, method in criteria],
        'action_policy': policy.model_dump(mode='json'), 'identity_ref': request.identity_ref,
        'budget_profile': BudgetProfile().model_dump(mode='json'), 'memory_mode': 'disabled',
        'snapshot_id': None, 'batch_id': None, 'schedule_slot': schedule_slot,
        'provenance': deepcopy(provenance), 'created_at': created_at,
    }
    try:
        validated = TaskContract.model_validate_json(canonical_json(contract))
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise _invalid('contract') from None
    return Compilation(validated.model_dump(mode='json'), [])


def _rules(parameters: dict) -> tuple:
    """General program-owned output/acceptance rules, bound to declared facts."""
    scenario = parameters['scenario']
    time_scope = {'start': None, 'end': None, 'basis': '以明确的业务对象版本为准。'}
    if scenario == 'finance':
        target = {'object_id': parameters['entity_id'], 'kind': 'entity', 'canonical_name': parameters['entity_id']}
        fields = [('metrics', '全部指定财务指标及其来源证据'), ('report_version', '报告版本及报告期间'),
                  ('currency', '币种'), ('period_type', '期间口径')]
        criteria = [('entity', '证据主体必须等于指定 entity_id。', 'rule'),
                    ('report', '报告版本、期间口径、币种必须与任务参数一致。', 'rule'),
                    ('metrics', '每项指定指标必须有相同期间口径的原始来源证据。', 'rule')]
        time_scope['basis'] = '指定报告版本：' + parameters['report_version']
    elif scenario == 'operations' and parameters['operation_kind'] == 'code_repair':
        target = {'object_id': parameters['repository'], 'kind': 'repository', 'canonical_name': parameters['repository']}
        fields = [('changes', '授权文件范围内的变更及对应版本'), ('checks', '指定检查及独立规则的结果')]
        criteria = [('write_scope', '仓库、基础版本、工作分支、文件与动作均必须位于显式写入授权范围内。', 'rule'),
                    ('required_checks', '全部指定检查必须在交付版本上通过。', 'rule'),
                    ('independent_rules', '指定的独立验证规则必须通过。', 'independent_test')]
        time_scope['basis'] = '指定基础提交：' + parameters['base_sha']
    elif scenario == 'operations':
        target = {'object_id': parameters['dashboard_id'], 'kind': 'dashboard', 'canonical_name': parameters['dashboard_id']}
        fields = [('panels', '指定面板在明确时间窗口内的可见数据'), ('variables', '实际使用的变量'), ('timezone', '实际使用的时区')]
        criteria = [('dashboard', '仪表盘和面板标识必须与指定对象一致。', 'rule'),
                    ('window', '变量、时区和起止时间必须与任务契约一致。', 'rule')]
    elif scenario == 'research':
        object_id = 'queries-' + hashlib.sha256(canonical_json(parameters['queries']).encode()).hexdigest()[:40]
        target = {'object_id': object_id, 'kind': 'publication', 'canonical_name': ' / '.join(parameters['queries'])}
        fields = [('publications', '符合主题与截止时间的文献、原始链接及支持证据')]
        criteria = [('topic', '所选文献必须满足每项明确的主题条件。', 'rule'),
                    ('cutoff', '所选文献发布时间必须不晚于指定截止时间。', 'rule'),
                    ('count', '交付数量不得超过指定 max_items。', 'rule')]
        time_scope = {'start': None, 'end': parameters['cutoff_at'], 'basis': '明确的研究截止时间。'}
    else:
        target = {'object_id': parameters['source_id'], 'kind': 'monitor_source', 'canonical_name': parameters['source_id']}
        fields = [('items', '指定监测源在既定列表和详情上限内的条目'), ('boundary', '基线模式及已确认边界')]
        criteria = [('source', '条目必须来自显式授权的监测源且源类别相符。', 'rule'),
                    ('limits', '列表与详情读取数量不得超过契约上限。', 'rule'),
                    ('boundary', '必须保留显式基线模式与已确认边界，不得以猜测替代。', 'rule')]
        time_scope = {'start': None, 'end': parameters['scheduled_at'], 'basis': '明确的监测计划时刻。'}
    return target, fields, criteria, time_scope
