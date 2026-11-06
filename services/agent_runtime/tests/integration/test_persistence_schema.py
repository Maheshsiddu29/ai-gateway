"""The agent schema: migrations, their guards, database-enforced invariants, and the
application role's privileges. Runs against a real PostgreSQL server."""

import asyncio
import hashlib
import uuid

import psycopg
import pytest
from psycopg import errors, sql
from pydantic import SecretStr

from agent_runtime.persistence import migrations
from agent_runtime.persistence.database import Connection, connect

DEFAULTS: dict[str, object] = {
    "principal_id": "agent-key-0123456789ab",
    "request_hash": hashlib.sha256(b"request").digest(),
    "incident": "Disk full on db-1.",
    "workflow_version": "durable-1",
    "prompt_version": "linux-investigation-1",
    "max_llm_calls": 2,
    "max_tool_calls": 6,
    "max_output_tokens": 2048,
}


async def insert_investigation(conn: Connection, **values: object) -> str:
    row = {"investigation_id": f"inv-{uuid.uuid4().hex}", **DEFAULTS, **values}
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in [*row, "deadline_at"])
    placeholders = sql.SQL(", ").join(
        [sql.Placeholder()] * len(row) + [sql.SQL("now() + interval '1 hour'")]
    )
    await conn.execute(
        sql.SQL("INSERT INTO investigations ({}) VALUES ({})").format(columns, placeholders),
        list(row.values()),
    )
    return str(row["investigation_id"])


async def claim(conn: Connection, investigation_id: str) -> None:
    await conn.execute(
        "UPDATE investigations SET status = 'running', fencing_token = fencing_token + 1,"
        " attempts = attempts + 1, lease_owner = 'w1', lease_expires_at = now()"
        " WHERE investigation_id = %s",
        (investigation_id,),
    )


async def insert_operation(conn: Connection, investigation_id: str, **values: object) -> str:
    row = {
        "operation_id": uuid.uuid4(),
        "investigation_id": investigation_id,
        "operation_type": "plan",
        "attempt": 1,
        "state": "intent",
        "fencing_token": 1,
        "model_alias": "agent-planner",
        "gateway_request_id": f"{investigation_id}-plan-1",
        "max_tokens": 512,
        **values,
    }
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in row)
    await conn.execute(
        sql.SQL("INSERT INTO operations ({}) VALUES ({})").format(
            columns, sql.SQL(", ").join([sql.Placeholder()] * len(row))
        ),
        list(row.values()),
    )
    return str(row["operation_id"])


async def test_upgrade_creates_every_table_and_is_idempotent(
    owner: Connection, schema: str
) -> None:
    assert await migrations.upgrade(owner, schema) == []
    await migrations.verify(owner)

    cursor = await owner.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (schema,),
    )
    tables = {row["table_name"] for row in await cursor.fetchall()}
    assert {"schema_migrations", "investigations", "operations"} <= tables


async def test_verify_rejects_an_unmigrated_schema(database_url: SecretStr, schema: str) -> None:
    async with await connect(database_url, schema, "agent-tests") as conn:
        with pytest.raises(migrations.SchemaError, match="missing migration 1"):
            await migrations.verify(conn)


async def test_verify_rejects_a_newer_schema(owner: Connection, schema: str) -> None:
    await owner.execute(
        "INSERT INTO schema_migrations (version, name, checksum) VALUES (99, 'future', '\\x00')"
    )

    with pytest.raises(migrations.SchemaError, match="newer than this runtime"):
        await migrations.verify(owner)
    with pytest.raises(migrations.SchemaError, match="newer than this runtime"):
        await migrations.upgrade(owner, schema)
    with pytest.raises(migrations.SchemaError, match="newer than this runtime"):
        await migrations.downgrade(owner, schema, target=0, allow_data_loss=True)


async def test_verify_rejects_a_modified_migration(owner: Connection, schema: str) -> None:
    await owner.execute("UPDATE schema_migrations SET checksum = '\\x00' WHERE version = 1")

    with pytest.raises(migrations.SchemaError, match="differs"):
        await migrations.verify(owner)
    with pytest.raises(migrations.SchemaError, match="differs"):
        await migrations.upgrade(owner, schema)


