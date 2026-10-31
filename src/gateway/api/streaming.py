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

import logging
from collections.abc import AsyncGenerator

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from gateway.api.disconnect import ClientDisconnectedError, run_until_disconnect
from gateway.api.encoding import SSE_DONE, chunk_base, delta_chunk, sse_frame, usage_chunk
from gateway.api.errors import error_payload
from gateway.errors import DeadlineExceededError, GatewayError, InternalGatewayError
from gateway.providers.base import StreamEnd, StreamEvent, TextDelta
from gateway.resilience.deadline import Deadline

logger = logging.getLogger(__name__)


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
    try:
        yield sse_frame(delta_chunk(base, {"role": "assistant", "content": ""}))
        while event is not None:
            match event:
                case TextDelta(text=text):
                    yield sse_frame(delta_chunk(base, {"content": text}))
                case StreamEnd(finish_reason=finish_reason, usage=usage):
                    yield sse_frame(delta_chunk(base, {}, finish_reason))
                    if include_usage and usage is not None:
                        yield sse_frame(usage_chunk(base, usage))
                    yield SSE_DONE
            event = await anext(events, None)
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

    The deadline here is a backstop for clients that stop reading: ``send`` then
    blocks and the generator stays suspended, so the per-event deadline in the
    completion service never runs. No error event can be delivered in that case;
    the connection is closed.
    """

    media_type = "text/event-stream"

    def __init__(self, deadline: Deadline, content: AsyncGenerator[bytes]) -> None:
        super().__init__(content, headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
        self._deadline = deadline
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with self._deadline.limit():
                await run_until_disconnect(receive, self.stream_response(send))
        except (ClientDisconnectedError, OSError):
            # OSError is how ASGI 2.4 servers report a write to a closed connection.
            logger.info("client disconnected during stream")
        except DeadlineExceededError:
            logger.warning("request deadline exceeded while sending a stream")
        finally:
            await self._content.aclose()
