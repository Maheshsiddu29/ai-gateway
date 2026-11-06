from typing import Annotated

from fastapi import Depends, Request

from agent_runtime.api.errors import AuthenticationError
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


PrincipalDep = Annotated[Principal, Depends(require_principal)]
