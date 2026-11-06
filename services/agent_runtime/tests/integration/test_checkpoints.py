"""The investigation graph with LangGraph's PostgreSQL checkpointer.

Checks that a crashed run resumes from its last checkpoint without repeating
finished stages, that checkpoints are read strictly, and that the application
role can use the checkpointer without schema privileges.
"""

import dataclasses
import json
import logging

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import errors, sql
from pydantic import SecretStr

from agent_runtime.investigation.graph import (
    CheckpointStateError,
    OperationType,
    build_graph,
)
from agent_runtime.investigation.schemas import InvestigationResult
from agent_runtime.persistence import migrations
from agent_runtime.persistence.checkpoints import (
    LATEST_CHECKPOINT_MIGRATION,
    checkpoint_version,
    checkpointer,
    strict_serializer,
)
from agent_runtime.persistence.database import Connection, connect
from tests.fakes import CountingBackend, ScriptedLlm, stage_context

THREAD: RunnableConfig = {"configurable": {"thread_id": "inv-0123456789abcdef0123456789abcdef"}}


class SimulatedCrashError(Exception):
    pass


async def test_upgrade_creates_the_checkpoint_tables(owner: Connection) -> None:
    assert await checkpoint_version(owner) == LATEST_CHECKPOINT_MIGRATION
    await migrations.verify(owner)


async def test_verify_rejects_outdated_checkpoint_tables(owner: Connection) -> None:
    await owner.execute(
        "DELETE FROM checkpoint_migrations WHERE v = %s", (LATEST_CHECKPOINT_MIGRATION,)
    )

    with pytest.raises(migrations.SchemaError, match="checkpoint tables"):
        await migrations.verify(owner)


async def test_a_crashed_run_resumes_without_repeating_finished_stages(owner: Connection) -> None:
    graph = build_graph().compile(checkpointer=checkpointer(owner))
    backend = CountingBackend()
    crashing = ScriptedLlm()
    crashing.replies[OperationType.DIAGNOSE] = SimulatedCrashError("crash during diagnose")

    with pytest.raises(SimulatedCrashError):
        await graph.ainvoke({}, THREAD, context=stage_context(crashing, backend), durability="sync")

    snapshot = await graph.aget_state(THREAD)
    assert snapshot.next == ("diagnose",)
    assert len(snapshot.values["evidence"]) == 2

    resumed = ScriptedLlm()
    results: list[InvestigationResult] = []
    await graph.ainvoke(
        None, THREAD, context=stage_context(resumed, backend, results=results), durability="sync"
    )

    assert crashing.calls == [OperationType.PLAN, OperationType.DIAGNOSE]
    assert resumed.calls == [OperationType.DIAGNOSE]
    assert len(backend.requests) == 2  # collected once, before the crash
    assert results[0].status == "completed"
    assert results[0].diagnosis is not None
    assert (await graph.aget_state(THREAD)).next == ()


async def test_tampered_checkpoint_state_is_rejected(owner: Connection) -> None:
    graph = build_graph().compile(checkpointer=checkpointer(owner))
    backend = CountingBackend()
    crashing = ScriptedLlm()
    crashing.replies[OperationType.DIAGNOSE] = SimulatedCrashError()
    with pytest.raises(SimulatedCrashError):
        await graph.ainvoke({}, THREAD, context=stage_context(crashing, backend), durability="sync")
    # Rewind to before collection, and slip an unpermitted tool into the plan.
    history = [state async for state in graph.aget_state_history(THREAD)]
    before_collection = next(s for s in history if s.next == ("collect_diagnostics",))
    tampered = await graph.aupdate_state(
        before_collection.config,
        {"plan": [{"tool": "run_shell", "arguments": {"cmd": "id"}, "reason": "x"}]},
        as_node="validate_plan",
    )
    backend.requests.clear()

    with pytest.raises(CheckpointStateError):
        await graph.ainvoke(None, tampered, context=stage_context(ScriptedLlm(), backend))
    assert backend.requests == []


@dataclasses.dataclass
class Payload:
    command: str


async def test_checkpoints_never_construct_unlisted_types(
    owner: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    permissive = AsyncPostgresSaver(owner, serde=JsonPlusSerializer(allowed_msgpack_modules=True))
    graph = build_graph().compile(checkpointer=permissive)
    crashing = ScriptedLlm({OperationType.PLAN: SimulatedCrashError()})
    with pytest.raises(SimulatedCrashError):
        await graph.ainvoke({}, THREAD, context=stage_context(crashing, CountingBackend()))
    # Store an object of an arbitrary class in the checkpoint, as an attacker with
    # write access to the checkpoint tables could.
    await graph.aupdate_state(THREAD, {"plan_reply": Payload("echo PWNED")}, as_node="plan")

    strict = build_graph().compile(checkpointer=checkpointer(owner))
    state = (await strict.aget_state(THREAD)).values

    assert state["plan_reply"] == {"command": "echo PWNED"}
    assert not isinstance(state["plan_reply"], Payload)
    assert "Blocked deserialization" in caplog.text
    # The graph then refuses the malformed value instead of acting on it.
    with pytest.raises(CheckpointStateError):
        await strict.ainvoke(None, THREAD, context=stage_context(ScriptedLlm(), CountingBackend()))


def test_the_strict_serializer_round_trips_graph_state() -> None:
    serde = strict_serializer()
    state = {"plan": [{"tool": "memory_info", "arguments": {}, "reason": "r"}], "x": [1.5, None]}

    assert serde.loads_typed(serde.dumps_typed(state)) == state
    assert json.loads(json.dumps(state)) == state


async def test_the_application_role_can_checkpoint_but_not_alter_the_schema(
    database_url: SecretStr, owner: Connection, app_role: str, schema: str
) -> None:
    async with await connect(database_url, schema, "agent-tests") as conn:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(app_role)))
        graph = build_graph().compile(checkpointer=checkpointer(conn))
        results: list[InvestigationResult] = []
        await graph.ainvoke(
            {}, THREAD, context=stage_context(ScriptedLlm(), CountingBackend(), results=results)
        )
        await checkpointer(conn).adelete_thread(THREAD["configurable"]["thread_id"])
        await migrations.verify(conn)

        assert results[0].status == "completed"
        with pytest.raises(errors.InsufficientPrivilege):
            await checkpointer(conn).setup()
