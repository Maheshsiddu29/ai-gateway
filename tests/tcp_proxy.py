"""A TCP proxy that can be stopped to simulate a dependency becoming unreachable."""

import asyncio
import contextlib

_READ_SIZE = 65_536


class TcpProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        assert self._server is not None
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        """Refuse new connections and drop open ones."""
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                writer.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target_reader, target_writer = await asyncio.open_connection(*self._target)
        self._writers.update((writer, target_writer))

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            while data := await source.read(_READ_SIZE):
                sink.write(data)
                await sink.drain()

        tasks = [
            asyncio.create_task(pipe(reader, target_writer)),
            asyncio.create_task(pipe(target_reader, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for stream_writer in (writer, target_writer):
                self._writers.discard(stream_writer)
                stream_writer.close()
                with contextlib.suppress(OSError):
                    await stream_writer.wait_closed()
