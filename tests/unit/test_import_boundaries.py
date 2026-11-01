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
