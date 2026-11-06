"""The model call journal against PostgreSQL and a counting gateway stub.

Each test leaves the journal as a crash at a given point would and checks what a
resumed worker does, by the number of requests that reach the gateway.
"""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from agent_runtime.durable.journal import OUTCOME_UNKNOWN, JournaledLlmCaller
from agent_runtime.gateway_client import ChatMessage, GatewayClient, GatewayTimeouts
from agent_runtime.investigation.graph import LlmFailure, LlmReply, OperationType
from agent_runtime.persistence.database import Connection, Pool
from agent_runtime.persistence.store import InvestigationStore, Lease, LeaseLostError
from tests.db import insert_running
from tests.fakes import GATEWAY_API_KEY, GatewayHandler, GatewayStub, completion_body, gateway_error

MESSAGES = (ChatMessage(role="user", content="plan please"),)
REPLY = '{"steps": []}'
USAGE = {"prompt_tokens": 40, "completion_tokens": 7}


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=completion_body(REPLY, model="planner", usage=USAGE))


class Harness:
    def __init__(
        self,
        pool: Pool,
        lease: Lease,
        handler: GatewayHandler = ok,
        interruption: str | None = None,
    ) -> None:
        self.store = InvestigationStore(pool)
        self.stub = GatewayStub(handler)
        self.client = GatewayClient(
            "http://gateway.test",
            SecretStr(GATEWAY_API_KEY),
            timeouts=GatewayTimeouts(),
            max_connections=2,
            max_request_bytes=65_536,
            max_response_bytes=65_536,
            transport=self.stub,
        )
        self.caller = JournaledLlmCaller(
            self.store,
            lease,
            self.client,
            max_output_tokens=512,
            interruption=lambda: interruption,
        )

    async def plan(self) -> LlmReply | LlmFailure:
        try:
            return await self.caller.call(OperationType.PLAN, "planner", MESSAGES)
        finally:
            await self.client.aclose()


async def operations(conn: Connection, investigation_id: str) -> list[dict[str, object]]:
    cursor = await conn.execute(
        "SELECT state, may_have_executed, failure_code, reply, usage, gateway_request_id,"
        " gateway_status FROM operations WHERE investigation_id = %s ORDER BY started_at",
        (investigation_id,),
    )
    return list(await cursor.fetchall())


