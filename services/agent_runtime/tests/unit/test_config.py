import pytest
from pydantic import ValidationError

from agent_runtime.config import ApiSettings, Environment, WorkerSettings
from tests.fakes import AGENT_API_KEY, GATEWAY_API_KEY, make_settings, make_worker_settings

DATABASE = "postgresql://u:p@db/agent?sslmode=verify-full"
PRODUCTION_WORKER = {
    "environment": Environment.PRODUCTION,
    "gateway_url": "https://gateway.example",
    "gateway_api_key": "g" * 32,
    "database_url": DATABASE,
}


def test_reads_api_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_API_KEYS", f" {AGENT_API_KEY} , {'b' * 32},")
    monkeypatch.setenv("AGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("AGENT_DATABASE_URL", DATABASE)

    settings = ApiSettings()

    assert [key.get_secret_value() for key in settings.api_keys] == [AGENT_API_KEY, "b" * 32]
    assert settings.database_schema == "agent_runtime"


def test_defaults_to_production() -> None:
    settings = ApiSettings.model_validate({"api_keys": ("a" * 32,), "database_url": DATABASE})

    assert settings.environment is Environment.PRODUCTION


def test_the_api_holds_no_gateway_credentials() -> None:
    # The API never calls the gateway; only the worker has the gateway key.
    assert "gateway_api_key" not in ApiSettings.model_fields
    assert "api_keys" not in WorkerSettings.model_fields


def test_requires_at_least_one_key() -> None:
    with pytest.raises(ValidationError):
        make_settings(api_keys=())


def test_production_rejects_short_agent_keys() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make_settings(
            environment=Environment.PRODUCTION, database_url=DATABASE, api_keys=("short",)
        )


def test_production_rejects_a_short_gateway_key() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make_worker_settings(**{**PRODUCTION_WORKER, "gateway_api_key": "short"})


def test_development_accepts_short_keys() -> None:
    assert make_settings(api_keys=("short",)).api_keys[0].get_secret_value() == "short"


def test_validation_errors_do_not_echo_secrets() -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(
            environment=Environment.PRODUCTION, database_url=DATABASE, api_keys=("leaky-short-key",)
        )
    assert "leaky-short-key" not in str(caught.value)


def test_gateway_key_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        make_worker_settings(gateway_api_key="")


def test_production_requires_https_unless_explicitly_allowed() -> None:
    with pytest.raises(ValidationError, match="https"):
        make_worker_settings(**{**PRODUCTION_WORKER, "gateway_url": "http://gateway:8000"})

    settings = make_worker_settings(
        **{
            **PRODUCTION_WORKER,
            "gateway_url": "http://gateway:8000",
            "gateway_allow_insecure_http": True,
        }
    )
    assert settings.gateway_url.scheme == "http"


@pytest.mark.parametrize(
    "url",
    ["http://user:pass@gateway.test", "http://gateway.test/?a=1", "http://gateway.test/#x"],
)
def test_rejects_gateway_urls_with_credentials_query_or_fragment(url: str) -> None:
    with pytest.raises(ValidationError, match="gateway_url"):
        make_worker_settings(gateway_url=url)


def test_gateway_key_is_not_in_repr() -> None:
    assert GATEWAY_API_KEY not in repr(make_worker_settings())


def test_filesystem_paths_default_to_the_root_filesystem() -> None:
    assert make_worker_settings().filesystem_paths == ("/",)


def test_reads_filesystem_paths_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ENVIRONMENT", "development")
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://gateway:8000")
    monkeypatch.setenv("AGENT_GATEWAY_API_KEY", GATEWAY_API_KEY)
    monkeypatch.setenv("AGENT_PLANNER_MODEL", "fast")
    monkeypatch.setenv("AGENT_DIAGNOSIS_MODEL", "quality")
    monkeypatch.setenv("AGENT_DATABASE_URL", DATABASE)
    monkeypatch.setenv("AGENT_FILESYSTEM_PATHS", "/, /var/lib/postgresql")

    assert WorkerSettings().filesystem_paths == ("/", "/var/lib/postgresql")


@pytest.mark.parametrize("paths", [("relative",), ("/var/",), ("/a", "/a")])
def test_rejects_invalid_filesystem_paths(paths: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="filesystem paths"):
        make_worker_settings(filesystem_paths=paths)


def test_diagnostic_roots_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        make_worker_settings(procfs_root="proc")


def test_model_aliases_are_required() -> None:
    values = make_worker_settings().model_dump()
    del values["planner_model"]

    with pytest.raises(ValidationError, match="planner_model"):
        WorkerSettings.model_validate(values)


@pytest.mark.parametrize("alias", ["", "has space", "-leading", "a" * 65])
def test_rejects_invalid_model_aliases(alias: str) -> None:
    with pytest.raises(ValidationError):
        make_worker_settings(diagnosis_model=alias)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_llm_calls": 0},
        {"max_tool_calls": 17},
        {"max_output_tokens": 16_385},
        {"investigation_timeout_seconds": 0},
        {"max_active_investigations_per_principal": 0},
    ],
)
def test_rejects_api_limits_out_of_range(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


@pytest.mark.parametrize("overrides", [{"diagnostic_workers": 0}, {"diagnostic_workers": 17}])
def test_rejects_worker_limits_out_of_range(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_worker_settings(**overrides)
