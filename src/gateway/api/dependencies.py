from typing import Annotated

from fastapi import Depends, Request
from pydantic import ValidationError

from gateway.core.config import ResilienceConfig, Settings
from gateway.core.security import Authenticator, Principal, parse_bearer_token
from gateway.errors import (
    AuthenticationError,
    GatewayError,
    InternalGatewayError,
    InvalidRequestError,
    UnsupportedCapabilityError,
)
from gateway.resilience.deadline import Deadline
from gateway.schemas.completions import KNOWN_UNSUPPORTED_PARAMETERS, ChatCompletionRequest
from gateway.services.completion import CompletionService


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def start_deadline(request: Request) -> Deadline:
    resilience: ResilienceConfig = request.app.state.resilience
    return Deadline(resilience.request_deadline_seconds)


def get_completion_service(request: Request) -> CompletionService:
    service: CompletionService | None = request.app.state.completion_service
    if service is None:
        raise InternalGatewayError("The gateway is not ready.", code="not_ready")
    return service


async def require_principal(request: Request) -> Principal:
    token = parse_bearer_token(request.headers.get("authorization"))
    if token is None:
        raise AuthenticationError(
            "Missing or malformed Authorization header; expected a Bearer token.",
            code="missing_api_key",
        )
    authenticator: Authenticator = request.app.state.authenticator
    principal = authenticator.authenticate(token)
    if principal is None:
        raise AuthenticationError("Invalid API key.")
    request.state.principal_id = principal.id
    return principal


async def read_completion_request(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> ChatCompletionRequest:
    """Read the body with an explicit size limit and validate it.

    The body is parsed here instead of through a FastAPI body parameter so that the
    size limit also applies to chunked requests without a Content-Length, and so
    validation failures map onto the gateway's error categories.
    """
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise InvalidRequestError(
            "Content-Type must be application/json.",
            code="unsupported_media_type",
            status_code=415,
        )

    body = await _read_limited_body(request, settings.max_request_bytes)
    try:
        return ChatCompletionRequest.model_validate_json(body)
    except ValidationError as exc:
        raise _validation_error(exc) from None


async def _read_limited_body(request: Request, limit: int) -> bytes:
    too_large = InvalidRequestError(
        f"Request body exceeds the {limit}-byte limit.",
        code="request_too_large",
        status_code=413,
    )
    declared = request.headers.get("content-length")
    if declared is not None:
        # isdigit alone also accepts non-ASCII digits such as "²", which int() rejects.
        if not (declared.isascii() and declared.isdigit()):
            raise InvalidRequestError("Invalid Content-Length header.", code="invalid_header")
        if int(declared) > limit:
            raise too_large

    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    return bytes(body)


def _validation_error(exc: ValidationError) -> GatewayError:
    error = exc.errors(include_url=False, include_input=False)[0]
    location = [str(part) for part in error["loc"]]
    param = ".".join(location) or None

    if error["type"] == "json_invalid":
        return InvalidRequestError("Request body is not valid JSON.", code="invalid_json")
    if error["type"] == "extra_forbidden":
        if location[-1] in KNOWN_UNSUPPORTED_PARAMETERS:
            return UnsupportedCapabilityError(
                f"Parameter '{param}' is not supported by this gateway.",
                code="unsupported_parameter",
                param=param,
            )
        return InvalidRequestError(
            f"Unknown parameter '{param}'.", code="unknown_parameter", param=param
        )

    message = str(error["ctx"]["error"]) if error["type"] == "value_error" else error["msg"]
    if param is None:
        return InvalidRequestError(f"Invalid request: {message}.", code="invalid_parameter")
    return InvalidRequestError(
        f"Invalid value for '{param}': {message}.", code="invalid_parameter", param=param
    )


DeadlineDep = Annotated[Deadline, Depends(start_deadline)]
PrincipalDep = Annotated[Principal, Depends(require_principal)]
CompletionRequestDep = Annotated[ChatCompletionRequest, Depends(read_completion_request)]
CompletionServiceDep = Annotated[CompletionService, Depends(get_completion_service)]
