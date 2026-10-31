"""Per-request bridge between provider attempts and monetary accounting."""

import asyncio
import uuid

from gateway.cost.store import CostStore
from gateway.providers.base import ProviderRequest, Usage


class CostSession:
    def __init__(self, store: CostStore, principal_id: str, estimated_input_tokens: int) -> None:
        self.store = store
        self.principal_id = principal_id
        self.request_id = uuid.uuid4()
        self.estimated_input_tokens = estimated_input_tokens

    async def reserve(self, provider: str, request: ProviderRequest) -> uuid.UUID:
        if request.max_tokens is None:
            raise ValueError("a priced attempt requires a maximum output token count")
        attempt_id = uuid.uuid4()
        await self.store.reserve(
            attempt_id=attempt_id,
            request_id=self.request_id,
            principal_id=self.principal_id,
            provider=provider,
            model=request.model,
            estimated_input_tokens=self.estimated_input_tokens,
            maximum_output_tokens=request.max_tokens,
        )
        return attempt_id

    async def settle(
        self,
        attempt_id: uuid.UUID,
        usage: Usage | None,
        *,
        may_have_processed: bool,
        classification: str,
    ) -> None:
        task = asyncio.create_task(
            self.store.settle(
                attempt_id,
                usage,
                may_have_processed=may_have_processed,
                classification=classification,
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
