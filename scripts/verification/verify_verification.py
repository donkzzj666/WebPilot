#!/usr/bin/env python3
"""M1-15 isolated HTTP evidence and runtime aggregation acceptance.

The source generates fresh synthetic disclosures. The probe retrieves the real
HTTP response bytes and publishes them unchanged. No evaluator-only resources,
real accounts, user browser profiles or external providers are consulted.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.evidence.store import EvidenceStore
from webagent.models.schema import ProposeResult
from webagent.state import transition
from webagent.tasks.models import TaskContract
from webagent.verification.models import FieldBinding

SELF_RATING = 'EXECUTOR_SELF_RATING_MUST_NOT_APPEAR'
SYNTHETIC_PROVIDER_KEY = 'owned-verification-provider-without-external-access'


class OwnedHTTPFixture:
    """Bounded local HTTP source; its values are generated before verification."""

    def __init__(self):
        self.active = set()
        self.errors = []
        self.requests = []
        self.documents = {}
        self.handlers = {}
        self.now = utc_text()
        values = []
        for field in ('revenue', 'profit'):
            value = format(Decimal(secrets.randbelow(900000) + 100000) / Decimal('100'), '.2f')
            values.append({'field_id': field, 'entity_id': 'owned-fixture-entity',
                'report_version': 'owned-disclosure-v1', 'period_start': '2025-01-01T00:00:00Z',
                'period_end': '2025-12-31T23:59:59Z', 'period_type': 'annual',
                'metric_definition': field, 'currency': 'USD', 'raw_value': value,
                'disclosed_unit': 'unit', 'normalized_value': value, 'value_origin': 'disclosed',
                'formula': None, 'rounding_rule': 'exact',
                'rounding_lower': value, 'rounding_upper': value, 'channel': 'owned-http'})
        self.documents['/disclosure'] = canonical_json({'values': values}).encode()
        conflicting = deepcopy(values)
        value = format(Decimal(conflicting[0]['normalized_value']) + 1, '.2f')
        for field in ('raw_value', 'normalized_value', 'rounding_lower', 'rounding_upper'):
            conflicting[0][field] = value
        self.documents['/conflict'] = canonical_json({'values': conflicting}).encode()
        self.documents['/research'] = canonical_json({'publications': [{
            'canonical_id': 'owned-paper-v1', 'version': 'v1', 'title': 'Controlled systems study',
            'authors': ['Owned Fixture Author'], 'first_published_at': '2025-06-01T00:00:00Z',
            'revised_at': None, 'abstract': 'The fixture documents controlled systems.',
            'claims': [{'statement': 'The fixture documents controlled systems.'}], 'relations': []}]}).encode()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            assert len(header) < 65536
            lines = header.decode('ascii').split('\r\n')
            method, path, _ = lines[0].split(' ', 2)
            headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                       for line in lines[1:] if ':' in line}
            size = int(headers.get('content-length', '0'))
            assert 0 <= size <= 2 * 1024 * 1024
            body = await asyncio.wait_for(reader.readexactly(size), 5) if size else b''
            status = '200 OK'
            if method == 'GET' and path in self.documents:
                result = self.documents[path]
            elif method == 'POST' and path in self.handlers:
                result = self.handlers[path](json.loads(body))
                if asyncio.iscoroutine(result):
                    result = await result
                result = canonical_json(result).encode()
            else:
                status, result = '404 Not Found', b'{"error":"owned fixture route unavailable"}'
            self.requests.append({'method': method, 'path': path,
                                  'response_sha256': hashlib.sha256(result).hexdigest()})
            writer.write(('HTTP/1.1 ' + status + '\r\nContent-Type: application/json\r\n'
                          'Connection: close\r\nContent-Length: ' + str(len(result)) + '\r\n\r\n').encode() + result)
            await writer.drain()
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.active.discard(task)

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*tuple(self.active), return_exceptions=True)


def pointers(value, prefix=''):
    """All scalar facts, including explicit null, get their own source binding."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            if key != 'evidence_ids':
                escaped = key.replace('~', '~0').replace('/', '~1')
                yield from pointers(child, prefix + '/' + escaped)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from pointers(child, prefix + '/' + str(index))
    else:
        yield prefix


