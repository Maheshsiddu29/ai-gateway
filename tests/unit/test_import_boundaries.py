import asyncio
import json
import sys


async def test_the_gateway_never_loads_evaluation_execution_code() -> None:
    """Serving requests must not be able to start an evaluation, which can spend money.

    The gateway reads evaluation results through gateway.routing.evidence; the
    runner, live-run plan, CLI, simulated provider, and journal stay unloaded.
    """
    code = (
        "import json, sys, gateway.main; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('gateway.evaluation'))))"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)

    assert process.returncode == 0
    assert set(json.loads(stdout)) == {
        "gateway.evaluation",
        "gateway.evaluation.dataset",
        "gateway.evaluation.scoring",
        "gateway.evaluation.stats",
    }


async def _loaded_modules(entry_point: str, prefix: str) -> set[str]:
    code = (
        f"import json, sys, {entry_point}; "
        f"print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == {prefix!r})))"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode == 0
    return set(json.loads(stdout))


async def test_the_gateway_and_the_agent_runtime_do_not_import_each_other() -> None:
    """They communicate only over HTTP, so either can be deployed and changed alone."""
    assert await _loaded_modules("gateway.main", "agent_runtime") == set()
    assert await _loaded_modules("agent_runtime.main", "gateway") == set()
