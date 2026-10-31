"""Accounting for the upstream work done on behalf of one client request.

A request may make several provider attempts. Each attempt is charged against the
token-per-minute limit as follows:

* usage reported by the provider: the reported total;
* no usage reported, but the provider may have run the request (a success without
  usage, an interrupted stream, a timeout after sending): the full reservation,
  because the actual amount is unknown;
* the provider certainly did not run it (connection failure, explicit client rejection): zero.

Attempts are not free: two uncertain attempts are charged two reservations.
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from gateway.errors import ProviderError
from gateway.providers.base import Usage


class UsageStatus(StrEnum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"
    NONE = "none"


@dataclass
class Attempt:
    provider: str
    started_at: float = field(default_factory=time.perf_counter)
    usage: Usage | None = None
    may_have_processed: bool = True
    cost_attempt_id: uuid.UUID | None = None

    def succeeded(self, usage: Usage | None) -> None:
        self.usage = usage

    def failed(self, exc: BaseException) -> None:
        if isinstance(exc, ProviderError):
            self.may_have_processed = exc.may_have_processed
        # Every other exception after dispatch remains uncertain, including an
        # internal gateway failure while consuming partial provider output.


@dataclass(frozen=True)
class Charge:
    tokens: int
    status: UsageStatus


@dataclass
class UsageLedger:
    attempts: list[Attempt] = field(default_factory=list)

    def start(self, provider: str) -> Attempt:
        attempt = Attempt(provider)
        self.attempts.append(attempt)
        return attempt

    def charge(self, reserved_tokens: int) -> Charge:
        tokens = 0
        statuses: set[UsageStatus] = set()
        for attempt in self.attempts:
            if attempt.usage is not None:
                tokens += attempt.usage.total_tokens
                statuses.add(UsageStatus.OBSERVED)
            elif attempt.may_have_processed:
                tokens += reserved_tokens
                statuses.add(UsageStatus.UNKNOWN)
        if UsageStatus.UNKNOWN in statuses:
            return Charge(tokens, UsageStatus.UNKNOWN)
        if UsageStatus.OBSERVED in statuses:
            return Charge(tokens, UsageStatus.OBSERVED)
        return Charge(0, UsageStatus.NONE)
