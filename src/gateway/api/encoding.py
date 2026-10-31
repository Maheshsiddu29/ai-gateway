"""Serialization of provider-neutral results into OpenAI-compatible response bodies.

``model`` in every response is the gateway alias the client requested, not the
upstream model name. ``usage`` is included only when the provider reported it.
"""

import time
import uuid
from typing import Any

from gateway.providers.base import Completion, Usage


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def unix_time() -> int:
    return int(time.time())


def usage_body(usage: Usage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def completion_body(
    completion: Completion, *, completion_id: str, created: int, model: str
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion.content},
                "finish_reason": completion.finish_reason,
            }
        ],
    }
    if completion.usage is not None:
        body["usage"] = usage_body(completion.usage)
    return body
