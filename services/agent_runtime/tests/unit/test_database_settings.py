import pytest
from pydantic import SecretStr, ValidationError

from agent_runtime.config import MigrationSettings
from agent_runtime.persistence.database import connection_kwargs, validate_database_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db:5432/agent",
        "postgres://u:p@db/agent?sslmode=verify-full",
        "postgresql://u@/agent?host=/var/run/postgresql",
    ],
)
def test_accepts_postgresql_urls(url: str) -> None:
    validate_database_url(SecretStr(url), require_tls=False)


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@db/agent",
        "postgresql+asyncpg://u:p@db/agent",
        "postgresql://u:p@db",
        "postgresql://u:p@db:99999/agent",
        "postgresql://u:p@db/agent?options=-c%20search_path%3Dpublic",
        "postgresql://u:p@db/agent#x",
    ],
)
def test_rejects_other_urls(url: str) -> None:
    with pytest.raises(ValueError, match="database URL"):
        validate_database_url(SecretStr(url), require_tls=False)


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_tls_requirement_rejects_unencrypted_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="sslmode"):
        validate_database_url(SecretStr(f"postgresql://u:p@db/a?sslmode={mode}"), require_tls=True)
    with pytest.raises(ValueError, match="sslmode"):
        validate_database_url(SecretStr("postgresql://u:p@db/a"), require_tls=True)


def test_production_migration_settings_require_tls() -> None:
    with pytest.raises(ValidationError, match="sslmode"):
        MigrationSettings.model_validate({"database_owner_url": "postgresql://u:p@db/a"})
    settings = MigrationSettings.model_validate(
        {"database_owner_url": "postgresql://u:p@db/a", "database_allow_insecure": True}
    )
    assert settings.database_schema == "agent_runtime"


def test_errors_do_not_echo_the_password() -> None:
    with pytest.raises(ValidationError) as caught:
        MigrationSettings.model_validate({"database_owner_url": "mysql://u:hunter2@db/a"})
    assert "hunter2" not in str(caught.value)


@pytest.mark.parametrize("schema", ["Public", "a-b", "x; DROP TABLE y", ""])
def test_rejects_unsafe_schema_names(schema: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        connection_kwargs(schema, "test")
