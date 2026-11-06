"""Entry point of the durable worker: ``python -m agent_runtime.worker``.

Reads WorkerSettings from AGENT_* variables, refuses to start unless the database
schema is exactly at this runtime's version, and runs until SIGTERM or SIGINT,
then drains (see agent_runtime.durable.worker).
"""

import asyncio
import os
import re
import signal
import socket
import sys
import uuid
from collections.abc import Mapping

from agent_runtime.config import WorkerSettings
from agent_runtime.diagnostics.local import LocalDiagnostics
from agent_runtime.durable.worker import Worker, WorkerConfig
from agent_runtime.factories import build_diagnostic_executor, build_gateway_client
from agent_runtime.investigation.graph import ModelAliases
from agent_runtime.logging import configure_logging
from agent_runtime.persistence import migrations
from agent_runtime.persistence.checkpoints import checkpointer
from agent_runtime.persistence.database import create_pool
from agent_runtime.persistence.store import InvestigationStore

# LangSmith, a LangGraph dependency, exports run inputs, including prompts that
# carry incident text, when any of these is enabled.
TRACING_VARIABLES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)


class StartupError(Exception):
    pass


def check_tracing_disabled(environ: Mapping[str, str]) -> None:
    enabled = [
        name for name in TRACING_VARIABLES if environ.get(name, "").lower() in {"1", "true", "yes"}
    ]
    if enabled:
        raise StartupError(
            f"{', '.join(enabled)} would export prompts with incident text; unset it"
        )


def default_worker_id() -> str:
    host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())[:64] or "worker"
    return f"{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


async def serve(settings: WorkerSettings) -> None:
    check_tracing_disabled(os.environ)
    pool = create_pool(
        settings.database_url,
        settings.database_schema,
        "agent-runtime-worker",
        max_size=settings.database_pool_size,
        timeout=settings.database_timeout_seconds,
    )
    await pool.open(wait=True, timeout=settings.database_timeout_seconds)
    local = LocalDiagnostics(
        settings.procfs_root, settings.cgroup_root, workers=settings.diagnostic_workers
    )
    gateway = build_gateway_client(settings)
    try:
        async with pool.connection() as conn:
            await migrations.verify(conn)
        worker = Worker(
            InvestigationStore(pool),
            checkpointer(pool),
            gateway,
            build_diagnostic_executor(settings, local),
            WorkerConfig(
                worker_id=settings.worker_id or default_worker_id(),
                concurrency=settings.worker_concurrency,
                max_running=settings.max_running_investigations,
                lease_seconds=settings.lease_seconds,
                poll_seconds=settings.poll_seconds,
                shutdown_grace_seconds=settings.shutdown_grace_seconds,
                max_recovery_attempts=settings.max_recovery_attempts,
                models=ModelAliases(settings.planner_model, settings.diagnosis_model),
                allowed_paths=frozenset(settings.filesystem_paths),
            ),
        )
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, worker.request_shutdown)
        await worker.run()
    finally:
        await gateway.aclose()
        local.close()
        await pool.close()


def main() -> None:
    settings = WorkerSettings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(serve(settings))
    except (StartupError, migrations.SchemaError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
