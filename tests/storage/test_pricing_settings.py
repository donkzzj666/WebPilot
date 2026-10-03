"""Configured rate snapshots and actual adapter billing records."""
import asyncio
from copy import deepcopy

from webagent.db import connect
from webagent.models.adapter import ModelAdapter
from webagent.models.journal import list_attempts
from webagent.models.transport import ModelConfig, ProviderReply
from webagent.settings import service
from webagent.settings.models import ModelSettingsRequest
from storage.test_settings import FakeStore, body, ready_task, configured_run
from storage.test_model_adapter import FakeProvider, VALID, seed_input


RATES = {'currency': 'USD', 'input_per_million': '2', 'output_per_million': '3'}


def test_public_price_configuration_is_versioned_and_old_run_remains_frozen(database):
    store = FakeStore()
    initial = body()
    initial['model']['pricing'] = deepcopy(RATES)
    saved = service.update_model(database, store, ModelSettingsRequest.model_validate(initial))
    task_id = ready_task(database, store)
    configured_run(database, store, task_id=task_id)
    frozen = service.load_run_config(database, store, 'run-1')
    changed = body(1)
    changed['model']['pricing'] = {**RATES, 'output_per_million': '4'}
    second = service.update_model(database, store, ModelSettingsRequest.model_validate(changed))
    after = service.load_run_config(database, store, 'run-1')
    assert saved['model']['price_version'] != second['model']['price_version']
    assert after.model == frozen.model and after.model.pricing.output_per_million == '3'
    assert after.model.config_sha256 == frozen.model.config_sha256


def test_actual_provider_usage_becomes_an_exact_versioned_persistent_estimate(database):
    config = ModelConfig(pricing=RATES)
    provider = FakeProvider(ProviderReply(VALID, 'owned-provider', {
        'input_tokens': 12, 'output_tokens': 7, 'image_units': None, 'provider_usage': {}}), config=config)
    prepared = seed_input(database, provider)
    asyncio.run(ModelAdapter(database, provider).generate(prepared))
    stored = list_attempts(database, 'run-1')[0]['record']
    assert stored['estimated_cost'] == '0.000045'
    assert stored['price_version'] == config.price_version and stored['cost_currency'] == 'USD'
    with connect(database) as db:
        assert db.execute('SELECT model_config_sha256 FROM runs').fetchone()[0] == config.config_sha256
