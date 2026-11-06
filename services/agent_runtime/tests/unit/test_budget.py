import pytest

from agent_runtime.budget import (
    PER_MESSAGE_TOKEN_ALLOWANCE,
    ModelPrice,
    cost_micro_usd,
    input_token_bound,
    price_in,
    settlement,
)
from agent_runtime.gateway_client import ChatMessage

PRICE = ModelPrice(
    input_micro_usd_per_million_tokens=3_000_000, output_micro_usd_per_million_tokens=15_000_000
)


def test_input_bound_is_bytes_plus_a_per_message_allowance() -> None:
    messages = [ChatMessage(role="system", content="abc"), ChatMessage(role="user", content="é")]

    assert input_token_bound(messages) == 3 + 2 + 2 * PER_MESSAGE_TOKEN_ALLOWANCE


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "expected"),
    [(0, 0, 0), (1, 0, 3), (1_000_000, 0, 3_000_000), (0, 1, 15), (1, 1, 18), (333, 0, 999)],
)
def test_cost_is_rounded_up_to_whole_micro_usd(
    input_tokens: int, output_tokens: int, expected: int
) -> None:
    assert cost_micro_usd(PRICE, input_tokens, output_tokens) == expected


def test_a_fraction_of_a_micro_usd_is_charged_as_one() -> None:
    cheap = ModelPrice(input_micro_usd_per_million_tokens=1, output_micro_usd_per_million_tokens=1)

    assert cost_micro_usd(cheap, 1, 0) == 1


@pytest.mark.parametrize(
    ("may_have_executed", "price", "tokens", "expected"),
    [
        (False, PRICE, (10, 10), 0),  # the gateway reported it did not run
        (True, PRICE, (100, 10), 450),  # known usage
        (True, PRICE, (None, None), 1_000),  # unknown usage: the full reservation
        (True, None, (100, 10), 1_000),  # no price to value the usage
    ],
)
def test_settlement(
    may_have_executed: bool,
    price: ModelPrice | None,
    tokens: tuple[int | None, int | None],
    expected: int,
) -> None:
    assert (
        settlement(
            reserved=1_000,
            price=price,
            may_have_executed=may_have_executed,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
        )
        == expected
    )


def test_invalid_or_missing_snapshot_prices_are_unknown() -> None:
    assert price_in(None, "a") is None
    assert price_in({"b": PRICE.model_dump()}, "a") is None
    assert price_in({"a": {"input_micro_usd_per_million_tokens": "1"}}, "a") is None
    assert price_in({"a": PRICE.model_dump()}, "a") == PRICE
