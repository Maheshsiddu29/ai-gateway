"""A TCP proxy in front of a Redis Unix socket that injects connection failures.

When ``drop_script_replies`` is set, a script call (EVAL or EVALSHA) is forwarded to
Redis, which executes it, but the client connection is closed instead of returning
the reply. This reproduces a command that committed while its reply was lost.

``break_open_connections`` makes every currently open connection fail on its next
command without forwarding it, as a pooled connection does when the server went
away without the client noticing.
"""

import asyncio
import contextlib
from pathlib import Path

_SCRIPT_COMMANDS = (b"EVALSHA", b"EVAL")
_READ_SIZE = 65_536


class ReplyDroppingProxy:
    def __init__(self, redis_socket: Path) -> None:
        self._redis_socket = redis_socket
        self._server: asyncio.Server | None = None
        self.drop_script_replies = False
        self.dropped_replies = 0
        self._generation = 0
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"redis://{host}:{port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # wait_closed waits for open connections, which a failed test may leave.
            for writer in self._writers:
                writer.close()
            await self._server.wait_closed()

    def break_open_connections(self) -> None:
        self._generation += 1

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        redis_reader, redis_writer = await asyncio.open_unix_connection(str(self._redis_socket))
        self._writers.update((client_writer, redis_writer))
        script_pending = asyncio.Event()
        generation = self._generation

        async def client_to_redis() -> None:
            while data := await client_reader.read(_READ_SIZE):
                if generation != self._generation:
                    return
                if any(command in data.upper() for command in _SCRIPT_COMMANDS):
                    script_pending.set()
                redis_writer.write(data)
                await redis_writer.drain()

        async def redis_to_client() -> None:
            while data := await redis_reader.read(_READ_SIZE):
                if self.drop_script_replies and script_pending.is_set():
                    self.dropped_replies += 1
                    return
                script_pending.clear()
                client_writer.write(data)
                await client_writer.drain()

        tasks = [asyncio.create_task(client_to_redis()), asyncio.create_task(redis_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for writer in (client_writer, redis_writer):
                self._writers.discard(writer)
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
