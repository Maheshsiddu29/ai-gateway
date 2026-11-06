"""Client-facing errors of the runtime's HTTP API.

Errors use the same envelope as the gateway, ``{"error": {...}}``, with a stable
``type`` and ``code``. Messages are fixed strings written by the runtime; they never
contain request content, model output, or upstream error bodies.
"""

from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_runtime.api.disconnect import CLIENT_CLOSED_REQUEST, ClientDisconnectedError
from agent_runtime.logging import request_id_var

_HTTP_EXCEPTION_CODES = {404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    error_type: ClassVar[str] = "internal_error"
    default_status_code: ClassVar[int] = 500
    default_code: ClassVar[str] = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.param = param
        self.status_code = status_code or self.default_status_code


class AuthenticationError(ApiError):
    error_type = "authentication_error"
    default_status_code = 401
    default_code = "invalid_api_key"


class InvalidRequestError(ApiError):
    error_type = "invalid_request"
    default_status_code = 400
    default_code = "invalid_request"


class RateLimitedError(ApiError):
    error_type = "rate_limited"
    default_status_code = 429
    default_code = "rate_limited"


class NotReadyError(ApiError):
    error_type = "not_ready"
    default_status_code = 503
    default_code = "not_ready"


class InternalError(ApiError):
    pass


def error_payload(error: ApiError) -> dict[str, Any]:
    return {
        "error": {
            "type": error.error_type,
            "code": error.code,
            "message": error.message,
            "param": error.param,
            "request_id": request_id_var.get(),
        }
    }


def error_response(error: ApiError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(error, AuthenticationError) else {}
    return JSONResponse(error_payload(error), status_code=error.status_code, headers=headers)


def internal_error_response() -> JSONResponse:
    return error_response(InternalError("The agent runtime encountered an internal error."))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        request.state.error_type = exc.error_type
        return error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = InvalidRequestError(
            exc.detail,
            code=_HTTP_EXCEPTION_CODES.get(exc.status_code, "http_error"),
            status_code=exc.status_code,
        )
        request.state.error_type = error.error_type
        response = error_response(error)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(ClientDisconnectedError)
    async def handle_client_disconnect(request: Request, exc: ClientDisconnectedError) -> Response:
        return Response(status_code=CLIENT_CLOSED_REQUEST)
