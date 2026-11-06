import pytest
from pydantic import ValidationError

from agent_runtime.config import WorkerSettings
from agent_runtime.worker import StartupError, check_tracing_disabled, default_worker_id

WORKER = {
    "environment": "development",
    "gateway_url": "http://gateway:8000",
    "gateway_api_key": "gateway-key",
    "planner_model": "agent-planner",
    "diagnosis_model": "agent-diagnosis",
    "database_url": "postgresql://agent_app:pw@agent-postgres/agent",
}


def test_the_worker_needs_no_agent_api_keys() -> None:
    settings = WorkerSettings.model_validate(WORKER)

    assert not hasattr(settings, "api_keys")
    assert settings.worker_concurrency == 2
    assert settings.database_schema == "agent_runtime"


@pytest.mark.parametrize("missing", ["database_url", "gateway_api_key", "planner_model"])
def test_required_worker_settings(missing: str) -> None:
    values = {key: value for key, value in WORKER.items() if key != missing}

    with pytest.raises(ValidationError, match=missing):
        WorkerSettings.model_validate(values)


def test_production_workers_require_tls_and_long_keys() -> None:
    production = {**WORKER, "environment": "production", "gateway_url": "https://gw.example"}

    with pytest.raises(ValidationError, match=r"sslmode|32 characters"):
        WorkerSettings.model_validate(production)
    WorkerSettings.model_validate(
        {
            **production,
            "gateway_api_key": "g" * 32,
            "database_url": "postgresql://u:p@db/agent?sslmode=verify-full",
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"lease_seconds": 1},
        {"worker_concurrency": 0},
        {"max_recovery_attempts": 0},
        {"worker_id": "a b"},
    ],
)
def test_rejects_unsafe_worker_settings(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings.model_validate({**WORKER, **overrides})


@pytest.mark.parametrize(
    "name",
    ["LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING"],
)
def test_refuses_to_start_with_langsmith_tracing(name: str) -> None:
    with pytest.raises(StartupError, match=name):
        check_tracing_disabled({name: "true"})
    check_tracing_disabled({name: "false"})


def test_default_worker_ids_are_valid_and_unique() -> None:
    first, second = default_worker_id(), default_worker_id()

    assert first != second
    WorkerSettings.model_validate({**WORKER, "worker_id": first})
