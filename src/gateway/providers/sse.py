"""Server-Sent Events parsing for upstream streams.

Implements the event-stream interpretation rules from the HTML specification for
the fields provider APIs use (``event`` and ``data``).

Lines are split here rather than with ``httpx.Response.aiter_lines``: that method
follows ``str.splitlines`` and also breaks on characters such as U+2028 and U+0085,
which may appear unescaped inside JSON strings. The SSE format only recognizes
CRLF, LF, and CR as line terminators.
"""

import codecs
import re
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class ServerSentEvent:
    event: str
    data: str


async def iter_sse(chunks: AsyncIterable[bytes]) -> AsyncGenerator[ServerSentEvent]:
    event_type = ""
    data_lines: list[str] = []
    async for line in _iter_lines(chunks):
        if not line:
            if data_lines:
                yield ServerSentEvent(event=event_type or "message", data="\n".join(data_lines))
            event_type = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_type = value
    # An event not terminated by a blank line is incomplete and, per the
    # specification, discarded.


async def _iter_lines(chunks: AsyncIterable[bytes]) -> AsyncGenerator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    at_start = True
    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        if at_start and buffer:
            buffer = buffer.removeprefix("﻿")
            at_start = False
        lines, buffer = _split_complete_lines(buffer)
        for line in lines:
            yield line
    buffer += decoder.decode(b"", final=True)
    if buffer.endswith("\r"):
        lines, _ = _split_complete_lines(buffer + "\n")
        for line in lines:
            yield line


def _split_complete_lines(buffer: str) -> tuple[list[str], str]:
    lines: list[str] = []
    start = 0
    for match in _LINE_BREAK.finditer(buffer):
        # A trailing CR may be the first half of a CRLF split across reads.
        if match.group() == "\r" and match.end() == len(buffer):
            break
        lines.append(buffer[start : match.start()])
        start = match.end()
    return lines, buffer[start:]
