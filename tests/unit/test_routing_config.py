import copy
from typing import Any

import pytest
from pydantic import ValidationError

from gateway.core.config import AnthropicProviderConfig, GatewayConfig
from gateway.routing.config import AliasRouting, Capability, RoutingPolicy


def _price(provider: str, model: str) -> dict[str, str]:
    return {
        "provider": provider,
        "model": model,
        "version": "v1",
        "effective_at": "2020-01-01T00:00:00Z",
        "input_per_million_usd": "1",
        "output_per_million_usd": "2",
    }


def routed_raw() -> dict[str, Any]:
    return {
        "providers": {
            "openai": {"type": "openai_compatible", "base_url": "https://openai.test/v1"},
            "anthropic": {
                "type": "anthropic",
                "base_url": "https://anthropic.test",
                "api_key_env": "ANTHROPIC_API_KEY",
            },
        },
        "models": {
            "classify": {
                "provider": "openai",
                "model": "large",
                "max_output_tokens": 64,
                "routing": {
                    "policy": "lowest_cost",
                    "candidates": [
                        {"provider": "openai", "model": "small"},
                        {"provider": "anthropic", "model": "medium"},
                    ],
                },
            }
        },
        "cost": {
            "enabled": True,
            "pricing": [
                _price("openai", "large"),
                _price("openai", "small"),
                _price("anthropic", "medium"),
            ],
        },
        "routing": {
            "enabled": True,
            "models": [
                {
                    "provider": "openai",
                    "model": "large",
                    "context_window_tokens": 1000,
                    "max_output_tokens": 100,
                },
                {
                    "provider": "openai",
                    "model": "small",
                    "context_window_tokens": 1000,
                    "max_output_tokens": 100,
                    "capabilities": ["streaming", "temperature"],
                },
                {
                    "provider": "anthropic",
                    "model": "medium",
                    "context_window_tokens": 1000,
                    "max_output_tokens": 100,
                    "capabilities": ["streaming", "stop"],
                },
            ],
        },
    }


def test_valid_routed_configuration() -> None:
    config = GatewayConfig.model_validate(routed_raw())

    alias = config.models["classify"]
    assert alias.routing.policy is RoutingPolicy.LOWEST_COST
    assert alias.route_targets() == [
        ("openai", "large"),
        ("openai", "small"),
        ("anthropic", "medium"),
    ]
    assert config.routing.registry()[("openai", "small")].capabilities == {
        Capability.STREAMING,
        Capability.TEMPERATURE,
    }


def test_existing_alias_configuration_defaults_to_fixed_routing() -> None:
    config = GatewayConfig.model_validate(
        {
            "providers": {"p": {"type": "openai_compatible", "base_url": "https://p.test/v1"}},
            "models": {"fast": {"provider": "p", "model": "m"}},
        }
    )

    assert config.models["fast"].routing.policy is RoutingPolicy.FIXED
    assert config.routing.enabled is False
    assert config.routing.models == []


def _invalid(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        GatewayConfig.model_validate(raw)


def test_dynamic_routing_requires_cost_accounting() -> None:
    raw = routed_raw()
    raw["cost"] = {"enabled": False}
    _invalid(raw, "requires cost")


def test_every_route_target_needs_a_registry_entry() -> None:
    raw = routed_raw()
    raw["routing"]["models"].pop(1)
    _invalid(raw, "openai/small, which has no routing.models entry")


def test_every_route_target_needs_a_price() -> None:
    raw = routed_raw()
    raw["cost"]["pricing"].pop(2)
    _invalid(raw, "no price for anthropic/medium")


def test_registry_cannot_claim_capabilities_the_adapter_lacks() -> None:
    raw = routed_raw()
    raw["routing"]["models"][2]["capabilities"] = ["temperature"]
    _invalid(raw, "does not support: \\[<Capability.TEMPERATURE")


def test_registry_rejects_unknown_capabilities() -> None:
    raw = routed_raw()
    raw["routing"]["models"][1]["capabilities"] = ["tools"]
    _invalid(raw, "capabilities")


def test_registry_entries_require_known_limits() -> None:
    raw = routed_raw()
    del raw["routing"]["models"][0]["context_window_tokens"]
    _invalid(raw, "context_window_tokens")


def test_registry_rejects_duplicate_models_and_unknown_providers() -> None:
    raw = routed_raw()
    raw["routing"]["models"].append(copy.deepcopy(raw["routing"]["models"][0]))
    _invalid(raw, "must not repeat")
    raw = routed_raw()
    raw["routing"]["models"][0]["provider"] = "missing"
    _invalid(raw, "unknown provider 'missing'")


def test_candidates_must_reference_configured_providers() -> None:
    raw = routed_raw()
    raw["models"]["classify"]["routing"]["candidates"].append({"provider": "nope", "model": "x"})
    _invalid(raw, "unknown provider 'nope'")


def test_routed_alias_cannot_also_have_fallbacks() -> None:
    raw = routed_raw()
    raw["models"]["classify"]["fallbacks"] = [{"provider": "anthropic", "model": "medium"}]
    _invalid(raw, "instead of fallbacks")


def test_candidates_must_not_repeat_the_primary() -> None:
    raw = routed_raw()
    raw["models"]["classify"]["routing"]["candidates"].append(
        {"provider": "openai", "model": "large"}
    )
    _invalid(raw, "must not repeat the primary")


def test_default_output_allowance_must_fit_every_target() -> None:
    raw = routed_raw()
    raw["models"]["classify"]["max_output_tokens"] = 200
    _invalid(raw, "above the limit of 100")


def test_disabled_routing_still_validates_policies() -> None:
    raw = routed_raw()
    raw["routing"]["enabled"] = False
    raw["routing"]["models"].pop(1)
    _invalid(raw, "no routing.models entry")


def test_quality_policy_requires_explicit_evidence_rules() -> None:
    with pytest.raises(ValidationError, match="requires dataset, dataset_version"):
        AliasRouting.model_validate(
            {"policy": "quality_constrained", "candidates": [{"provider": "p", "model": "m"}]}
        )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"policy": "fixed", "candidates": [{"provider": "p", "model": "m"}]}, "takes no"),
        ({"policy": "lowest_cost"}, "requires candidates"),
        (
            {
                "policy": "lowest_cost",
                "candidates": [{"provider": "p", "model": "m"}],
                "min_success_rate": 0.9,
            },
            "only apply to the quality_constrained policy",
        ),
        (
            {
                "policy": "lowest_cost",
                "candidates": [{"provider": "p", "model": "m"}, {"provider": "p", "model": "m"}],
            },
            "must not repeat",
        ),
        ({"policy": "lowest_cost", "candidates": [], "rollout_percent": 101}, "less than"),
    ],
)
def test_alias_routing_field_rules(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AliasRouting.model_validate(fields)


def test_anthropic_capabilities_depend_on_api_version() -> None:
    current = AnthropicProviderConfig.model_validate(
        {"type": "anthropic", "base_url": "https://a.test", "api_key_env": "K"}
    )
    legacy = current.model_copy(update={"api_version": "2023-06-01"})

    assert Capability.TEMPERATURE not in current.capabilities
    assert Capability.TOP_P not in current.capabilities
    assert Capability.STOP in current.capabilities
    assert legacy.capabilities == frozenset(Capability)
