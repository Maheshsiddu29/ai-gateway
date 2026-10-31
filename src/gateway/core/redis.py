from pydantic import SecretStr
from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from gateway.core.config import AdmissionConfig


def create_redis_client(url: SecretStr, config: AdmissionConfig) -> Redis:
    """Create a Redis client with bounded waits and no automatic retries.

    redis-py retries failed commands by default. A retry after a lost reply can
    re-send a command that already ran, so the gateway disables it and handles
    failures itself (see admission.controller). A blocking pool bounds the number
    of connections; callers wait at most the pool timeout for one.
    """
    pool = BlockingConnectionPool.from_url(
        url.get_secret_value(),
        max_connections=config.redis_max_connections,
        timeout=config.redis_pool_timeout_seconds,
        socket_connect_timeout=config.redis_connect_timeout_seconds,
        socket_timeout=config.redis_operation_timeout_seconds,
        retry=Retry(NoBackoff(), retries=0),
        decode_responses=True,
    )
    return Redis(connection_pool=pool)
