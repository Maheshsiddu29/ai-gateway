import asyncio
import shutil
import socket
from pathlib import Path

import pytest
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError

from gateway.core.config import AdmissionConfig
from gateway.core.redis import create_redis_client
from tests.redis_server import RedisServer


async def test_redis_tls_verifies_certificate_and_hostname(tmp_path: Path) -> None:
    binary = shutil.which("redis-server")
    openssl = shutil.which("openssl")
    assert binary is not None
    assert openssl is not None
    certificate = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    process = await asyncio.create_subprocess_exec(
        openssl,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost",
        "-keyout",
        str(key),
        "-out",
        str(certificate),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await process.wait() == 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = RedisServer(
        binary,
        (
            "--tls-port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--tls-auth-clients",
            "no",
            "--tls-cert-file",
            str(certificate),
            "--tls-key-file",
            str(key),
        ),
    )
    try:
        await server.start()
        for host, ca, succeeds in [
            ("localhost", certificate, True),
            ("127.0.0.1", certificate, False),
            ("localhost", None, False),
        ]:
            client = create_redis_client(
                SecretStr(f"rediss://{host}:{port}"), AdmissionConfig(), ca
            )
            try:
                if succeeds:
                    assert await client.ping()
                else:
                    with pytest.raises(RedisConnectionError, match="CERTIFICATE_VERIFY_FAILED"):
                        await client.ping()
            finally:
                await client.aclose(close_connection_pool=True)
    finally:
        await server.stop()
        server.cleanup()
