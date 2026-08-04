"""PR8 gate — GameConfig round-trips, prices, validates, and hides secrets."""

from __future__ import annotations

import pytest

from config import (
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_MODEL,
    MODEL_PRICING,
    GameConfig,
    MemoryStrategy,
    ModelRoster,
    api_key_from_env,
    cost_usd,
    validate_models,
)
from engine.state import Role, VotingMode


# --------------------------------------------------------------------------- #
# JSON round-trip
# --------------------------------------------------------------------------- #
def test_default_config_round_trips_json() -> None:
    cfg = GameConfig()
    restored = GameConfig.model_validate_json(cfg.model_dump_json())
    assert restored == cfg


def test_custom_config_round_trips_json() -> None:
    cfg = GameConfig(
        seed=42,
        voting_mode=VotingMode.SEQUENTIAL,
        memory_strategy=MemoryStrategy.BELIEFS_ONLY,
        models=ModelRoster(mafia="claude-opus-5", narrator="claude-haiku-4-5"),
        prompt_version="v3",
        cost_ceiling_usd=2.50,
    )
    restored = GameConfig.model_validate_json(cfg.model_dump_json())
    assert restored == cfg
    assert restored.models.mafia == "claude-opus-5"


# --------------------------------------------------------------------------- #
# Pricing + cost
# --------------------------------------------------------------------------- #
def test_cost_usd_computes_from_pricing() -> None:
    # Haiku: $1/MTok input, $5/MTok output.
    assert cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)
    assert cost_usd("claude-haiku-4-5", 0, 0) == 0.0
    assert cost_usd("claude-opus-5", 2_000_000, 0) == pytest.approx(10.0)


def test_cost_usd_rejects_unpriced_model() -> None:
    with pytest.raises(KeyError):
        cost_usd("no-such-model", 100, 100)


def test_pricing_table_has_expected_models() -> None:
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert model in MODEL_PRICING
        assert MODEL_PRICING[model].input_per_mtok > 0
        assert MODEL_PRICING[model].output_per_mtok > 0


# --------------------------------------------------------------------------- #
# validate_models
# --------------------------------------------------------------------------- #
def test_validate_models_accepts_default() -> None:
    validate_models(GameConfig())  # must not raise


def test_validate_models_rejects_unpriced_model() -> None:
    cfg = GameConfig(models=ModelRoster(seer="totally-made-up-model"))
    with pytest.raises(ValueError, match="unpriced"):
        validate_models(cfg)


# --------------------------------------------------------------------------- #
# Roster helpers
# --------------------------------------------------------------------------- #
def test_roster_maps_roles() -> None:
    roster = ModelRoster(
        mafia="claude-opus-5",
        seer="claude-sonnet-5",
        villager="claude-haiku-4-5",
    )
    assert roster.for_role(Role.MAFIA) == "claude-opus-5"
    assert roster.for_role(Role.SEER) == "claude-sonnet-5"
    assert roster.for_role(Role.VILLAGER) == "claude-haiku-4-5"


def test_roster_defaults_to_cheapest() -> None:
    roster = ModelRoster()
    assert roster.for_role(Role.VILLAGER) == DEFAULT_MODEL
    assert roster.all_models() == {DEFAULT_MODEL}  # all four default the same


# --------------------------------------------------------------------------- #
# No secrets in the serialized form
# --------------------------------------------------------------------------- #
def test_api_key_never_in_serialized_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-SECRET-should-not-appear")
    cfg = GameConfig()
    dumped = cfg.model_dump_json()
    assert "SECRET" not in dumped
    assert "api_key" not in GameConfig.model_fields  # not even a field


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-example")
    assert api_key_from_env() == "sk-ant-example"
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    assert api_key_from_env() is None
