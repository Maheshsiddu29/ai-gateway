"""Read-only diagnostics of the host, or container, the runtime runs on.

Each tool reads a fixed set of files under the configured procfs and cgroup roots,
or calls ``os.statvfs`` on an allowed path. Nothing is executed. Reads happen in
worker threads because file I/O blocks, and each file read is capped in size.

Missing procfs or cgroup files mean the information is unavailable, as on macOS
or without a memory controller, and are reported as such rather than as failures.
"""

import asyncio
import os
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from agent_runtime.diagnostics.tools import DataValue, DiagnosticResult, ToolName, ToolRequest

_MAX_FILE_BYTES = 65_536
# cgroup v1 reports "no limit" as a page-aligned value near 2**63.
_V1_UNLIMITED = 2**62
_MEMINFO_FIELDS = {
    "MemTotal": "mem_total_bytes",
    "MemFree": "mem_free_bytes",
    "MemAvailable": "mem_available_bytes",
    "Buffers": "buffers_bytes",
    "Cached": "cached_bytes",
    "Shmem": "shmem_bytes",
    "SwapTotal": "swap_total_bytes",
    "SwapFree": "swap_free_bytes",
}
_REQUIRED_MEMINFO_FIELDS = ("MemTotal", "MemFree")


class _ParseError(Exception):
    """A file existed but did not have the expected format."""


