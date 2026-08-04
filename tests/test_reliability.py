"""PR7 gate — orchestrator reliability against scripted mock agents (no API).

Proven here:
- a HANG times out, backs off, falls back, and the game still COMPLETES;
- an outage (RAISE) is retried with backoff then falls back;
- illegal-then-legal is RETRIED and recovers (`retried_ok`), no fallback;
- persistent illegal falls back after the retry budget;
- a cost-ceiling breach ABORTS the game and records why;
- the reliability wrapper does not disturb the happy path (random games still
  complete and stay reproducible).
"""

from __future__ import annotations

from agents.interface import AgentInterface
from agents.mock_agent import ScriptBehavior, ScriptedAgent
from agents.random_agent import RandomAgent
from engine.state import (
    NARRATOR_ID,
    NUM_PLAYERS,
    EventType,
    Faction,
    Phase,
    VotingMode,
)
from orchestrator import (
    ReliabilityConfig,
    build_random_agents,
    play_game,
    play_random_game,
)

# Fast knobs so hangs/backoff don't slow the suite.
FAST = ReliabilityConfig(
    malformed_retries=1,
    api_retries=1,
    backoff_base=0.0,
    timeout_seconds=0.02,
)


def _all(agent_factory: object) -> dict[str, AgentInterface]:
    agents: dict[str, AgentInterface] = {
        f"p{i}": agent_factory() for i in range(NUM_PLAYERS)  # type: ignore[operator]
    }
    agents[NARRATOR_ID] = agent_factory()  # type: ignore[operator]
    return agents


def _finished(result_state) -> None:  # type: ignore[no-untyped-def]
    assert result_state.phase is Phase.GAME_OVER
    assert result_state.history[-1].type is EventType.GAME_OVER
    assert result_state.history[-1].winner in (Faction.TOWN, Faction.MAFIA)


# --------------------------------------------------------------------------- #
# Hang -> timeout -> fallback -> still completes
# --------------------------------------------------------------------------- #
async def test_hanging_agents_time_out_and_game_completes() -> None:
    agents = _all(lambda: ScriptedAgent(default=ScriptBehavior.HANG))
    for mode in (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL):
        result = await play_game(agents, seed=1, mode=mode, reliability=FAST)
        _finished(result.final_state)
        assert result.fallbacks > 0  # every turn fell back
        assert result.api_retries > 0  # backoff was attempted before falling back


# --------------------------------------------------------------------------- #
# Outage (raise) -> retried with backoff -> fallback -> completes
# --------------------------------------------------------------------------- #
async def test_raising_agents_are_retried_then_fall_back() -> None:
    agents = _all(lambda: ScriptedAgent(default=ScriptBehavior.RAISE))
    result = await play_game(agents, seed=2, mode=VotingMode.SEQUENTIAL, reliability=FAST)
    _finished(result.final_state)
    assert result.fallbacks > 0
    assert result.api_retries > 0


# --------------------------------------------------------------------------- #
# Malformed path: illegal-then-legal recovers; persistent illegal falls back
# --------------------------------------------------------------------------- #
async def test_illegal_then_legal_is_retried_ok() -> None:
    # p0 answers illegally once, then legally forever; everyone else is legal.
    agents: dict[str, AgentInterface] = {
        f"p{i}": RandomAgent(seed=i) for i in range(NUM_PLAYERS)
    }
    agents["p0"] = ScriptedAgent(
        script=[ScriptBehavior.ILLEGAL], default=ScriptBehavior.LEGAL, seed=99
    )
    agents[NARRATOR_ID] = RandomAgent(seed=100)
    result = await play_game(agents, seed=7, mode=VotingMode.SEQUENTIAL, reliability=FAST)
    _finished(result.final_state)
    assert result.retried_ok >= 1  # the illegal answer was retried and recovered


async def test_persistent_illegal_falls_back() -> None:
    agents = _all(lambda: ScriptedAgent(default=ScriptBehavior.ILLEGAL))
    result = await play_game(agents, seed=8, mode=VotingMode.SIMULTANEOUS, reliability=FAST)
    _finished(result.final_state)
    assert result.fallbacks > 0
    assert result.retried_ok == 0  # it never recovers, so nothing is "retried_ok"


# --------------------------------------------------------------------------- #
# Cost ceiling -> recorded abort
# --------------------------------------------------------------------------- #
async def test_cost_ceiling_aborts_and_is_recorded() -> None:
    cfg = ReliabilityConfig(cost_per_call=1.0, cost_ceiling=3.0, timeout_seconds=0.02)
    result = await play_random_game(0, VotingMode.SEQUENTIAL, reliability=cfg)
    assert result.aborted is True
    assert result.abort_reason is not None and "cost ceiling" in result.abort_reason
    assert result.final_state.phase is not Phase.GAME_OVER  # stopped early
    assert result.cost.total > 3.0


async def test_generous_ceiling_does_not_abort() -> None:
    cfg = ReliabilityConfig(cost_per_call=1.0, cost_ceiling=10_000.0, timeout_seconds=0.02)
    result = await play_random_game(0, VotingMode.SEQUENTIAL, reliability=cfg)
    assert result.aborted is False
    _finished(result.final_state)


async def test_narrator_billed_separately() -> None:
    cfg = ReliabilityConfig(cost_per_call=1.0, cost_ceiling=10_000.0, timeout_seconds=0.02)
    result = await play_random_game(0, VotingMode.SEQUENTIAL, reliability=cfg)
    assert NARRATOR_ID in result.cost.per_actor
    # the Narrator has its own ledger key, distinct from any player's
    assert result.cost.per_actor[NARRATOR_ID] > 0


# --------------------------------------------------------------------------- #
# Happy path unchanged by the reliability wrapper
# --------------------------------------------------------------------------- #
async def test_happy_path_still_reproducible_and_clean() -> None:
    a = await play_random_game(3, VotingMode.SIMULTANEOUS)
    b = await play_random_game(3, VotingMode.SIMULTANEOUS)
    _finished(a.final_state)
    assert a.final_state == b.final_state
    assert a.actions == b.actions
    assert a.fallbacks == 0 and a.retried_ok == 0 and a.api_retries == 0
    assert a.aborted is False
