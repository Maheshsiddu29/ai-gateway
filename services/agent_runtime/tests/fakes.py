"""Shared test doubles and settings."""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from agent_runtime.config import Environment, Settings
from agent_runtime.diagnostics.tools import (
    DiagnosticBackend,
    DiagnosticExecutor,
    DiagnosticResult,
    ToolRequest,
)
from agent_runtime.gateway_client import ChatMessage
from agent_runtime.investigation.graph import (
    LlmCaller,
    LlmOutcome,
    LlmReply,
    ModelAliases,
    Node,
    OperationType,
    StageContext,
    UsageTotals,
)
from agent_runtime.investigation.schemas import InvestigationResult

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
        "planner_model": "planner",
        "diagnosis_model": "diagnosis",
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


PLAN_REPLY = json.dumps(
    {
        "steps": [
            {"tool": "memory_info", "arguments": {}, "reason": "System memory."},
            {"tool": "filesystem_capacity", "arguments": {"path": "/"}, "reason": "Disk."},
        ]
    }
)
DIAGNOSIS_REPLY = json.dumps(
    {
        "conclusion": "supported",
        "summary": "Memory is exhausted.",
        "findings": [{"claim": "Little memory is free.", "evidence": ["E1"]}],
        "next_steps": [],
    }
)


class ScriptedLlm:
    """An ``LlmCaller`` answering each operation from a script; records every call.

    A script entry that is an exception is raised, which simulates a crash in the
    middle of a model call.
    """

    def __init__(self, replies: dict[OperationType, str | BaseException] | None = None) -> None:
        self.replies = replies or {
            OperationType.PLAN: PLAN_REPLY,
            OperationType.DIAGNOSE: DIAGNOSIS_REPLY,
        }
        self.calls: list[OperationType] = []

    async def call(
        self, operation: OperationType, model: str, messages: Sequence[ChatMessage]
    ) -> LlmOutcome:
        self.calls.append(operation)
        reply = self.replies[operation]
        if isinstance(reply, BaseException):
            raise reply
        return LlmReply(reply)

    async def usage(self) -> UsageTotals:
        return UsageTotals(len(self.calls), 10 * len(self.calls), 5 * len(self.calls))


class CountingBackend:
    """A diagnostic backend that returns fixed data and counts its reads."""

    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []

    async def run(self, request: ToolRequest) -> DiagnosticResult:
        self.requests.append(request)
        return DiagnosticResult.ok({"mem_free_bytes": 1024, "used_percent": 99.0})


def stage_context(
    llm: LlmCaller,
    backend: DiagnosticBackend,
    *,
    investigation_id: str = "inv-0123456789abcdef0123456789abcdef",
    results: list[InvestigationResult] | None = None,
) -> StageContext:
    """A context for running the graph directly, collecting results in ``results``."""
    collected = results if results is not None else []

    async def incident() -> str:
        return "The database host is out of memory."

    async def enter(node: Node) -> None:
        return None

    async def elapsed_ms() -> float:
        return 1.0

    async def finish(result: InvestigationResult) -> None:
        collected.append(result)

    return StageContext(
        investigation_id=investigation_id,
        llm=llm,
        diagnostics=DiagnosticExecutor(backend, timeout_seconds=5, max_output_bytes=16_384),
        models=ModelAliases(planner="planner", diagnosis="diagnosis"),
        max_tool_calls=4,
        allowed_paths=frozenset({"/"}),
        incident=incident,
        enter=enter,
        elapsed_ms=elapsed_ms,
        finish=finish,
    )
