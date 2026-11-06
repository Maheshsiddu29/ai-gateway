import pytest
from pydantic import ValidationError

from agent_runtime.config import Environment, Settings
from tests.fakes import AGENT_API_KEY, GATEWAY_API_KEY, make_settings

PRODUCTION = {
    "environment": Environment.PRODUCTION,
    "gateway_url": "https://gateway.example",
    "gateway_api_key": "g" * 32,
}


def test_reads_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_API_KEYS", f" {AGENT_API_KEY} , {'b' * 32},")
    monkeypatch.setenv("AGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("AGENT_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("AGENT_GATEWAY_API_KEY", "g" * 32)

    settings = Settings()

    assert [key.get_secret_value() for key in settings.api_keys] == [AGENT_API_KEY, "b" * 32]
    assert str(settings.gateway_url) == "https://gateway.example/"


def test_defaults_to_production() -> None:
    settings = Settings.model_validate({"api_keys": (AGENT_API_KEY,), **PRODUCTION})

    assert settings.environment is Environment.PRODUCTION


def test_requires_at_least_one_key() -> None:
    with pytest.raises(ValidationError):
        make_settings(api_keys=())


@pytest.mark.parametrize(
    "overrides", [{"api_keys": ("short",)}, {"gateway_api_key": "short"}], ids=["agent", "gateway"]
)
def test_production_rejects_short_keys(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make_settings(**{**PRODUCTION, **overrides})


def test_development_accepts_short_keys() -> None:
    assert make_settings(api_keys=("short",)).api_keys[0].get_secret_value() == "short"


def test_validation_errors_do_not_echo_secrets() -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(**PRODUCTION, api_keys=("leaky-short-key",))

    assert "leaky-short-key" not in str(caught.value)


def test_gateway_key_must_differ_from_agent_keys() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        make_settings(api_keys=(AGENT_API_KEY, GATEWAY_API_KEY))


def test_gateway_key_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        make_settings(gateway_api_key="")


def test_production_requires_https_unless_explicitly_allowed() -> None:
    with pytest.raises(ValidationError, match="https"):
        make_settings(**{**PRODUCTION, "gateway_url": "http://gateway:8000"})

    settings = make_settings(
        **{**PRODUCTION, "gateway_url": "http://gateway:8000", "gateway_allow_insecure_http": True}
    )
    assert settings.gateway_url.scheme == "http"


@pytest.mark.parametrize(
    "url",
    ["http://user:pass@gateway.test", "http://gateway.test/?a=1", "http://gateway.test/#x"],
)
def test_rejects_gateway_urls_with_credentials_query_or_fragment(url: str) -> None:
    with pytest.raises(ValidationError, match="gateway_url"):
        make_settings(gateway_url=url)


def test_gateway_key_is_not_in_repr() -> None:
    assert GATEWAY_API_KEY not in repr(make_settings())
