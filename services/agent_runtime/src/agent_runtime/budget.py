"""Estimated spending for workflow admission, in integer micro-USD.

These are estimates for admission control, not invoices. A model call is admitted
only if its worst-case estimate fits the investigation's budget:

* input tokens are bounded by the UTF-8 byte length of the messages plus an
  allowance per message. Tokenizers that encode bytes produce at most one token
  per byte, but a provider's count is not guaranteed to stay under this bound;
* output tokens are bounded by ``max_tokens``, which the gateway enforces;
* each alias's price must be at least that of the most expensive model the
  gateway may route the alias to. The runtime cannot see the gateway's routing
  decision, so it cannot check this.

The gateway keeps its own, authoritative ledger and budgets; nothing here
replaces them.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime.gateway_client import ChatMessage

MICRO_USD_LIMIT = 10**12
# Role markers and message framing, per message.
PER_MESSAGE_TOKEN_ALLOWANCE = 16


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_micro_usd_per_million_tokens: int = Field(ge=0, le=MICRO_USD_LIMIT)
    output_micro_usd_per_million_tokens: int = Field(ge=0, le=MICRO_USD_LIMIT)


def input_token_bound(messages: Sequence[ChatMessage]) -> int:
    return sum(len(m.content.encode()) + PER_MESSAGE_TOKEN_ALLOWANCE for m in messages)


def cost_micro_usd(price: ModelPrice, input_tokens: int, output_tokens: int) -> int:
    """The cost of the given token counts, rounded up to a whole micro-USD."""
    total = (
        input_tokens * price.input_micro_usd_per_million_tokens
        + output_tokens * price.output_micro_usd_per_million_tokens
    )
    return -(-total // 1_000_000)


def price_in(pricing: Mapping[str, Any] | None, alias: str) -> ModelPrice | None:
    """The price of ``alias`` in a stored pricing snapshot; None if absent or invalid."""
    if pricing is None or alias not in pricing:
        return None
    try:
        return ModelPrice.model_validate(pricing[alias])
    except ValidationError:
        return None


def settlement(
    *,
    reserved: int,
    price: ModelPrice | None,
    may_have_executed: bool,
    input_tokens: int | None,
    output_tokens: int | None,
) -> int:
    """What a closed call is charged against the investigation's budget.

    Nothing when the gateway reported that the call did not run; the cost of the
    reported tokens when they are known; otherwise the full reservation, because an
    unknown cost is never assumed to be smaller than the bound.
    """
    if not may_have_executed:
        return 0
    if price is None or input_tokens is None or output_tokens is None:
        return reserved
    return cost_micro_usd(price, input_tokens, output_tokens)
