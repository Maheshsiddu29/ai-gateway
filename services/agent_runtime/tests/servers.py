"""An ASGI app served by uvicorn on an ephemeral localhost port, in-process, and a
fake gateway that records every request it receives."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from tests.fakes import DIAGNOSIS_REPLY, PLAN_REPLY, completion_body


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
    for _ in range(500):
        if server.started or task.done():
            break
        await asyncio.sleep(0.01)
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


class RecordingGateway:
    """Answers planner and diagnosis calls and records each request's X-Request-ID.

    ``on_request`` is called with the model alias before answering; a test can use
    it to kill the worker that sent the request. ``hold`` makes a model's calls wait.
    """

    def __init__(self) -> None:
        self.request_ids: list[str] = []
        self.models: list[str] = []
        self.on_request: Callable[[str], None] | None = None
        self.hold: dict[str, asyncio.Event] = {}
        self.arrived = asyncio.Event()
        self.app = Starlette(
            routes=[Route("/v1/chat/completions", self.complete, methods=["POST"])]
        )

    async def complete(self, request: Request) -> Response:
        body = json.loads(await request.body())
        model = body["model"]
        self.models.append(model)
        self.request_ids.append(request.headers["x-request-id"])
        self.arrived.set()
        if self.on_request is not None:
            self.on_request(model)
        if model in self.hold:
            await self.hold[model].wait()
        reply = PLAN_REPLY if model == "planner" else DIAGNOSIS_REPLY
        usage = {"prompt_tokens": 30, "completion_tokens": 6}
        return JSONResponse(
            completion_body(reply, model=model, usage=usage),
            headers={"x-request-id": request.headers["x-request-id"]},
        )
