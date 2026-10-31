"""Run ASGI applications in uvicorn on ephemeral localhost ports, in-process."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from starlette.types import ASGIApp

STARTUP_TIMEOUT_SECONDS = 5
_STARTUP_POLL_SECONDS = 0.01


@asynccontextmanager
async def run_server(app: ASGIApp) -> AsyncIterator[str]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # uvicorn exposes startup only as a flag.
    for _ in range(int(STARTUP_TIMEOUT_SECONDS / _STARTUP_POLL_SECONDS)):
        if server.started or task.done():
            break
        await asyncio.sleep(_STARTUP_POLL_SECONDS)
    if task.done():
        task.result()
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