def candidate(document, evidence_id, now):
    values = [{**item, 'evidence_ids': [evidence_id]} for item in document['values']]
    value = {'type': 'ProposeResult', 'items': {'scenario': 'finance', 'values': values},
        'coverage': {'searched_sources': ['owned-http'], 'queries': [], 'cutoff_at': None,
                     'content_pages': 1, 'unread_candidates': [], 'gaps': [], 'complete': True},
        'evidence_ids': [evidence_id], 'unresolved': [], 'existing_operation_ids': []}
    bindings = [FieldBinding(result_path=pointer, evidence_id=evidence_id,
                             evidence_path=pointer) for pointer in pointers(document)]
    return ProposeResult.model_validate_json(canonical_json(value)), bindings


def publish(directory, fixture, run_id, raw, route='/disclosure'):
    return EvidenceStore(directory).publish(run_id, raw, source_url=fixture.origin + route,
        captured_at=datetime.now(timezone.utc), object_id='owned-fixture-entity',
        query_scope='owned HTTP disclosure', locator_or_page='response JSON',
        sensitivity='restricted', redaction_status='BLOCKED', artifact_kind='text', retain=True)


def seed(directory, fixture, name, *, assistance_count=0, optional=False, config_sha256='a' * 64):
    from webagent.tasks.compiler import compile_draft
    path = directory / 'business.sqlite3'
    migrate(path)
    contract = compile_draft({'instruction': 'Verify the owned HTTP annual disclosure',
        'scenario': 'finance', 'source_ids': ['local-fixture'], 'parameters': {
            'entity_id': 'owned-fixture-entity', 'report_version': 'owned-disclosure-v1',
            'period_type': 'annual', 'metrics': ['revenue', 'profit'], 'currency': 'USD'}},
        task_id='task-' + name, version=1, created_at=fixture.now, provenance=[{
            'origin': 'explicit_test_configuration', 'reference': 'runtime-verification-probe-v1',
            'content_sha256': 'a' * 64, 'authorizes_execution': True}]).contract
    contract['sources'] = [{'source_id': 'owned-http', 'site_id': 'owned-http',
                            'origin': fixture.origin, 'path_prefix': '/'}]
    contract['start_urls'] = [fixture.origin + '/disclosure']
    contract['output_schema'] = [{'field_id': name, 'required': True,
                                  'description': 'Explicitly requested ' + name} for name in ('revenue', 'profit')]
    contract['time_scope'] = {'start': '2025-01-01T00:00:00Z', 'end': '2025-12-31T23:59:59Z',
                             'basis': 'Explicit owned HTTP annual disclosure period'}
    if optional:
        contract['acceptance_criteria'].append({'criterion_id': 'optional_management_note',
            'expected_rule': 'An optional management note has an independent source',
            'check_method': 'rule', 'critical': False})
    contract = TaskContract.model_validate_json(canonical_json(contract)).model_dump(mode='json')
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=name, task_id=contract['task_id'], contract_version=1,
                   graph_version='verification-probe-v1', graph_state_schema_version='verification-probe-v1',
                   model_config_sha256=config_sha256, runtime_config_sha256='b' * 64)
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES (?,?)', ('budget-' + name, name))
        if assistance_count:
            db.execute('UPDATE runs SET assistance_count=? WHERE run_id=?', (assistance_count, name))
    transition(path, run_id=name, expected_state_version=0, target='RUNNING')
    return contract


def write_fact(path, contract, run_id, evidence_id, *, historical=False, confirmed=False):
    operation = 'historical-operation' if historical else 'unauthorized-operation'
    with connect(path) as db, transaction(db):
        originating_run = run_id
        if historical:
            originating_run = run_id + '-prior'
            create_run(db, run_id=originating_run, task_id=contract['task_id'], contract_version=1,
                graph_version='verification-probe-v1', graph_state_schema_version='verification-probe-v1',
                model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,
            target,expected_change,identity_ref,precondition_version,status,receipt,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (operation, operation, contract['task_id'], originating_run,
                'owned-fixture-target', 'create_pr', 'synthetic-unapproved-identity', 'source-v1',
                'CONFIRMED' if confirmed else 'UNKNOWN', 'synthetic-receipt' if confirmed else None,
                utc_text(), utc_text()))
        if confirmed:
            db.execute('INSERT INTO write_intent_evidence VALUES(?,?,?,?)',
                       (contract['task_id'], operation, run_id, evidence_id))
    return operation


