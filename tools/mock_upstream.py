"""A mock LLM provider for local development and demonstrations.

Implements just enough of the OpenAI Chat Completions and Anthropic Messages wire
formats, streaming and non-streaming, for the gateway's adapters to talk to it.
Replies are deterministic. Reported "token" counts are whitespace-separated word
counts, not real tokenizer output.

Run with: uv run uvicorn mock_upstream:app --app-dir tools --port 9000
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


def _reply_for(body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    last = messages[-1].get("content", "") if messages else ""
    return f"Mock reply from {body.get('model')}: received {len(last.split())} words."


def _prompt_words(body: dict[str, Any]) -> int:
    return sum(len(str(message.get("content", "")).split()) for message in body["messages"])


def _sse(data: dict[str, Any], event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data)}\n\n".encode()


def create_app(stream_delay_seconds: float = 0.05) -> Starlette:
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

        if not body.get("stream"):
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

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async def anthropic_messages(request: Request) -> Response:
        if not request.headers.get("x-api-key"):
            error = {"type": "error", "error": {"type": "authentication_error"}}
            return JSONResponse(error, status_code=401)
        body = await request.json()
        reply = _reply_for(body)
        input_tokens = _prompt_words(body)

        if not body.get("stream"):
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

        return StreamingResponse(events(), media_type="text/event-stream")

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/v1/chat/completions", openai_chat, methods=["POST"]),
            Route("/v1/messages", anthropic_messages, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ]
    )


app = create_app(float(os.environ.get("MOCK_STREAM_DELAY_SECONDS", "0.05")))
