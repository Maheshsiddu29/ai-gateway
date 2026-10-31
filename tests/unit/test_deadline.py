import asyncio

import pytest

from gateway.errors import DeadlineExceededError
from gateway.resilience.deadline import Deadline


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_remaining_time_counts_down_and_never_goes_negative() -> None:
    clock = FakeClock()
    deadline = Deadline(10, clock=clock)

    clock.now += 4
    assert deadline.remaining() == 6
    assert not deadline.expired

    clock.now += 7
    assert deadline.remaining() == 0
    assert deadline.expired


async def test_limit_cancels_work_that_outlives_the_deadline() -> None:
    cancelled = asyncio.Event()

    async def stalled() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(DeadlineExceededError):
        async with Deadline(0.02).limit():
            await stalled()

    assert cancelled.is_set()


async def test_successive_limits_share_one_budget() -> None:
    deadline = Deadline(0.05)

    async with deadline.limit():
        await asyncio.sleep(0.03)
    with pytest.raises(DeadlineExceededError):
        async with deadline.limit():
            await asyncio.sleep(0.03)


async def test_an_expired_deadline_rejects_new_work() -> None:
    clock = FakeClock()
    deadline = Deadline(1, clock=clock)
    clock.now += 2

    with pytest.raises(DeadlineExceededError):
        async with deadline.limit():
            await asyncio.sleep(0)


async def test_timeout_errors_from_the_work_itself_are_not_relabelled() -> None:
    with pytest.raises(TimeoutError) as exc_info:
        async with Deadline(10).limit():
            raise TimeoutError("inner")

    assert not isinstance(exc_info.value, DeadlineExceededError)
    assert str(exc_info.value) == "inner"
