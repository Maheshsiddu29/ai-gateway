import asyncio
from collections.abc import AsyncGenerator

from starlette.types import Message

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