async def test_downgrade_and_upgrade_round_trip(owner: Connection, schema: str) -> None:
    reverted = await migrations.downgrade(owner, schema, target=0, allow_data_loss=False)

    assert reverted == list(range(migrations.LATEST_VERSION, 0, -1))
    cursor = await owner.execute("SELECT to_regclass('investigations') AS t")
    assert (await cursor.fetchone()) == {"t": None}
    assert await migrations.upgrade(owner, schema) == list(range(1, migrations.LATEST_VERSION + 1))
    await migrations.verify(owner)


async def test_downgrade_refuses_while_investigations_are_unfinished(
    owner: Connection, schema: str
) -> None:
    await insert_investigation(owner)

    with pytest.raises(migrations.SchemaError, match="1 investigations are unfinished"):
        await migrations.downgrade(owner, schema, target=0, allow_data_loss=True)
    await migrations.verify(owner)


async def test_downgrade_refuses_to_delete_finished_investigations_by_default(
    owner: Connection, schema: str
) -> None:
    investigation_id = await insert_investigation(owner)
    await owner.execute(
        "UPDATE investigations SET status = 'failed', failure_code = 'x', result = '{}',"
        " incident = NULL, completed_at = now() WHERE investigation_id = %s",
        (investigation_id,),
    )

    with pytest.raises(migrations.SchemaError, match="--allow-data-loss"):
        await migrations.downgrade(owner, schema, target=0, allow_data_loss=False)
    assert await migrations.downgrade(owner, schema, target=0, allow_data_loss=True)


async def test_concurrent_upgrades_apply_each_migration_once(
    database_url: SecretStr, schema: str
) -> None:
    async def run() -> list[int]:
        async with await connect(database_url, schema, "agent-tests") as conn:
            return await migrations.upgrade(conn, schema)

    results = await asyncio.gather(*(run() for _ in range(4)))

    assert sorted(len(applied) for applied in results) == [0, 0, 0, migrations.LATEST_VERSION]


@pytest.mark.parametrize(
    "values",
    [
        {"investigation_id": "inv-not-hex"},
        {"principal_id": "Has Spaces"},
        {"request_hash": b"short"},
        {"incident": ""},
        {"status": "paused"},
        {"status": "running", "lease_owner": "w1"},
        {"status": "completed", "incident": None},
        {"idempotency_key": "has space"},
        {"max_llm_calls": 9},
        {"fencing_token": 3},
    ],
)
async def test_rejects_rows_that_break_invariants(
    owner: Connection, values: dict[str, object]
) -> None:
    with pytest.raises((errors.CheckViolation, errors.NotNullViolation)):
        await insert_investigation(owner, **values)


async def test_idempotency_keys_are_unique_per_principal(owner: Connection) -> None:
    await insert_investigation(owner, idempotency_key="k1")
    await insert_investigation(owner, idempotency_key="k1", principal_id="agent-key-other")
    await insert_investigation(owner)
    await insert_investigation(owner)

    with pytest.raises(errors.UniqueViolation):
        await insert_investigation(owner, idempotency_key="k1")


