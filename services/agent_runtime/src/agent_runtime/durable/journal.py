"""Journaled model calls: each gateway call is recorded before and after it is sent.

    intent  -> committed before anything is sent, after admission
    dispatched -> committed immediately before the HTTP request
    succeeded | failed -> committed with the observed outcome

After a crash, the journal decides what may happen (docs/agent-durable-execution.md):
an intent may be sent, because it provably never was; a dispatched call without an
outcome may have reached the provider and is never sent again; a recorded outcome
is replayed. The gateway does not deduplicate requests, and the stable
``X-Request-ID`` sent with each call is for correlation only.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence

import psycopg

from agent_runtime.budget import input_token_bound
from agent_runtime.gateway_client import ChatMessage, GatewayCallError, TokenUsage
from agent_runtime.investigation.graph import (
    LlmFailure,
    LlmOutcome,
    LlmReply,
    OperationType,
    UsageTotals,
)
from agent_runtime.investigation.workflow import CompletionClient
from agent_runtime.persistence.store import (
    AdmissionRefusedError,
    InvestigationStore,
    Lease,
    LeaseLostError,
    ObservedUsage,
    Operation,
    OperationState,
)

logger = logging.getLogger(__name__)

# How long a cancelled call may spend recording that it was abandoned.
_RECORD_ON_CANCEL_SECONDS = 5.0

OUTCOME_UNKNOWN = LlmFailure(
    "llm_outcome_unknown",
    "A model call was sent but its outcome was not recorded; it is not sent again.",
    reconciliation_required=True,
)


class JournaledLlmCaller:
    """The durable ``LlmCaller``. One instance per claimed investigation.

    ``interruption`` tells a cancelled call why it was cancelled: when it returns a
    code (the deadline passed, or shutdown ran out of time) the call is recorded as
    failed and possibly executed. When it returns None (the lease was lost) nothing
    can be written, and the next owner finds the call dispatched.
    """

    def __init__(
        self,
        store: InvestigationStore,
        lease: Lease,
        gateway: CompletionClient,
        *,
        max_output_tokens: int,
        interruption: Callable[[], str | None],
    ) -> None:
        self._store = store
        self._lease = lease
        self._gateway = gateway
        self._max_output_tokens = max_output_tokens
        self._interruption = interruption

    async def call(
        self, operation: OperationType, model: str, messages: Sequence[ChatMessage]
    ) -> LlmOutcome:
        investigation_id = self._lease.investigation_id
        existing = await self._store.latest_operation(investigation_id, operation)
        if existing is None:
            try:
                existing = await self._store.create_intent(
                    self._lease,
                    operation,
                    model,
                    self._max_output_tokens,
                    input_token_bound(messages),
                )
            except AdmissionRefusedError as exc:
                return LlmFailure(exc.code, exc.message)
        replayed = self._replay(existing)
        if replayed is not None:
            if existing.state is OperationState.DISPATCHED:
                await self._store.mark_uncertain(self._lease, existing)
                logger.warning(
                    "model call outcome unknown",
                    extra={
                        "investigation_id": investigation_id,
                        "operation_id": str(existing.operation_id),
                        "operation_type": operation.value,
                    },
                )
            return replayed
        # Only an intent reaches this point: it was never sent.
        await self._store.mark_dispatched(self._lease, existing)
        return await self._send(existing, messages)

    def _replay(self, existing: Operation) -> LlmOutcome | None:
        match existing.state:
            case OperationState.INTENT:
                return None
            case OperationState.SUCCEEDED if existing.reply is not None:
                return LlmReply(existing.reply)
            case OperationState.FAILED if existing.failure_code is not None:
                return LlmFailure(
                    existing.failure_code,
                    "The model call failed; its recorded outcome was replayed.",
                    existing.gateway_status,
                    existing.gateway_error_code,
                )
            case _:
                # dispatched, uncertain, or an outcome without its details.
                return OUTCOME_UNKNOWN

    async def _send(self, operation: Operation, messages: Sequence[ChatMessage]) -> LlmOutcome:
        try:
            completion = await self._gateway.complete(
                model=operation.model_alias,
                messages=messages,
                max_tokens=operation.max_tokens,
                request_id=operation.gateway_request_id,
            )
        except GatewayCallError as exc:
            await self._store.record_failure(
                self._lease,
                operation,
                code=exc.code.value,
                may_have_executed=exc.may_have_executed,
                gateway_status=exc.status_code,
                gateway_error_code=exc.gateway_error_code,
                usage=_observed(exc.usage),
            )
            return LlmFailure(exc.code.value, exc.message, exc.status_code, exc.gateway_error_code)
        except asyncio.CancelledError:
            code = self._interruption()
            if code is not None:
                await self._record_abandoned(operation, code)
            raise
        await self._store.record_success(
            self._lease, operation, completion.content, _observed(completion.usage)
        )
        return LlmReply(completion.content)

    async def _record_abandoned(self, operation: Operation, code: str) -> None:
        # Shielded so the record survives the cancellation that triggered it.
        record = self._store.record_failure(
            self._lease, operation, code=code, may_have_executed=True
        )
        try:
            await asyncio.wait_for(asyncio.shield(record), _RECORD_ON_CANCEL_SECONDS)
        except (TimeoutError, psycopg.Error, LeaseLostError):
            logger.warning(
                "could not record an abandoned model call",
                extra={
                    "investigation_id": self._lease.investigation_id,
                    "operation_id": str(operation.operation_id),
                },
            )

    async def usage(self) -> UsageTotals:
        return await self._store.usage(self._lease.investigation_id)


def _observed(usage: TokenUsage | None) -> ObservedUsage | None:
    return None if usage is None else ObservedUsage(usage.prompt_tokens, usage.completion_tokens)
