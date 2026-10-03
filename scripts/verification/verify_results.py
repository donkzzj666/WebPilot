#!/usr/bin/env python3
"""M1-22 real browser/API result reads over independently aggregated fixtures.

Contracts, Run creation, assistance counts and one UNKNOWN write intent are
explicit trusted ledger fixtures. Owned HTTP response bytes are published and
verified by the production VerificationService; no result/verdict is inserted.
This checks result presentation, not the M1-25 public business execution loop.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).resolve().parent)]
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.cache/ms-playwright')

from verify_startup import Process, TRACE_FLAGS, free_ports, now
from verify_task_entry import public_artifacts, safe_error, scan_secrets, wait_until
from verify_verification import OwnedHTTPFixture, candidate, publish, seed, write_fact
from webagent.api import create_app
from webagent.config import Settings, disable_external_tracing
from webagent.db import LATEST_VERSION, connect, transaction
from webagent.db.repository import canonical_json, create_run
from webagent.evidence.redaction import TextRedactor
from webagent.evidence.service import POLICY_VERSION
from webagent.evidence.store import EvidenceStore
from webagent.models.schema import ProposeResult
from webagent.security import LocalApiPolicy, load_or_create_token
from webagent.state import transition
from webagent.verification.models import FieldBinding
from webagent.verification.service import VerificationService

import httpx
from playwright.async_api import async_playwright, expect
import uvicorn


def request_summary(method: str, path: str, status: int) -> dict:
    """Export known route names, never arbitrary IDs, query, headers or bodies."""
    route = urlsplit(path).path
    routes = ((r'/v1/tasks/[A-Za-z0-9_-]+/results', 'task_results'),
              (r'/v1/evidence/[A-Za-z0-9_-]+/content', 'evidence_content'),
              (r'/v1/evidence/[A-Za-z0-9_-]+', 'evidence_metadata'),
              (r'/v1/runs/[A-Za-z0-9_-]+/result', 'run_result'))
    return {'method': method if method in ('GET', 'POST', 'OPTIONS') else 'other',
            'route': next((name for pattern, name in routes if re.fullmatch(pattern, route)), 'other'),
            'status': status if type(status) is int else 0}


def proposal_with_wrong_value(proposal: ProposeResult, index: int, *, retain_first_only=False):
    data = proposal.model_dump(mode='json')
    value = format(Decimal(data['items']['values'][index]['normalized_value']) + 7, '.2f')
    for field in ('raw_value', 'normalized_value', 'rounding_lower', 'rounding_upper'):
        data['items']['values'][index][field] = value
    if retain_first_only:
        data['items']['values'] = data['items']['values'][:1]
    return ProposeResult.model_validate_json(canonical_json(data))


def display_copy(directory: Path, metadata: dict, raw: bytes, secrets: tuple[str, ...]) -> dict:
    """Trusted redaction publication, bound to the immutable original identity."""
    filtered = TextRedactor(secrets).filter(raw.decode('utf-8')).encode('utf-8')
    assert all(secret.encode() not in filtered for secret in secrets)
    return EvidenceStore(directory).publish(metadata['run_id'], filtered,
        source_url=metadata['source_url'], captured_at=metadata['captured_at'],
        object_id=metadata['object_id'], query_scope=metadata['query_scope'],
        locator_or_page=metadata['locator_or_page'], artifact_kind='text',
        sensitivity='redacted', original_evidence_id=metadata['evidence_id'],
        redaction_status='FILTERED', policy_version=POLICY_VERSION, retain=True)


def ledger_digest(path: Path) -> str:
    """Read-only UI must not rewrite task history, final results or write facts."""
    with connect(path) as db:
        content = {table: [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                   for table in ('tasks', 'contracts', 'runs', 'run_verifications', 'run_results',
                                 'write_intents', 'task_events')}
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


def scoped_document(raw: bytes, run_id: str) -> bytes:
    """Give fault cases independent content-addressed original files."""
    content = json.loads(raw)
    content['owned_run_fixture'] = run_id
    return canonical_json(content).encode()


async def prepare_cases(directory: Path, fixture, raw: bytes, conflict_raw: bytes,
                        canaries: dict[str, str], *, fetch_document=None) -> dict[str, dict]:
    """Real verification/aggregation over bounded, explicitly seeded runs."""
    cases = {}
    document = {'values': json.loads(raw)['values']}
    service = VerificationService(directory)
    aliases = ('positive', 'assisted', 'partial', 'failed', 'insufficient', 'conflict',
               'unknown', 'retry', 'corrupt', 'blocked', 'read_failure', 'paging', 'cancelled', 'not_ready')
    for alias in aliases:
        run_id = 'results-' + alias
        contract = seed(directory, fixture, run_id, assistance_count=2 if alias == 'assisted' else 0)
        task_id = contract['task_id']
        if alias == 'paging':
            # Twenty-one explicitly seeded terminal histories force real API
            # pagination. They make no success/verification claim themselves.
            first_history = run_id
            transition(directory / 'business.sqlite3', run_id=first_history,
                       expected_state_version=1, target='FAILED')
            for index in range(20):
                historical = first_history + '-archive-' + str(index)
                with connect(directory / 'business.sqlite3') as db, transaction(db):
                    create_run(db, run_id=historical, task_id=task_id, contract_version=1,
                        graph_version='verification-probe-v1', graph_state_schema_version='verification-probe-v1',
                        model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
                transition(directory / 'business.sqlite3', run_id=historical,
                           expected_state_version=0, target='CANCELLED')
            run_id = first_history + '-current'
            with connect(directory / 'business.sqlite3') as db, transaction(db):
                create_run(db, run_id=run_id, task_id=task_id, contract_version=1,
                    parent_run_id=first_history, graph_version='verification-probe-v1',
                    graph_state_schema_version='verification-probe-v1', model_config_sha256='a' * 64,
                    runtime_config_sha256='b' * 64)
                db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES (?,?)', ('budget-' + run_id, run_id))
            transition(directory / 'business.sqlite3', run_id=run_id, expected_state_version=0, target='RUNNING')
        with connect(directory / 'business.sqlite3') as db, transaction(db):
            db.execute("UPDATE tasks SET preparation_status='READY',current_contract_version=1,"
                       "current_run_id=?,requested_fields_json='[]',state_version=state_version+1 WHERE task_id=?",
                       (run_id, task_id))
        case = {'task_id': task_id, 'run_id': run_id, 'alias': alias}
        cases[alias] = case
        if alias == 'cancelled':
            transition(directory / 'business.sqlite3', run_id=run_id, expected_state_version=1, target='CANCELLED')
            case.update(outcome='CANCELLED', result_available=False)
            continue
        if alias == 'not_ready':
            case.update(outcome='RUNNING', result_available=False)
            continue
        scoped_raw = scoped_document(raw, run_id)
        if fetch_document is not None:
            scoped_raw = await fetch_document(scoped_raw, '/disclosure')
        original = publish(directory, fixture, run_id, scoped_raw)
        case['original_id'] = original['evidence_id']
        case['original_path'] = original['artifact_path']
        if alias != 'blocked':
            derivative = display_copy(directory, original, scoped_raw, tuple(canaries.values()))
            case['display_id'] = derivative['evidence_id']
            case['display_path'] = derivative['artifact_path']
        proposal, bindings = candidate(document, original['evidence_id'], fixture.now)
        if alias in ('failed', 'retry', 'partial'):
            proposal = proposal_with_wrong_value(proposal, 1 if alias == 'partial' else 0,
                                                 retain_first_only=alias != 'partial')
            if alias != 'partial':
                bindings = [item for item in bindings if item.result_path.startswith('/values/0/')]
        if alias == 'insufficient':
            (directory / original['artifact_path']).unlink()
        if alias == 'conflict':
            scoped_conflict = scoped_document(conflict_raw, run_id)
            if fetch_document is not None:
                scoped_conflict = await fetch_document(scoped_conflict, '/conflict')
            other = publish(directory, fixture, run_id, scoped_conflict, '/conflict')
            display_copy(directory, other, scoped_conflict, tuple(canaries.values()))
            data = proposal.model_dump(mode='json')
            data['evidence_ids'].append(other['evidence_id'])
            data['items']['values'][0]['evidence_ids'].append(other['evidence_id'])
            proposal = ProposeResult.model_validate_json(canonical_json(data))
            bindings += [FieldBinding(result_path=item.result_path, evidence_path=item.evidence_path,
                                      evidence_id=other['evidence_id']) for item in list(bindings)
                         if item.result_path.startswith('/values/0/')]
        if alias == 'unknown':
            case['operation_id'] = write_fact(directory / 'business.sqlite3', contract, run_id,
                                              original['evidence_id'])
        service.begin(run_id, expected_state_version=1)
        verified = await service.verify(run_id, proposal, bindings, expected_state_version=2)
        result = service.finalize(verified['verification_id'], expected_state_version=2)
        case.update(outcome=result.outcome, result_available=True,
                    verdicts=sorted({item['verdict'] for item in verified['evaluation']['fields']}),
                    assistance_count=result.assistance_count)
        if alias == 'retry':
            # This is explicit trusted Run creation, not a public retry action.
            new_run = run_id + '-second'
            with connect(directory / 'business.sqlite3') as db, transaction(db):
                create_run(db, run_id=new_run, task_id=task_id, contract_version=1,
                    parent_run_id=run_id, graph_version='verification-probe-v1',
                    graph_state_schema_version='verification-probe-v1', model_config_sha256='a' * 64,
                    runtime_config_sha256='b' * 64)
                db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES (?,?)', ('budget-' + new_run, new_run))
                db.execute('UPDATE tasks SET current_run_id=? WHERE task_id=?', (new_run, task_id))
            transition(directory / 'business.sqlite3', run_id=new_run, expected_state_version=0, target='RUNNING')
            second_raw = scoped_document(raw, new_run)
            if fetch_document is not None:
                second_raw = await fetch_document(second_raw, '/disclosure')
            second_original = publish(directory, fixture, new_run, second_raw)
            display_copy(directory, second_original, second_raw, tuple(canaries.values()))
            second_proposal, second_bindings = candidate(document, second_original['evidence_id'], fixture.now)
            service.begin(new_run, expected_state_version=1)
            second_verified = await service.verify(new_run, second_proposal, second_bindings, expected_state_version=2)
            second_result = service.finalize(second_verified['verification_id'], expected_state_version=2)
            assert case['outcome'] == 'FAILED' and second_result.outcome == 'SUCCEEDED'
            case.update(second_run_id=new_run, second_outcome=second_result.outcome)
    # A real post-aggregation file corruption must remove display success even
    # though the immutable historic aggregation outcome remains SUCCEEDED.
    (directory / cases['corrupt']['original_path']).write_bytes(b'owned post-aggregation damaged artifact')
    return cases


async def verify(output: Path, report: dict, *, headed: bool):
    disable_external_tracing()
    data = output / '.private' / 'data'
    data.mkdir(parents=True)
    token = load_or_create_token(data)
    canaries = {'local_api_token': token, 'provider_key': 'sk-SYNTHETIC_PROVIDER_' + uuid4().hex,
               'password': 'SYNTHETIC_PASSWORD_' + uuid4().hex, 'otp': 'SYNTHETIC_OTP_' + uuid4().hex,
               'cookie': 'SYNTHETIC_COOKIE_' + uuid4().hex, 'private_reasoning': 'SYNTHETIC_REASONING_' + uuid4().hex}
    fixture = OwnedHTTPFixture()
    for route in ('/disclosure', '/conflict'):
        document = json.loads(fixture.documents[route])
        document['private_debug'] = canaries
        fixture.documents[route] = canonical_json(document).encode()
    frontend = browser = context = playwright = server = serving = None
    read_fault = None
    api_requests, browser_requests, page_errors, rejected_network = [], [], [], []
    report.update(checks=[], processes=[], fixture_cases=[], configuration={
        'trusted_ledger_fixtures': ['compiled contract and Run creation', 'assistance_count=2',
                                  'one UNKNOWN write intent', 'retry parent link', 'cancel/no-result transition',
                                  '21 terminal non-result Run histories for actual API pagination'],
        'production_paths': ['owned HTTP evidence bytes', 'EvidenceStore and TextRedactor',
                             'VerificationService.verify/finalize', 'FastAPI', 'Vite', 'Chromium'],
        'provider_calls': 0, 'physical_writes': 0, 'real_external_requests': 0,
        'public_business_execution_claimed': False, 'headed': headed})

    def record(name, **details):
        report['checks'].append({'name': name, 'passed': True, **details})
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    try:
        await fixture.start()
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            original = await client.get(fixture.origin + '/disclosure')
            conflict = await client.get(fixture.origin + '/conflict')
            original.raise_for_status()
            conflict.raise_for_status()
            assert original.content == fixture.documents['/disclosure']
            async def fetch_document(raw, route):
                fixture.documents[route] = raw
                response = await client.get(fixture.origin + route)
                response.raise_for_status()
                assert response.content == raw
                return response.content
            cases = await prepare_cases(data, fixture, original.content, conflict.content, canaries,
                                        fetch_document=fetch_document)
        record('owned_http_originals_are_verified_and_aggregated_without_inserting_results',
               cases=len(cases), aggregated_runs=sum(case['result_available'] for case in cases.values()) + 1)
        verdicts = {verdict for case in cases.values() for verdict in case.get('verdicts', [])}
        assert verdicts == {'PASS', 'FAIL', 'INSUFFICIENT', 'CONFLICT'}
        record('all_four_field_verdicts_are_derived_from_original_values_or_missing_evidence')
        assert cases['positive']['outcome'] == cases['assisted']['outcome'] == 'SUCCEEDED'
        assert cases['partial']['outcome'] == 'PARTIAL' and cases['failed']['outcome'] == 'FAILED'
        assert cases['unknown']['outcome'] != 'SUCCEEDED'
        ledger_before = ledger_digest(data / 'business.sqlite3')
        api_port, ui_port = free_ports()
        api_url, ui_url = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
        policy = LocalApiPolicy(token, frozenset({f'127.0.0.1:{api_port}'}), frozenset({api_url, ui_url}))
        app = create_app(Settings(data), secret_store=object(), local_api_policy=policy)

        @app.middleware('http')
        async def summarize(request, call_next):
            nonlocal read_fault
            response = await call_next(request)
            api_requests.append(request_summary(request.method, request.url.path, response.status_code))
            if read_fault is not None and request.url.path == read_fault[0] and response.status_code == 200:
                # The genuine metadata read has succeeded; change only an owned
                # file before the browser makes its subsequent content request.
                read_fault[1].write_bytes(b'owned display derivative corruption after metadata read')
                read_fault = None
            return response

        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=api_port,
            log_config=None, log_level='critical', access_log=False))
        serving = asyncio.create_task(server.serve())
        async def api_ready():
            if serving.done():
                await serving
                raise RuntimeError('Owned results API exited before readiness')
            return server.started
        await wait_until(api_ready, 'owned results API')
        env = {**os.environ, 'WEBAGENT_DATA_DIR': str(data), 'WEBAGENT_API_PORT': str(api_port),
               'WEBAGENT_UI_PORT': str(ui_port), 'PYTHONUNBUFFERED': '1', 'NO_COLOR': '1'}
        for key in ('NODE_OPTIONS', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            env.pop(key, None)
        for key in TRACE_FLAGS:
            env[key] = 'false'
        frontend = Process('results-frontend', 'frontend', output, env)
        async with httpx.AsyncClient(base_url=api_url, trust_env=False, timeout=10,
                                    headers={'Authorization': 'Bearer ' + token}) as client:
            async def ui_ready():
                try: return (await client.get(ui_url)).status_code == 200
                except httpx.HTTPError: return False
            await wait_until(ui_ready, 'owned results frontend', frontend=frontend)
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=not headed, args=[
                '--disable-background-networking', '--disable-component-update',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
            context = await browser.new_context(viewport={'width': 1280, 'height': 960}, service_workers='block')
            async def restrict(route):
                parsed = urlsplit(route.request.url)
                if parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port in (api_port, ui_port):
                    await route.continue_()
                else:
                    rejected_network.append({'method': route.request.method, 'allowed': False})
                    await route.abort()
            await context.route('**/*', restrict)
            page = await context.new_page()
            page.on('pageerror', lambda _: page_errors.append({'kind': 'pageerror'}))
            page.on('request', lambda request: browser_requests.append(request_summary(request.method, request.url, 0)))

            async def projection(alias, run_id=None):
                case = cases[alias]
                response = await client.get('/v1/tasks/' + case['task_id'] + '/results',
                                            params={'run_id': run_id} if run_id else None)
                assert response.status_code == 200
                assert response.headers.get('cache-control') == 'no-store'
                value = response.json()
                assert value['task_id'] == case['task_id']
                for secret in canaries.values():
                    assert secret not in response.text
                return value

            async def select(alias, run_id=None):
                case = cases[alias]
                await page.goto(ui_url + '/?task=' + case['task_id'] + '&login_session=owned-results-login-preserved' +
                                ('&result_run=' + run_id if run_id else ''), wait_until='domcontentloaded')
                await expect(page.get_by_test_id('results-page')).to_be_visible()
                await expect(page.get_by_test_id('results-selected-run')).to_contain_text(
                    run_id or case.get('second_run_id', case['run_id']))
                return page.get_by_test_id('results-page')

            positive = await projection('positive')
            assert positive['display_complete_success'] is True and positive['result']['outcome'] == 'SUCCEEDED'
            panel = await select('positive')
            await expect(page.get_by_test_id('results-outcome')).to_contain_text('SUCCEEDED')
            await expect(page.get_by_test_id('results-checks')).to_contain_text('PASS')
            await expect(page.get_by_test_id('results-assistance')).to_contain_text('autonomous')
            await expect(panel).to_have_attribute('data-complete-success', 'true')
            record('real_result_api_and_ui_show_verified_autonomous_success')
            for index, item in enumerate(positive['result']['items']['values']):
                field_path = '/values/' + str(index) + '/normalized_value'
                persisted_value = item['normalized_value']
                assert isinstance(persisted_value, str) and persisted_value
                field_row = page.get_by_test_id('results-fields').locator('[data-result-path="' + field_path + '"]')
                shown_value = field_row.get_by_test_id('result-field-value')
                await expect(shown_value).to_be_visible()
                await expect(shown_value).to_have_text(persisted_value)
                await expect(field_row).to_contain_text('PASS')
            record('visible_business_field_values_match_actual_persisted_aggregation_not_checker_codes',
                   checked_fields=len(positive['result']['items']['values']))
            evidence = next(item for item in positive['evidence'] if item['evidence_id'] == cases['positive']['original_id'])
            assert evidence['displayable'] and evidence['display_evidence_id'] == cases['positive']['display_id']
            async with page.expect_response(lambda response: urlsplit(response.url).path ==
                    '/api/v1/evidence/' + evidence['display_evidence_id'] + '/content') as response_event:
                await page.get_by_test_id('results-evidence').get_by_role('button', name='查看证据 ' + evidence['evidence_id'], exact=True).click()
            response = await response_event.value
            assert response.status == 200
            content = await response.body()
            assert hashlib.sha256(content).hexdigest() == evidence['display_sha256']
            assert all(secret.encode() not in content for secret in canaries.values())
            await expect(page.get_by_test_id('result-evidence-content')).to_contain_text('revenue')
            record('field_original_id_resolves_to_verified_redacted_derivative_via_controlled_content_route')
            date_text = await page.evaluate("value => new Date(value).toLocaleString('zh-CN',{hour12:false})", evidence['captured_at'])
            evidence_card = page.get_by_test_id('result-evidence-' + evidence['evidence_id'])
            await evidence_card.get_by_text('校验摘要', exact=True).click()
            for text in (evidence['evidence_id'], evidence['source_url'], date_text, evidence['sha256']):
                await expect(evidence_card).to_contain_text(text)
            await expect(evidence_card.get_by_text('SHA-256 ' + evidence['sha256'], exact=True)).to_be_visible()
            record('evidence_provenance_displays_id_source_time_and_original_hash')
            await panel.evaluate("element => element.scrollIntoView({behavior:'instant',block:'start'})")
            heading_box = await page.get_by_role('heading', name='结果与证据', exact=True).bounding_box()
            assert heading_box and 0 <= heading_box['y'] <= 120
            await page.screenshot(path=str(output / '01-results-desktop.png'))
            await page.reload(wait_until='domcontentloaded')
            await expect(page.get_by_test_id('results-selected-run')).to_contain_text(cases['positive']['run_id'])
            await expect(page.get_by_test_id('results-outcome')).to_contain_text('SUCCEEDED')
            record('page_reload_restores_result_from_persistent_task_and_run_identity')

            for alias, outcome in (('partial', 'PARTIAL'), ('failed', 'FAILED')):
                value = await projection(alias)
                assert value['result']['outcome'] == outcome and not value['display_complete_success']
                await select(alias)
                await expect(page.get_by_test_id('results-outcome')).to_contain_text(outcome)
                await expect(page.get_by_test_id('results-unresolved')).not_to_be_empty()
                await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
                record(alias + '_outcome_preserves_unresolved_items_and_does_not_display_complete_success')
            for alias, verdict in (('failed', 'FAIL'), ('insufficient', 'INSUFFICIENT'), ('conflict', 'CONFLICT')):
                value = await projection(alias)
                assert any(item['verdict'] == verdict for item in value['field_checks'])
                await select(alias)
                await expect(page.get_by_test_id('results-fields')).to_contain_text(verdict)
                record('actual_' + verdict.lower() + '_field_verdict_is_visible')
            await select('assisted')
            await expect(page.get_by_test_id('results-assistance')).to_contain_text('assisted')
            assert (await projection('assisted'))['result']['assistance_count'] == 2
            record('persistent_human_assistance_is_not_mislabelled_as_autonomous')
            value = await projection('unknown')
            assert value['pending_write_count'] == 1 and not value['display_complete_success']
            await select('unknown')
            await expect(page.get_by_test_id('results-side-effects')).to_contain_text('UNKNOWN')
            await expect(page.get_by_test_id('results-side-effects')).to_contain_text(cases['unknown']['operation_id'])
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
            record('unknown_write_fact_remains_visible_and_prevents_complete_success')
            for alias in ('cancelled', 'not_ready'):
                value = await projection(alias)
                assert value['result'] is None and not value['display_complete_success']
                await select(alias)
                await expect(page.get_by_test_id('results-outcome')).to_contain_text(cases[alias]['outcome'])
                await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
                record(alias + '_without_aggregation_is_explicit_and_cannot_be_claimed_as_success')

            retry = await projection('retry')
            assert retry['selected_run']['run_id'] == cases['retry']['second_run_id']
            assert retry['result']['outcome'] == 'SUCCEEDED' and len(retry['runs']) == 2
            old = await projection('retry', cases['retry']['run_id'])
            assert old['result']['outcome'] == 'FAILED'
            await select('retry')
            await expect(page.get_by_test_id('result-run-' + cases['retry']['run_id'])).to_contain_text('运行失败')
            await expect(page.get_by_test_id('result-run-' + cases['retry']['second_run_id'])).to_contain_text('验证成功')
            await page.get_by_role('button', name='查看运行 ' + cases['retry']['run_id'], exact=True).click()
            await expect(page.get_by_test_id('results-selected-run')).to_contain_text(cases['retry']['run_id'])
            await expect(page.get_by_test_id('results-outcome')).to_contain_text('FAILED')
            await page.reload(wait_until='domcontentloaded')
            await expect(page.get_by_test_id('results-outcome')).to_contain_text('FAILED')
            assert parse_qs(urlsplit(page.url).query)['login_session'] == ['owned-results-login-preserved']
            record('new_success_preserves_prior_failed_run_and_explicit_history_selection_across_reload')
            for alias in ('corrupt', 'blocked'):
                value = await projection(alias)
                assert value['result']['outcome'] == 'SUCCEEDED' and not value['display_complete_success']
                assert value['display_blockers'] and any(not item['displayable'] for item in value['evidence'])
                await select(alias)
                await expect(page.get_by_test_id('results-outcome')).to_contain_text('SUCCEEDED')
                # The persisted outcome remains historic; current display must
                # have an explicit evidence blocker rather than a green claim.
                await expect(page.get_by_test_id('results-evidence')).to_contain_text(value['evidence'][0]['evidence_id'])
                assert not await page.get_by_test_id('results-evidence').get_by_role('button', name='查看证据 ' + value['evidence'][0]['evidence_id'], exact=True).is_enabled()
                await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
                record(alias + '_evidence_preserves_historic_outcome_but_blocks_current_complete_success')
            await select('read_failure')
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'true')
            read_fault = ('/v1/evidence/' + cases['read_failure']['display_id'],
                          data / cases['read_failure']['display_path'])
            async with page.expect_response(lambda response: urlsplit(response.url).path ==
                    '/api/v1/evidence/' + cases['read_failure']['display_id'] + '/content') as read_event:
                await page.get_by_test_id('results-evidence').get_by_role('button', name='查看证据 ' + cases['read_failure']['original_id'], exact=True).click()
            assert (await read_event.value).status == 409
            assert read_fault is None
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
            record('actual_artifact_corruption_between_metadata_and_read_revokes_display_success',
                   filesystem_fault='owned derivative changed after successful metadata read')
            await select('positive')
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'true')
            failed_route = re.compile(re.escape(ui_url + '/api/v1/tasks/' + cases['positive']['task_id'] + '/results') + r'(?:\?.*)?$')
            async def unavailable(route):
                await route.fulfill(status=503, content_type='application/json', body='{"error":{"code":"SERVICE_UNAVAILABLE"}}')
            await page.route(failed_route, unavailable)
            await page.get_by_role('button', name='刷新结果', exact=True).click()
            await expect(page.get_by_test_id('results-error')).to_be_visible()
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
            await page.unroute(failed_route, unavailable)
            record('explicit_refresh_transport_503_revokes_previous_success_without_changing_ledger',
                   delivery_fault='Playwright transport-only 503 response on one owned results GET')
            await select('paging')
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'true')
            assert (await projection('paging'))['next_cursor'] is not None
            (data / cases['paging']['original_path']).write_bytes(b'owned original damage before reading next historical page')
            async with page.expect_response(lambda response: urlsplit(response.url).path ==
                    '/api/v1/tasks/' + cases['paging']['task_id'] + '/results' and
                    'before' in parse_qs(urlsplit(response.url).query)) as page_event:
                await page.get_by_role('button', name='加载更早运行', exact=True).click()
            paged_response = await page_event.value
            assert paged_response.status == 200
            paged = await paged_response.json()
            assert paged['selected_run']['run_id'] == cases['paging']['run_id']
            assert paged['display_complete_success'] is False and 'evidence_not_readable' in paged['display_blockers']
            await expect(page.get_by_test_id('results-page')).to_have_attribute('data-complete-success', 'false')
            await expect(page.get_by_test_id('results-blockers')).to_be_visible()
            record('real_history_pagination_applies_new_evidence_failure_instead_of_retaining_stale_success',
                   trusted_terminal_history_runs=21, response_injected=False)
            rejected = await client.get('/v1/tasks/' + cases['positive']['task_id'] + '/results',
                                        params={'run_id': cases['failed']['run_id']})
            assert rejected.status_code == 404
            await page.goto(ui_url + '/?task=' + cases['positive']['task_id'] +
                            '&result_run=' + cases['failed']['run_id'], wait_until='domcontentloaded')
            await expect(page.get_by_test_id('results-error')).to_be_visible()
            assert await page.get_by_test_id('results-selected-run').count() == 0
            record('foreign_task_run_is_rejected_by_real_api_and_ui_without_fallback_to_success')
            rejected = await client.get('/v1/tasks/' + cases['positive']['task_id'] + '/results',
                                        params={'path': '/etc/passwd'})
            assert rejected.status_code == 422
            for params in ({'path': '/etc/passwd'}, {'run_id': cases['failed']['run_id']}):
                rejected = await client.get('/v1/evidence/' + cases['positive']['display_id'] + '/content', params=params)
                assert rejected.status_code == 422
            record('results_and_evidence_routes_reject_file_paths_and_unrecognized_read_authority')
            assert (await client.get('/v1/tasks/' + cases['positive']['task_id'] + '/results',
                                     headers={'Authorization': ''})).status_code == 401
            assert (await client.get('/v1/tasks/' + cases['positive']['task_id'] + '/results',
                                     headers={'Origin': 'https://untrusted.example'})).status_code == 403
            record('result_reads_preserve_local_api_authentication_and_origin_boundary')
            await select('partial')
            await page.set_viewport_size({'width': 375, 'height': 900})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.get_by_test_id('results-page').evaluate("element => element.scrollIntoView({behavior:'instant',block:'start'})")
            heading_box = await page.get_by_role('heading', name='结果与证据', exact=True).bounding_box()
            assert heading_box and 0 <= heading_box['y'] <= 120
            await page.screenshot(path=str(output / '02-results-mobile.png'))
            record('375_pixel_results_layout_has_no_horizontal_overflow_and_safe_visual_evidence')
            for secret in canaries.values():
                assert secret not in await page.locator('body').inner_text() and secret not in page.url
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            assert not page_errors and not rejected_network
            assert not any(item['method'] == 'POST' for item in browser_requests)
            assert ledger_digest(data / 'business.sqlite3') == ledger_before
            record('all_result_navigation_is_read_only_and_leaves_persistent_history_write_facts_unchanged')
            safe = {alias: await projection(alias) for alias in cases}
            (output / 'result-projections.json').write_text(json.dumps(safe, ensure_ascii=False, indent=2) + '\n')
            report['fixture_cases'] = [{key: value for key, value in case.items() if key not in ('original_path', 'display_path')}
                                       for case in cases.values()]
        assert not fixture.errors
        report['passed'] = True
    finally:
        cleanup = []
        for resource, method in ((context, 'close'), (browser, 'close'), (playwright, 'stop')):
            if resource is not None:
                try: await getattr(resource, method)()
                except Exception as error: cleanup.append(safe_error(error))
        if frontend is not None:
            try:
                closed = frontend.stop()
                report['processes'].append(closed)
                assert not closed['forced_kill']
            except Exception as error: cleanup.append(safe_error(error))
        if serving is not None:
            try:
                server.should_exit = True
                await asyncio.wait_for(serving, 12)
            except Exception as error: cleanup.append(safe_error(error))
        if hasattr(fixture, 'server'):
            try: await fixture.close()
            except Exception as error: cleanup.append(safe_error(error))
        report['cleanup_errors'] = cleanup
        report['owned_lifecycle'] = {'api_awaited': serving is not None and serving.done(),
            'frontend_exited': frontend is not None and not frontend.alive(),
            'browser_disconnected': browser is not None and not browser.is_connected(),
            'owned_http_closed': hasattr(fixture, 'server') and not fixture.server.is_serving() and not fixture.active}
        if (data / 'business.sqlite3').exists():
            with connect(data / 'business.sqlite3') as db:
                report['storage'] = {'schema_version': db.execute('PRAGMA user_version').fetchone()[0],
                    'integrity': db.execute('PRAGMA integrity_check').fetchone()[0],
                    'foreign_key_errors': len(db.execute('PRAGMA foreign_key_check').fetchall()),
                    'tasks': db.execute('SELECT count(*) FROM tasks').fetchone()[0],
                    'runs': db.execute('SELECT count(*) FROM runs').fetchone()[0],
                    'results': db.execute('SELECT count(*) FROM run_results').fetchone()[0],
                    'unknown_writes': db.execute("SELECT count(*) FROM write_intents WHERE status='UNKNOWN'").fetchone()[0]}
        (output / 'request-summary.json').write_text(json.dumps({'api': api_requests, 'browser': browser_requests,
            'page_errors': page_errors, 'rejected_network': rejected_network,
            'source_requests': fixture.requests}, indent=2) + '\n')
        # Scan exports and the would-be report; private raw fixtures are
        # deliberately retained only below .private and never hashed/exported.
        scan = scan_secrets(output, canaries)
        serialized = canonical_json(report)
        scan['report_canaries_absent'] = all(value not in serialized for value in canaries.values())
        report['secret_scan'] = scan
        if cleanup or not scan['passed'] or not scan['report_canaries_absent']:
            report['passed'] = False
        if report['passed']:
            assert all(report['owned_lifecycle'].values())
            assert report['storage']['schema_version'] == LATEST_VERSION
            assert report['storage']['integrity'] == 'ok' and report['storage']['foreign_key_errors'] == 0
            assert report['storage']['unknown_writes'] == 1
            record('owned_services_exit_before_integrity_and_six_canary_public_export_checks')
        report['scope_counts'] = {'checks': len(report['checks']), 'synthetic_secret_classes': len(canaries),
                                  'http_model_calls': 0, 'external_site_requests': 0, 'physical_repository_writes': 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('results-%Y%m%dT%H%M%S.%fZ')
    output = (args.output_dir or ROOT / 'artifacts' / 'verification' / 'M1-22' / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'task': 'M1-22', 'probe': 'results-ui-http', 'started_at': now(), 'passed': False,
              'scope': 'Owned HTTP source; explicit preparation/run/assistance/write-intent fixtures; actual verification aggregation and real browser API reads. No public business loop, provider call, physical write or user credential.'}
    try:
        async def bounded():
            async with asyncio.timeout(300):
                await verify(output, report, headed=args.headed)
        asyncio.run(bounded())
    except BaseException as error:
        report['passed'] = False
        report['error'] = safe_error(error)
    finally:
        report['finished_at'] = now()
        report['artifact_sha256'] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in public_artifacts(output)}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report.get('checks', [])),
                      'report': str(output / 'report.json')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
