from typing import Annotated

from fastapi import Depends, Request
from pydantic import ValidationError

from agent_runtime.api.errors import AuthenticationError, InvalidRequestError, NotReadyError
from agent_runtime.config import Settings
from agent_runtime.investigation.schemas import InvestigationRequest
from agent_runtime.investigation.workflow import Investigator
from agent_runtime.security import Authenticator, Principal, parse_bearer_token


async def require_principal(request: Request) -> Principal:
    authenticator: Authenticator = request.app.state.authenticator
    token = parse_bearer_token(request.headers.get("authorization"))
    if token is None:
        raise AuthenticationError(
            "Missing or malformed Authorization header; expected a Bearer token.",
            code="missing_api_key",
        )
    principal = authenticator.authenticate(token)
    if principal is None:
        raise AuthenticationError("Invalid API key.")
    request.state.principal_id = principal.id
    return principal


async def get_investigator(request: Request) -> Investigator:
    investigator: Investigator | None = request.app.state.investigator
    if investigator is None:
        raise NotReadyError("The agent runtime is not ready.")
    return investigator


async def read_investigation_request(request: Request) -> InvestigationRequest:
    """Read the body with an explicit size limit, also for chunked requests, and
    validate it strictly."""
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise InvalidRequestError(
            "Content-Type must be application/json.",
            code="unsupported_media_type",
            status_code=415,
        )
    settings: Settings = request.app.state.settings
    limit = settings.max_request_bytes
    too_large = InvalidRequestError(
        f"Request body exceeds the {limit}-byte limit.", code="request_too_large", status_code=413
    )
    declared = request.headers.get("content-length")
    if declared is not None:
        if not (declared.isascii() and declared.isdigit()):
            raise InvalidRequestError("Invalid Content-Length header.", code="invalid_header")
        if int(declared) > limit:
            raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    try:
        return InvestigationRequest.model_validate_json(bytes(body))
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_input=False)[0]
        if error["type"] == "json_invalid":
            raise InvalidRequestError(
                "Request body is not valid JSON.", code="invalid_json"
            ) from None
        param = ".".join(str(part) for part in error["loc"]) or None
        code = "unknown_parameter" if error["type"] == "extra_forbidden" else "invalid_parameter"
        raise InvalidRequestError(
            f"Invalid request: {error['msg']}.", code=code, param=param
        ) from None


PrincipalDep = Annotated[Principal, Depends(require_principal)]
InvestigatorDep = Annotated[Investigator, Depends(get_investigator)]
InvestigationRequestDep = Annotated[InvestigationRequest, Depends(read_investigation_request)]
