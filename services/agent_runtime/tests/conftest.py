"""PostgreSQL fixtures. Each test gets its own schema, dropped afterwards.

Set AGENT_TEST_DATABASE_URL to a postgresql:// URL whose user may create schemas
and roles. Without it the database tests are skipped, unless
AGENT_REQUIRE_DATABASE_TESTS=1 (as in CI), which makes them fail instead.
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from psycopg import sql
from pydantic import SecretStr

from agent_runtime.persistence import migrations
from agent_runtime.persistence.database import Connection, connect


@pytest.fixture
def database_url() -> SecretStr:
    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("AGENT_REQUIRE_DATABASE_TESTS") == "1":
            pytest.fail("AGENT_TEST_DATABASE_URL is required but not set")
        pytest.skip("AGENT_TEST_DATABASE_URL is not set")
    return SecretStr(url)


@pytest.fixture
async def schema(database_url: SecretStr) -> AsyncIterator[str]:
    name = f"agent_test_{uuid.uuid4().hex[:12]}"
    try:
        yield name
    finally:
        async with await connect(database_url, "public", "agent-tests") as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
            )


@pytest.fixture
async def owner(database_url: SecretStr, schema: str) -> AsyncIterator[Connection]:
    """An owner connection to a fully migrated test schema."""
    async with await connect(database_url, schema, "agent-tests") as conn:
        await migrations.upgrade(conn, schema)
        yield conn


@pytest.fixture
async def app_role(database_url: SecretStr, owner: Connection, schema: str) -> AsyncIterator[str]:
    """A login-less role holding exactly the application privileges."""
    role = f"agent_app_{uuid.uuid4().hex[:12]}"
    await owner.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
    try:
        await migrations.grant(owner, schema, role)
        yield role
    finally:
        async with await connect(database_url, "public", "agent-tests") as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
            await conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            await conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
