"""Shared test doubles and settings."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from agent_runtime.config import Environment, Settings

AGENT_API_KEY = "test-agent-key-0123456789abcdef0123"
AUTH_HEADERS = {"Authorization": f"Bearer {AGENT_API_KEY}"}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_keys": (AGENT_API_KEY,),
        "environment": Environment.DEVELOPMENT,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield an in-process client for it."""
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client,
    ):
        yield client
