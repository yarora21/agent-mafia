"""PR12 gate — the Narrator agent, against a stub model (no real API, that is PR14).

Proves the two things that make the Narrator safe and correct:
- it produces a `Narration` (and follows the system's failure classes: malformed →
  `None`, outage → propagates), and
- it is fed only the public-only `narrator_view`, so its rendered prompt bytes
  carry no mafia/seer/kill markers. The corpus is proven non-vacuous: the
  omniscient history at that state *does* hold the secrets that could leak.
"""

from __future__ import annotations

from typing import TypeVar, cast

from pydantic import BaseModel

from agents.beliefs import TextOutput
from agents.interface import AgentInterface
from agents.llm_agent import TokenUsage
from agents.narrator_agent import NarratorAgent
from agents.prompts import render_prompt
from config import NARRATOR_MODEL, cost_usd
from engine.actions import Action, ActionRequest, ActionType, Narration
from engine.engine import apply, next_requests, resolve_automatic
from engine.referee import fallback_action
from engine.state import EventType, GameState, NARRATOR_ID, Phase, VotingMode, new_game
from engine.visibility import ObservableState, narrator_view, observable_view

_USAGE = TokenUsage(input_tokens=80, output_tokens=40)
_S = TypeVar("_S", bound=BaseModel)


# --------------------------------------------------------------------------- #
# Stub model client
# --------------------------------------------------------------------------- #
class _StubClient:
    """A ModelClient that records calls and returns one canned structured value."""

    def __init__(self, out: TextOutput | None, *, raise_outage: bool = False) -> None:
        self.out = out
        self.raise_outage = raise_outage
        self.structured_calls = 0
        self.text_calls = 0

    async def complete_structured(
        self, *, system: str, user: str, schema: type[_S]
    ) -> tuple[_S | None, TokenUsage]:
        self.structured_calls += 1
        if self.raise_outage:
            raise RuntimeError("simulated outage")
        return cast("_S | None", self.out), _USAGE

    async def complete_text(
        self, *, system: str, user: str
    ) -> tuple[str | None, TokenUsage]:
        self.text_calls += 1  # a Narrator must never reach this
        return None, _USAGE


# --------------------------------------------------------------------------- #
# Drive a real mid-game DAY_NARRATE state (night 1 already happened)
# --------------------------------------------------------------------------- #
def _first_narrate() -> tuple[GameState, ObservableState, ActionRequest]:
    """The first DAY_NARRATE turn, reached with legal fallback actions."""

    state = new_game(seed=5, mode=VotingMode.SIMULTANEOUS)
    for _ in range(1000):
        state = resolve_automatic(state)
        if state.phase is Phase.DAY_NARRATE:
            req = next_requests(state)[0]
            return state, observable_view(state, req.actor_id), req
        req = next_requests(state)[0]
        state = apply(state, fallback_action(state, req))
    raise AssertionError("never reached DAY_NARRATE")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_is_agent_interface() -> None:
    assert isinstance(NarratorAgent(_StubClient(TextOutput(message="x"))), AgentInterface)


def test_request_is_the_narrator() -> None:
    _, view, req = _first_narrate()
    assert req.action_type is ActionType.NARRATE
    assert req.actor_id == NARRATOR_ID
    assert view.self_id == NARRATOR_ID


async def test_produces_a_narration() -> None:
    _, view, req = _first_narrate()
    stub = _StubClient(TextOutput(message="A cold wind carries a name away."))
    action = await NarratorAgent(stub).act(view, req)
    assert isinstance(action, Narration)
    assert action.actor_id == NARRATOR_ID  # stamped from the request, not the model
    assert action.message == "A cold wind carries a name away."
    assert stub.structured_calls == 1  # exactly one model call
    assert stub.text_calls == 0  # no reasoning node


async def test_malformed_returns_none() -> None:
    _, view, req = _first_narrate()
    action = await NarratorAgent(_StubClient(None)).act(view, req)
    assert action is None  # feeds referee CANNED_NARRATION fallback


async def test_outage_propagates() -> None:
    _, view, req = _first_narrate()
    stub = _StubClient(None, raise_outage=True)
    raised = False
    try:
        await NarratorAgent(stub).act(view, req)
    except RuntimeError:
        raised = True
    assert raised  # no internal retry, no swallowing


async def test_cost_charged_at_narrator_model() -> None:
    _, view, req = _first_narrate()
    charged: list[float] = []
    stub = _StubClient(TextOutput(message="x"))
    await NarratorAgent(stub, cost_callback=charged.append).act(view, req)
    assert charged == [cost_usd(NARRATOR_MODEL, _USAGE.input_tokens, _USAGE.output_tokens)]


def test_view_is_the_narrator_view() -> None:
    state, view, _ = _first_narrate()
    assert view == narrator_view(state)


def test_prompt_bytes_carry_no_private_markers() -> None:
    """The rendered Narrator prompt leaks no mafia chat, kill, or seer content."""

    state, view, req = _first_narrate()

    # Non-vacuous: the omniscient history really does hold secrets to leak.
    types = {e.type for e in state.history}
    assert EventType.MAFIA_CHAT in types
    assert EventType.SEER_RESULT in types
    assert EventType.NIGHT_KILL in types

    user = render_prompt(view, req).user
    assert "[Mafia]" not in user  # mafia chat + night-kill marker
    assert "chose to kill" not in user  # night-kill line
    assert "[Your investigation]" not in user  # seer result marker
