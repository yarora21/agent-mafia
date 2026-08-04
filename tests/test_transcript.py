"""PR13 gate — the transcript proves itself, against mock games (no API).

Every game here is played with `ScriptedAgent`s, so the whole tier is deterministic
and offline. It proves:
- `build_transcript` yields a schema-valid record;
- `replay_check` passes in both voting modes;
- write → load → re-serialize is byte-stable;
- a game that used fallbacks still replays;
- an aborted (cost-ceiling) game replays to its stopping point;
- the `NodeOutputs` shape round-trips (its PR14 population aside).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agents.beliefs import BeliefState, TextOutput
from agents.interface import AgentInterface
from agents.mock_agent import ScriptBehavior, ScriptedAgent
from config import GameConfig
from engine.actions import Speech
from engine.state import NARRATOR_ID, NUM_PLAYERS, Faction, Phase, VotingMode
from orchestrator import ReliabilityConfig, RunResult, play_game
from transcript import (
    ActionRecord,
    NodeOutputs,
    Transcript,
    build_transcript,
    load_transcript,
    replay_check,
    write_transcript,
)

FAST = ReliabilityConfig(malformed_retries=1, api_retries=1, timeout_seconds=0.02)


def _agents(factory: Callable[[], AgentInterface]) -> dict[str, AgentInterface]:
    agents: dict[str, AgentInterface] = {
        f"p{i}": factory() for i in range(NUM_PLAYERS)
    }
    agents[NARRATOR_ID] = factory()
    return agents


async def _play(
    factory: Callable[[], AgentInterface],
    *,
    seed: int,
    mode: VotingMode,
    reliability: ReliabilityConfig = FAST,
) -> tuple[GameConfig, RunResult]:
    result = await play_game(
        _agents(factory), seed=seed, mode=mode, reliability=reliability
    )
    config = GameConfig(seed=seed, voting_mode=mode)
    return config, result


# --------------------------------------------------------------------------- #
# Build + replay, both modes
# --------------------------------------------------------------------------- #
async def test_build_and_replay_both_modes() -> None:
    for mode in (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL):
        config, run = await _play(
            lambda: ScriptedAgent(default=ScriptBehavior.LEGAL, seed=1),
            seed=3,
            mode=mode,
        )
        transcript = build_transcript(config, run)
        assert transcript.final_state.phase is Phase.GAME_OVER
        assert transcript.winner in (Faction.TOWN, Faction.MAFIA)
        assert len(transcript.actions) == len(run.actions)
        assert replay_check(transcript) is True


# --------------------------------------------------------------------------- #
# Byte-stability
# --------------------------------------------------------------------------- #
async def test_write_load_is_byte_stable(tmp_path: Path) -> None:
    config, run = await _play(
        lambda: ScriptedAgent(seed=2), seed=5, mode=VotingMode.SIMULTANEOUS
    )
    transcript = build_transcript(config, run)

    path = write_transcript(transcript, tmp_path, timestamp="20260804T000000Z")
    assert path.name == "20260804T000000Z_5_simultaneous.json"

    first = path.read_text()
    reloaded = load_transcript(path)
    assert reloaded == transcript  # structural round-trip
    second = reloaded.model_dump_json(indent=2)
    assert first == second  # byte-stable re-serialization
    assert replay_check(reloaded) is True


# --------------------------------------------------------------------------- #
# Fallback-heavy game still replays
# --------------------------------------------------------------------------- #
async def test_fallback_game_replays() -> None:
    # Every agent always answers illegally → every turn falls back after retry.
    config, run = await _play(
        lambda: ScriptedAgent(default=ScriptBehavior.ILLEGAL, seed=1),
        seed=4,
        mode=VotingMode.SEQUENTIAL,
    )
    assert run.fallbacks > 0  # the game really did rely on fallbacks
    transcript = build_transcript(config, run)
    assert replay_check(transcript) is True


# --------------------------------------------------------------------------- #
# Aborted game replays to its stopping point
# --------------------------------------------------------------------------- #
async def test_aborted_game_replays_to_stopping_point() -> None:
    cfg = ReliabilityConfig(
        cost_per_call=1.0, cost_ceiling=3.0, timeout_seconds=0.02
    )
    config, run = await _play(
        lambda: ScriptedAgent(seed=1),
        seed=6,
        mode=VotingMode.SIMULTANEOUS,
        reliability=cfg,
    )
    assert run.aborted is True
    transcript = build_transcript(config, run)
    assert transcript.run.aborted is True
    assert transcript.run.abort_reason is not None
    assert transcript.final_state.phase is not Phase.GAME_OVER  # stopped early
    assert replay_check(transcript) is True


# --------------------------------------------------------------------------- #
# Schema shapes
# --------------------------------------------------------------------------- #
def test_node_outputs_round_trips() -> None:
    rec = ActionRecord(
        action=Speech(actor_id="p0", message="hello"),
        node_outputs=NodeOutputs(
            system_prompt="sys",
            user_prompt="usr",
            reasoning="because",
            output=TextOutput(message="a line"),
            belief_after=BeliefState(notes="n", suspicions={"p1": 0.5}),
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
        ),
    )
    back = ActionRecord.model_validate_json(rec.model_dump_json())
    assert back == rec
    assert isinstance(back.node_outputs, NodeOutputs)
    assert isinstance(back.node_outputs.output, TextOutput)
    assert isinstance(back.action, Speech)


def test_transcript_history_is_final_history() -> None:
    from engine.state import new_game

    state = new_game(seed=1, mode=VotingMode.SIMULTANEOUS)
    t = Transcript(config=GameConfig(seed=1), final_state=state)
    assert t.history == state.history
