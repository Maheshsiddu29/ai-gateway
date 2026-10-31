"""Request correlation, access logging, and the last-resort error boundary.

This is a plain ASGI middleware rather than ``BaseHTTPMiddleware`` so that it does
not buffer streaming responses and so that the request ID context variable it sets
is inherited by every task the request spawns.
"""

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.api.errors import internal_error_response
from gateway.core.logging import request_id_var
from gateway.core.metrics import Metrics

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
# Client-supplied IDs are reflected in headers and logs, so only a conservative
# character set and length are accepted; anything else is replaced.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")


def _client_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == REQUEST_ID_HEADER.encode():
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None
    return None


def _route_template(scope: Scope) -> str:
    # The matched route's path template rather than the raw path, so that requests
    # to arbitrary unknown paths cannot create unbounded metric label values.
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

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
            duration = time.perf_counter() - started
            route = _route_template(scope)
            self.metrics.http_requests.labels(route, str(status_code)).inc()
            self.metrics.http_request_duration.labels(route).observe(duration)
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "principal_id": state.get("principal_id"),
                    "error_type": state.get("error_type"),
                },
            )
            request_id_var.reset(token)