def seed_research(directory, fixture, config):
    from webagent.tasks.compiler import compile_draft
    path = directory / 'business.sqlite3'
    migrate(path)
    run_id = 'verification-independent-semantic'
    contract = compile_draft({'instruction': 'Find owned controlled systems research', 'scenario': 'research',
        'source_ids': ['local-fixture'], 'parameters': {'queries': ['controlled systems'],
            'topic_criteria': ['controlled systems'], 'cutoff_at': fixture.now, 'max_items': 2}},
        task_id='task-' + run_id, version=1, created_at=fixture.now, provenance=[{
            'origin': 'explicit_test_configuration', 'reference': 'runtime-verification-probe-v1',
            'content_sha256': 'a' * 64, 'authorizes_execution': True}]).contract
    contract['sources'] = [{'source_id': 'owned-http', 'site_id': 'owned-http',
                            'origin': fixture.origin, 'path_prefix': '/'}]
    contract['start_urls'] = [fixture.origin + '/research']
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=run_id, task_id=contract['task_id'], contract_version=1,
            graph_version='verification-probe-v1', graph_state_schema_version='verification-probe-v1',
            model_config_sha256=config.config_sha256, runtime_config_sha256='b' * 64)
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES (?,?)', ('budget-' + run_id, run_id))
    transition(path, run_id=run_id, expected_state_version=0, target='RUNNING')
    return contract, run_id


async def semantic_probe(directory, fixture, check, summaries, *, unsupported=False):
    import httpx
    from webagent.models.transport import DeepSeekTransport, ModelConfig
    from webagent.verification.service import VerificationService

    provider_requests = []
    def semantic_reply(request):
        messages = request['messages']
        assert len(messages) == 2 and messages[0]['role'] == 'system' and messages[1]['role'] == 'user'
        assert 'tools' not in request and 'tool_choice' not in request
        payload = json.loads(messages[1]['content'])
        serialized = canonical_json(payload)
        assert SELF_RATING not in serialized and 'topic_basis' not in serialized
        assert set(payload) == {'protocol_version', 'frozen_requirements', 'candidate_facts', 'untrusted_evidence'}
        assert payload['protocol_version'] == 'm1-15-verifier-v1'
        requirements = payload['frozen_requirements']
        documents = payload['untrusted_evidence']
        facts = payload['candidate_facts']
        source_texts = [publication['abstract'] for document in documents
                        for publication in document['content']['publications']]
        supported = all(topic in ' '.join(source_texts) for topic in requirements['parameters']['topic_criteria'])
        supported = supported and all(claim['statement'] in source_texts
            for publication in facts['publications'] for claim in publication['claims'])
        refs = sorted({document['evidence_id'] for document in documents})
        response = {'checks': [{'criterion_id': criterion['criterion_id'],
            'verdict': 'PASS' if supported else 'FAIL', 'evidence_ids': refs,
            'actual': {'code': 'supported' if supported else 'contradicted'}} for criterion in requirements['criteria']]}
        provider_requests.append({'independent_context': True, 'executor_self_rating_absent': True,
            'tool_authority_absent': True, 'evidence_count': len(documents),
            'deterministic_source_support': supported})
        return {'id': 'owned-verification-provider-reply', 'model': 'deepseek-flash',
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': canonical_json(response)}}],
            'usage': {'prompt_tokens': 17, 'completion_tokens': 11, 'total_tokens': 28}}

    fixture.handlers['/chat/completions'] = semantic_reply
    config = ModelConfig(base_url=fixture.origin, connect_seconds=1.0, read_seconds=3.0, total_seconds=5.0)
    contract, run_id = seed_research(directory, fixture, config)
    # Add the source URL before serving the actual response; the runtime receives
    # only bytes fetched by this client, never this fixture's Python objects.
    document = json.loads(fixture.documents['/research'])
    document['publications'][0]['source_url'] = fixture.origin + '/research'
    if unsupported:
        document['publications'][0]['abstract'] = 'The fixture discusses a different subject.'
    document['coverage'] = {'searched_sources': ['owned-http'], 'queries': ['controlled systems'],
        'cutoff_at': contract['parameters']['cutoff_at'], 'content_pages': 1,
        'unread_candidates': [], 'gaps': [], 'complete': True}
    fixture.documents['/research'] = canonical_json(document).encode()
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(fixture.origin + '/research')
        response.raise_for_status()
    metadata = publish(directory, fixture, run_id, response.content, '/research')
    fetched = json.loads(response.content)
    evidence_id = metadata['evidence_id']
    source_publication = {key: value for key, value in fetched['publications'][0].items() if key != 'abstract'}
    publication = {**source_publication, 'topic_basis': SELF_RATING, 'evidence_ids': [evidence_id]}
    publication['claims'] = [{**claim, 'evidence_ids': [evidence_id]} for claim in publication['claims']]
    coverage = fetched['coverage']
    proposal = ProposeResult.model_validate_json(canonical_json({'type': 'ProposeResult',
        'items': {'scenario': 'research', 'publications': [publication]}, 'coverage': coverage,
        'evidence_ids': [evidence_id], 'unresolved': [], 'existing_operation_ids': []}))
    bindings = [FieldBinding(result_path=pointer, evidence_path=pointer, evidence_id=evidence_id)
                for pointer in pointers({'publications': [source_publication]})]
    bindings += [FieldBinding(result_path=pointer, evidence_path=pointer, evidence_id=evidence_id)
                 for pointer in pointers({'coverage': coverage})]
    service = VerificationService(directory)
    service.begin(run_id, expected_state_version=1)
    transport = DeepSeekTransport(config, SYNTHETIC_PROVIDER_KEY, allow_test_loopback=True)
    try:
        verification = await service.verify(run_id, proposal, bindings, expected_state_version=2, provider=transport)
    finally:
        await transport.aclose()
    result = service.finalize(verification['verification_id'], expected_state_version=2)
    prefix = 'unsupported_' if unsupported else ''
    check(prefix + 'semantic_model_has_independent_context_without_executor_self_rating', len(provider_requests) == 1
          and provider_requests[0]['executor_self_rating_absent'])
    check(prefix + 'semantic_provider_receives_no_browser_or_action_tools', provider_requests[0]['tool_authority_absent'])
    check(prefix + 'semantic_checks_are_derived_from_transferred_original_support',
          provider_requests[0]['deterministic_source_support'] == (not unsupported))
    if unsupported:
        check('unsupported_semantic_claim_cannot_succeed', result.outcome != 'SUCCEEDED'
              and any(item.verdict.value == 'FAIL' for item in result.checks))
    else:
        check('supported_semantic_and_rule_checks_can_succeed', result.outcome == 'SUCCEEDED')
    with connect(directory / 'business.sqlite3') as db:
        used = db.execute('SELECT model_calls_used FROM run_budgets WHERE run_id=?', (run_id,)).fetchone()[0]
        check(prefix + 'semantic_model_call_uses_original_run_budget', used == 1)
    summaries.extend(provider_requests)


