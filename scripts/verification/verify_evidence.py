#!/usr/bin/env python3
"""M1-14 real managed-browser/model/HTTP evidence acceptance, synthetic only.

Raw originals stay in the disposable private data domain. Exported artifacts are
the filtered text and the canonical neutral image, plus bounded audit summaries.
No user browser profile, Keychain credential or external provider is accessed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import errno
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

import verify_gateway as gateway_fixture
from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.evidence.redaction import is_neutral_png
from webagent.evidence.service import EvidenceService
from webagent.evidence.store import EvidenceStore
from webagent.state import transition

FAKE_KEY = 'sk-m114-SYNTHETIC-NO-PROVIDER-ACCESS-123456789'
PROVIDER_CREDENTIAL = 'owned-fixture-model-credential-without-provider-access'
PAGE = """<!doctype html><html><meta charset="utf-8"><title>Evidence fixture __SYNTHETIC_KEY__</title>
<style>body{font:18px Arial;margin:40px;background:#fff;color:#111}canvas{display:block;border:1px solid #222}
*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}</style>
<main><h1>Owned evidence fixture</h1><p>API key: __SYNTHETIC_KEY__</p>
<p>Public fixture value: 42.</p><canvas id="secret-pixels" width="700" height="80"></canvas></main>
<script>const c=document.getElementById('secret-pixels').getContext('2d');c.fillStyle='#fff';c.fillRect(0,0,700,80);
c.fillStyle='#000';c.font='16px Arial';c.fillText('__SYNTHETIC_KEY__',10,42);document.body.dataset.canvasReady='yes';</script></html>""".replace('__SYNTHETIC_KEY__', FAKE_KEY).encode()


class OwnedProvider:
    def __init__(self):
        self.requests = []
        self.active = set()
        self.errors = []

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            lines = header.decode('ascii').split('\r\n')
            method, path, _ = lines[0].split(' ', 2)
            headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                       for line in lines[1:] if ':' in line}
            size = int(headers['content-length'])
            assert 0 < size <= 2 * 1024 * 1024
            raw = await asyncio.wait_for(reader.readexactly(size), 5)
            request = json.loads(raw)
            user = request['messages'][1]['content']
            parts = user if type(user) is list else []
            payload = json.loads(parts[0]['text'] if parts else user)
            images = [part for part in parts if part.get('type') == 'image_url']
            decoded = [base64.b64decode(part['image_url']['url'].split(',', 1)[1], validate=True)
                       for part in images]
            assert method == 'POST' and path == '/chat/completions'
            assert headers['authorization'] == 'Bearer ' + PROVIDER_CREDENTIAL
            assert FAKE_KEY.encode() not in raw
            assert '[REDACTED]' in payload['observation']['visible_excerpt']
            assert len(decoded) == 1 and all(is_neutral_png(image) for image in decoded)
            self.requests.append({'method': method, 'path': path, 'fake_credential_absent': True,
                                  'text_filter_verified': True, 'canonical_mask_count': len(decoded),
                                  'image_sha256': [hashlib.sha256(image).hexdigest() for image in decoded]})
            response = canonical_json({'id': 'evidence-fixture-response', 'model': 'deepseek-flash',
                'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
                    'content': canonical_json({'type': 'RequestInput', 'requested_fields': ['target'],
                                               'reason': 'Synthetic acceptance response'})}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '
                         + str(len(response)).encode() + b'\r\n\r\n' + response)
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
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*tuple(self.active), return_exceptions=True)


def seed(path, fixture, name, config_sha256='a' * 64, scheduler=None):
    from webagent.tasks.compiler import compile_draft
    from webagent.scheduler.models import Resource
    now = utc_text()
    contract = compile_draft({'instruction': 'Inspect owned synthetic evidence only', 'scenario': 'research',
        'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixture'], 'topic_criteria': ['synthetic'],
        'cutoff_at': now, 'max_items': 3}}, task_id='task-' + name, version=1, created_at=now,
        provenance=[{'origin': 'explicit_test_configuration', 'reference': 'evidence-acceptance-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}]).contract
    contract['sources'] = [{'source_id': 'local-fixture', 'site_id': 'local-fixture',
                            'origin': fixture.origin, 'path_prefix': '/fixture'}]
    contract['start_urls'] = [fixture.url]
    with connect(path) as db, transaction(db):
        create_task(db, task_id=contract['task_id'], instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=name, task_id=contract['task_id'], contract_version=1,
                   graph_version='evidence-fixture-v1', graph_state_schema_version='fixture-v1',
                   model_config_sha256=config_sha256, runtime_config_sha256='b' * 64)
    if scheduler is not None:
        scheduler.enqueue(name, [Resource.site_identity('local-fixture', None, realm='webarena'),
                                Resource.browser_context(name)], expected_state_version=0, queue_class='webarena')
    return contract


def publication(store, fixture, *, run_id='evidence-plain', **overrides):
    values = dict(source_url=fixture.url, captured_at=datetime.now(timezone.utc), object_id='synthetic-object',
                  query_scope='synthetic fixture', locator_or_page='page 1', sensitivity='public',
                  redaction_status='FILTERED', policy_version='fixture-v1')
    values.update(overrides)
    return store.publish(run_id, b'safe synthetic artifact', **values)


def rejection(call, expected):
    try:
        call()
    except BusinessError as error:
        assert error.code == expected, (error.code, expected)
        return
    raise AssertionError('Synthetic evidence boundary did not reject')


def filesystem_probes(base, fixture, check):
    summaries = []
    for stage in ('mid_write', 'after_rename', 'before_commit'):
        directory = base / stage
        migrate(directory / 'business.sqlite3')
        seed(directory / 'business.sqlite3', fixture, 'evidence-plain')
        store = EvidenceStore(directory)
        referenced = publication(store, fixture)
        def interrupted(current):
            if current == stage:
                raise RuntimeError('synthetic interrupted evidence publication')
        store.fault_hook = store.files.fault_hook = interrupted
        try:
            publication(store, fixture)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Interrupted write unexpectedly completed')
        with connect(store.database) as db:
            check(stage + '_does_not_commit_fake_index_or_audit',
                  db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 1
                  and db.execute('SELECT count(*) FROM evidence_events').fetchone()[0] == 1)
        store.fault_hook = store.files.fault_hook = None
        check(stage + '_grace_preserves_recent_orphan', store.scan_orphans(grace_seconds=3600) == [])
        orphans = store.scan_orphans(grace_seconds=0)
        check(stage + '_orphan_is_registered', len(orphans) == 1 and orphans[0]['status'] == 'REGISTERED')
        cleaned = store.scan_orphans(grace_seconds=0, cleanup=True)
        check(stage + '_cleanup_preserves_referenced_blob', len(cleaned) == 1
              and cleaned[0]['status'] == 'CLEANED' and store.read(referenced['evidence_id'])[1] == b'safe synthetic artifact')
        summaries.append({'fault_stage': stage, 'orphans_registered': len(orphans), 'orphans_cleaned': len(cleaned)})
    directory = base / 'missing-success'
    migrate(directory / 'business.sqlite3')
    seed(directory / 'business.sqlite3', fixture, 'evidence-plain')
    store = EvidenceStore(directory)
    item = publication(store, fixture)
    transition(store.database, run_id='evidence-plain', expected_state_version=0, target='RUNNING')
    transition(store.database, run_id='evidence-plain', expected_state_version=1, target='VERIFYING')
    (directory / item['artifact_path']).unlink()
    rejection(lambda: transition(store.database, run_id='evidence-plain', expected_state_version=2,
                                target='SUCCEEDED'), 'EVIDENCE_MISSING')
    with connect(store.database) as db:
        check('missing_original_prevents_terminal_success', db.execute('SELECT state FROM runs').fetchone()[0] == 'VERIFYING')
    return summaries


async def verify(output, report):
    from fastapi.testclient import TestClient
    from webagent.api import create_app
    from webagent.config import Settings, disable_external_tracing
    from webagent.gateway.service import BrowserGateway
    from webagent.models.adapter import ModelAdapter
    from webagent.models.schema import ModelInput
    from webagent.models.transport import DeepSeekTransport, ModelConfig
    from webagent.network.config import NetworkConfig
    from webagent.network.policy import Endpoint
    from webagent.scheduler.store import SchedulerStore
    from webagent.security import LocalApiPolicy
    from webagent.sessions.manager import ManagedBrowser
    from webagent.sessions.models import SessionOwner

    def check(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    disable_external_tracing()
    original_page = gateway_fixture.FIXTURE
    gateway_fixture.FIXTURE = PAGE
    fixture, provider_server, manager, transport, temporary = None, None, None, None, None
    try:
        fixture = await gateway_fixture.GatewayFixture().start()
        provider_server = await OwnedProvider().start()
        temporary = tempfile.TemporaryDirectory(prefix='webpilot-evidence-')
        directory = temporary.name
        base = Path(directory).resolve()
        settings = Settings(base / 'managed')
        migrate(settings.business_db)
        config = ModelConfig(base_url=f'http://127.0.0.1:{provider_server.port}', connect_seconds=1.0,
                             read_seconds=2.0, total_seconds=5.0)
        scheduler = SchedulerStore(settings.business_db, lease_seconds=300)
        contract = seed(settings.business_db, fixture, 'evidence-managed', config.config_sha256, scheduler)
        manager = ManagedBrowser(settings, headless=False,
            network_config=NetworkConfig(webarena_endpoints=(Endpoint('http', '127.0.0.1', fixture.port),)))
        await manager.start()
        generation = scheduler.start_worker(manager.manager_id)
        token = scheduler.claim(manager.manager_id, generation)
        owner = SessionOwner('run', token.run_id, 'local-fixture', realm='webarena')
        session = await manager.create(owner, execution_token=token)
        gateway = BrowserGateway.from_managed(manager, session, scheduler=scheduler)
        navigation = await gateway.navigate(token, fixture.url, 'evidence-bootstrap')
        check('managed_browser_uses_registered_owned_fixture', navigation['status'] == 'COMPLETED')
        context = await manager.context(session.session_id, owner, execution_token=token)
        page = context.pages[0]
        await page.wait_for_load_state('load')
        check('real_canvas_contains_synthetic_sensitive_pixels', await page.get_attribute('body', 'data-canvas-ready') == 'yes')
        snapshot = await gateway.observe(token, include_screenshot=True)
        view = gateway.model_observation(snapshot['snapshot_id'])
        evidence = gateway.evidence
        check('gateway_publishes_separate_filtered_model_dto', snapshot['redaction_status'] == 'BLOCKED'
              and view['redaction_status'] == 'FILTERED' and bool(view['evidence_ids']))
        check('visible_body_and_title_credentials_are_filtered', FAKE_KEY not in canonical_json(view)
              and '[REDACTED]' in view['visible_excerpt'] and '[REDACTED]' in view['title'])
        original_shot = snapshot['screenshot_evidence_id']
        rejection(lambda: evidence.store.read(original_shot), 'FORBIDDEN')
        rejection(lambda: evidence.model_images(snapshot['snapshot_id'], [original_shot], run_id=token.run_id), 'INPUT_BLOCKED')
        check('opaque_canvas_screenshot_is_blocked_before_model_transfer')
        text_id = view['evidence_ids'][0]
        display, data = evidence.display(text_id)
        original_text = display['original_evidence_id']
        _, private_data = evidence.store.read(original_text, allow_restricted=True)
        check('private_original_is_preserved_without_display_export', FAKE_KEY.encode() in private_data
              and FAKE_KEY.encode() not in data)
        (output / 'filtered-body.txt').write_bytes(data)

        # A trusted adapter may explicitly preserve this same captured view
        # under a new immutable snapshot and produce a wholly inert mask.
        masked_snapshot = {**snapshot, 'snapshot_id': 'masked-evidence-snapshot'}
        masked_snapshot.pop('screenshot_evidence_id', None)
        capture = gateway.local_result(snapshot['snapshot_id'])
        masked = evidence.publish_observation(masked_snapshot, capture, execution_token=token, mask_screenshot=True)
        image_ids = [identifier for identifier in masked['evidence_ids']
                     if evidence.store.metadata(identifier)['artifact_kind'] == 'screenshot']
        images = evidence.model_images(masked['snapshot_id'], image_ids, run_id=token.run_id)
        check('explicit_mask_is_verified_canonical_neutral_png', len(images) == 1 and is_neutral_png(images[0].data))
        (output / 'neutral-display.png').write_bytes(images[0].data)
        with connect(settings.business_db) as db:
            budget_id = db.execute('SELECT budget_record_id FROM run_budgets WHERE run_id=?', (token.run_id,)).fetchone()[0]
            event_id = db.execute('SELECT MAX(event_id) FROM task_events WHERE run_id=?', (token.run_id,)).fetchone()[0]
        payload = {'run_id': token.run_id, 'contract': contract, 'observation': masked,
            'verified_checkpoint': {'checkpoint_id': 'trusted-evidence-probe-checkpoint', 'task_id': contract['task_id'],
                'run_id': token.run_id, 'contract_version': 1, 'current_subgoal': 'inspect-synthetic-fixture',
                'verified_item_ids': [], 'pending_item_ids': ['fixture-object'], 'current_object_id': 'fixture-object',
                'current_object_version': None, 'current_snapshot_id': masked['snapshot_id'], 'flow_version': None,
                'action_sequence': 1, 'business_event_id': event_id, 'budget_record_ref': budget_id,
                'identity_ref': None, 'pending_operation_ids': [], 'epoch': token.epoch,
                'evidence_ids': [], 'saved_at': utc_text()}, 'image_evidence_ids': image_ids,
            'allowed_action_schema_ref': 'urn:webagent:m0-contract-v1:Action', 'selected_flow_versions': []}
        model_input = ModelInput.model_validate_json(canonical_json(payload))
        transport = DeepSeekTransport(config, PROVIDER_CREDENTIAL, allow_test_loopback=True)
        reply = await ModelAdapter(settings.business_db, transport).generate(model_input, images=images, execution_token=token)
        check('real_loopback_model_receives_only_filtered_text_and_neutral_pixels',
              reply.output.type == 'RequestInput' and len(provider_server.requests) == 1 and not provider_server.errors)
        altered = model_input.model_copy(deep=True)
        altered = altered.model_copy(update={'observation': altered.observation.model_copy(update={'title': 'Forged caller FILTERED label'})})
        try:
            await ModelAdapter(settings.business_db, transport).generate(altered, images=images, execution_token=token)
        except BusinessError as error:
            check('caller_cannot_forge_filtered_observation', error.code == 'INPUT_BLOCKED' and len(provider_server.requests) == 1)
        else:
            raise AssertionError('Forged model input reached provider')

        api_credential = secrets.token_urlsafe(48)
        policy = LocalApiPolicy(api_credential, frozenset({'127.0.0.1:18081'}), frozenset({'http://127.0.0.1:18081'}))
        with TestClient(create_app(settings, secret_store=object(), local_api_policy=policy),
                        base_url='http://127.0.0.1:18081') as client:
            headers = {'Authorization': 'Bearer ' + api_credential}
            url = '/v1/evidence/' + text_id
            check('evidence_http_requires_local_authentication', client.get(url).status_code == 401)
            check('evidence_http_checks_exact_origin', client.get(url, headers={**headers, 'Origin': 'https://attacker.invalid'}).status_code == 403)
            metadata = client.get(url, headers=headers)
            check('id_only_metadata_omits_filesystem_path_and_sensitive_values', metadata.status_code == 200
                  and 'artifact_path' not in metadata.json() and FAKE_KEY not in metadata.text)
            response = client.get(url + '/content', headers=headers)
            check('controlled_content_returns_verified_filtered_bytes', response.status_code == 200 and response.content == data
                  and response.headers['x-content-type-options'] == 'nosniff' and response.headers['cache-control'] == 'no-store')
            check('sensitive_original_is_not_exported_over_api', client.get('/v1/evidence/' + original_text + '/content', headers=headers).status_code == 403)
            check('raw_canvas_image_is_not_exported_over_api', client.get('/v1/evidence/' + original_shot + '/content', headers=headers).status_code == 403)
            masked_response = client.get('/v1/evidence/' + image_ids[0] + '/content', headers=headers)
            check('only_verified_neutral_image_can_be_read_over_api', masked_response.status_code == 200
                  and masked_response.content == images[0].data and is_neutral_png(masked_response.content))
            check('path_query_cannot_read_arbitrary_host_files', client.get(url + '?path=/etc/passwd', headers=headers).status_code == 422)
            traversal = client.get('/v1/evidence/%2e%2e%2fetc%2fpasswd/content', headers=headers)
            check('encoded_path_traversal_is_rejected', traversal.status_code in (404, 422))
            blob = settings.data_dir / evidence.store.metadata(text_id)['artifact_path']
            blob.write_bytes(b'synthetic damaged display copy')
            damaged = client.get(url + '/content', headers=headers)
            check('tampered_blob_never_leaves_api', damaged.status_code == 409 and damaged.json()['code'] == 'EVIDENCE_CORRUPT')

        report['filesystem_faults'] = filesystem_probes(base / 'faults', fixture, check)
        # Keep this Run active and its token current while inducing failure,
        # then verify the actual driver cannot be called again.
        actions_before = scheduler.budgets.status(token.run_id)['actions_used']
        rpc_calls = []
        execute = gateway.browser.execute
        async def observed_execute(*args, **kwargs):
            rpc_calls.append('execute')
            return await execute(*args, **kwargs)
        gateway.browser.execute = observed_execute
        def disk_full(stage):
            if stage == 'mid_write':
                raise OSError(errno.ENOSPC, 'synthetic evidence disk full')
        evidence.store.fault_hook = evidence.store.files.fault_hook = disk_full
        rejection(lambda: publication(evidence.store, fixture, run_id=token.run_id, execution_token=token),
                  'EVIDENCE_STORAGE_UNAVAILABLE')
        try:
            await gateway.dispatch(token, gateway_fixture.action(token, snapshot, 'after-storage-full', 'read_visible'))
        except BusinessError as error:
            check('disk_full_blocks_actual_gateway_before_browser_rpc', error.code == 'EVIDENCE_STORAGE_UNAVAILABLE'
                  and not rpc_calls and scheduler.budgets.status(token.run_id)['actions_used'] == actions_before)
        else:
            raise AssertionError('Disk-full gateway dispatched')
        rejection(lambda: EvidenceStore(settings.data_dir), 'EVIDENCE_STORAGE_UNAVAILABLE')
        check('disk_full_fault_gate_survives_new_process_equivalent_store')
        try:
            await ModelAdapter(settings.business_db, transport).generate(model_input, images=images, execution_token=token)
        except BusinessError as error:
            check('disk_full_blocks_model_before_provider_request', error.code == 'EVIDENCE_STORAGE_UNAVAILABLE'
                  and len(provider_server.requests) == 1)
        else:
            raise AssertionError('Disk-full model request dispatched')
        report['provider_requests'] = provider_server.requests
        report['scope'] = {'owned_browser_contexts': 1, 'synthetic_http_origins': 1,
            'owned_loopback_model_providers': 1, 'real_external_requests': 0,
            'api_validation': 'FastAPI TestClient through actual local auth middleware',
            'raw_originals': 'Private disposable data only; exports contain filtered/neutral derivatives'}
        await transport.aclose()
        transport = None
        await manager.aclose()
        manager = None
        report['passed'] = True
    finally:
        gateway_fixture.FIXTURE = original_page
        if transport is not None:
            await transport.aclose()
        if manager is not None:
            await manager.aclose()
        if provider_server is not None:
            await provider_server.close()
        if fixture is not None:
            await fixture.close()
        if temporary is not None:
            temporary.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {'task': 'M1-14', 'passed': False, 'checks': {}, 'started_at': utc_text()}
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
        for path in sorted(args.output_dir.rglob('*')) if path.is_file() and path.name != 'report.json' and '.security' not in path.parts}
    path = args.output_dir / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(path)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
