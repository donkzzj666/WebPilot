"""Model requests require real filtered artifacts, not caller-provided labels."""
import asyncio
import base64
from copy import deepcopy
import json

import httpx
import pytest

from webagent.db import connect, migrate, transaction
from webagent.db.repository import add_contract, canonical_json, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.evidence.redaction import is_neutral_png
from webagent.evidence.service import EvidenceService
from webagent.models.adapter import ModelAdapter
from webagent.models.journal import list_attempts
from webagent.models.schema import ModelInput
from webagent.models.transport import DeepSeekTransport, ModelConfig, ModelImage, ProviderReply
from webagent.state import transition
from webagent.tasks.compiler import compile_draft
from webagent.tasks.extraction import extract


NOW = '2026-10-01T00:00:00Z'
SECRET = 'sk-SYNTHETIC_BOUNDARY_SECRET_731'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQVDIGAACuAGcVHqFfAAAAAElFTkSuQmCC')
VALID = canonical_json({'type': 'RequestInput', 'requested_fields': ['selection'],
                        'reason': 'Choose the next source'})


class Provider:
    config = ModelConfig()

    def __init__(self):
        self.calls = []

    async def complete(self, model_input, schema, *, images=(), repair_errors=None):
        self.calls.append((deepcopy(model_input), tuple(images)))
        return ProviderReply(VALID)

    async def complete_compilation(self, payload, schema):
        self.calls.append(deepcopy(payload))
        return ProviderReply(canonical_json({'scenario': None, 'parameters': {}, 'ambiguous_fields': []}))


