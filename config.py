"""The one settings object that defines and reproduces a game.

`GameConfig` gathers everything that determines a game's outcome — seed, voting
mode, which model plays each role group, memory strategy, prompt version, cost
ceiling, and retry/backoff — into a single serializable object. It is stamped
into every transcript (PR13), so a stored game ties back to the exact settings
(and prompt version) that produced it, and a replay reloads them.

Two rules this module keeps:

- **Pure and leaf.** It imports only `engine` domain enums; no LLM, no network,
  no orchestrator. `ReliabilityConfig` (PR7's stopgap) is superseded by the
  fields here — PR14 maps `GameConfig` onto the orchestrator's knobs at the call
  site, so config.py never depends on the orchestrator.
- **No secrets in the serialized form.** The API key is read from the
  environment at runtime (`api_key_from_env`) and is deliberately NOT a field, so
  dumping a config to JSON never leaks it into a shared transcript.
"""

from __future__ import annotations

import os
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from engine.state import Role, VotingMode

# --------------------------------------------------------------------------- #
# Memory strategy
# --------------------------------------------------------------------------- #
class MemoryStrategy(str, Enum):
    """How much history an agent is shown each call (spec 3.2).

    A config flag because the Section 5 voting experiment holds it fixed while
    varying only the voting mode.
    """

    FULL_TRANSCRIPT = "full_transcript"  # entire public history each call (baseline)
    BELIEFS_ONLY = "beliefs_only"  # BeliefState + events since last action
    BELIEFS_PLUS_RECENT = "beliefs_plus_recent"  # BeliefState + last N public events


# --------------------------------------------------------------------------- #
# Model pricing + cost
# --------------------------------------------------------------------------- #
class ModelPricing(BaseModel):
    """Per-token price for one model, in USD per million tokens."""

    model_config = ConfigDict(frozen=True)

    input_per_mtok: float
    output_per_mtok: float


# USD per million tokens (input / output). Anthropic first-party API rates.
# Update when prices change; `validate_models` rejects any model missing here.
MODEL_PRICING: dict[str, ModelPricing] = {
    "claude-fable-5": ModelPricing(input_per_mtok=10.0, output_per_mtok=50.0),
    "claude-opus-5": ModelPricing(input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-opus-4-8": ModelPricing(input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-sonnet-5": ModelPricing(input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-sonnet-4-6": ModelPricing(input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-haiku-4-5": ModelPricing(input_per_mtok=1.0, output_per_mtok=5.0),
}

DEFAULT_MODEL = "claude-haiku-4-5"
"""Cheapest model; the v0 default for every role so full games run cheaply."""

NARRATOR_MODEL = "claude-haiku-4-5"
"""The Narrator is a degenerate agent — pin it to the cheapest model (spec 3.2)."""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of a call, from token counts and `MODEL_PRICING`.

    Raises `KeyError` for an unpriced model — a config that names one can't be
    cost-accounted or budget-aborted, so it must fail loudly.
    """

    try:
        pricing = MODEL_PRICING[model]
    except KeyError:
        raise KeyError(
            f"no pricing for model {model!r}; add it to MODEL_PRICING"
        ) from None
    return (
        input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
    ) / 1_000_000


# --------------------------------------------------------------------------- #
# Model roster (models per role group)
# --------------------------------------------------------------------------- #
class ModelRoster(BaseModel):
    """Which model plays each role group, plus the Narrator."""

    model_config = ConfigDict(frozen=True)

    mafia: str = DEFAULT_MODEL
    seer: str = DEFAULT_MODEL
    villager: str = DEFAULT_MODEL
    narrator: str = NARRATOR_MODEL

    def for_role(self, role: Role) -> str:
        """The model that plays a given player role."""

        return {
            Role.MAFIA: self.mafia,
            Role.SEER: self.seer,
            Role.VILLAGER: self.villager,
        }[role]

    def all_models(self) -> set[str]:
        """Every distinct model this roster references (incl. the Narrator)."""

        return {self.mafia, self.seer, self.villager, self.narrator}


# --------------------------------------------------------------------------- #
# The game config
# --------------------------------------------------------------------------- #
class GameConfig(BaseModel):
    """The single reproducibility-and-cost contract, serialized into transcripts.

    The reliability fields (retries, backoff, timeout, cost ceiling) supersede
    PR7's stopgap `ReliabilityConfig`; PR14 maps them onto the orchestrator.
    """

    model_config = ConfigDict(frozen=True)

    seed: int = 0
    voting_mode: VotingMode = VotingMode.SIMULTANEOUS
    memory_strategy: MemoryStrategy = MemoryStrategy.BELIEFS_PLUS_RECENT
    recent_events_count: int = 20  # N for BELIEFS_PLUS_RECENT
    models: ModelRoster = Field(default_factory=ModelRoster)
    prompt_version: str = "v1"  # stamped from prompts.PROMPT_VERSION at PR10/PR14; keep in sync
    cost_ceiling_usd: float | None = None
    malformed_retries: int = 1
    api_retries: int = 3
    backoff_base_seconds: float = 1.0
    timeout_seconds: float = 60.0


def validate_models(config: GameConfig) -> None:
    """Raise if the config names any model absent from `MODEL_PRICING`.

    An unpriced model can't be cost-accounted or budget-aborted, so a config
    that references one is invalid.
    """

    unpriced = sorted(m for m in config.models.all_models() if m not in MODEL_PRICING)
    if unpriced:
        raise ValueError(
            f"config references unpriced model(s) {unpriced}; "
            f"add them to MODEL_PRICING"
        )


# --------------------------------------------------------------------------- #
# API key (from the environment; never serialized)
# --------------------------------------------------------------------------- #
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"


def api_key_from_env() -> str | None:
    """The Anthropic API key from the environment, or ``None`` if unset.

    Deliberately not a `GameConfig` field — keeping it out of the serialized form
    means a shared transcript never carries a secret.
    """

    return os.environ.get(ANTHROPIC_API_KEY_ENV)
