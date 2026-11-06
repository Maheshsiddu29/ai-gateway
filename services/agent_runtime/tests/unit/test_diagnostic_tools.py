import asyncio

import pytest

from agent_runtime.diagnostics.tools import (
    MAX_ALLOWED_PATHS,
    DiagnosticExecutor,
    DiagnosticResult,
    DiagnosticStatus,
    ToolName,
    ToolRequest,
    ToolRequestError,
    parse_tool_request,
    validate_allowed_paths,
)

ALLOWED = frozenset({"/", "/var/lib/data"})


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("memory_info", {}, ToolRequest(ToolName.MEMORY_INFO)),
        ("cgroup_memory", {}, ToolRequest(ToolName.CGROUP_MEMORY)),
        ("filesystem_capacity", {"path": "/"}, ToolRequest(ToolName.FILESYSTEM_CAPACITY, "/")),
        (
            "filesystem_inodes",
            {"path": "/var/lib/data"},
            ToolRequest(ToolName.FILESYSTEM_INODES, "/var/lib/data"),
        ),
    ],
)
def test_accepts_permitted_requests(
    tool: str, arguments: dict[str, object], expected: ToolRequest
) -> None:
    assert parse_tool_request(tool, arguments, ALLOWED) == expected


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [
        ("shell", {"command": "rm -rf /"}, "unknown_tool"),
        ("MEMORY_INFO", {}, "unknown_tool"),
        ("memory_info ", {}, "unknown_tool"),
        ("memory_info", {"verbose": True}, "invalid_tool_arguments"),
        ("filesystem_capacity", {}, "invalid_tool_arguments"),
        ("filesystem_capacity", {"path": 1}, "invalid_tool_arguments"),
        ("filesystem_capacity", {"path": "/", "follow": True}, "invalid_tool_arguments"),
        ("filesystem_capacity", {"path": "/etc"}, "path_not_allowed"),
        ("filesystem_capacity", {"path": "/var/lib/data/"}, "path_not_allowed"),
        ("filesystem_capacity", {"path": "/var/lib/data/../../etc"}, "path_not_allowed"),
        ("filesystem_inodes", {"path": "/; cat /etc/shadow"}, "path_not_allowed"),
    ],
)
def test_rejects_requests_that_are_not_permitted(
    tool: str, arguments: dict[str, object], code: str
) -> None:
    with pytest.raises(ToolRequestError) as caught:
        parse_tool_request(tool, arguments, ALLOWED)

    assert caught.value.code == code
    assert "rm -rf" not in caught.value.message


@pytest.mark.parametrize(
    "paths",
    [
        (),
        ("relative",),
        ("/var/",),
        ("/var/../etc",),
        ("/var/./lib",),
        ("//var",),
        ("/", "/"),
        tuple(f"/p{i}" for i in range(MAX_ALLOWED_PATHS + 1)),
    ],
)
def test_rejects_invalid_allowed_paths(paths: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="filesystem paths"):
        validate_allowed_paths(paths)


class StubBackend:
    def __init__(self, result: DiagnosticResult, delay: float = 0) -> None:
        self.result = result
        self.delay = delay
        self.requests: list[ToolRequest] = []

    async def run(self, request: ToolRequest) -> DiagnosticResult:
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        return self.result


async def test_executor_returns_the_backend_result() -> None:
    backend = StubBackend(DiagnosticResult.ok({"usage_bytes": 1}))
    executor = DiagnosticExecutor(backend, timeout_seconds=1, max_output_bytes=1024)

    executed = await executor.run(ToolRequest(ToolName.CGROUP_MEMORY))

    assert executed.result == DiagnosticResult.ok({"usage_bytes": 1})
    assert executed.request == ToolRequest(ToolName.CGROUP_MEMORY)
    assert executed.duration_ms >= 0


async def test_executor_times_out_slow_reads() -> None:
    backend = StubBackend(DiagnosticResult.ok({}), delay=10)
    executor = DiagnosticExecutor(backend, timeout_seconds=0.01, max_output_bytes=1024)

    executed = await executor.run(ToolRequest(ToolName.MEMORY_INFO))

    assert executed.result == DiagnosticResult.failed("timeout")


async def test_executor_bounds_output_size() -> None:
    data = {f"field_{i}": i for i in range(200)}
    executor = DiagnosticExecutor(
        StubBackend(DiagnosticResult.ok(data)), timeout_seconds=1, max_output_bytes=1024
    )

    executed = await executor.run(ToolRequest(ToolName.MEMORY_INFO))

    assert executed.result.status is DiagnosticStatus.FAILED
    assert executed.result.reason == "output_too_large"
    assert executed.result.data == {}