def seeded(tmp_path, provider, *, run_id='boundary-run', title='Synthetic page',
           text='Synthetic revenue: 42 USD', screenshot=None, mask=False, publish=True,
           dimensions=(1, 1)):
    path = tmp_path / 'business.sqlite3'
    migrate(path)
    task_id = 'task-' + run_id
    contract = compile_draft(
        {'instruction': 'Inspect synthetic publications', 'scenario': 'research',
         'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixture'],
            'topic_criteria': ['fixture evidence'], 'cutoff_at': NOW, 'max_items': 3}},
        task_id=task_id, version=1, created_at=NOW,
        provenance=[{'origin': 'api', 'reference': 'boundary-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    with connect(path) as db, transaction(db):
        create_task(db, task_id=task_id, instruction=contract['original_instruction'], requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id=run_id, task_id=task_id, contract_version=1,
            graph_version='test-v1', graph_state_schema_version='test-v1',
            model_config_sha256=provider.config.config_sha256, runtime_config_sha256='b' * 64)
        db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES(?,?)', ('budget-' + run_id, run_id))
    transition(path, run_id=run_id, expected_state_version=0, target='RUNNING')
    observation = {'snapshot_id': 'snapshot-' + run_id, 'run_id': run_id, 'captured_at': NOW,
        'source_url': contract['start_urls'][0], 'title': title,
        'tab_id': 'tab', 'frame_id': 'frame', 'page_version': 'page',
        'width': dimensions[0], 'height': dimensions[1],
        'visible_excerpt': text, 'evidence_ids': [], 'redaction_status': 'FILTERED'}
    service = EvidenceService(tmp_path)
    if publish:
        observation = service.publish_observation(observation,
            {'title': title, 'text': text, 'screenshot': screenshot}, mask_screenshot=mask)
    body = {'run_id': run_id, 'contract': contract, 'observation': observation,
        'verified_checkpoint': {'checkpoint_id': 'checkpoint-' + run_id, 'task_id': task_id,
            'run_id': run_id, 'contract_version': 1, 'current_subgoal': 'inspect',
            'verified_item_ids': [], 'pending_item_ids': ['item'], 'current_object_id': 'report',
            'current_object_version': None, 'current_snapshot_id': observation['snapshot_id'],
            'flow_version': None, 'action_sequence': 0, 'business_event_id': 0,
            'budget_record_ref': 'budget-' + run_id, 'identity_ref': None,
            'pending_operation_ids': [], 'epoch': 1, 'evidence_ids': [], 'saved_at': NOW},
        'image_evidence_ids': [], 'allowed_action_schema_ref': 'urn:webagent:m0-contract-v1:Action',
        'selected_flow_versions': []}
    if mask:
        body['image_evidence_ids'] = [ref for ref in observation['evidence_ids']
            if service.store.metadata(ref)['artifact_kind'] == 'screenshot']
    return path, service, ModelInput.model_validate_json(canonical_json(body))


def reject_before_provider(path, provider, model_input, *, images=()):
    with pytest.raises(BusinessError) as caught:
        asyncio.run(ModelAdapter(path, provider).generate(model_input, images=images))
    assert SECRET not in str(caught.value)
    assert provider.calls == [] and list_attempts(path, model_input.run_id) == []
    with connect(path) as db:
        assert db.execute('SELECT model_calls_used FROM run_budgets WHERE run_id=?',
                          (model_input.run_id,)).fetchone()[0] == 0


def test_caller_filtered_label_without_persisted_artifacts_never_authorizes_model(tmp_path):
    provider = Provider()
    path, _, model_input = seeded(tmp_path, provider, publish=False)
    reject_before_provider(path, provider, model_input)


@pytest.mark.parametrize('flag', [True, False])
def test_original_envelope_retains_gateway_text_completeness_without_public_dto_changes(tmp_path, flag):
    provider = Provider()
    _, service, model_input = seeded(tmp_path, provider, publish=False)
    observation = model_input.observation.model_dump(mode='json')
    view = service.publish_observation(observation, {'title': 'Fixture', 'text': 'Original text',
        'text_truncated': flag})
    assert 'text_truncated' not in view
    display = service.store.metadata(view['evidence_ids'][0])
    _, raw = service.store.read(display['original_evidence_id'], allow_restricted=True)
    assert json.loads(raw)['text_truncated'] is flag


@pytest.mark.parametrize('field', ['title', 'visible_excerpt', 'page_version', 'captured_at', 'evidence_ids'])
def test_filtered_observation_cannot_be_forged_after_publication(tmp_path, field):
    provider = Provider()
    path, _, model_input = seeded(tmp_path, provider)
    body = model_input.model_dump(mode='json')
    body['observation'][field] = {'evidence_ids': [], 'captured_at': '2026-10-02T00:00:00Z'}.get(field, 'forged')
    reject_before_provider(path, provider, ModelInput.model_validate_json(canonical_json(body)))


def test_other_run_evidence_is_not_a_substitute_for_current_artifacts(tmp_path):
    provider = Provider()
    path, _, current = seeded(tmp_path, provider)
    _, _, other = seeded(tmp_path, provider, run_id='other-run')
    body = current.model_dump(mode='json')
    body['observation']['evidence_ids'] = other.observation.evidence_ids
    reject_before_provider(path, provider, ModelInput.model_validate_json(canonical_json(body)))


@pytest.mark.parametrize('mutation', ['missing', 'corrupt'])
def test_model_boundary_reverifies_artifact_bytes_before_reserving_attempt(tmp_path, mutation):
    provider = Provider()
    path, service, model_input = seeded(tmp_path, provider)
    metadata = service.store.metadata(model_input.observation.evidence_ids[0])
    artifact = tmp_path / metadata['artifact_path']
    if mutation == 'missing':
        artifact.unlink()
    else:
        artifact.write_bytes(b'altered synthetic evidence')
    reject_before_provider(path, provider, model_input)


def test_raw_screenshot_cannot_cross_boundary_even_with_a_forged_model_image(tmp_path):
    provider = Provider()
    path, service, model_input = seeded(tmp_path, provider, screenshot=PNG)
    with connect(path) as db:
        image_id = db.execute("SELECT evidence_id FROM evidence WHERE artifact_kind='screenshot'").fetchone()[0]
    assert image_id not in model_input.observation.evidence_ids
    with pytest.raises(BusinessError):
        service.model_images(model_input.observation.snapshot_id, [image_id], run_id=model_input.run_id)
    body = model_input.model_dump(mode='json')
    body['observation']['evidence_ids'].append(image_id)
    body['image_evidence_ids'] = [image_id]
    reject_before_provider(path, provider, ModelInput.model_validate_json(canonical_json(body)),
                           images=(ModelImage(image_id, PNG, 'image/png'),))


def test_explicit_whole_image_mask_uses_immutable_derivative_bytes(tmp_path):
    provider = Provider()
    path, service, model_input = seeded(tmp_path, provider, screenshot=PNG, mask=True)
    images = service.model_images(model_input.observation.snapshot_id,
                                 model_input.image_evidence_ids, run_id=model_input.run_id)
    assert len(images) == 1 and images[0].data != PNG and is_neutral_png(images[0].data)
    metadata = service.store.metadata(images[0].evidence_id)
    assert metadata['sensitivity'] == 'redacted' and metadata['original_evidence_id']
    asyncio.run(ModelAdapter(path, provider).generate(model_input, images=images))
    assert provider.calls[0][1] == images


def test_swapped_image_payload_cannot_reuse_legitimate_derivative_id(tmp_path):
    provider = Provider()
    path, _, model_input = seeded(tmp_path, provider, screenshot=PNG, mask=True)
    forged = ModelImage(model_input.image_evidence_ids[0], PNG, 'image/png')
    reject_before_provider(path, provider, model_input, images=(forged,))


@pytest.mark.parametrize('dimensions', [(2, 1), (1, 2), (100, 100)])
def test_screenshot_dimensions_must_match_observation_before_publication(tmp_path, dimensions):
    provider = Provider()
    with pytest.raises(BusinessError):
        seeded(tmp_path, provider, screenshot=PNG, dimensions=dimensions)
    assert provider.calls == []
    with connect(tmp_path / 'business.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM filtered_observations').fetchone()[0] == 0


@pytest.mark.parametrize('pixels', [PNG[:20], b'\x89PNG\r\n\x1a\nshort-fake', b'opaque-unknown-bytes'])
def test_invalid_raster_cannot_leave_a_complete_observation_publication(tmp_path, pixels):
    provider = Provider()
    with pytest.raises(BusinessError):
        seeded(tmp_path, provider, screenshot=pixels)
    assert provider.calls == []
    with connect(tmp_path / 'business.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM filtered_observations').fetchone()[0] == 0


def test_credential_body_and_title_are_filtered_before_any_provider_sees_them(tmp_path):
    provider = Provider()
    path, service, model_input = seeded(tmp_path, provider,
        title='Report api_key=' + SECRET, text='Revenue: 42 USD\nAuthorization: Bearer ' + SECRET)
    asyncio.run(ModelAdapter(path, provider).generate(model_input))
    assert provider.calls and SECRET not in repr(provider.calls)
    assert '42 USD' in provider.calls[0][0]['observation']['visible_excerpt']
    assert '[REDACTED]' in provider.calls[0][0]['observation']['title']
    with connect(path) as db:
        assert SECRET not in repr([tuple(row) for row in db.execute('SELECT * FROM observations')])
    for ref in model_input.observation.evidence_ids:
        assert SECRET.encode() not in service.store.read(ref)[1]


@pytest.mark.parametrize('area,field', [('contract', 'objective'), ('contract', 'original_instruction'),
    ('verified_checkpoint', 'current_subgoal'), ('verified_checkpoint', 'current_object_id')])
def test_secrets_in_immutable_model_semantics_are_rejected_not_silently_remapped(tmp_path, area, field):
    provider = Provider()
    path, _, model_input = seeded(tmp_path, provider)
    body = model_input.model_dump(mode='json')
    body[area][field] = SECRET
    reject_before_provider(path, provider, ModelInput.model_validate_json(canonical_json(body)))


def test_checkpoint_evidence_must_be_same_run_available_artifact(tmp_path):
    provider = Provider()
    path, _, model_input = seeded(tmp_path, provider)
    body = model_input.model_dump(mode='json')
    body['verified_checkpoint']['evidence_ids'] = ['invented-history']
    reject_before_provider(path, provider, ModelInput.model_validate_json(canonical_json(body)))


def test_provider_known_literal_filters_unlabelled_observation_content(tmp_path):
    provider = Provider()
    provider.sensitive_literals = ('SYNTHETIC_OPAQUE_CREDENTIAL_971',)
    path, _, model_input = seeded(tmp_path, provider,
        text='Revenue: 42 USD\nSYNTHETIC_OPAQUE_CREDENTIAL_971')
    asyncio.run(ModelAdapter(path, provider).generate(model_input))
    assert provider.calls and 'SYNTHETIC_OPAQUE_CREDENTIAL_971' not in repr(provider.calls)
    assert '42 USD' in provider.calls[0][0]['observation']['visible_excerpt']


def test_artifact_lost_between_reservation_and_send_prevents_provider_call(tmp_path, monkeypatch):
    from webagent.models import adapter as module
    provider = Provider()
    path, service, model_input = seeded(tmp_path, provider)
    metadata = service.store.metadata(model_input.observation.evidence_ids[0])
    artifact = tmp_path / metadata['artifact_path']
    original = module.reserve_attempt

    def reserve_and_remove(*args, **kwargs):
        original(*args, **kwargs)
        artifact.unlink()

    monkeypatch.setattr(module, 'reserve_attempt', reserve_and_remove)
    with pytest.raises(BusinessError):
        asyncio.run(ModelAdapter(path, provider).generate(model_input))
    assert provider.calls == []
    attempts = list_attempts(path, model_input.run_id)
    assert len(attempts) == 1 and attempts[0]['status'] == 'ERROR'


def test_artifact_corruption_between_format_repairs_blocks_second_request(tmp_path):
    class MutatingProvider(Provider):
        async def complete(self, model_input, schema, *, images=(), repair_errors=None):
            self.calls.append(deepcopy(model_input))
            artifact.write_bytes(b'corruption after first format failure')
            return ProviderReply('{invalid output')

    provider = MutatingProvider()
    path, service, model_input = seeded(tmp_path, provider)
    artifact = tmp_path / service.store.metadata(model_input.observation.evidence_ids[0])['artifact_path']
    with pytest.raises(BusinessError):
        asyncio.run(ModelAdapter(path, provider).generate(model_input))
    assert len(provider.calls) == 1
    attempts = list_attempts(path, model_input.run_id)
    assert [item['status'] for item in attempts] == ['INVALID', 'ERROR']


@pytest.mark.parametrize('field', ['instruction', 'web_context'])
def test_compiler_free_text_credentials_are_filtered_before_extraction_provider(tmp_path, field):
    provider = Provider()
    content = {'instruction': 'Inspect a synthetic report', 'parameters': {}}
    content[field] = 'Read report\napi_key=' + SECRET
    original = deepcopy(content)
    asyncio.run(extract(provider, content))
    assert provider.calls and SECRET not in canonical_json(provider.calls)
    assert '[REDACTED]' in canonical_json(provider.calls)
    assert content == original


@pytest.mark.parametrize('field', ['entity_id', 'queries', 'variables'])
def test_compiler_sensitive_explicit_parameters_never_reach_provider(tmp_path, field):
    provider = Provider()
    value = {'entity_id': SECRET, 'queries': [SECRET], 'variables': {'team': SECRET}}[field]
    with pytest.raises(BusinessError) as caught:
        asyncio.run(extract(provider, {'instruction': 'Inspect a synthetic report',
                                      'parameters': {field: value}}))
    assert provider.calls == [] and SECRET not in str(caught.value)


@pytest.mark.parametrize('pixels', [PNG, b'\x89PNG\r\n\x1a\nsynthetic-fake-image'])
def test_direct_provider_transport_does_not_bypass_opaque_pixel_filter(pixels):
    requests = []

    async def exercise():
        def handle(request):
            requests.append(request)
            raise AssertionError('Opaque pixels reached HTTP transport')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = DeepSeekTransport(ModelConfig(), 'synthetic-key', client=client)
            with pytest.raises(BusinessError) as caught:
                await provider.complete({'observation': {'visible_excerpt': 'Synthetic'},
                                         'image_evidence_ids': ['opaque-image']}, {},
                    images=(ModelImage('opaque-image', pixels, 'image/png'),))
            assert caught.value.code == 'INPUT_BLOCKED'

    asyncio.run(exercise())
    assert requests == []


@pytest.mark.parametrize('field', ['provider_request_id', 'system_fingerprint'])
def test_execution_provider_credential_metadata_never_enters_call_journal(tmp_path, field):
    class EchoProvider(Provider):
        async def complete(self, model_input, schema, *, images=(), repair_errors=None):
            self.calls.append(deepcopy(model_input))
            return ProviderReply(VALID, provider_request_id=SECRET if field == 'provider_request_id' else 'request-1',
                usage={'input_tokens': None, 'output_tokens': None, 'image_units': None,
                       'provider_usage': {'system_fingerprint': SECRET} if field == 'system_fingerprint' else {}})

    provider = EchoProvider()
    path, _, model_input = seeded(tmp_path, provider)
    with pytest.raises(BusinessError) as caught:
        asyncio.run(ModelAdapter(path, provider).generate(model_input))
    assert SECRET not in str(caught.value) and len(provider.calls) == 1
    assert SECRET not in repr(list_attempts(path, model_input.run_id))
    assert list_attempts(path, model_input.run_id)[0]['status'] == 'ERROR'


@pytest.mark.parametrize('field', ['provider_request_id', 'system_fingerprint'])
def test_compilation_provider_credential_metadata_is_not_returned_to_caller(field):
    class EchoProvider(Provider):
        async def complete_compilation(self, payload, schema):
            self.calls.append(deepcopy(payload))
            return ProviderReply(canonical_json({'scenario': None, 'parameters': {}, 'ambiguous_fields': []}),
                provider_request_id=SECRET if field == 'provider_request_id' else 'request-1',
                usage={'input_tokens': None, 'output_tokens': None, 'image_units': None,
                       'provider_usage': {'system_fingerprint': SECRET} if field == 'system_fingerprint' else {}})

    provider = EchoProvider()
    with pytest.raises(BusinessError) as caught:
        asyncio.run(extract(provider, {'instruction': 'Inspect a synthetic report', 'parameters': {}}))
    assert len(provider.calls) == 1 and SECRET not in str(caught.value)
    assert SECRET not in repr(getattr(caught.value, 'reply', None))


@pytest.mark.parametrize('field', ['navigation_url', 'locator', 'input_text', 'option_label'])
def test_credential_action_parameters_are_blocked_before_intent_and_browser_preparation(tmp_path, field):
    from test_gateway_service import gateway_for

    async def exercise():
        path, store, token, browser, gateway = gateway_for(tmp_path)
        snapshot = await gateway.observe(token)
        target = {'page_url': snapshot['source_url'], 'tab_id': snapshot['tab_id'],
                  'frame_id': snapshot['frame_id'], 'write_scope': None,
                  'locator': {'strategy': 'dom', 'attribute': 'id', 'value': 'synthetic-field'}}
        if field == 'navigation_url':
            target['locator'] = None
            kind, args = 'navigate', {'url': snapshot['source_url'] + '?api_key=' + SECRET}
        elif field == 'locator':
            target['locator']['value'] = SECRET
            kind, args = 'click', {}
        elif field == 'input_text':
            kind, args = 'input', {'text': SECRET}
        else:
            kind, args = 'select', {'option_label': SECRET}
        proposal = {'run_id': token.run_id, 'step_id': 'credential-attempt', 'epoch': token.epoch,
                    'snapshot_id': snapshot['snapshot_id'], 'action_type': kind,
                    'expected_effect': 'read', 'target': target, 'args': args}
        with pytest.raises(BusinessError) as caught:
            await gateway.dispatch(token, proposal)
        assert caught.value.code == 'INPUT_BLOCKED' and SECRET not in str(caught.value)
        assert browser.prepare_calls == browser.execute_calls == 0
        assert store.budgets.status(token.run_id)['actions_used'] == 0
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM steps').fetchone()[0] == 0
            assert SECRET not in repr([tuple(row) for row in db.execute('SELECT * FROM gateway_attempts')])

    asyncio.run(exercise())
