"""End-to-end request deadlines measured on the monotonic clock."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from gateway.errors import DeadlineExceededError


class Deadline:
    def __init__(self, budget_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._expires_at = clock() + budget_seconds

    def remaining(self) -> float:
        return max(0.0, self._expires_at - self._clock())

    @property
    def expired(self) -> bool:
        return self.remaining() == 0.0

    def check(self) -> None:
        if self.expired:
            raise DeadlineExceededError("The request deadline was exceeded.")

    @asynccontextmanager
    async def limit(self) -> AsyncIterator[None]:
        """Cancel the enclosed work when the deadline passes, raising DeadlineExceededError.

        The block must not contain a generator ``yield``: asyncio timeouts must be
        entered and exited in the same task, and a suspended generator may be resumed
        by a different one.
        """
        self.check()
        timeout = asyncio.timeout(self.remaining())
        try:
            async with timeout:
                yield
        except TimeoutError:
            # Only translate the timeout this block armed; a TimeoutError raised by
            # the enclosed code itself propagates unchanged.
            if timeout.expired():
                raise DeadlineExceededError("The request deadline was exceeded.") from None
            raise
