from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.core.config import Environment, Settings, load_config
from gateway.errors import ConfigurationError

LONG_KEY = "k" * 32

VALID_CONFIG = """
[providers.openai]
type = "openai_compatible"
base_url = "https://llm.example.com/v1"
api_key_env = "OPENAI_API_KEY"

[providers.anthropic]
type = "anthropic"
base_url = "https://anthropic.example.com"
api_key_env = "ANTHROPIC_API_KEY"

[models.fast]
provider = "openai"
model = "upstream-fast"

[models.quality]
provider = "anthropic"
model = "upstream-quality"
default_max_tokens = 1024
"""

PROVIDER_KEYS = {"OPENAI_API_KEY": "openai-secret", "ANTHROPIC_API_KEY": "anthropic-secret"}


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "gateway.toml"
    path.write_text(content)
    return path


def make_settings(config_file: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_keys": (LONG_KEY,),
        "config_file": config_file,
        "environment": Environment.PRODUCTION,
    }
    values.update(overrides)
    return Settings.model_validate(values)


class TestSettings:
    def test_api_keys_are_split_from_comma_separated_environment_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GATEWAY_API_KEYS", f" {LONG_KEY} ,, {'x' * 40}")

        settings = Settings()

        assert [key.get_secret_value() for key in settings.api_keys] == [LONG_KEY, "x" * 40]

    def test_defaults_to_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_API_KEYS", LONG_KEY)

        assert Settings().environment is Environment.PRODUCTION

    def test_missing_api_keys_are_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)

        with pytest.raises(ValidationError):
            Settings()

    def test_empty_api_keys_are_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_API_KEYS", " , ")

        with pytest.raises(ValidationError):
            Settings()

    def test_short_keys_are_rejected_in_production_without_echoing_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GATEWAY_API_KEYS", "short-secret-value")

        with pytest.raises(ValidationError) as exc_info:
            Settings()

        assert "short-secret-value" not in str(exc_info.value)

    def test_short_keys_are_allowed_in_development(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_API_KEYS", "dev-key")
        monkeypatch.setenv("GATEWAY_ENVIRONMENT", "development")

        assert Settings().environment is Environment.DEVELOPMENT

    def test_api_keys_are_masked_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_API_KEYS", LONG_KEY)

        assert LONG_KEY not in repr(Settings())


class TestLoadConfig:
    def test_loads_providers_models_and_credentials(self, tmp_path: Path) -> None:
        settings = make_settings(write_config(tmp_path, VALID_CONFIG))

        loaded = load_config(settings, PROVIDER_KEYS)

        assert set(loaded.gateway.models) == {"fast", "quality"}
        assert loaded.gateway.models["quality"].default_max_tokens == 1024
        openai_key = loaded.provider_api_keys["openai"]
        assert openai_key is not None
        assert openai_key.get_secret_value() == "openai-secret"

    def test_missing_file(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "missing.toml")

        with pytest.raises(ConfigurationError, match="not found"):
            load_config(settings, PROVIDER_KEYS)

    def test_invalid_toml(self, tmp_path: Path) -> None:
        settings = make_settings(write_config(tmp_path, "[providers"))

        with pytest.raises(ConfigurationError, match="not valid TOML"):
            load_config(settings, PROVIDER_KEYS)

    def test_model_referencing_unknown_provider(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace('provider = "openai"', 'provider = "missing"')
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError, match="unknown provider 'missing'"):
            load_config(settings, PROVIDER_KEYS)

    def test_unknown_provider_type(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace('type = "openai_compatible"', 'type = "custom"')
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError, match=r"providers\.openai"):
            load_config(settings, PROVIDER_KEYS)

    def test_unknown_fields_are_rejected(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace('model = "upstream-fast"', 'model = "x"\nretries = 3')
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError, match="retries"):
            load_config(settings, PROVIDER_KEYS)

    @pytest.mark.parametrize("alias", ["bad alias", "-leading-dash", "a" * 65])
    def test_invalid_alias_names(self, tmp_path: Path, alias: str) -> None:
        config = VALID_CONFIG.replace("[models.fast]", f'[models."{alias}"]')
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError):
            load_config(settings, PROVIDER_KEYS)

    def test_at_least_one_model_is_required(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.split("[models.fast]")[0] + "[models]\n"
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError, match="models"):
            load_config(settings, PROVIDER_KEYS)

    def test_missing_provider_credential(self, tmp_path: Path) -> None:
        settings = make_settings(write_config(tmp_path, VALID_CONFIG))

        with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
            load_config(settings, {"OPENAI_API_KEY": "openai-secret"})

    def test_blank_provider_credential_is_treated_as_missing(self, tmp_path: Path) -> None:
        settings = make_settings(write_config(tmp_path, VALID_CONFIG))

        with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
            load_config(settings, {**PROVIDER_KEYS, "OPENAI_API_KEY": "  "})

    def test_anthropic_requires_a_credential_variable(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace('api_key_env = "ANTHROPIC_API_KEY"\n', "")
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError, match="api_key_env"):
            load_config(settings, PROVIDER_KEYS)

    def test_openai_compatible_provider_may_omit_credentials(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace('api_key_env = "OPENAI_API_KEY"\n', "")
        settings = make_settings(write_config(tmp_path, config))

        loaded = load_config(settings, PROVIDER_KEYS)

        assert loaded.provider_api_keys["openai"] is None

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://user:pass@llm.example.com/v1",
            "https://llm.example.com/v1?key=abc",
            "ftp://llm.example.com",
        ],
    )
    def test_rejects_unsafe_base_urls(self, tmp_path: Path, base_url: str) -> None:
        config = VALID_CONFIG.replace("https://llm.example.com/v1", base_url)
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError, match="base_url"):
            load_config(settings, PROVIDER_KEYS)

    def test_production_rejects_plain_http(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace("https://llm.example.com", "http://llm.internal")
        settings = make_settings(write_config(tmp_path, config))

        with pytest.raises(ConfigurationError, match="non-HTTPS"):
            load_config(settings, PROVIDER_KEYS)

    def test_production_allows_plain_http_with_explicit_opt_in(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace(
            'base_url = "https://llm.example.com/v1"',
            'base_url = "http://llm.internal/v1"\nallow_insecure_http = true',
        )
        settings = make_settings(write_config(tmp_path, config))

        assert load_config(settings, PROVIDER_KEYS).gateway.providers["openai"].allow_insecure_http

    def test_development_allows_plain_http(self, tmp_path: Path) -> None:
        config = VALID_CONFIG.replace("https://llm.example.com", "http://localhost:9000")
        settings = make_settings(
            write_config(tmp_path, config), environment=Environment.DEVELOPMENT
        )

        load_config(settings, PROVIDER_KEYS)


@pytest.mark.parametrize(
    "path", sorted(Path(__file__).parents[2].joinpath("config").glob("*.toml")), ids=str
)
def test_repository_config_files_are_valid(path: Path) -> None:
    settings = make_settings(path, environment=Environment.DEVELOPMENT)
    environ = {
        "OPENAI_API_KEY": "x",
        "ANTHROPIC_API_KEY": "x",
        "MOCK_UPSTREAM_API_KEY": "x",
    }

    load_config(settings, environ)