def reject(call, accepted_codes):
    try:
        call()
    except BusinessError as error:
        assert error.code in accepted_codes, (error.code, accepted_codes)
        return error.code
    raise AssertionError('A prohibited operation unexpectedly succeeded')


async def verify(output, report):
    import httpx
    from webagent.verification.service import VerificationService

    def check(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    fixture = await OwnedHTTPFixture().start()
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(fixture.origin + '/disclosure')
            response.raise_for_status()
            raw = response.content
            conflict_response = await client.get(fixture.origin + '/conflict')
            conflict_response.raise_for_status()
        check('source_uses_actual_owned_http_response', raw == fixture.documents['/disclosure'])
        (output / 'owned-disclosure.json').write_bytes(raw)
        document = json.loads(raw)
        scenarios = ('positive', 'assisted', 'wrong_field', 'missing', 'conflict', 'historical_unknown',
                     'unauthorized', 'partial', 'optional_missing')
        summaries = []
        semantic_requests = []
        with tempfile.TemporaryDirectory(prefix='webpilot-verification-') as temporary:
            for name in scenarios:
                directory = Path(temporary).resolve() / name
                run_id = 'verification-' + name
                contract = seed(directory, fixture, run_id, assistance_count=2 if name == 'assisted' else 0,
                                optional=name == 'optional_missing')
                metadata = publish(directory, fixture, run_id, raw)
                proposal, bindings = candidate(document, metadata['evidence_id'], fixture.now)
                service = VerificationService(directory)
                service.begin(run_id, expected_state_version=1)
                if name == 'positive':
                    code = reject(lambda: transition(directory / 'business.sqlite3', run_id=run_id,
                        expected_state_version=2, target='SUCCEEDED'), {'INVALID_PARAMETER'})
                    check('direct_state_cannot_invent_success', bool(code))
                if name in ('wrong_field', 'partial'):
                    data = proposal.model_dump(mode='json')
                    index = 0 if name == 'wrong_field' else 1
                    value = format(Decimal(data['items']['values'][index]['normalized_value']) + 7, '.2f')
                    for field in ('raw_value', 'normalized_value', 'rounding_lower', 'rounding_upper'):
                        data['items']['values'][index][field] = value
                    if name == 'wrong_field':
                        # No independently supported output remains in this case.
                        data['items']['values'] = data['items']['values'][:1]
                        bindings = [binding for binding in bindings if binding.result_path.startswith('/values/0/')]
                    proposal = ProposeResult.model_validate_json(canonical_json(data))
                if name == 'missing':
                    (directory / metadata['artifact_path']).unlink()
                if name == 'conflict':
                    other = publish(directory, fixture, run_id, conflict_response.content, '/conflict')
                    data = proposal.model_dump(mode='json')
                    data['evidence_ids'].append(other['evidence_id'])
                    data['items']['values'][0]['evidence_ids'].append(other['evidence_id'])
                    proposal = ProposeResult.model_validate_json(canonical_json(data))
                    bindings += [FieldBinding(result_path=binding.result_path, evidence_path=binding.evidence_path,
                        evidence_id=other['evidence_id']) for binding in list(bindings)
                        if binding.result_path.startswith('/values/0/')]
                if name == 'historical_unknown':
                    write_fact(directory / 'business.sqlite3', contract, run_id, metadata['evidence_id'], historical=True)
                if name == 'unauthorized':
                    write_fact(directory / 'business.sqlite3', contract, run_id, metadata['evidence_id'], confirmed=True)
                verification = await service.verify(run_id, proposal, bindings, expected_state_version=2)
                result = service.finalize(verification['verification_id'], expected_state_version=2)
                result = result.model_dump(mode='json')
                fields = verification['evaluation']['fields']
                if name in ('positive', 'assisted'):
                    check(name + '_verified_fields_can_succeed', result['outcome'] == 'SUCCEEDED'
                          and all(item['verdict'] == 'PASS' for item in result['checks'])
                          and all(item['verdict'] == 'PASS' for item in fields))
                elif name == 'partial':
                    check('partial_retains_supported_item_and_exposes_failed_field', result['outcome'] == 'PARTIAL'
                          and bool(result['unresolved']) and any(item['verdict'] == 'FAIL' for item in fields))
                elif name == 'optional_missing':
                    check('noncritical_gap_remains_explicit', any(item['criterion_id'] == 'optional_management_note'
                          and item['verdict'] == 'INSUFFICIENT' for item in result['checks']) and bool(result['unresolved']))
                else:
                    check(name + '_cannot_succeed', result['outcome'] != 'SUCCEEDED')
                    if name == 'conflict':
                        check('conflicting_original_values_have_conflict_verdict',
                              any(item['verdict'] == 'CONFLICT' for item in fields))
                    if name == 'historical_unknown':
                        check('prior_run_unknown_write_is_still_visible', any(item['operation_id'] == 'historical-operation'
                              and item['status'] == 'UNKNOWN' for item in result['side_effects']))
                    if name == 'unauthorized':
                        check('read_only_contract_records_critical_write_violation',
                              any(item['critical_violation'] for item in result['side_effects']))
                stored = service.read(run_id)
                check(name + '_result_matches_committed_run', stored['result'] == result)
                if name == 'assisted':
                    check('assistance_comes_from_persistent_run', result['assistance_count'] == 2 and stored['assistance'] == 'assisted')
                if name == 'positive':
                    check('autonomous_success_has_explicit_marker', stored['assistance'] == 'autonomous')
                    again = service.finalize(verification['verification_id'], expected_state_version=2)
                    check('repeat_finalize_returns_same_immutable_result', again.model_dump(mode='json') == result)
                    from fastapi.testclient import TestClient
                    from webagent.api import create_app
                    from webagent.config import Settings
                    from webagent.security import LocalApiPolicy
                    credential = secrets.token_urlsafe(48)
                    policy = LocalApiPolicy(credential, frozenset({'127.0.0.1:18082'}),
                                            frozenset({'http://127.0.0.1:18082'}))
                    with TestClient(create_app(Settings(directory), secret_store=object(), local_api_policy=policy),
                                    base_url='http://127.0.0.1:18082') as client:
                        url = '/v1/runs/' + run_id + '/result'
                        headers = {'Authorization': 'Bearer ' + credential}
                        check('result_http_requires_local_authentication', client.get(url).status_code == 401)
                        check('result_http_rejects_foreign_origin', client.get(url, headers={**headers,
                            'Origin': 'https://attacker.invalid'}).status_code == 403)
                        response = client.get(url, headers=headers)
                        check('result_http_returns_committed_aggregation_and_field_evidence', response.status_code == 200
                            and response.json() == stored and response.headers['cache-control'] == 'no-store')
                        check('result_http_cannot_supply_verdicts_or_file_paths', client.get(url + '?outcome=SUCCEEDED',
                            headers=headers).status_code == 422 and client.get(url + '?path=/etc/passwd',
                            headers=headers).status_code == 422)
                        check('result_http_has_no_public_finalization_mutation', client.post(url, headers=headers,
                            json={'outcome': 'SUCCEEDED'}).status_code == 405)
                with connect(directory / 'business.sqlite3') as db:
                    row = db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()
                    events = db.execute("SELECT count(*) FROM task_events WHERE run_id=? AND event_type='result_ready'", (run_id,)).fetchone()[0]
                    check(name + '_terminal_and_single_result_event_agree', row['state'] == result['outcome'] and events == 1)
                summaries.append({'case': name, 'outcome': result['outcome'],
                    'check_verdicts': [item['verdict'] for item in result['checks']],
                    'field_count': len(fields), 'assistance_count': result['assistance_count'],
                    'unresolved_count': len(result['unresolved']), 'side_effect_count': len(result['side_effects'])})
            directory = Path(temporary).resolve() / 'changed_after_verification'
            run_id = 'verification-changed-after-verification'
            seed(directory, fixture, run_id)
            metadata = publish(directory, fixture, run_id, raw)
            proposal, bindings = candidate(document, metadata['evidence_id'], fixture.now)
            service = VerificationService(directory)
            service.begin(run_id, expected_state_version=1)
            verification = await service.verify(run_id, proposal, bindings, expected_state_version=2)
            (directory / metadata['artifact_path']).write_bytes(b'changed synthetic original')
            reject(lambda: service.finalize(verification['verification_id'], expected_state_version=2),
                   {'STATE_CONFLICT', 'EVIDENCE_CORRUPT'})
            with connect(directory / 'business.sqlite3') as db:
                check('original_changed_after_verification_blocks_finalization',
                    db.execute('SELECT state FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] == 'VERIFYING'
                    and db.execute('SELECT count(*) FROM run_results WHERE run_id=?', (run_id,)).fetchone()[0] == 0)
                check('failed_finalization_does_not_emit_result_event', db.execute(
                    "SELECT count(*) FROM task_events WHERE run_id=? AND event_type='result_ready'", (run_id,)).fetchone()[0] == 0)
            await semantic_probe(Path(temporary).resolve() / 'semantic', fixture, check, semantic_requests)
            await semantic_probe(Path(temporary).resolve() / 'semantic-unsupported', fixture, check, semantic_requests,
                                 unsupported=True)
        report['scenarios'] = summaries
        report['semantic_requests'] = semantic_requests
        report['source_requests'] = fixture.requests
        report['scope'] = {'synthetic_http_origins': 1, 'real_external_requests': 0,
                           'evaluator_only_access': False, 'private_data': 'disposable isolated SQLite and evidence directory'}
        check('owned_source_has_no_protocol_errors', not fixture.errors)
        report['passed'] = True
    finally:
        await fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {'task': 'M1-15', 'passed': False, 'checks': {}, 'started_at': utc_text()}
    try:
        asyncio.run(verify(args.output_dir, report))
    except Exception as error:
        report['error_type'] = type(error).__name__
        if isinstance(error, BusinessError):
            report['error'] = {'code': error.code, 'field': error.field, 'status': error.status}
        report['error_locations'] = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                                    for frame in traceback.extract_tb(error.__traceback__)[-8:]]
    report['finished_at'] = utc_text()
    report['artifact_sha256'] = {str(path.relative_to(args.output_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(args.output_dir.rglob('*')) if path.is_file() and path.name != 'report.json'
        and '.security' not in path.parts}
    path = args.output_dir / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(path)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
