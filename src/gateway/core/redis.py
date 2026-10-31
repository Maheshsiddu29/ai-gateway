from pathlib import Path

from pydantic import SecretStr
from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from gateway.core.config import AdmissionConfig, validate_redis_url


def create_redis_client(
    url: SecretStr, config: AdmissionConfig, ca_file: Path | None = None
) -> Redis:
    """Create a Redis client with bounded waits and no automatic retries.

    redis-py retries failed commands by default. A retry after a lost reply can
    re-send a command that already ran, so the gateway disables it and handles
    failures itself (see admission.controller). A blocking pool bounds the number
    of connections; callers wait at most the pool timeout for one.
    """
    validate_redis_url(url)
    tls: dict[str, object] = {}
    if url.get_secret_value().startswith("rediss://"):
        tls = {"ssl_cert_reqs": "required", "ssl_check_hostname": True}
        if ca_file is not None:
            tls["ssl_ca_certs"] = str(ca_file)
    pool = BlockingConnectionPool.from_url(
        url.get_secret_value(),
        max_connections=config.redis_max_connections,
        timeout=config.redis_pool_timeout_seconds,
        socket_connect_timeout=config.redis_connect_timeout_seconds,
        socket_timeout=config.redis_operation_timeout_seconds,
        retry=Retry(NoBackoff(), retries=0),
        decode_responses=True,
        **tls,
    )
    return Redis(connection_pool=pool)
