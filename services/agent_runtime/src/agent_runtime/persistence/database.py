"""PostgreSQL connections for the agent runtime.

Every connection sets ``search_path`` to the runtime's own schema, so the
runtime's tables, and LangGraph's checkpoint tables, never mix with anything
else in the database. Connections are in autocommit mode; multi-statement work
uses explicit transactions.
"""

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

SCHEMA_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})

type Connection = AsyncConnection[DictRow]
type Pool = AsyncConnectionPool[Connection]


def validate_database_url(url: SecretStr, *, require_tls: bool) -> None:
    """Accept only postgresql:// URLs that leave ``search_path`` to the runtime.

    ``require_tls`` demands an ``sslmode`` that encrypts the connection.
    """
    try:
        parsed = urlsplit(url.get_secret_value())
        query = parse_qs(parsed.query)
        # Reading the port validates its syntax and range.
        port = parsed.port
        valid = (
            parsed.scheme in {"postgresql", "postgres"}
            and (port is None or port > 0)
            and bool(parsed.hostname or query.get("host"))
            and bool(parsed.path.strip("/"))
            and not parsed.fragment
            # The runtime sets search_path itself; a URL must not override it.
            and "options" not in query
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "database URL must be postgresql://user:password@host/database without an options"
            " parameter"
        )
    if require_tls and not _TLS_MODES.intersection(query.get("sslmode", [])):
        raise ValueError(
            "production database URLs require sslmode=require, verify-ca, or verify-full"
        )


def search_path_option(schema: str) -> str:
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError("invalid database schema name")
    return f"-c search_path={schema}"


def connection_kwargs(schema: str, application_name: str) -> dict[str, Any]:
    """Settings for every connection. LangGraph's checkpointer requires dict rows;
    prepared statements are disabled so transaction-pooling proxies also work."""
    return {
        "autocommit": True,
        "row_factory": dict_row,
        "prepare_threshold": None,
        "options": search_path_option(schema),
        "application_name": application_name,
    }


async def connect(url: SecretStr, schema: str, application_name: str) -> Connection:
    return await AsyncConnection.connect(
        url.get_secret_value(),
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=None,
        options=search_path_option(schema),
        application_name=application_name,
    )


def create_pool(
    url: SecretStr, schema: str, application_name: str, *, max_size: int, timeout: float
) -> Pool:
    """A closed pool; open it with ``await pool.open(wait=True)``."""
    return AsyncConnectionPool(
        url.get_secret_value(),
        connection_class=AsyncConnection[DictRow],
        kwargs=connection_kwargs(schema, application_name),
        min_size=1,
        max_size=max_size,
        timeout=timeout,
        open=False,
    )
