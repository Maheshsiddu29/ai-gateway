from fastapi import APIRouter
from fastapi.responses import JSONResponse

from gateway.api.dependencies import CompletionRequestDep, CompletionServiceDep, PrincipalDep
from gateway.api.encoding import completion_body, new_completion_id, unix_time

router = APIRouter()


# The principal dependency is declared first so that authentication is checked
# before the request body is read.
@router.post("/v1/chat/completions")
async def create_chat_completion(
    principal: PrincipalDep,
    body: CompletionRequestDep,
    service: CompletionServiceDep,
) -> JSONResponse:
    completion = await service.complete(body, principal)
    return JSONResponse(
        completion_body(
            completion, completion_id=new_completion_id(), created=unix_time(), model=body.model
        )
    )
