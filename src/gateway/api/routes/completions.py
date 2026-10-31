from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gateway.api.dependencies import (
    CompletionRequestDep,
    CompletionServiceDep,
    DeadlineDep,
    PrincipalDep,
)
from gateway.api.disconnect import run_until_disconnect
from gateway.api.encoding import completion_body, new_completion_id, unix_time
from gateway.api.streaming import EventStreamResponse, encode_stream

router = APIRouter()


# Dependencies are resolved in declaration order: the deadline starts first so it
# covers authentication and body reading, and authentication is checked before the
# request body is read.
@router.post("/v1/chat/completions")
async def create_chat_completion(
    deadline: DeadlineDep,
    principal: PrincipalDep,
    body: CompletionRequestDep,
    service: CompletionServiceDep,
    request: Request,
) -> Response:
    completion_id = new_completion_id()
    created = unix_time()

    if body.stream:
        events = service.stream(body, principal, deadline)
        try:
            # Wait for the first upstream event before starting the response, so
            # failures that happen before any output get a regular error status.
            first = await run_until_disconnect(request.receive, anext(events))
        except BaseException:
            await events.aclose()
            raise
        return EventStreamResponse(
            deadline,
            encode_stream(
                first,
                events,
                completion_id=completion_id,
                created=created,
                model=body.model,
                include_usage=body.include_usage,
            ),
        )

    completion = await run_until_disconnect(
        request.receive, service.complete(body, principal, deadline)
    )
    return JSONResponse(
        completion_body(completion, completion_id=completion_id, created=created, model=body.model)
    )
