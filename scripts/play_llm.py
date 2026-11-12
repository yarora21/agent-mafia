"""PR14b — the API switch-on: wire real (or stubbed) LLM agents and play a game.

This is the first place every layer runs together — `LlmAgent` + `NarratorAgent`
(PR11/12) behind the `AnthropicModelClient` (PR14a), driven by the orchestrator
(PR7), recorded by the transcript writer (PR13). It ships three modes:

- ``--dry-run`` — a zero-cost offline stub client, so the whole pipeline (both
  voting modes, transcript, replay_check) runs in CI with no API key and no spend.
- ``--smoke`` — one live single-call sanity check (auth + parsing + cost logging).
- default — a full live game, written to ``games/`` and replay-checked.

Two invariants it enforces:

- **Prompt version is stamped here, at the call site.** `GameConfig.prompt_version`
  cannot default to `prompts.PROMPT_VERSION` because `prompts` imports `config`
  (a circular import), so the live value is stamped onto the config right before
  it is frozen into the transcript. That ties every stored game to the exact
  wording that produced it.
- **One shared `CostLedger`.** Each agent charges it (per-actor, Narrator separate)
  through its cost callback, and the orchestrator reads the same ledger for its
  ceiling check — so real, agent-reported spend actually gates the game.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from pydantic import BaseModel

from collections.abc import Mapping, Sequence

from agents.anthropic_client import AnthropicModelClient
from agents.tracing import configure_tracing, flush_tracing, player_turn
from agents.beliefs import BeliefState, TextOutput
from agents.interface import AgentInterface
from agents.llm_agent import CostCallback, LlmAgent, ModelClient, StepRecord, TokenUsage
from agents.narrator_agent import NarratorAgent
from agents.prompts import PROMPT_VERSION
from config import GameConfig, api_key_from_env, validate_models
from engine.actions import Action
from engine.state import NARRATOR_ID, GameState, VotingMode, new_game
from orchestrator import CostLedger, ReliabilityConfig, RunResult, play_game
from scripts.build_replay import build_steps
from transcript import (
    NodeOutputs,
    Transcript,
    build_transcript,
    replay_check,
    write_transcript,
)

ClientFactory = Callable[[str], ModelClient]
_S = TypeVar("_S", bound=BaseModel)


# --------------------------------------------------------------------------- #
# Client factories
# --------------------------------------------------------------------------- #
class _DryRunClient:
    """An offline, zero-token stub `ModelClient` for `--dry-run`.

    Returns well-formed free-text output and an empty belief, and `None` for
    target actions (it cannot know legal targets without the request) — those fall
    back through the referee. Zero tokens → zero cost, so a dry run never spends.
    """

    async def complete_structured(
        self, *, system: str, user: str, schema: type[_S]
    ) -> tuple[_S | None, TokenUsage]:
        if schema is BeliefState:
            return cast("_S | None", BeliefState()), TokenUsage()
        if schema is TextOutput:
            return cast("_S | None", TextOutput(message="(dry run)")), TokenUsage()
        return None, TokenUsage()  # TargetOutput → referee fallback

    async def complete_text(
        self, *, system: str, user: str
    ) -> tuple[str | None, TokenUsage]:
        return "(dry run reasoning)", TokenUsage()


def dry_run_factory() -> ClientFactory:
    """A factory that hands every role the same offline stub."""

    stub = _DryRunClient()
    return lambda _model: stub


def anthropic_factory(api_key: str | None) -> ClientFactory:
    """A factory building a live `AnthropicModelClient` per model name."""

    return lambda model: AnthropicModelClient(model=model, api_key=api_key)


# --------------------------------------------------------------------------- #
# Agent wiring
# --------------------------------------------------------------------------- #
def _charge(ledger: CostLedger, actor_id: str) -> CostCallback:
    """A cost callback that charges `ledger` under a fixed actor id."""

    def cb(usd: float) -> None:
        ledger.charge(actor_id, usd)

    return cb


def build_agents(
    state: GameState,
    config: GameConfig,
    factory: ClientFactory,
    ledger: CostLedger,
) -> dict[str, AgentInterface]:
    """One `LlmAgent` per dealt player (model chosen by role) + the `NarratorAgent`.

    The deal is read from `state` only to pick each player's model — the agents
    themselves still receive only their `ObservableState`. Each agent's cost is
    charged to `ledger` under its own id (the Narrator billed separately).
    """

    agents: dict[str, AgentInterface] = {}
    for player in state.players:
        model = config.models.for_role(player.role)
        agents[player.id] = LlmAgent(
            factory(model),
            model=model,
            memory_strategy=config.memory_strategy,
            recent_events_count=config.recent_events_count,
            cost_callback=_charge(ledger, player.id),
        )
    narrator_model = config.models.narrator
    agents[NARRATOR_ID] = NarratorAgent(
        factory(narrator_model),
        model=narrator_model,
        cost_callback=_charge(ledger, NARRATOR_ID),
    )
    return agents


def collect_step_records(
    agents: Mapping[str, AgentInterface],
) -> dict[str, Sequence[StepRecord]]:
    """Pull each `LlmAgent`'s per-turn `step_records` out for the transcript.

    Only LLM players carry them; the `NarratorAgent` is degenerate (no beliefs,
    no reasoning) and contributes none. Keyed by actor id.
    """

    records: dict[str, Sequence[StepRecord]] = {}
    for actor_id, agent in agents.items():
        steps = getattr(agent, "step_records", None)
        if steps:
            records[actor_id] = steps
    return records


def _reliability(config: GameConfig) -> ReliabilityConfig:
    """Map the durable `GameConfig` knobs onto the orchestrator's runtime config.

    `cost_per_call` is 0: real spend flows in from the agents via the shared
    ledger, not a flat per-call stopgap.
    """

    return ReliabilityConfig(
        malformed_retries=config.malformed_retries,
        api_retries=config.api_retries,
        backoff_base=config.backoff_base_seconds,
        timeout_seconds=config.timeout_seconds,
        cost_per_call=0.0,
        cost_ceiling=config.cost_ceiling_usd,
    )


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
async def play_llm(
    config: GameConfig,
    *,
    factory: ClientFactory,
    write: bool = False,
    out_dir: str = "games",
    on_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
) -> tuple[Transcript, RunResult]:
    """Play one full game with LLM agents from `factory`; build + self-check it.

    Stamps the live `PROMPT_VERSION` onto the config, plays via the shared-ledger
    orchestrator, builds the transcript, and asserts `replay_check` — so a run that
    cannot reproduce itself fails loudly rather than writing a bad record.
    """

    validate_models(config)
    stamped = config.model_copy(update={"prompt_version": PROMPT_VERSION})

    state = new_game(seed=stamped.seed, mode=stamped.voting_mode)
    ledger = CostLedger()
    agents = build_agents(state, stamped, factory, ledger)

    # Live X-ray stream: emit the *same* step shape the replay renders, so the
    # live board and the saved replay are the one renderer. Each flush fuses the
    # history-so-far with the reasoning-so-far via the shared `build_steps`, then
    # streams only the newly appeared steps (plus the roster, once, up front).
    live_state: dict[str, int | bool] = {"emitted": 0, "roster_sent": False}

    def _live_reasoning() -> dict[str, list[NodeOutputs]]:
        out: dict[str, list[NodeOutputs]] = {}
        for aid, ag in agents.items():
            recs = getattr(ag, "step_records", None)
            if recs:
                out[aid] = [NodeOutputs.from_step_record(s) for s in recs]
        return out

    async def _flush(at_state: GameState) -> None:
        if on_event is None:
            return
        players, all_steps = build_steps(at_state, _live_reasoning())
        if not live_state["roster_sent"]:
            await on_event(
                {
                    "type": "roster",
                    "players": players,
                    "meta": {"seed": stamped.seed, "mode": stamped.voting_mode.value},
                }
            )
            live_state["roster_sent"] = True
        emitted = int(live_state["emitted"])
        for step in all_steps[emitted:]:
            await on_event({"type": "step", "step": step})
        live_state["emitted"] = len(all_steps)

    async def _on_action(action: Action, at_state: GameState) -> None:
        await _flush(at_state)

    run = await play_game(
        agents,
        seed=stamped.seed,
        mode=stamped.voting_mode,
        reliability=_reliability(stamped),
        cost_ledger=ledger,
        on_action=_on_action if on_event is not None else None,
    )
    if on_event is not None:
        # Final automatic events (last vote outcome, game-over) land after the
        # last agent action, so no on_action fires for them — flush them now.
        await _flush(run.final_state)

    transcript = build_transcript(
        stamped, run, step_records=collect_step_records(agents)
    )
    if not replay_check(transcript):
        raise RuntimeError("replay_check failed: the transcript does not reproduce")
    if write:
        path = write_transcript(transcript, out_dir)
        print(f"wrote {path}")
    return transcript, run


async def smoke(model: str, api_key: str | None) -> None:
    """One live single call: prove auth, structured parsing, and cost logging.

    Deliberately tiny and separate from a full game — the cheapest possible check
    that the API is reachable and the client parses a real response.
    """

    client = AnthropicModelClient(model=model, api_key=api_key)
    with player_turn(session_id=None, metadata={"feature": "smoke"}, input="Say hello to the table"):
        out, usage = await client.complete_structured(
            system="You are a Mafia player. Reply with a one-line message.",
            user="Say hello to the table in character.",
            schema=TextOutput,
        )
    print(f"smoke: parsed={out!r} usage={usage}")


# --------------------------------------------------------------------------- #
# Reporting + CLI
# --------------------------------------------------------------------------- #
def _summary(transcript: Transcript, run: RunResult) -> str:
    r = transcript.run
    lines = [
        f"winner: {transcript.winner.value if transcript.winner else '(none)'}",
        f"actions: {len(transcript.actions)}   aborted: {r.aborted}",
        f"fallbacks: {r.fallbacks}   retried_ok: {r.retried_ok}   api_retries: {r.api_retries}",
        f"total cost: ${r.total_cost_usd:.4f}",
        f"prompt_version: {transcript.config.prompt_version}",
    ]
    if r.tracing_session_id:
        lines.append(f"langfuse session: {r.tracing_session_id}")
    if r.abort_reason:
        lines.append(f"abort_reason: {r.abort_reason}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Play a game with LLM agents.")
    p.add_argument("--dry-run", action="store_true", help="offline stub client, zero spend")
    p.add_argument("--smoke", action="store_true", help="one live single-call sanity check")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--mode",
        choices=[m.value for m in VotingMode],
        default=VotingMode.SIMULTANEOUS.value,
    )
    p.add_argument("--ceiling", type=float, default=None, help="cost ceiling in USD")
    p.add_argument("--no-write", action="store_true", help="do not write the transcript")
    return p.parse_args(argv)


def _main(argv: list[str] | None = None) -> None:
    configure_tracing()
    args = _parse_args(argv)
    api_key = api_key_from_env()

    if args.smoke:
        asyncio.run(smoke("claude-haiku-4-5", api_key))
        return

    config = GameConfig(
        seed=args.seed,
        voting_mode=VotingMode(args.mode),
        cost_ceiling_usd=args.ceiling,
    )
    factory = dry_run_factory() if args.dry_run else anthropic_factory(api_key)
    transcript, run = asyncio.run(
        play_llm(config, factory=factory, write=not args.no_write)
    )
    print(_summary(transcript, run))


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    finally:
        flush_tracing()


if __name__ == "__main__":  # pragma: no cover
    main()
