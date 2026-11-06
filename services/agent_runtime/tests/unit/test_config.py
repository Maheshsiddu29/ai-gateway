import pytest
from pydantic import ValidationError

from agent_runtime.config import Environment, Settings
from tests.fakes import AGENT_API_KEY, make_settings


def test_reads_comma_separated_keys_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_API_KEYS", f" {AGENT_API_KEY} , {'b' * 32},")
    monkeypatch.setenv("AGENT_ENVIRONMENT", "production")

    settings = Settings()

    assert [key.get_secret_value() for key in settings.api_keys] == [AGENT_API_KEY, "b" * 32]


def test_defaults_to_production() -> None:
    settings = Settings.model_validate({"api_keys": (AGENT_API_KEY,)})

    assert settings.environment is Environment.PRODUCTION


def test_requires_at_least_one_key() -> None:
    with pytest.raises(ValidationError):
        make_settings(api_keys=())


def test_production_rejects_short_keys() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make_settings(environment=Environment.PRODUCTION, api_keys=("short",))


def test_development_accepts_short_keys() -> None:
    assert make_settings(api_keys=("short",)).api_keys[0].get_secret_value() == "short"


def test_validation_errors_do_not_echo_secrets() -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(environment=Environment.PRODUCTION, api_keys=("leaky-short-key",))

    assert "leaky-short-key" not in str(caught.value)
