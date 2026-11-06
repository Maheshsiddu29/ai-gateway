"""Shared test doubles and settings."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from agent_runtime.config import Environment, Settings

AGENT_API_KEY = "test-agent-key-0123456789abcdef0123"
GATEWAY_API_KEY = "test-gateway-key-0123456789abcdef"
GATEWAY_URL = "http://gateway.test"
AUTH_HEADERS = {"Authorization": f"Bearer {AGENT_API_KEY}"}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_keys": (AGENT_API_KEY,),
        "environment": Environment.DEVELOPMENT,
        "gateway_url": GATEWAY_URL,
        "gateway_api_key": GATEWAY_API_KEY,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield an in-process client for it."""
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client,
    ):
        yield client


type GatewayHandler = Callable[[httpx.Request], httpx.Response]


class GatewayStub(httpx.AsyncBaseTransport):
    """An httpx transport standing in for the gateway. It records every request."""

    def __init__(self, handler: GatewayHandler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return self._handler(request)

    def json_bodies(self) -> list[Any]:
        return [json.loads(request.content) for request in self.requests]


def completion_body(
    content: str | None,
    *,
    model: str = "planner",
    finish_reason: str | None = "stop",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """A gateway chat completion response body."""
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_793_422_274,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def gateway_error(status: int, error_type: str, code: str, **headers: str) -> httpx.Response:
    body = {"error": {"type": error_type, "code": code, "message": "x", "param": None}}
    return httpx.Response(status, json=body, headers=headers)
