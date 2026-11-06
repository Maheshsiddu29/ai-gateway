import re
from typing import Annotated

from fastapi import Depends, Request
from pydantic import ValidationError

from agent_runtime.api.errors import (
    AuthenticationError,
    InvalidRequestError,
    NotReadyError,
)
from agent_runtime.config import ApiSettings
from agent_runtime.investigation.schemas import InvestigationRequest
from agent_runtime.persistence.store import InvestigationStore
from agent_runtime.security import Authenticator, Principal, parse_bearer_token

IDEMPOTENCY_KEY_HEADER = "idempotency-key"
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")


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


async def get_store(request: Request) -> InvestigationStore:
    store: InvestigationStore | None = request.app.state.store
    if store is None:
        raise NotReadyError("The agent runtime is not ready.")
    return store


async def read_idempotency_key(request: Request) -> str | None:
    key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if key is not None and not _IDEMPOTENCY_KEY.fullmatch(key):
        raise InvalidRequestError(
            "Idempotency-Key must be 1 to 128 characters from A-Z a-z 0-9 . _ : -.",
            code="invalid_idempotency_key",
            param="Idempotency-Key",
        )
    return key


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
    settings: ApiSettings = request.app.state.settings
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
StoreDep = Annotated[InvestigationStore, Depends(get_store)]
IdempotencyKeyDep = Annotated[str | None, Depends(read_idempotency_key)]
InvestigationRequestDep = Annotated[InvestigationRequest, Depends(read_investigation_request)]
