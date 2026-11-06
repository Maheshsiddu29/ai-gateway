"""The diagnostic tool catalog, request validation, and bounded execution.

Tools are fixed, read-only operations with typed arguments. A model can only name
a tool from the catalog and supply arguments that pass its schema, and a path
argument must exactly match a path the operator allowed. Nothing a model writes
is executed or interpreted as a command.

``DiagnosticBackend`` is the boundary to whatever performs the reads. The local
backend reads the host the runtime runs on; a remote, authenticated diagnostic
service can implement the same protocol later.
"""

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_ALLOWED_PATHS = 16
MAX_PATH_LENGTH = 256

type DataValue = int | float | bool | None


class ToolName(StrEnum):
    MEMORY_INFO = "memory_info"
    CGROUP_MEMORY = "cgroup_memory"
    FILESYSTEM_CAPACITY = "filesystem_capacity"
    FILESYSTEM_INODES = "filesystem_inodes"


class DiagnosticStatus(StrEnum):
    OK = "ok"
    # The environment does not provide this information, e.g. no procfs on macOS.
    UNAVAILABLE = "unavailable"
    # The information should exist but could not be read or understood.
    FAILED = "failed"


@dataclass(frozen=True)
class DiagnosticResult:
    """``data`` holds only numbers and flags under fixed keys, never raw host text,
    so tool output cannot carry instructions to the model. ``reason`` is a fixed
    code explaining an unavailable or failed result."""

    status: DiagnosticStatus
    data: Mapping[str, DataValue] = field(default_factory=dict)
    reason: str | None = None

    @classmethod
    def ok(cls, data: Mapping[str, DataValue]) -> "DiagnosticResult":
        return cls(DiagnosticStatus.OK, data)

    @classmethod
    def unavailable(cls, reason: str) -> "DiagnosticResult":
        return cls(DiagnosticStatus.UNAVAILABLE, reason=reason)

    @classmethod
    def failed(cls, reason: str) -> "DiagnosticResult":
        return cls(DiagnosticStatus.FAILED, reason=reason)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NoArguments(_Arguments):
    pass


class PathArguments(_Arguments):
    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    description: str
    arguments: type[_Arguments]


TOOLS: Mapping[ToolName, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            ToolName.MEMORY_INFO,
            "System-wide memory and swap totals from /proc/meminfo, in bytes.",
            NoArguments,
        ),
        ToolSpec(
            ToolName.CGROUP_MEMORY,
            "Memory usage, limit, peak, and OOM event counts of the runtime's own cgroup.",
            NoArguments,
        ),
        ToolSpec(
            ToolName.FILESYSTEM_CAPACITY,
            "Size, free, and available bytes of the filesystem containing an allowed path.",
            PathArguments,
        ),
        ToolSpec(
            ToolName.FILESYSTEM_INODES,
            "Total, free, and available inodes of the filesystem containing an allowed path.",
            PathArguments,
        ),
    )
}


@dataclass(frozen=True)
class ToolRequest:
    tool: ToolName
    path: str | None = None

    @property
    def arguments(self) -> dict[str, str]:
        return {} if self.path is None else {"path": self.path}


class ToolRequestError(Exception):
    """A tool request that is not permitted. ``code`` is stable; the message never
    repeats the request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_allowed_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Check operator-configured paths: absolute, normalized, unique, and few."""
    if not 1 <= len(paths) <= MAX_ALLOWED_PATHS:
        raise ValueError(f"between 1 and {MAX_ALLOWED_PATHS} filesystem paths are required")
    if len(set(paths)) != len(paths):
        raise ValueError("filesystem paths must be unique")
    for path in paths:
        pure = PurePosixPath(path)
        if (
            len(path) > MAX_PATH_LENGTH
            or not pure.is_absolute()
            or path.startswith("//")
            or pure.as_posix() != path
            or ".." in pure.parts
        ):
            raise ValueError("filesystem paths must be absolute and normalized")
    return paths


def parse_tool_request(
    tool: str, arguments: Mapping[str, object], allowed_paths: frozenset[str]
) -> ToolRequest:
    """Turn an untrusted tool name and arguments into a permitted ``ToolRequest``."""
    try:
        name = ToolName(tool)
    except ValueError:
        raise ToolRequestError(
            "unknown_tool", "The plan names a tool that does not exist."
        ) from None
    spec = TOOLS[name]
    try:
        parsed = spec.arguments.model_validate(dict(arguments))
    except ValidationError:
        raise ToolRequestError(
            "invalid_tool_arguments", f"The arguments for {name} do not match its schema."
        ) from None
    if isinstance(parsed, PathArguments):
        if parsed.path not in allowed_paths:
            raise ToolRequestError(
                "path_not_allowed", f"The path for {name} is not in the allowed list."
            )
        return ToolRequest(name, parsed.path)
    return ToolRequest(name)


class DiagnosticBackend(Protocol):
    async def run(self, request: ToolRequest) -> DiagnosticResult: ...


@dataclass(frozen=True)
class ExecutedTool:
    request: ToolRequest
    result: DiagnosticResult
    duration_ms: float


class DiagnosticExecutor:
    """Runs one permitted request at a time with a timeout and an output size limit.

    A timeout abandons the read: the backend's worker thread cannot be interrupted
    and finishes in the background, but the investigation does not wait for it.
    """

    def __init__(
        self, backend: DiagnosticBackend, *, timeout_seconds: float, max_output_bytes: int
    ) -> None:
        self._backend = backend
        self._timeout = timeout_seconds
        self._max_output_bytes = max_output_bytes

    async def run(self, request: ToolRequest) -> ExecutedTool:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                result = await self._backend.run(request)
        except TimeoutError:
            result = DiagnosticResult.failed("timeout")
        if len(json.dumps(result.data)) > self._max_output_bytes:
            result = DiagnosticResult.failed("output_too_large")
        return ExecutedTool(request, result, round((time.perf_counter() - started) * 1000, 2))
