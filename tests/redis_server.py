"""A real redis-server process for integration tests.

Each server listens only on a Unix socket in a fresh short directory (socket paths
are limited to about 100 bytes) and keeps no data on disk, so tests are isolated
and a restart loses all state, as an unpersisted Redis would.
"""

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

STARTUP_TIMEOUT_SECONDS = 5
_STARTUP_POLL_SECONDS = 0.01


class RedisServer:
    def __init__(self, binary: str) -> None:
        self._binary = binary
        self._directory = Path(tempfile.mkdtemp(prefix="gw-redis-", dir="/tmp"))
        self.socket_path = self._directory / "redis.sock"
        self._process: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return f"unix://{self.socket_path}"

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self._binary,
            "--port", "0",
            "--unixsocket", str(self.socket_path),
            "--unixsocketperm", "700",
            "--save", "",
            "--appendonly", "no",
            "--dir", str(self._directory),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        client = Redis(unix_socket_path=str(self.socket_path))
        try:
            # There is no readiness signal from the child process, only the socket.
            for _ in range(int(STARTUP_TIMEOUT_SECONDS / _STARTUP_POLL_SECONDS)):
                if await _responds(client):
                    return
                await asyncio.sleep(_STARTUP_POLL_SECONDS)
        finally:
            await client.aclose()
        raise RuntimeError("redis-server did not start")

    async def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        await self._process.wait()
        self._process = None
        self.socket_path.unlink(missing_ok=True)

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def cleanup(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


async def _responds(client: Redis) -> bool:
    try:
        return bool(await client.ping())
    except RedisConnectionError:
        return False
