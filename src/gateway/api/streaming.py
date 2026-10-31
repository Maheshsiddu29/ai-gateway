"""Server-Sent Events responses for streamed completions.

Data flow: provider adapter -> completion service -> ``encode_stream`` -> ASGI
``send``. Every stage is a pull-based async generator with no intermediate
buffer, so the next upstream chunk is read only after the previous one has been
handed to the server. A slow client therefore slows reads from the provider
instead of growing memory.

Once the response has started, the HTTP status can no longer change. Failures
after that point are reported as a final ``data:`` event carrying an ``error``
object, and the ``[DONE]`` sentinel is withheld, so clients can tell an
interrupted stream from a complete one.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from gateway.api.disconnect import ClientDisconnectedError, run_until_disconnect
from gateway.api.encoding import SSE_DONE, chunk_base, delta_chunk, sse_frame, usage_chunk
from gateway.api.errors import error_payload, error_response
from gateway.errors import DeadlineExceededError, GatewayError, InternalGatewayError
from gateway.providers.base import StreamEnd, StreamEvent, TextDelta
from gateway.resilience.deadline import Deadline

logger = logging.getLogger(__name__)

# Bounded time to report a timeout to a client that is still reading.
ERROR_DELIVERY_SECONDS = 0.1


async def encode_stream(
    first: StreamEvent,
    events: AsyncGenerator[StreamEvent],
    *,
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
) -> AsyncGenerator[bytes]:
    """Encode provider events as OpenAI chat completion chunks.

    ``first`` has already been received from ``events``; reading it before the
    response starts lets errors that occur before any output keep their HTTP
    status.
    """
    base = chunk_base(completion_id=completion_id, created=created, model=model)
    event: StreamEvent | None = first
    end: StreamEnd | None = None
    try:
        yield sse_frame(delta_chunk(base, {"role": "assistant", "content": ""}))
        while event is not None:
            match event:
                case TextDelta(text=text):
                    yield sse_frame(delta_chunk(base, {"content": text}))
                case StreamEnd():
                    end = event
            event = await anext(events, None)
        if end is None:
            raise InternalGatewayError("The provider stream ended unexpectedly.")
        # Exhaustion also finishes provider cleanup and reservation settlement. Do
        # not report success before that work can still raise an error.
        yield sse_frame(delta_chunk(base, {}, end.finish_reason))
        if include_usage and end.usage is not None:
            yield sse_frame(usage_chunk(base, end.usage))
        yield SSE_DONE
    except GatewayError as exc:
        yield sse_frame(error_payload(exc))
    except Exception:
        logger.exception("unexpected error while streaming a completion")
        yield sse_frame(error_payload(InternalGatewayError("The stream failed unexpectedly.")))
    finally:
        await events.aclose()


class EventStreamResponse(StreamingResponse):
    """A streaming response that stops when the client disconnects or the deadline passes.

    Starlette's ``StreamingResponse`` only watches for disconnects on servers that
    report ASGI spec versions before 2.4, and it leaves the body generator to be
    finalized by the garbage collector when cancelled. This class watches on every
    server and closes the generator before returning, which closes the upstream
    connection deterministically.

    Each read and write is bounded by the deadline. For clients that stop reading, ``send`` then
    blocks and the generator stays suspended, so the per-event deadline in the
    completion service never runs. No error event can be delivered in that case;
    the connection is closed after a bounded error-delivery grace.
    """

    media_type = "text/event-stream"

    def __init__(
        self,
        deadline: Deadline,
        content: AsyncGenerator[bytes],
        *,
        close_upstream: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(content, headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
        self._deadline = deadline
        self._content = content
        self._close_upstream = close_upstream
        self._started = False

    async def stream_response(self, send: Send) -> None:
        async with self._deadline.limit():
            self._started = True
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
        try:
            while True:
                async with self._deadline.limit():
                    chunk = await anext(self._content, None)
                if chunk is None:
                    break
                async with self._deadline.limit():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except DeadlineExceededError as exc:
            logger.warning("request deadline exceeded while sending a stream")
            # Reads and writes share the deadline, but no second timer wraps the
            # whole response. That timer raced the read timeout and suppressed the
            # error event. A blocked client gets at most this reporting grace.
            async with asyncio.timeout(ERROR_DELIVERY_SECONDS):
                await send(
                    {
                        "type": "http.response.body",
                        "body": sse_frame(error_payload(exc)),
                        "more_body": True,
                    }
                )
        async with asyncio.timeout(ERROR_DELIVERY_SECONDS):
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await run_until_disconnect(receive, self.stream_response(send))
        except DeadlineExceededError as exc:
            if self._started:
                raise
            await error_response(exc)(scope, receive, send)
        except (ClientDisconnectedError, OSError, TimeoutError):
            logger.info("client disconnected or stopped reading during stream")
        finally:
            try:
                await self._content.aclose()
            finally:
                # Closing an unstarted async generator does not run its finally
                # block. The prefetched provider must also be closed explicitly.
                if self._close_upstream is not None:
                    await self._close_upstream()
