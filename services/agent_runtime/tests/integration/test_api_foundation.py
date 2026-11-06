"""Health, authentication, request IDs, and the error envelope."""

from fastapi import FastAPI

from agent_runtime.api.dependencies import PrincipalDep
from agent_runtime.main import build_app
from tests.fakes import AUTH_HEADERS, make_settings, serve


def _app_with_protected_route() -> FastAPI:
    app = build_app(make_settings())

    @app.get("/protected")
    async def protected(principal: PrincipalDep) -> dict[str, str]:
        return {"principal": principal.id}

    return app


async def test_health_endpoints_need_no_credentials() -> None:
    async with serve(build_app(make_settings())) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert ready.json() == {"status": "ready"}


async def test_shutdown_closes_the_gateway_client() -> None:
    app = build_app(make_settings())
    async with serve(app) as client:
        assert (await client.get("/health/ready")).status_code == 200
        gateway = app.state.gateway_client
        assert not gateway.is_closed

    assert app.state.ready is False
    assert gateway.is_closed


async def test_rejects_missing_and_invalid_credentials() -> None:
    async with serve(_app_with_protected_route()) as client:
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


async def test_reflects_valid_request_ids_and_replaces_others() -> None:
    async with serve(build_app(make_settings())) as client:
        kept = await client.get("/health/live", headers={"X-Request-ID": "client-id-1234"})
        replaced = await client.get("/health/live", headers={"X-Request-ID": "bad id\n"})

    assert kept.headers["x-request-id"] == "client-id-1234"
    assert replaced.headers["x-request-id"] != "bad id\n"
    assert len(replaced.headers["x-request-id"]) == 32


async def test_unknown_paths_use_the_error_envelope() -> None:
    async with serve(build_app(make_settings())) as client:
        response = await client.get("/nope", headers={"X-Request-ID": "client-id-1234"})

    assert response.status_code == 404
    assert response.json()["error"] == {
        "type": "invalid_request",
        "code": "not_found",
        "message": "Not Found",
        "param": None,
        "request_id": "client-id-1234",
    }
