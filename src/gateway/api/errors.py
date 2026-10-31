"""Client-facing error responses.

Errors use the OpenAI-style envelope ``{"error": {...}}`` so that existing client
libraries surface them, with ``type`` set to a stable gateway category.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from gateway.api.disconnect import ClientDisconnectedError
from gateway.core.logging import request_id_var
from gateway.errors import (
    AuthenticationError,
    GatewayError,
    InternalGatewayError,
    InvalidRequestError,
    ProviderRateLimitError,
)

_HTTP_EXCEPTION_CODES = {404: "not_found", 405: "method_not_allowed"}
# Not sent to anyone, since the client is gone; recorded in the access log. The
# value follows the common "client closed request" convention.
CLIENT_CLOSED_REQUEST = 499


def error_payload(error: GatewayError) -> dict[str, Any]:
    return {
        "error": {
            "type": error.error_type.value,
            "code": error.code,
            "message": error.message,
            "param": error.param,
            "request_id": request_id_var.get(),
        }
    }


def error_response(error: GatewayError) -> JSONResponse:
    headers: dict[str, str] = {}
    if isinstance(error, AuthenticationError):
        headers["WWW-Authenticate"] = "Bearer"
    if isinstance(error, ProviderRateLimitError) and error.retry_after_seconds is not None:
        headers["Retry-After"] = str(error.retry_after_seconds)
    return JSONResponse(error_payload(error), status_code=error.status_code, headers=headers)


def internal_error_response() -> JSONResponse:
    return error_response(InternalGatewayError("The gateway encountered an internal error."))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        # Read by the request context middleware for the access log.
        request.state.error_type = exc.error_type.value
        return error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = InvalidRequestError(
            exc.detail,
            code=_HTTP_EXCEPTION_CODES.get(exc.status_code, "http_error"),
            status_code=exc.status_code,
        )
        request.state.error_type = error.error_type.value
        response = error_response(error)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(ClientDisconnectedError)
    async def handle_client_disconnect(request: Request, exc: ClientDisconnectedError) -> Response:
        return Response(status_code=CLIENT_CLOSED_REQUEST)
