"""Per-candidate cost estimates for routing decisions.

Estimates use the same ``Price.charge_micro_usd`` arithmetic as budget reservations
(integer micro-USD, rounded once), so the maximum estimate for a candidate equals
the amount its reservation would hold at the same pricing version. They are
estimates of gateway-accounted cost, not provider invoices.
"""

from dataclasses import dataclass
from datetime import datetime

from gateway.cost.pricing import PriceCatalog


@dataclass(frozen=True)
class CostEstimate:
    pricing_version: str
    estimated_input_tokens: int
    maximum_output_tokens: int
    # Estimated input plus the full output allowance: the budget reservation.
    maximum_micro_usd: int
    # Mean output length measured on the alias's evaluation dataset, capped at the
    # allowance. None when there is no measurement; it is never guessed.
    expected_output_tokens: int | None
    expected_micro_usd: int | None


def estimate_cost(
    catalog: PriceCatalog,
    provider: str,
    model: str,
    at: datetime,
    *,
    estimated_input_tokens: int,
    maximum_output_tokens: int,
    expected_output_tokens: int | None = None,
) -> CostEstimate | None:
    """Return None when no price is effective at ``at``; a missing price is never zero."""
    price = catalog.find(provider, model, at)
    if price is None:
        return None
    expected_output = (
        None
        if expected_output_tokens is None
        else min(expected_output_tokens, maximum_output_tokens)
    )
    return CostEstimate(
        pricing_version=price.version,
        estimated_input_tokens=estimated_input_tokens,
        maximum_output_tokens=maximum_output_tokens,
        maximum_micro_usd=price.charge_micro_usd(estimated_input_tokens, maximum_output_tokens),
        expected_output_tokens=expected_output,
        expected_micro_usd=(
            None
            if expected_output is None
            else price.charge_micro_usd(estimated_input_tokens, expected_output)
        ),
    )