async def test_terminal_investigations_cannot_change(owner: Connection) -> None:
    investigation_id = await insert_investigation(owner)
    await claim(owner, investigation_id)
    await owner.execute(
        "UPDATE investigations SET status = 'completed', result = '{}', incident = NULL,"
        " lease_owner = NULL, lease_expires_at = NULL, completed_at = now()"
        " WHERE investigation_id = %s",
        (investigation_id,),
    )

    for statement in (
        "UPDATE investigations SET status = 'running' WHERE investigation_id = %s",
        "UPDATE investigations SET result = '{\"x\": 1}' WHERE investigation_id = %s",
        "UPDATE investigations SET updated_at = now() WHERE investigation_id = %s",
    ):
        with pytest.raises(errors.CheckViolation, match="terminal"):
            await owner.execute(statement, (investigation_id,))


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        ("status = 'accepted'", "cannot return to accepted"),
        ("fencing_token = 0", "cannot decrease"),
        ("incident = 'something else'", "can only be cleared"),
        ("principal_id = 'agent-key-other'", "immutable"),
        ("max_llm_calls = 8", "immutable"),
        ("deadline_at = now()", "immutable"),
    ],
)
async def test_running_investigations_keep_their_invariants(
    owner: Connection, assignment: str, message: str
) -> None:
    investigation_id = await insert_investigation(owner)
    await claim(owner, investigation_id)

    with pytest.raises(errors.CheckViolation, match=message):
        await owner.execute(
            sql.SQL("UPDATE investigations SET {} WHERE investigation_id = %s").format(
                sql.SQL(assignment)
            ),
            (investigation_id,),
        )


async def test_only_one_operation_may_be_open(owner: Connection) -> None:
    investigation_id = await insert_investigation(owner)
    await insert_operation(owner, investigation_id)

    with pytest.raises(errors.UniqueViolation):
        await insert_operation(
            owner,
            investigation_id,
            operation_type="diagnose",
            gateway_request_id=f"{investigation_id}-diag-1",
        )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("dispatched", "intent"),
        ("succeeded", "failed"),
        ("uncertain", "succeeded"),
        ("abandoned", "dispatched"),
    ],
)
async def test_operations_only_move_forward(owner: Connection, first: str, second: str) -> None:
    investigation_id = await insert_investigation(owner)
    operation_id = await insert_operation(owner, investigation_id)
    steps = {
        "dispatched": "state = 'dispatched', dispatched_at = now()",
        "succeeded": "state = 'succeeded', completed_at = now(), may_have_executed = true",
        "uncertain": "state = 'uncertain', completed_at = now()",
        "abandoned": "state = 'abandoned', completed_at = now(), may_have_executed = false",
    }
    update = sql.SQL("UPDATE operations SET {} WHERE operation_id = %s")
    if first != "abandoned":
        await owner.execute(update.format(sql.SQL(steps["dispatched"])), (operation_id,))
    if first != "dispatched":
        await owner.execute(update.format(sql.SQL(steps[first])), (operation_id,))

    with pytest.raises(errors.CheckViolation, match="cannot go from"):
        await owner.execute(
            "UPDATE operations SET state = %s WHERE operation_id = %s", (second, operation_id)
        )


async def as_role(
    conn: Connection, role: str, statement: str, params: tuple[object, ...] = ()
) -> None:
    async with conn.transaction():
        await conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        await conn.execute(statement, params)


async def test_the_application_role_has_only_its_privileges(
    owner: Connection, app_role: str
) -> None:
    investigation_id = await insert_investigation(owner)

    await as_role(owner, app_role, "SELECT * FROM investigations")
    await as_role(owner, app_role, "SELECT * FROM schema_migrations")
    await as_role(
        owner,
        app_role,
        "UPDATE investigations SET current_stage = 'plan' WHERE investigation_id = %s",
        (investigation_id,),
    )
    for statement in (
        "DELETE FROM investigations",
        "TRUNCATE operations",
        "INSERT INTO schema_migrations (version, name, checksum) VALUES (50, 'x', '\\x00')",
        "ALTER TABLE investigations DISABLE TRIGGER investigations_guard",
        "DROP TABLE operations",
        "CREATE TABLE extra (x int)",
    ):
        with pytest.raises(psycopg.Error) as caught:
            await as_role(owner, app_role, statement)
        assert isinstance(caught.value, (errors.InsufficientPrivilege, errors.UndefinedTable)) or (
            "must be owner" in str(caught.value)
        ), statement


async def test_grant_refuses_superusers(owner: Connection, schema: str) -> None:
    cursor = await owner.execute("SELECT current_user AS name")
    row = await cursor.fetchone()
    assert row is not None

    with pytest.raises(migrations.SchemaError, match="superuser"):
        await migrations.grant(owner, schema, row["name"])
