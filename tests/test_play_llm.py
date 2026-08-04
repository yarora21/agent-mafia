"""PR14b gate — the end-to-end wiring runs offline, both modes, zero spend.

Uses the `--dry-run` stub client (no API key, no network). Proves the whole stack
composes — LlmAgent + NarratorAgent + orchestrator + transcript + replay — and the
two PR14 invariants: the live `PROMPT_VERSION` is stamped into the transcript, and
a dry run spends nothing. The live `--smoke` / full-game paths are manual (spend-
gated) and not exercised here.
"""

from __future__ import annotations

from pathlib import Path

from agents.prompts import PROMPT_VERSION
from config import GameConfig, ModelRoster
from engine.state import NARRATOR_ID, Phase, VotingMode
from scripts.play_llm import build_agents, dry_run_factory, play_llm
from transcript import load_transcript, replay_check


async def test_dry_run_completes_both_modes_zero_spend() -> None:
    for mode in (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL):
        config = GameConfig(seed=2, voting_mode=mode)
        transcript, run = await play_llm(config, factory=dry_run_factory(), write=False)

        assert transcript.final_state.phase is Phase.GAME_OVER
        assert replay_check(transcript) is True
        assert run.aborted is False
        assert run.cost.total == 0.0  # zero tokens → zero spend


async def test_reasoning_stream_is_captured() -> None:
    # Dry-run players are real LlmAgents (stub client), so they produce step
    # records offline — the reasoning stream must be populated per player.
    config = GameConfig(seed=2, voting_mode=VotingMode.SIMULTANEOUS)
    transcript, _ = await play_llm(config, factory=dry_run_factory(), write=False)

    player_ids = {p.id for p in transcript.final_state.players}
    assert transcript.reasoning  # non-empty
    assert set(transcript.reasoning) <= player_ids  # narrator contributes none
    # Every captured turn carries its prompts and the action type it answered.
    for steps in transcript.reasoning.values():
        assert steps
        for node in steps:
            assert node.system_prompt and node.user_prompt
            assert node.action_type is not None


async def test_reasoning_survives_write_reload(tmp_path: Path) -> None:
    config = GameConfig(seed=8, voting_mode=VotingMode.SIMULTANEOUS)
    transcript, _ = await play_llm(
        config, factory=dry_run_factory(), write=True, out_dir=str(tmp_path)
    )
    reloaded = load_transcript(next(iter(tmp_path.glob("*.json"))))
    assert reloaded.reasoning == transcript.reasoning


async def test_prompt_version_is_stamped() -> None:
    # A config carrying a stale version must be overwritten with the live value.
    config = GameConfig(seed=1, prompt_version="STALE")
    transcript, _ = await play_llm(config, factory=dry_run_factory(), write=False)
    assert transcript.config.prompt_version == PROMPT_VERSION


async def test_transcript_is_written_and_reloads(tmp_path: Path) -> None:
    config = GameConfig(seed=3, voting_mode=VotingMode.SEQUENTIAL)
    transcript, _ = await play_llm(
        config, factory=dry_run_factory(), write=True, out_dir=str(tmp_path)
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert replay_check(load_transcript(files[0])) is True


def test_build_agents_covers_every_seat_and_narrator() -> None:
    from engine.state import new_game
    from orchestrator import CostLedger

    config = GameConfig(models=ModelRoster())
    state = new_game(seed=1, mode=VotingMode.SIMULTANEOUS)
    agents = build_agents(state, config, dry_run_factory(), CostLedger())

    assert set(agents) == {p.id for p in state.players} | {NARRATOR_ID}