class _UnavailableError(Exception):
    """The environment does not provide the information."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LocalDiagnostics:
    def __init__(self, procfs_root: Path, cgroup_root: Path) -> None:
        self._procfs = procfs_root
        self._cgroup = cgroup_root

    async def run(self, request: ToolRequest) -> DiagnosticResult:
        operations: Mapping[ToolName, Callable[[], Mapping[str, DataValue]]] = {
            ToolName.MEMORY_INFO: self._memory_info,
            ToolName.CGROUP_MEMORY: self._cgroup_memory,
            ToolName.FILESYSTEM_CAPACITY: lambda: _filesystem_capacity(_path(request)),
            ToolName.FILESYSTEM_INODES: lambda: _filesystem_inodes(_path(request)),
        }
        return await asyncio.to_thread(_collect, operations[request.tool])

    def _memory_info(self) -> Mapping[str, DataValue]:
        text = _read_or_unavailable(self._procfs / "meminfo", "procfs_unavailable")
        values: dict[str, int] = {}
        for line in text.splitlines():
            key, sep, rest = line.partition(":")
            if sep and key in _MEMINFO_FIELDS:
                values[key] = _kibibytes(rest)
        if any(key not in values for key in _REQUIRED_MEMINFO_FIELDS):
            raise _ParseError
        return {name: values.get(key) for key, name in _MEMINFO_FIELDS.items()}

    def _cgroup_memory(self) -> Mapping[str, DataValue]:
        membership = _read_or_unavailable(self._procfs / "self" / "cgroup", "procfs_unavailable")
        unified: str | None = None
        v1_memory: str | None = None
        for line in membership.splitlines():
            if not line:
                continue
            hierarchy, controllers, path = _cgroup_line(line)
            if hierarchy == "0" and controllers == "":
                unified = path
            elif "memory" in controllers.split(","):
                v1_memory = path
        if v1_memory is not None:
            return self._cgroup_v1(v1_memory)
        if unified is not None and (self._cgroup / "cgroup.controllers").is_file():
            return self._cgroup_v2(unified)
        raise _UnavailableError("cgroup_memory_unavailable")

    def _cgroup_v2(self, path: str) -> Mapping[str, DataValue]:
        directory = self._cgroup.joinpath(*PurePosixPath(path).parts[1:])
        # The root cgroup has no memory.current, and without the memory controller
        # enabled for this cgroup neither does any other.
        if not (directory / "memory.current").is_file():
            raise _UnavailableError("cgroup_memory_unavailable")
        events = _key_values(_read(directory / "memory.events"))
        limit = _read(directory / "memory.max").strip()
        return {
            "cgroup_version": 2,
            "usage_bytes": _integer(_read(directory / "memory.current")),
            "limit_bytes": None if limit == "max" else _integer(limit),
            "peak_bytes": _optional_integer(directory / "memory.peak"),
            "max_events": events.get("max"),
            "oom_events": events.get("oom"),
            "oom_kill_events": events.get("oom_kill"),
        }

    def _cgroup_v1(self, path: str) -> Mapping[str, DataValue]:
        root = self._cgroup / "memory"
        directory = root.joinpath(*PurePosixPath(path).parts[1:])
        if not (directory / "memory.usage_in_bytes").is_file():
            # Inside a container the hierarchy is usually mounted at the container's
            # own cgroup, while /proc/self/cgroup shows the host path.
            directory = root
        if not (directory / "memory.usage_in_bytes").is_file():
            raise _UnavailableError("cgroup_memory_unavailable")
        limit = _integer(_read(directory / "memory.limit_in_bytes"))
        oom_control = _key_values(_read(directory / "memory.oom_control"))
        return {
            "cgroup_version": 1,
            "usage_bytes": _integer(_read(directory / "memory.usage_in_bytes")),
            "limit_bytes": None if limit >= _V1_UNLIMITED else limit,
            "peak_bytes": _optional_integer(directory / "memory.max_usage_in_bytes"),
            "max_events": _optional_integer(directory / "memory.failcnt"),
            "oom_events": None,
            "oom_kill_events": oom_control.get("oom_kill"),
        }


def _collect(operation: Callable[[], Mapping[str, DataValue]]) -> DiagnosticResult:
    try:
        return DiagnosticResult.ok(operation())
    except _UnavailableError as exc:
        return DiagnosticResult.unavailable(exc.reason)
    except _ParseError:
        return DiagnosticResult.failed("unrecognized_format")
    except FileNotFoundError:
        return DiagnosticResult.failed("file_not_found")
    except PermissionError:
        return DiagnosticResult.failed("permission_denied")
    except OSError:
        return DiagnosticResult.failed("read_failed")


def _path(request: ToolRequest) -> str:
    if request.path is None:
        raise ValueError(f"{request.tool} requires a path")
    return request.path


def _statvfs(path: str) -> os.statvfs_result:
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        raise _UnavailableError("statvfs_unavailable")
    result: os.statvfs_result = statvfs(path)
    return result


def _filesystem_capacity(path: str) -> Mapping[str, DataValue]:
    stat = _statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bfree * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    used = total - free
    return {
        "total_bytes": total,
        "free_bytes": free,
        "available_bytes": available,
        "used_bytes": used,
        # As df reports it: blocks reserved for root count as neither used nor available.
        "used_percent": _percent(used, used + available),
        "read_only": bool(stat.f_flag & os.ST_RDONLY),
    }


def _filesystem_inodes(path: str) -> Mapping[str, DataValue]:
    stat = _statvfs(path)
    if stat.f_files == 0:
        # Filesystems that allocate inodes dynamically report no inode counts.
        raise _UnavailableError("inodes_not_reported")
    used = stat.f_files - stat.f_ffree
    return {
        "total_inodes": stat.f_files,
        "free_inodes": stat.f_ffree,
        "available_inodes": stat.f_favail,
        "used_inodes": used,
        "used_percent": _percent(used, used + stat.f_favail),
    }


def _percent(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole > 0 else None


def _read(path: Path) -> str:
    with path.open("rb") as file:
        content = file.read(_MAX_FILE_BYTES + 1)
    if len(content) > _MAX_FILE_BYTES:
        raise _ParseError
    try:
        return content.decode("ascii")
    except UnicodeDecodeError:
        raise _ParseError from None


def _read_or_unavailable(path: Path, reason: str) -> str:
    try:
        return _read(path)
    except FileNotFoundError:
        raise _UnavailableError(reason) from None


def _optional_integer(path: Path) -> int | None:
    try:
        return _integer(_read(path))
    except FileNotFoundError:
        return None


def _integer(text: str) -> int:
    value = text.strip()
    if not (value.isascii() and value.isdigit()) or len(value) > 20:
        raise _ParseError
    return int(value)


def _kibibytes(text: str) -> int:
    number, _, unit = text.strip().partition(" ")
    if unit.strip() != "kB":
        raise _ParseError
    return _integer(number) * 1024


def _key_values(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key:
            values[key] = _integer(value)
    return values


def _cgroup_line(line: str) -> tuple[str, str, str]:
    hierarchy, sep1, rest = line.partition(":")
    controllers, sep2, path = rest.partition(":")
    pure = PurePosixPath(path)
    if not (sep1 and sep2) or not pure.is_absolute() or ".." in pure.parts or len(path) > 4096:
        raise _ParseError
    return hierarchy, controllers, path
