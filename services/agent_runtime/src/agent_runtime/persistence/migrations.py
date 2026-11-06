"""Versioned schema migrations for the agent runtime's own PostgreSQL schema.

Migrations are plain SQL files shipped in the package, applied in order, each in
its own transaction together with its row in ``schema_migrations``. Every upgrade
then creates or upgrades LangGraph's checkpoint tables in the same schema. They run only
through ``python -m agent_runtime.persistence`` with the schema owner's
credentials; the API and worker connect as an unprivileged role and only
``verify`` the schema, refusing to start on any mismatch.
"""

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files

from psycopg import sql

from agent_runtime.persistence.checkpoints import (
    CHECKPOINT_TABLES,
    LATEST_CHECKPOINT_MIGRATION,
    checkpoint_version,
    create_checkpoint_tables,
    drop_checkpoint_tables,
)
from agent_runtime.persistence.database import SCHEMA_PATTERN, Connection

_LOCK_TIMEOUT_SECONDS = 300
_FILE = re.compile(r"(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.up\.sql")


class SchemaError(Exception):
    """The database schema does not match this version of the runtime."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str

    @property
    def checksum(self) -> bytes:
        return hashlib.sha256(self.up.encode()).digest()


def _load() -> tuple[Migration, ...]:
    directory = files("agent_runtime.persistence") / "migrations"
    migrations = []
    for entry in directory.iterdir():
        match = _FILE.fullmatch(entry.name)
        if match is None:
            continue
        down = directory / entry.name.replace(".up.sql", ".down.sql")
        migrations.append(
            Migration(int(match["version"]), match["name"], entry.read_text(), down.read_text())
        )
    migrations.sort(key=lambda migration: migration.version)
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise RuntimeError("migration versions must be consecutive from 1")
    return tuple(migrations)


MIGRATIONS = _load()
LATEST_VERSION = MIGRATIONS[-1].version

# Exactly what the unprivileged application role may do. Nothing is deleted by
# the application, and it cannot change the schema or its triggers.
APP_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "schema_migrations": ("SELECT",),
    "investigations": ("SELECT", "INSERT", "UPDATE"),
    "operations": ("SELECT", "INSERT", "UPDATE"),
    "checkpoint_migrations": ("SELECT",),
    # LangGraph upserts checkpoints and deletes a thread's when it is finished.
    **dict.fromkeys(CHECKPOINT_TABLES, ("SELECT", "INSERT", "UPDATE", "DELETE")),
}


async def applied_versions(conn: Connection) -> dict[int, bytes]:
    """Applied versions and checksums; empty if the schema was never migrated."""
    cursor = await conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS present")
    row = await cursor.fetchone()
    if row is None or not row["present"]:
        return {}
    cursor = await conn.execute("SELECT version, checksum FROM schema_migrations")
    return {row["version"]: bytes(row["checksum"]) for row in await cursor.fetchall()}


async def verify(conn: Connection) -> None:
    """Raise ``SchemaError`` unless every migration, and only those, is applied."""
    applied = await applied_versions(conn)
    known = {migration.version: migration for migration in MIGRATIONS}
    newer = sorted(set(applied) - set(known))
    if newer:
        raise SchemaError(
            f"database schema version {newer[-1]} is newer than this runtime ({LATEST_VERSION})"
        )
    missing = sorted(set(known) - set(applied))
    if missing:
        raise SchemaError(
            f"database schema is missing migration {missing[0]}; run"
            " `python -m agent_runtime.persistence upgrade`"
        )
    for version, checksum in applied.items():
        if checksum != known[version].checksum:
            raise SchemaError(f"migration {version} differs from the one applied to the database")
    checkpoints = await checkpoint_version(conn)
    if checkpoints != LATEST_CHECKPOINT_MIGRATION:
        raise SchemaError(
            f"checkpoint tables are at version {checkpoints}, expected"
            f" {LATEST_CHECKPOINT_MIGRATION}; run `python -m agent_runtime.persistence upgrade`"
        )


async def upgrade(conn: Connection, schema: str, *, target: int | None = None) -> list[int]:
    """Apply pending migrations up to ``target`` (default: all). Returns the versions applied."""
    target = LATEST_VERSION if target is None else target
    if not 1 <= target <= LATEST_VERSION:
        raise ValueError(f"target must be between 1 and {LATEST_VERSION}")
    async with _migration_lock(conn, schema):
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version integer PRIMARY KEY, name text NOT NULL, checksum bytea NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = await applied_versions(conn)
        if max(applied, default=0) > LATEST_VERSION:
            raise SchemaError("the database schema is newer than this runtime; refusing to upgrade")
        done = []
        for migration in MIGRATIONS:
            if migration.version in applied:
                if applied[migration.version] != migration.checksum:
                    raise SchemaError(f"migration {migration.version} differs from the one applied")
                continue
            if migration.version > target:
                break
            async with conn.transaction():
                await conn.execute(migration.up)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            done.append(migration.version)
        if target == LATEST_VERSION:
            await create_checkpoint_tables(conn)
        return done


async def downgrade(
    conn: Connection, schema: str, *, target: int, allow_data_loss: bool
) -> list[int]:
    """Revert migrations above ``target``. Returns the versions reverted.

    Refused while any investigation is unfinished, because running work depends
    on the current schema, and refused when reverting the first migration would
    delete investigations unless ``allow_data_loss`` is set.
    """
    if not 0 <= target <= LATEST_VERSION:
        raise ValueError(f"target must be between 0 and {LATEST_VERSION}")
    async with _migration_lock(conn, schema):
        applied = await applied_versions(conn)
        if max(applied, default=0) > LATEST_VERSION:
            raise SchemaError(
                "the database schema is newer than this runtime; refusing to downgrade"
            )
        reverting = sorted((v for v in applied if v > target), reverse=True)
        if not reverting:
            return []
        cursor = await conn.execute(
            "SELECT count(*) FILTER (WHERE status NOT IN ('completed', 'failed',"
            " 'reconciliation_required')) AS active, count(*) AS total FROM investigations"
        )
        counts = await cursor.fetchone()
        if counts is None:
            raise SchemaError("could not count investigations")
        if counts["active"]:
            raise SchemaError(
                f"{counts['active']} investigations are unfinished; downgrade refused"
            )
        if 1 in reverting and counts["total"] and not allow_data_loss:
            raise SchemaError(
                f"reverting migration 1 deletes {counts['total']} investigations;"
                " pass --allow-data-loss to proceed"
            )
        known = {migration.version: migration for migration in MIGRATIONS}
        if 1 in reverting:
            await drop_checkpoint_tables(conn)
        for version in reverting:
            async with conn.transaction():
                await conn.execute(known[version].down)
                await conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        return reverting


async def grant(conn: Connection, schema: str, role: str) -> None:
    """Give ``role`` exactly the privileges in ``APP_TABLE_PRIVILEGES``.

    Refuses superusers and the schema's owner, which would bypass the limits.
    """
    cursor = await conn.execute(
        "SELECT r.rolsuper, n.nspowner = r.oid AS owns_schema"
        " FROM pg_roles r CROSS JOIN pg_namespace n WHERE r.rolname = %s AND n.nspname = %s",
        (role, schema),
    )
    row = await cursor.fetchone()
    if row is None:
        raise SchemaError(f"role {role!r} or schema {schema!r} does not exist")
    if row["rolsuper"] or row["owns_schema"]:
        raise SchemaError("the application role must not be a superuser or own the schema")
    async with conn.transaction():
        await conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        await conn.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        for table, privileges in APP_TABLE_PRIVILEGES.items():
            await conn.execute(
                sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                    sql.SQL(", ").join(sql.SQL(privilege) for privilege in privileges),
                    sql.Identifier(schema, table),
                    sql.Identifier(role),
                )
            )


@asynccontextmanager
async def _migration_lock(conn: Connection, schema: str) -> AsyncIterator[None]:
    """Serialize migration runs on one schema across processes."""
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError("invalid database schema name")
    key = f"agent_runtime.migrations:{schema}"
    # Polled rather than awaited: a session blocked inside pg_advisory_lock holds a
    # snapshot, and LangGraph's CREATE INDEX CONCURRENTLY, run by the lock holder,
    # waits for every such snapshot, which would deadlock.
    loop = asyncio.get_running_loop()
    give_up = loop.time() + _LOCK_TIMEOUT_SECONDS
    while True:
        cursor = await conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked", (key,)
        )
        row = await cursor.fetchone()
        if row is not None and row["locked"]:
            break
        if loop.time() > give_up:
            raise SchemaError("another migration is still running; try again later")
        await asyncio.sleep(0.2)
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
