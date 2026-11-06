"""Request correlation, access logging, and the last-resort error boundary.

A plain ASGI middleware, so the request ID context variable it sets is inherited
by every task the request spawns.
"""

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_runtime.api.errors import internal_error_response
from agent_runtime.logging import request_id_var

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
# Client-supplied IDs are reflected in headers and logs, and forwarded to the
# gateway, so only the character set and length the gateway accepts are kept.
VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")


def _client_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == REQUEST_ID_HEADER.encode():
            candidate = value.decode("latin-1")
            return candidate if VALID_REQUEST_ID.fullmatch(candidate) else None
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _client_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        state = scope.setdefault("state", {})
        started = time.perf_counter()
        status_code: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("unhandled error while processing request")
            if status_code is not None:
                raise
            await internal_error_response()(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "principal_id": state.get("principal_id"),
                    "error_type": state.get("error_type"),
                },
            )
            request_id_var.reset(token)
