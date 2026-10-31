import asyncio
from collections.abc import AsyncGenerator

import httpx
from starlette.types import Message

from gateway.api.encoding import SSE_DONE
from gateway.api.streaming import EventStreamResponse
from gateway.resilience.deadline import Deadline


async def test_response_deadline_sends_error_and_completes_asgi_body() -> None:
    clock = [100.0]
    deadline = Deadline(1, clock=lambda: clock[0])
    messages: list[Message] = []
    closed = asyncio.Event()

    async def body() -> AsyncGenerator[bytes]:
        try:
            yield b'data: {"choices": []}\n\n'
            # Expire exactly between reads, without relying on timer ordering.
            clock[0] += 2
            deadline.check()
        finally:
            closed.set()

    async def send(message: Message) -> None:
        messages.append(message)

    response = EventStreamResponse(deadline, body())
    await response.stream_response(send)

    assert closed.is_set()
    assert b'"type":"deadline_exceeded"' in messages[-2]["body"]
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert not any(b"[DONE]" in m.get("body", b"") for m in messages)


async def test_expiry_before_response_start_closes_the_prefetched_provider() -> None:
    clock = [100.0]
    deadline = Deadline(1, clock=lambda: clock[0])
    released = asyncio.Event()

    async def provider() -> AsyncGenerator[bytes]:
        try:
            yield b"prefetched"
        finally:
            released.set()

    upstream = provider()
    assert await anext(upstream) == b"prefetched"

    async def unstarted_encoder() -> AsyncGenerator[bytes]:
        async for chunk in upstream:
            yield chunk

    response = EventStreamResponse(deadline, unstarted_encoder(), close_upstream=upstream.aclose)
    clock[0] += 2
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=response), base_url="http://test"
    ) as client:
        result = await client.get("/")
    assert result.status_code == 504
    assert released.is_set()


async def test_deadline_after_done_cannot_append_an_error_event() -> None:
    clock = [0.0]
    deadline = Deadline(1, clock=lambda: clock[0])
    sent: list[Message] = []

    async def body() -> AsyncGenerator[bytes]:
        yield SSE_DONE

    async def send(message: Message) -> None:
        sent.append(message)
        if message.get("body") == SSE_DONE:
            clock[0] += 2

    content = body()
    await EventStreamResponse(deadline, content).stream_response(send)
    await content.aclose()
    assert [m["body"] for m in sent if m["type"] == "http.response.body"] == [SSE_DONE, b""]