async def admitted(conn: Connection, investigation_id: str) -> int:
    cursor = await conn.execute(
        "SELECT llm_calls_admitted FROM investigations WHERE investigation_id = %s",
        (investigation_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row["llm_calls_admitted"])


async def test_a_call_is_dispatched_in_the_journal_before_it_is_sent(
    owner: Connection, pool: Pool
) -> None:
    lease = await insert_running(owner)
    seen_while_sending: list[object] = []

    async def observe(request: httpx.Request) -> httpx.Response:
        seen_while_sending.extend(
            op["state"] for op in await operations(owner, lease.investigation_id)
        )
        return ok(request)

    harness = Harness(pool, lease, observe)
    outcome = await harness.plan()

    assert outcome == LlmReply(REPLY)
    assert seen_while_sending == ["dispatched"]
    [operation] = await operations(owner, lease.investigation_id)
    assert operation["state"] == "succeeded"
    assert operation["reply"] == REPLY
    assert operation["usage"] == USAGE
    assert harness.stub.requests[0].headers["x-request-id"] == operation["gateway_request_id"]
    assert await admitted(owner, lease.investigation_id) == 1
    assert (await harness.store.usage(lease.investigation_id)).prompt_tokens == 40


async def test_a_recorded_reply_is_replayed_without_a_call(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner)
    await Harness(pool, lease).plan()

    again = Harness(pool, lease)
    outcome = await again.plan()

    assert outcome == LlmReply(REPLY)
    assert again.stub.requests == []
    assert await admitted(owner, lease.investigation_id) == 1


async def test_an_intent_left_by_a_crash_is_sent_once(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    await store.create_intent(lease, OperationType.PLAN, "planner", 512)  # then the crash

    harness = Harness(pool, lease)
    outcome = await harness.plan()

    assert outcome == LlmReply(REPLY)
    assert len(harness.stub.requests) == 1
    assert [op["state"] for op in await operations(owner, lease.investigation_id)] == ["succeeded"]
    assert await admitted(owner, lease.investigation_id) == 1


async def test_a_dispatched_call_without_an_outcome_is_never_resent(
    owner: Connection, pool: Pool
) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    operation = await store.create_intent(lease, OperationType.PLAN, "planner", 512)
    # The crash happened after dispatch was recorded: during the request, or after
    # the reply arrived but before it was recorded.
    await store.mark_dispatched(lease, operation)

    harness = Harness(pool, lease)
    outcome = await harness.plan()

    assert outcome == OUTCOME_UNKNOWN
    assert outcome.reconciliation_required
    assert harness.stub.requests == []
    [recorded] = await operations(owner, lease.investigation_id)
    assert recorded["state"] == "uncertain"
    assert recorded["may_have_executed"] is True
    # And it stays unknown on every later attempt.
    later = Harness(pool, lease)
    assert await later.plan() == OUTCOME_UNKNOWN
    assert later.stub.requests == []


@pytest.mark.parametrize(
    ("response", "code", "may_have_executed"),
    [
        (
            gateway_error(429, "rate_limited", "tokens_per_minute_exceeded"),
            "gateway_rate_limited",
            False,
        ),
        (
            gateway_error(502, "provider_unavailable", "provider_server_error"),
            "gateway_unavailable",
            True,
        ),
        (
            httpx.Response(200, json=completion_body("x", model="planner", finish_reason="length")),
            "completion_truncated",
            True,
        ),
    ],
)
async def test_gateway_failures_are_recorded_and_replayed(
    owner: Connection,
    pool: Pool,
    response: httpx.Response,
    code: str,
    may_have_executed: bool,
) -> None:
    lease = await insert_running(owner)
    first = Harness(pool, lease, lambda _: response)

    outcome = await first.plan()

    assert isinstance(outcome, LlmFailure)
    assert outcome.code == code
    assert not outcome.reconciliation_required
    [recorded] = await operations(owner, lease.investigation_id)
    assert (recorded["state"], recorded["failure_code"]) == ("failed", code)
    assert recorded["may_have_executed"] is may_have_executed
    replay = Harness(pool, lease)
    replayed = await replay.plan()
    assert isinstance(replayed, LlmFailure)
    assert replayed.code == code
    assert replay.stub.requests == []


async def test_the_call_limit_is_admitted_atomically(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner, max_llm_calls=1)
    await Harness(pool, lease).plan()

    harness = Harness(pool, lease)
    outcome = await harness.caller.call(OperationType.DIAGNOSE, "diagnosis", MESSAGES)
    await harness.client.aclose()

    assert isinstance(outcome, LlmFailure)
    assert outcome.code == "llm_call_limit_exceeded"
    assert harness.stub.requests == []
    assert len(await operations(owner, lease.investigation_id)) == 1


async def test_a_stale_lease_cannot_start_a_call(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner, fencing_token=2)
    stale = Lease(lease.investigation_id, "worker-old", 1)
    harness = Harness(pool, stale)

    with pytest.raises(LeaseLostError):
        await harness.plan()
    assert harness.stub.requests == []
    assert await operations(owner, lease.investigation_id) == []


async def test_losing_the_lease_during_a_call_leaves_it_dispatched(
    owner: Connection, pool: Pool
) -> None:
    lease = await insert_running(owner)

    async def taken_over(request: httpx.Request) -> httpx.Response:
        # Another worker claims the investigation while this call is in flight.
        await owner.execute(
            "UPDATE investigations SET fencing_token = fencing_token + 1"
            " WHERE investigation_id = %s",
            (lease.investigation_id,),
        )
        return ok(request)

    harness = Harness(pool, lease, taken_over)
    with pytest.raises(LeaseLostError):
        await harness.plan()

    [recorded] = await operations(owner, lease.investigation_id)
    assert recorded["state"] == "dispatched"
    assert recorded["reply"] is None


@pytest.mark.parametrize(
    ("interruption", "state"),
    [("investigation_timeout", "failed"), (None, "dispatched")],
)
async def test_a_cancelled_call_is_recorded_only_when_it_may_be(
    owner: Connection, pool: Pool, interruption: str | None, state: str
) -> None:
    lease = await insert_running(owner)
    sent = asyncio.Event()

    async def hang(_: httpx.Request) -> httpx.Response:
        sent.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    harness = Harness(pool, lease, hang, interruption=interruption)
    task = asyncio.create_task(harness.plan())
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [recorded] = await operations(owner, lease.investigation_id)
    assert recorded["state"] == state
    if state == "failed":
        assert recorded["failure_code"] == interruption
        assert recorded["may_have_executed"] is True
