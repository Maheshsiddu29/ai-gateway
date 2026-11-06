"""Health, authentication, request IDs, and the error envelope."""

import pytest
from fastapi import FastAPI

from agent_runtime.api.dependencies import PrincipalDep
from agent_runtime.main import build_app
from agent_runtime.persistence import migrations
from agent_runtime.persistence.database import Connection, Pool
from tests.fakes import AUTH_HEADERS, make_settings, serve


def _app_with_protected_route(pool: Pool) -> FastAPI:
    app = build_app(make_settings(), pool)

    @app.get("/protected")
    async def protected(principal: PrincipalDep) -> dict[str, str]:
        return {"principal": principal.id}

    return app


async def test_health_endpoints_need_no_credentials(pool: Pool) -> None:
    async with serve(build_app(make_settings(), pool)) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert ready.json() == {"status": "ready"}


async def test_not_ready_after_shutdown(pool: Pool) -> None:
    app = build_app(make_settings(), pool)
    async with serve(app) as client:
        assert (await client.get("/health/ready")).status_code == 200

    assert app.state.store is None


async def test_not_ready_when_the_database_is_unreachable(pool: Pool) -> None:
    app = build_app(make_settings(), pool)
    async with serve(app) as client:
        await pool.close()
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}


async def test_refuses_to_start_on_an_unmigrated_schema(pool: Pool, owner: Connection) -> None:
    await owner.execute("DELETE FROM schema_migrations")

    with pytest.raises(migrations.SchemaError, match="missing migration"):
        async with serve(build_app(make_settings(), pool)):
            pass


async def test_rejects_missing_and_invalid_credentials(pool: Pool) -> None:
    async with serve(_app_with_protected_route(pool)) as client:
        missing = await client.get("/protected")
        wrong = await client.get("/protected", headers={"Authorization": "Bearer nope"})
        ok = await client.get("/protected", headers=AUTH_HEADERS)

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "missing_api_key"
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_api_key"
    assert ok.status_code == 200
    assert ok.json()["principal"].startswith("agent-key-")


async def test_reflects_valid_request_ids_and_replaces_others(pool: Pool) -> None:
    async with serve(build_app(make_settings(), pool)) as client:
        kept = await client.get("/health/live", headers={"X-Request-ID": "client-id-1234"})
        replaced = await client.get("/health/live", headers={"X-Request-ID": "bad id\n"})

    assert kept.headers["x-request-id"] == "client-id-1234"
    assert replaced.headers["x-request-id"] != "bad id\n"
    assert len(replaced.headers["x-request-id"]) == 32


async def test_unknown_paths_use_the_error_envelope(pool: Pool) -> None:
    async with serve(build_app(make_settings(), pool)) as client:
        response = await client.get("/nope", headers={"X-Request-ID": "client-id-1234"})

    assert response.status_code == 404
    assert response.json()["error"] == {
        "type": "invalid_request",
        "code": "not_found",
        "message": "Not Found",
        "param": None,
        "request_id": "client-id-1234",
    }
