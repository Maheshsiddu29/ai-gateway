"""LangGraph checkpoints in the agent schema, read with a strict serializer.

``AsyncPostgresSaver`` stores graph state in its own tables (``checkpoints``,
``checkpoint_blobs``, ``checkpoint_writes``, ``checkpoint_migrations``), created
in the agent schema by the migration command, never at application startup.

Checkpoints are msgpack. By default LangGraph will rebuild instances of any
importable class named in a checkpoint and only warns. Here
``allowed_msgpack_modules=None`` restricts that to LangGraph's built-in list of
plain value types (CVE-2026-28277), and pickle is disabled. Anything else comes
back as plain data, which the graph then rejects when it validates its state.
"""

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent_runtime.persistence.database import Connection, Pool

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
LATEST_CHECKPOINT_MIGRATION = len(AsyncPostgresSaver.MIGRATIONS) - 1


def strict_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        pickle_fallback=False, allowed_json_modules=None, allowed_msgpack_modules=None
    )


def checkpointer(conn: Connection | Pool) -> AsyncPostgresSaver:
    return AsyncPostgresSaver(conn, serde=strict_serializer())


async def create_checkpoint_tables(conn: Connection) -> None:
    """Create or upgrade LangGraph's tables. Needs the schema owner and autocommit,
    because some of LangGraph's migrations create indexes concurrently."""
    await checkpointer(conn).setup()


async def checkpoint_version(conn: Connection) -> int | None:
    cursor = await conn.execute(
        "SELECT to_regclass('checkpoint_migrations') IS NOT NULL AS present"
    )
    row = await cursor.fetchone()
    if row is None or not row["present"]:
        return None
    cursor = await conn.execute("SELECT max(v) AS version FROM checkpoint_migrations")
    row = await cursor.fetchone()
    return None if row is None else row["version"]


async def drop_checkpoint_tables(conn: Connection) -> None:
    await conn.execute(
        "DROP TABLE IF EXISTS checkpoint_writes, checkpoint_blobs, checkpoints,"
        " checkpoint_migrations"
    )
