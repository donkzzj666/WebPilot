"""Caller-configured, immutable token prices; no live prices or guessed usage."""
from __future__ import annotations

from decimal import Decimal, localcontext
import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..db.repository import canonical_json

Rate = Annotated[str, Field(pattern=r'^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,12})?$', max_length=22)]


class TokenPricing(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    basis: Literal['reported_input_output_tokens'] = 'reported_input_output_tokens'
    currency: Literal['USD', 'CNY']
    input_per_million: Rate
    output_per_million: Rate
    cache_hit_input_per_million: Rate | None = None

    @field_validator('input_per_million', 'output_per_million', 'cache_hit_input_per_million')
    @classmethod
    def canonical_rate(cls, value):
        if value is None:
            return None
        return value.rstrip('0').rstrip('.') if '.' in value else value

    @property
    def version(self) -> str:
        # Rates and currency define the version. Arbitrary labels cannot be
        # mistaken for prices, and no mutable catalog reprices an old Run.
        return 'price-' + hashlib.sha256(canonical_json(self.model_dump(mode='json')).encode()).hexdigest()


def estimate_token_cost(pricing: TokenPricing | None, usage) -> str | None:
    """Return an exact configured token estimate, or None if inputs are unknown.

    Images are not assigned a guessed unit price. This schedule explicitly
    prices the provider's reported input/output token totals only.
    """
    if pricing is None:
        return None
    incoming, outgoing = usage.input_tokens, usage.output_tokens
    if any(type(n) is not int or not 0 <= n <= 2**63 - 1 for n in (incoming, outgoing)):
        return None
    hit = 0
    if pricing.cache_hit_input_per_million is not None:
        hit = usage.provider_usage.get('prompt_cache_hit_tokens')
        miss = usage.provider_usage.get('prompt_cache_miss_tokens')
        if type(hit) is not int or not 0 <= hit <= incoming:
            return None
        if miss is not None and (type(miss) is not int or miss != incoming - hit):
            return None
    with localcontext() as context:
        context.prec = 64
        result = (Decimal(incoming - hit) * Decimal(pricing.input_per_million)
                  + Decimal(hit) * Decimal(pricing.cache_hit_input_per_million or '0')
                  + Decimal(outgoing) * Decimal(pricing.output_per_million)) / Decimal(1_000_000)
        text = format(result, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text
