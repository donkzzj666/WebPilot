"""Owned SQLite and synthetic gateway ledger; no browser/network/model call."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from webagent.budgets.store import BudgetStore
from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.evidence.service import EvidenceService
from webagent.gateway.store import GatewayStore
from webagent.graph.source import StructuredJSONSourceAdapter
from webagent.models.schema import ProposeResult
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.sessions.models import SessionOwner
from webagent.sessions.store import SessionRegistry
from webagent.tasks.natural import compile_natural
from webagent.verification.arxiv import parse_visible
from webagent.verification.service import VerificationService
from storage.test_gateway_store import Clock
from unit.test_arxiv_direct_parser import URL, TEXT, envelope


def prepared(tmp_path, *, navigation=True, outcome='COMPLETED', gateway=True, query=None,
             extra_navigation=False):
    path = tmp_path / 'business.sqlite3'
    migrate(path)
    clock = Clock()
    clock.wall -= timedelta(seconds=60)  # A synthetic clock must not date evidence in the future.
    content = {'instruction': 'Synthetic direct arxiv:2401.00001v1 research metadata task',
        'compiler_mode': 'natural_language', 'scenario': 'research',
        'sources': [{'source_id': 'arxiv-direct', 'site_id': 'arxiv.org',
                     'origin': 'https://arxiv.org', 'path_prefix': '/abs/2401.00001v1'}],
        'start_urls': [URL], 'parameters': {'queries': [query or 'arxiv:2401.00001v1'],
            'topic_criteria': ['metadata'], 'cutoff_at': '2026-10-01T00:00:00Z', 'max_items': 1},
        'action_policy': {'mode': 'read_only'}, 'identity_ref': None}
    contract = compile_natural(content, task_id='direct-task', version=1,
        created_at=utc_text(clock.utcnow()), provenance=[{'origin': 'explicit_test_configuration',
            'reference': 'synthetic-direct-fixture', 'content_sha256': 'c'*64,
            'authorizes_execution': True}]).contract
    with connect(path) as db, transaction(db):
        create_task(db, task_id='direct-task', instruction=content['instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id='direct-run', task_id='direct-task', contract_version=1,
            graph_version='test-v1', graph_state_schema_version='test-v1',
            model_config_sha256='a'*64, runtime_config_sha256='b'*64)
    budgets = BudgetStore(path, clock=clock)
    scheduler = SchedulerStore(path, budgets=budgets, clock=clock.utcnow, lease_seconds=300)
    generation = scheduler.start_worker('direct-worker')
    scheduler.enqueue('direct-run', [Resource.browser_context('direct-run'), Resource.site_identity('arxiv.org')],
                      expected_state_version=0)
    token = scheduler.claim('direct-worker', generation)
    registry = SessionRegistry(path)
    session = registry.reserve('direct-manager', SessionOwner('run', 'direct-run', 'arxiv.org'), execution_token=token)
    registry.opened(session.session_id, 'direct-manager', execution_token=token)
    store = GatewayStore(path, budgets)
    binding = dict(session_id=session.session_id, manager_id='direct-manager', session_generation=1,
        tab_id='tab-direct', frame_id='frame-direct', page_version='page-direct', width=800, height=600)
    if navigation:
        store.record_observation(token, binding, {'snapshot_id': 'blank', 'source_url': 'about:blank'},
                                 attempt_id='capture-blank')
        action = {'run_id': 'direct-run', 'step_id': 'navigate-direct', 'epoch': token.epoch,
            'snapshot_id': 'blank', 'action_type': 'navigate', 'expected_effect': 'read',
            'target': {'page_url': URL, 'tab_id': binding['tab_id'], 'frame_id': binding['frame_id'],
                       'locator': None, 'write_scope': None}, 'args': {'url': URL}}
        store.prepare(token, action, binding)
        if outcome != 'INTENT':
            store.finish(token, 'navigate-direct', outcome=outcome, result={'source_url': URL})
        if extra_navigation:
            clock.advance(3)
            store.record_observation(token, binding, {'snapshot_id': 'intermediate', 'source_url': URL},
                                     attempt_id='capture-intermediate')
            repeated = deepcopy(action)
            repeated.update(step_id='navigate-repeated', snapshot_id='intermediate')
            store.prepare(token, repeated, binding)
            store.finish(token, 'navigate-repeated', result={'source_url': URL})
    clock.advance(1)
    capture = {'snapshot_id': 'page', 'source_url': URL}
    if gateway:
        observation = store.record_observation(token, binding, capture, attempt_id='capture-page')
    else:
        observation = {**binding, **capture, 'run_id': 'direct-run', 'captured_at': utc_text(clock.utcnow())}
    evidence = EvidenceService(tmp_path)
    view = evidence.publish_observation(observation, envelope(), execution_token=token)
    publication = {**parse_visible(URL, envelope()), 'topic_basis': 'Candidate metadata assessment',
                   'evidence_ids': view['evidence_ids']}
    proposal = ProposeResult.model_validate_json(canonical_json({'type': 'ProposeResult',
        'items': {'scenario': 'research', 'publications': [publication]},
        'coverage': {'searched_sources': ['arxiv-direct'], 'queries': ['arxiv:2401.00001v1'],
                     'cutoff_at': '2026-10-01T00:00:00Z', 'content_pages': 1,
                     'unread_candidates': [], 'gaps': [], 'complete': True},
        'evidence_ids': view['evidence_ids'], 'unresolved': [], 'existing_operation_ids': []}))
    verifier = VerificationService(tmp_path, scheduler=scheduler)
    token = verifier.begin('direct-run', 1, token)
    return SimpleNamespace(path=path, verifier=verifier, token=token, proposal=proposal,
                           evidence=evidence, view=view, binding=binding, clock=clock)


def evaluate(f, proposal=None):
    proposal = proposal or f.proposal
    bindings = StructuredJSONSourceAdapter(f.path.parent, verifier=f.verifier).bindings('direct-run', proposal,
        execution_token=f.token)
    result = asyncio.run(f.verifier.verify('direct-run', proposal, bindings, expected_state_version=2,
        execution_token=f.token))
    return bindings, result


def test_default_source_proves_metadata_and_one_page_coverage_from_original_and_ledgers(tmp_path):
    f = prepared(tmp_path)
    bindings, record = evaluate(f)
    assert bindings and all(value.evidence_path.startswith('/arxiv_direct/') for value in bindings)
    assert all(value['verdict'] == 'PASS' for value in record['evaluation']['fields'])
    assert record['evaluation']['violations'] == []
    # No fixture model substitutes for the independent semantic provider.
    assert next(value for value in record['checks'] if value['criterion_id'] == 'topic')['verdict'] == 'INSUFFICIENT'


@pytest.mark.parametrize('change', [
    lambda value: value['items']['publications'][0].update(title='A model-invented title'),
    lambda value: value['items']['publications'][0].update(authors=['Model-invented author']),
    lambda value: value['items']['publications'][0].update(first_published_at='2024-02-01T00:00:00Z'),
    lambda value: value['coverage'].update(content_pages=0),
    lambda value: value['coverage'].update(queries=['a claimed web search']),
    lambda value: value['coverage'].update(searched_sources=['unread-source']),
])
def test_candidates_cannot_define_their_own_metadata_or_coverage_facts(tmp_path, change):
    f = prepared(tmp_path)
    payload = f.proposal.model_dump(mode='json')
    change(payload)
    proposal = ProposeResult.model_validate_json(canonical_json(payload))
    bindings, record = evaluate(f, proposal)
    assert bindings
    assert any(value['verdict'] == 'FAIL' for value in record['evaluation']['fields'])


@pytest.mark.parametrize('options', [{'navigation': False}, {'outcome': 'FAILED'}, {'outcome': 'UNKNOWN'},
    {'outcome': 'INTENT'}, {'gateway': False}, {'query': 'an ambiguous general search'},
    {'extra_navigation': True}])
def test_missing_actual_gateway_completed_navigation_or_fixed_query_is_insufficient(tmp_path, options):
    f = prepared(tmp_path, **options)
    bindings, record = evaluate(f)
    assert bindings == []
    assert all(value['verdict'] == 'INSUFFICIENT' for value in record['evaluation']['fields'])


def test_original_corruption_with_unchanged_display_never_keeps_metadata_pass(tmp_path):
    f = prepared(tmp_path)
    display = f.evidence.store.metadata(f.view['evidence_ids'][0])
    original = f.evidence.store.metadata(display['original_evidence_id'])
    (tmp_path / original['artifact_path']).write_bytes(b'Changed original bytes')
    bindings, record = evaluate(f)
    assert bindings == []
    assert all(value['verdict'] != 'PASS' for value in record['evaluation']['fields'])


def test_original_json_cannot_self_declare_a_parser_or_coverage_projection(tmp_path):
    f = prepared(tmp_path)
    claimed = {'title': 'Not a gateway envelope', 'text': 'Candidate-only text',
        'arxiv_direct': {**f.proposal.items.model_dump(mode='json'),
            'coverage': f.proposal.coverage.model_dump(mode='json')}}
    row = f.evidence.store.metadata(f.view['evidence_ids'][0])
    published = f.evidence.store.publish('direct-run', canonical_json(claimed).encode(),
        source_url=URL, captured_at=row['captured_at'], object_id='page', snapshot_id='page',
        query_scope='explicit negative fixture', locator_or_page='fixture', execution_token=f.token)
    payload = f.proposal.model_dump(mode='json')
    payload['evidence_ids'] = [published['evidence_id']]
    payload['items']['publications'][0]['evidence_ids'] = [published['evidence_id']]
    bindings, record = evaluate(f, ProposeResult.model_validate_json(canonical_json(payload)))
    assert bindings == []
    assert all(value['verdict'] == 'INSUFFICIENT' for value in record['evaluation']['fields'])
