from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from gateway.cost.pricing import Price, PriceCatalog
from gateway.cost.store import period_start


def price(version: str, effective: datetime, rate: str) -> Price:
    return Price(
        provider="mock",
        model="model",
        version=version,
        effective_at=effective,
        input_per_million_usd=Decimal(rate),
        output_per_million_usd=Decimal("0.5"),
    )


def test_exact_fractional_micro_usd_and_rounding() -> None:
    entry = price("v1", datetime(2020, 1, 1, tzinfo=UTC), "0.5")
    assert entry.charge_micro_usd(1, 0) == 1
    assert entry.charge_micro_usd(0, 1) == 1
    assert entry.charge_micro_usd(1_000_000_000, 0) == 500_000_000
    assert entry.charge_micro_usd(1, 1) == 1


def test_version_selection_does_not_substitute_another_model() -> None:
    before = datetime(2020, 1, 1, tzinfo=UTC)
    after = before + timedelta(days=1)
    catalog = PriceCatalog([price("v2", after, "2"), price("v1", before, "1")])
    assert catalog.select("mock", "model", before).version == "v1"
    assert catalog.select("mock", "model", after).version == "v2"
    with pytest.raises(ValueError, match="no effective price"):
        catalog.select("mock", "other", after)
    with pytest.raises(ValueError, match="duplicate pricing version"):
        PriceCatalog([price("v1", before, "1"), price("v1", after, "2")])


def test_prices_reject_binary_float() -> None:
    with pytest.raises(ValidationError):
        Price.model_validate(
            {
                "provider": "mock",
                "model": "model",
                "version": "v1",
                "effective_at": datetime(2020, 1, 1, tzinfo=UTC),
                "input_per_million_usd": 0.1,
                "output_per_million_usd": "1",
            }
        )


def test_utc_period_boundaries_and_leap_year() -> None:
    just_before = datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC)
    next_day = just_before + timedelta(seconds=1)
    assert period_start(just_before, "daily") == datetime(2024, 2, 29, tzinfo=UTC)
    assert period_start(next_day, "daily") == datetime(2024, 3, 1, tzinfo=UTC)
    assert period_start(just_before, "monthly") == datetime(2024, 2, 1, tzinfo=UTC)
    assert period_start(next_day, "monthly") == datetime(2024, 3, 1, tzinfo=UTC)
