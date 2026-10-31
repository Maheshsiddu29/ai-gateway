import asyncio

import httpx
import pytest
from starlette.types import Message

from gateway.api.responses import CompletionResponse
from gateway.errors import DeadlineExceededError
from gateway.resilience.deadline import Deadline


async def test_buffered_response_checks_deadline_before_headers() -> None:
    clock = [0.0]
    deadline = Deadline(1, clock=lambda: clock[0])
    response = CompletionResponse({"choices": []}, deadline)
    clock[0] += 2
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=response), base_url="http://test"
    ) as client:
        result = await client.get("/")
    assert result.status_code == 504


async def test_buffered_response_deadline_cancels_a_blocked_body_write() -> None:
    response = CompletionResponse({"choices": []}, Deadline(0.02))
    cancelled = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with pytest.raises(DeadlineExceededError):
        await response({"type": "http"}, receive, send)
    assert cancelled.is_set()
