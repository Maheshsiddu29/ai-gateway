"""Exact USD pricing for configured provider and model versions."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MICRO_USD = Decimal("1000000")


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    version: str
    effective_at: datetime
    input_per_million_usd: Decimal = Field(ge=0, le=Decimal("1000000"), decimal_places=9)
    output_per_million_usd: Decimal = Field(ge=0, le=Decimal("1000000"), decimal_places=9)

    @field_validator("input_per_million_usd", "output_per_million_usd", mode="before")
    @classmethod
    def _require_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str | Decimal):
            raise ValueError("prices must be decimal strings")
        return value

    @field_validator("effective_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("effective_at must include a timezone")
        return value.astimezone(UTC)

    def charge_micro_usd(self, input_tokens: int, output_tokens: int) -> int:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        # One micro-USD equals one millionth USD; rates per million tokens
        # therefore multiply directly by token counts before rounding once.
        amount = self.input_per_million_usd * input_tokens
        amount += self.output_per_million_usd * output_tokens
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class PriceCatalog:
    def __init__(self, prices: list[Price]) -> None:
        self._prices: dict[tuple[str, str], list[Price]] = {}
        identities: set[tuple[str, str, str]] = set()
        effective_times: set[tuple[str, str, datetime]] = set()
        for price in prices:
            identity = (price.provider, price.model, price.version)
            if identity in identities:
                raise ValueError(f"duplicate pricing version: {identity}")
            effective_identity = (price.provider, price.model, price.effective_at)
            if effective_identity in effective_times:
                raise ValueError(f"duplicate pricing effective time: {effective_identity}")
            identities.add(identity)
            effective_times.add(effective_identity)
            self._prices.setdefault(identity[:2], []).append(price)
        for versions in self._prices.values():
            versions.sort(key=lambda price: price.effective_at)

    def select(self, provider: str, model: str, at: datetime) -> Price:
        eligible = [
            price for price in self._prices.get((provider, model), []) if price.effective_at <= at
        ]
        if not eligible:
            raise ValueError(f"no effective price for {provider}/{model}")
        return eligible[-1]

    def has_model(self, provider: str, model: str) -> bool:
        return (provider, model) in self._prices
