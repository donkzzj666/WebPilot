"""Exact versioned token estimates and backwards-compatible frozen snapshots."""
from copy import deepcopy
import hashlib

import pytest
from pydantic import ValidationError

from webagent.db.repository import canonical_json
from webagent.models.journal import ModelUsage
from webagent.models.pricing import estimate_token_cost
from webagent.models.transport import ModelConfig
from webagent.settings.models import ModelConnection


def configured(**rates):
    return ModelConfig(pricing={'currency': 'USD', 'input_per_million': '2',
                                'output_per_million': '3', **rates})


def test_legacy_unpriced_json_and_hash_are_preserved():
    legacy = {'provider': 'deepseek', 'model_id': 'deepseek-flash',
              'base_url': 'https://api.deepseek.com', 'prompt_version': 'm1-05-model-v1',
              'max_tokens': 1024, 'connect_seconds': 10.0, 'read_seconds': 30.0,
              'total_seconds': 60.0, 'price_version': None}
    model = ModelConnection.model_validate_json(canonical_json(legacy))
    assert model.model_dump(mode='json') == legacy
    assert model.config_sha256 == hashlib.sha256(canonical_json(legacy).encode()).hexdigest()


def test_rates_are_immutable_and_define_the_price_and_config_versions():
    initial = configured(input_per_million='2.000')
    same = configured()
    changed = configured(output_per_million='4')
    currency = configured(currency='CNY')
    assert initial.price_version == same.price_version
    assert initial.config_sha256 == same.config_sha256
    assert len({initial.price_version, changed.price_version, currency.price_version}) == 3
    assert len({initial.config_sha256, changed.config_sha256, currency.config_sha256}) == 3
    assert ModelConfig.model_validate_json(initial.model_dump_json()) == initial
    with pytest.raises(ValidationError):
        initial.pricing.input_per_million = '999'


def test_only_matching_version_is_accepted_and_public_version_without_rates_is_rejected():
    model = configured()
    assert ModelConnection(pricing=model.pricing, price_version=model.price_version).price_version == model.price_version
    with pytest.raises(ValidationError):
        ModelConnection(price_version='invented-price')
    body = model.model_dump(mode='json')
    body['price_version'] = 'wrong-price'
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(body)


@pytest.mark.parametrize('bad', [True, -1, 1.5, 'NaN', 'Infinity', '1e6', '-0', '01', '1.0000000000001'])
def test_rates_reject_coercion_nonfinite_negative_or_unbounded_precision(bad):
    with pytest.raises(ValidationError):
        configured(input_per_million=bad)


def test_exact_decimal_cost_and_zero_are_not_float_estimates():
    pricing = configured(input_per_million='0.1', output_per_million='0.2').pricing
    assert estimate_token_cost(pricing, ModelUsage(input_tokens=3, output_tokens=7)) == '0.0000017'
    assert estimate_token_cost(pricing, ModelUsage(input_tokens=0, output_tokens=0)) == '0'
    assert estimate_token_cost(pricing, ModelUsage(input_tokens=2**63 - 1, output_tokens=0)) == '922337203685.4775807'


@pytest.mark.parametrize('incoming,outgoing', [(None, None), (10, None), (None, 20)])
def test_unknown_usage_or_unconfigured_prices_never_invent_an_amount(incoming, outgoing):
    usage = ModelUsage(input_tokens=incoming, output_tokens=outgoing)
    assert estimate_token_cost(configured().pricing, usage) is None
    assert estimate_token_cost(None, usage) is None


def test_cache_discount_requires_a_consistent_reported_split():
    pricing = configured(cache_hit_input_per_million='1').pricing
    usage = ModelUsage(input_tokens=1000000, output_tokens=1000000,
                       provider_usage={'prompt_cache_hit_tokens': 400000,
                                       'prompt_cache_miss_tokens': 600000})
    assert estimate_token_cost(pricing, usage) == '4.6'
    assert estimate_token_cost(pricing, ModelUsage(input_tokens=10, output_tokens=20)) is None
    for hit, miss in [(11, 0), (3, 6)]:
        assert estimate_token_cost(pricing, ModelUsage(input_tokens=10, output_tokens=20,
            provider_usage={'prompt_cache_hit_tokens': hit, 'prompt_cache_miss_tokens': miss})) is None
