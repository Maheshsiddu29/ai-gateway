import asyncio
from collections.abc import Awaitable, Callable

POLL_INTERVAL_SECONDS = 0.01
POLL_LIMIT_SECONDS = 5


async def eventually(condition: Callable[[], Awaitable[bool]]) -> None:
    """Wait for state outside this process (such as Redis) to satisfy a condition."""
    for _ in range(int(POLL_LIMIT_SECONDS / POLL_INTERVAL_SECONDS)):
        if await condition():
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError("condition was not met in time")
