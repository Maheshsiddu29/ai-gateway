from datetime import UTC, datetime
from decimal import Decimal

from gateway.cost.pricing import Price, PriceCatalog
from gateway.routing.estimate import estimate_cost


def price(version: str, effective: datetime, input_rate: str, output_rate: str) -> Price:
    return Price(
        provider="p",
        model="m",
        version=version,
        effective_at=effective,
        input_per_million_usd=Decimal(input_rate),
        output_per_million_usd=Decimal(output_rate),
    )


JAN = datetime(2026, 1, 1, tzinfo=UTC)
FEB = datetime(2026, 2, 1, tzinfo=UTC)
CATALOG = PriceCatalog(
    [price("v1", JAN, "0.15", "0.60"), price("v2", FEB, "0.30", "1.20")],
)


def test_maximum_and_expected_cost_use_exact_decimal_arithmetic() -> None:
    estimate = estimate_cost(
        CATALOG,
        "p",
        "m",
        datetime(2026, 1, 15, tzinfo=UTC),
        estimated_input_tokens=1001,
        maximum_output_tokens=500,
        expected_output_tokens=37,
    )

    assert estimate is not None
    assert estimate.pricing_version == "v1"
    # 1001 * 0.15 + 500 * 0.60 = 450.15, rounded half up once.
    assert estimate.maximum_micro_usd == 450
    # 1001 * 0.15 + 37 * 0.60 = 172.35
    assert estimate.expected_micro_usd == 172
    assert estimate.expected_output_tokens == 37


def test_maximum_matches_the_budget_reservation_formula() -> None:
    at = datetime(2026, 3, 1, tzinfo=UTC)
    estimate = estimate_cost(
        CATALOG, "p", "m", at, estimated_input_tokens=12345, maximum_output_tokens=678
    )

    assert estimate is not None
    assert estimate.maximum_micro_usd == CATALOG.select("p", "m", at).charge_micro_usd(12345, 678)


def test_pricing_version_follows_effective_time() -> None:
    before = estimate_cost(
        CATALOG, "p", "m", datetime(2026, 1, 31, 23, 59, tzinfo=UTC), **_tokens()
    )
    at_boundary = estimate_cost(CATALOG, "p", "m", FEB, **_tokens())

    assert before is not None
    assert at_boundary is not None
    assert (before.pricing_version, at_boundary.pricing_version) == ("v1", "v2")
    assert at_boundary.maximum_micro_usd == 2 * before.maximum_micro_usd


def test_missing_price_is_not_zero() -> None:
    assert estimate_cost(CATALOG, "p", "unknown", FEB, **_tokens()) is None
    assert estimate_cost(CATALOG, "p", "m", datetime(2025, 1, 1, tzinfo=UTC), **_tokens()) is None


def test_expected_cost_is_unknown_without_a_measurement() -> None:
    estimate = estimate_cost(CATALOG, "p", "m", FEB, **_tokens())

    assert estimate is not None
    assert estimate.expected_output_tokens is None
    assert estimate.expected_micro_usd is None


def test_expected_output_is_capped_at_the_allowance() -> None:
    estimate = estimate_cost(
        CATALOG,
        "p",
        "m",
        FEB,
        estimated_input_tokens=0,
        maximum_output_tokens=10,
        expected_output_tokens=50,
    )

    assert estimate is not None
    assert estimate.expected_output_tokens == 10
    assert estimate.expected_micro_usd == estimate.maximum_micro_usd


def _tokens() -> dict[str, int]:
    return {"estimated_input_tokens": 1000, "maximum_output_tokens": 1000}
