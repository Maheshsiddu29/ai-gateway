import os
import shutil
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from gateway.core.config import AdmissionConfig
from gateway.core.redis import create_redis_client
from tests.redis_server import RedisServer


@pytest.fixture
async def redis_server() -> AsyncIterator[RedisServer]:
    binary = shutil.which("redis-server")
    if binary is None:
        # CI sets this so that missing Redis fails the build instead of silently
        # skipping the distributed-correctness tests.
        if os.environ.get("GATEWAY_REQUIRE_REDIS_TESTS") == "1":
            pytest.fail("redis-server is required but not installed")
        pytest.skip("redis-server is not installed")
    server = RedisServer(binary)
    try:
        await server.start()
        yield server
    finally:
        await server.stop()
        server.cleanup()


@pytest.fixture
async def redis_client(redis_server: RedisServer) -> AsyncIterator[Redis]:
    client = create_redis_client(SecretStr(redis_server.url), AdmissionConfig())
    try:
        yield client
    finally:
        await client.aclose(close_connection_pool=True)
