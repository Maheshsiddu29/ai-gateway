"""A mock LLM provider for local development and demonstrations.

Implements just enough of the OpenAI Chat Completions and Anthropic Messages wire
formats, streaming and non-streaming, for the gateway's adapters to talk to it.
Replies are deterministic. Reported "token" counts are whitespace-separated word
counts, not real tokenizer output.

For load tests it can delay responses (MOCK_RESPONSE_DELAY_SECONDS for whole
responses, MOCK_STREAM_DELAY_SECONDS between streamed words) and reports the peak
number of concurrent requests per model at GET /stats (POST /stats/reset clears it).

Run with: uv run uvicorn mock_upstream:app --app-dir tools --port 9000
"""

import asyncio
import json
import os
import time
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# Scripted replies for the agent runtime's investigation (services/agent_runtime).
# The plan requests every diagnostic; the diagnosis cites only E1, the root
# filesystem capacity, because that is the one diagnostic every POSIX host provides.
AGENT_PLANNER_MODEL = "mock-agent-planner"
AGENT_DIAGNOSIS_MODEL = "mock-agent-diagnosis"
_AGENT_PLAN = {
    "steps": [
        {"tool": "filesystem_capacity", "arguments": {"path": "/"}, "reason": "Disk space."},
        {"tool": "filesystem_inodes", "arguments": {"path": "/"}, "reason": "Inode use."},
        {"tool": "memory_info", "arguments": {}, "reason": "System memory."},
        {"tool": "cgroup_memory", "arguments": {}, "reason": "Container memory limit."},
    ]
}
_AGENT_DIAGNOSIS = {
    "conclusion": "inconclusive",
    "summary": "Mock diagnosis: the mock provider does not interpret evidence.",
    "findings": [{"claim": "The root filesystem capacity was measured.", "evidence": ["E1"]}],
    "next_steps": ["Run the investigation against a real model alias."],
}
_SCRIPTED_REPLIES = {
    AGENT_PLANNER_MODEL: json.dumps(_AGENT_PLAN),
    AGENT_DIAGNOSIS_MODEL: json.dumps(_AGENT_DIAGNOSIS),
}


def _reply_for(body: dict[str, Any]) -> str:
    scripted = _SCRIPTED_REPLIES.get(body.get("model", ""))
    if scripted is not None:
        return scripted
    messages = body.get("messages", [])
    last = messages[-1].get("content", "") if messages else ""
    return f"Mock reply from {body.get('model')}: received {len(last.split())} words."


def _prompt_words(body: dict[str, Any]) -> int:
    return sum(len(str(message.get("content", "")).split()) for message in body["messages"])


def _sse(data: dict[str, Any], event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data)}\n\n".encode()


class _InFlight:
    """Concurrent requests per model, and the most seen at once."""

    def __init__(self) -> None:
        self.current: Counter[str] = Counter()
        self.peak: Counter[str] = Counter()
        self.total: Counter[str] = Counter()

    def enter(self, model: str) -> None:
        self.current[model] += 1
        self.total[model] += 1
        self.peak[model] = max(self.peak[model], self.current[model])

    def leave(self, model: str) -> None:
        self.current[model] -= 1

    async def track(self, model: str, body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        try:
            async for chunk in body:
                yield chunk
        finally:
            self.leave(model)


def create_app(
    stream_delay_seconds: float = 0.05, response_delay_seconds: float = 0.0
) -> Starlette:
    in_flight = _InFlight()

    async def words(text: str) -> AsyncIterator[str]:
        for index, word in enumerate(text.split(" ")):
            await asyncio.sleep(stream_delay_seconds)
            yield word if index == 0 else f" {word}"

    async def openai_chat(request: Request) -> Response:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return JSONResponse({"error": {"type": "invalid_api_key"}}, status_code=401)
        body = await request.json()
        reply = _reply_for(body)
        usage = {
            "prompt_tokens": _prompt_words(body),
            "completion_tokens": len(reply.split()),
            "total_tokens": _prompt_words(body) + len(reply.split()),
        }
        base = {"id": "mock-chatcmpl", "created": int(time.time()), "model": body["model"]}
        in_flight.enter(body["model"])

        if not body.get("stream"):
            try:
                await asyncio.sleep(response_delay_seconds)
            finally:
                in_flight.leave(body["model"])
            choice = {"index": 0, "message": {"role": "assistant", "content": reply}}
            return JSONResponse(
                {
                    **base,
                    "object": "chat.completion",
                    "choices": [{**choice, "finish_reason": "stop"}],
                    "usage": usage,
                }
            )

        async def chunks() -> AsyncIterator[bytes]:
            chunk = {**base, "object": "chat.completion.chunk"}
            async for word in words(reply):
                yield _sse({**chunk, "choices": [{"index": 0, "delta": {"content": word}}]})
            yield _sse({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if body.get("stream_options", {}).get("include_usage"):
                yield _sse({**chunk, "choices": [], "usage": usage})
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            in_flight.track(body["model"], chunks()), media_type="text/event-stream"
        )

    async def anthropic_messages(request: Request) -> Response:
        if not request.headers.get("x-api-key"):
            error = {"type": "error", "error": {"type": "authentication_error"}}
            return JSONResponse(error, status_code=401)
        body = await request.json()
        reply = _reply_for(body)
        input_tokens = _prompt_words(body)
        in_flight.enter(body["model"])

        if not body.get("stream"):
            try:
                await asyncio.sleep(response_delay_seconds)
            finally:
                in_flight.leave(body["model"])
            return JSONResponse(
                {
                    "id": "mock-msg",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "text", "text": reply}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": input_tokens, "output_tokens": len(reply.split())},
                }
            )

        async def events() -> AsyncIterator[bytes]:
            message = {"id": "mock-msg", "type": "message", "usage": {"input_tokens": input_tokens}}
            yield _sse({"type": "message_start", "message": message}, "message_start")
            block = {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
            yield _sse(block, "content_block_start")
            async for word in words(reply):
                delta = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": word},
                }
                yield _sse(delta, "content_block_delta")
            yield _sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
            end = {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": len(reply.split())},
            }
            yield _sse(end, "message_delta")
            yield _sse({"type": "message_stop"}, "message_stop")

        return StreamingResponse(
            in_flight.track(body["model"], events()), media_type="text/event-stream"
        )

    async def stats(_: Request) -> Response:
        return JSONResponse(
            {
                model: {"peak_concurrent": in_flight.peak[model], "requests": total}
                for model, total in in_flight.total.items()
            }
        )

    async def reset_stats(_: Request) -> Response:
        in_flight.peak.clear()
        in_flight.total.clear()
        return JSONResponse({})

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/v1/chat/completions", openai_chat, methods=["POST"]),
            Route("/v1/messages", anthropic_messages, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/stats", stats, methods=["GET"]),
            Route("/stats/reset", reset_stats, methods=["POST"]),
        ]
    )


app = create_app(
    stream_delay_seconds=float(os.environ.get("MOCK_STREAM_DELAY_SECONDS", "0.05")),
    response_delay_seconds=float(os.environ.get("MOCK_RESPONSE_DELAY_SECONDS", "0")),
)
