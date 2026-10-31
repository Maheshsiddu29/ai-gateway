"""Propagating client disconnects to in-flight upstream work.

ASGI servers do not cancel a request handler when the client goes away; they
report it through ``receive()``. Without watching for that message, an abandoned
request would keep its upstream call (and its token spend) running to completion.
"""

import asyncio
from collections.abc import Awaitable

from starlette.types import Receive


class ClientDisconnectedError(Exception):
    """The client closed the connection before the response was complete."""


async def run_until_disconnect[T](receive: Receive, work: Awaitable[T]) -> T:
    """Await ``work``, cancelling it if the client disconnects first.

    Must only be called after the request body has been consumed, so that the
    next ASGI message can only be ``http.disconnect``.
    """
    work_task = asyncio.ensure_future(work)
    disconnect_task = asyncio.ensure_future(_wait_for_disconnect(receive))
    work_finished = False
    try:
        await asyncio.wait({work_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
        work_finished = work_task.done()
    finally:
        # Also reached when the caller is cancelled, for example on server
        # shutdown; waiting here lets the work release upstream connections.
        disconnect_task.cancel()
        work_task.cancel()
        await asyncio.gather(work_task, disconnect_task, return_exceptions=True)

    if not work_finished:
        raise ClientDisconnectedError
    return work_task.result()


async def _wait_for_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
