import httpx
import pytest

from gateway.admission.tokens import (
    MESSAGE_OVERHEAD_TOKENS,
    REQUEST_OVERHEAD_TOKENS,
    estimate_input_tokens,
)
from gateway.admission.usage import Charge, UsageLedger, UsageStatus
from gateway.errors import DeadlineExceededError, InternalGatewayError, InvalidRequestError
from gateway.providers.base import Usage
from gateway.providers.http import error_from_status, error_from_transport
from gateway.schemas.completions import ChatMessage

RESERVED = 500


def test_estimate_counts_utf8_bytes_and_overheads() -> None:
    messages = [
        ChatMessage(role="system", content="abcdef"),
        ChatMessage(role="user", content="é" * 3),
    ]

    # "abcdef" is 6 bytes (2 tokens); "ééé" is 6 bytes (2 tokens).
    expected = REQUEST_OVERHEAD_TOKENS + 2 * MESSAGE_OVERHEAD_TOKENS + 2 + 2
    assert estimate_input_tokens(messages) == expected


def test_estimate_rounds_partial_tokens_up() -> None:
    [message] = [ChatMessage(role="user", content="a")]

    assert estimate_input_tokens([message]) == REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS + 1


def test_no_attempts_charge_nothing() -> None:
    assert UsageLedger().charge(RESERVED) == Charge(0, UsageStatus.NONE)


def test_reported_usage_is_charged_exactly() -> None:
    ledger = UsageLedger()
    ledger.start("p").succeeded(Usage(10, 5, 15))

    assert ledger.charge(RESERVED) == Charge(15, UsageStatus.OBSERVED)


def test_success_without_usage_is_charged_the_reservation() -> None:
    ledger = UsageLedger()
    ledger.start("p").succeeded(None)

    assert ledger.charge(RESERVED) == Charge(RESERVED, UsageStatus.UNKNOWN)


@pytest.mark.parametrize(
    ("exc", "charged"),
    [
        (error_from_status(httpx.Response(503)), RESERVED),
        (error_from_status(httpx.Response(429)), 0),
        (error_from_transport(httpx.ConnectError("refused")), 0),
        (error_from_transport(httpx.ConnectTimeout("slow")), 0),
        (error_from_status(httpx.Response(504)), RESERVED),
        (error_from_transport(httpx.ReadTimeout("slow")), RESERVED),
        (error_from_transport(httpx.RemoteProtocolError("closed")), RESERVED),
        (InvalidRequestError("unexpected after dispatch"), RESERVED),
        (InternalGatewayError("stream ended after partial output"), RESERVED),
        (DeadlineExceededError("late"), RESERVED),
        (RuntimeError("bug"), RESERVED),
    ],
)
def test_failed_attempts_are_charged_unless_the_provider_certainly_did_not_run(
    exc: Exception, charged: int
) -> None:
    ledger = UsageLedger()
    ledger.start("p").failed(exc)

    assert ledger.charge(RESERVED).tokens == charged


def test_every_attempt_that_may_have_run_is_charged() -> None:
    ledger = UsageLedger()
    ledger.start("primary").failed(error_from_transport(httpx.ReadTimeout("slow")))
    ledger.start("primary").failed(error_from_status(httpx.Response(503)))
    ledger.start("secondary").succeeded(Usage(40, 10, 50))

    assert ledger.charge(RESERVED) == Charge(2 * RESERVED + 50, UsageStatus.UNKNOWN)
