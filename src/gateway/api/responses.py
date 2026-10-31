"""Deadline enforcement for buffered completion responses."""

from typing import Any

from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from gateway.api.errors import error_response
from gateway.errors import DeadlineExceededError
from gateway.resilience.deadline import Deadline


class CompletionResponse(JSONResponse):
    def __init__(self, content: dict[str, Any], deadline: Deadline) -> None:
        super().__init__(content)
        self._deadline = deadline

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def bounded_send(message: Message) -> None:
            nonlocal started
            async with self._deadline.limit():
                if message["type"] == "http.response.start":
                    started = True
                await send(message)

        try:
            await super().__call__(scope, receive, bounded_send)
        except DeadlineExceededError as exc:
            if started:
                # The status cannot change. Propagating aborts the connection, and
                # Content-Length lets the client detect the incomplete JSON body.
                raise
            await error_response(exc)(scope, receive, send)
