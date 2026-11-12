"""PR13 — the transcript: one self-proving JSON record per game.

A transcript is the single source of truth for the replay UI (PR15) and any later
analysis, so its shape is a contract. It bundles the *inputs* that determine a game
(`GameConfig` — seed, voting mode, prompt version) with the *moves* actually
applied (`ActionRecord`s, in application order) and the *result* they produced
(the full final `GameState`, including the whole event history with visibility
tags and `event_id`s intact).

"Self-proving" is the point of `replay_check`: because the engine is pure and
deterministic, re-applying the recorded actions through it must reproduce the
recorded final state exactly. A transcript that fails that check has been corrupted
or tampered with. Replaying re-runs the *engine*, never the agents — the raw model
outputs are stored verbatim (`NodeOutputs`, populated at PR14) so a replay never
calls the API.

This module is not part of `engine/` — it may import agent-facing shapes — but it
still touches no network and no LLM. It reads a finished `RunResult` (PR7) and a
`GameConfig` (PR8); it does not drive games.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from agents.beliefs import BeliefState, TargetOutput, TextOutput
from config import GameConfig
from engine.actions import Action, ActionType
from engine.engine import apply, next_requests, resolve_automatic, winner
from engine.state import Event, Faction, GameState, Phase, new_game
from orchestrator import RunResult

if TYPE_CHECKING:  # avoid a runtime import of llm_agent (and its LangGraph dep)
    from agents.llm_agent import StepRecord

_REPLAY_GUARD = 1000
"""Same hard iteration cap the orchestrator uses; a real game halts far sooner."""


# --------------------------------------------------------------------------- #
# Per-action records
# --------------------------------------------------------------------------- #
class NodeOutputs(BaseModel):
    """The raw model outputs behind one LLM turn, stored verbatim.

    Mirrors `llm_agent.StepRecord` without pulling in its LangGraph dependency:
    the exact prompts, the reasoning, the parsed output, the belief before/after,
    and the tokens/cost. `None`/absent for fallback and non-LLM (random, mock)
    turns. Populated when the real agent runs (PR14); defined here so the
    transcript shape is fixed before the API is switched on.
    """

    model_config = ConfigDict(frozen=True)

    action_type: ActionType | None = None  # what this turn was asked to produce
    system_prompt: str
    user_prompt: str
    reasoning: str | None = None
    output: TargetOutput | TextOutput | None = None
    belief_before: BeliefState = BeliefState()
    belief_after: BeliefState = BeliefState()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @classmethod
    def from_step_record(cls, step: StepRecord) -> NodeOutputs:
        """Project a PR11 `StepRecord` onto the transcript's storage shape."""

        return cls(
            action_type=step.action_type,
            system_prompt=step.system_prompt,
            user_prompt=step.user_prompt,
            reasoning=step.reasoning,
            output=step.output,
            belief_before=step.belief_before,
            belief_after=step.belief_after,
            input_tokens=step.usage.input_tokens,
            output_tokens=step.usage.output_tokens,
            cost_usd=step.cost_usd,
        )


class ActionRecord(BaseModel):
    """One applied action plus (optionally) the model outputs that produced it.

    `action` is the engine `Action` actually folded into the state — a real move,
    a retried move, or a coerced fallback. It is all `replay_check` needs;
    `node_outputs` is the private detail the X-ray replay (PR15) reads.
    """

    model_config = ConfigDict(frozen=True)

    action: Action
    node_outputs: NodeOutputs | None = None


# --------------------------------------------------------------------------- #
# Run-level reliability + cost summary
# --------------------------------------------------------------------------- #
class RunSummary(BaseModel):
    """What the reliability and cost machinery (PR7) did over the whole run."""

    model_config = ConfigDict(frozen=True)

    tracing_session_id: str | None = None
    retried_ok: int = 0
    fallbacks: int = 0
    api_retries: int = 0
    aborted: bool = False
    abort_reason: str | None = None
    cost_per_actor: dict[str, float] = Field(default_factory=dict)
    cursors: dict[str, int] = Field(default_factory=dict)

    @property
    def total_cost_usd(self) -> float:
        return sum(self.cost_per_actor.values())


# --------------------------------------------------------------------------- #
# The transcript
# --------------------------------------------------------------------------- #
class Transcript(BaseModel):
    """A complete, self-proving record of one game.

    `config` is the seed of the game; `actions` are the moves in application order;
    `final_state` is the authoritative end state (its `history` is the full event
    log). `replay_check` re-derives `final_state` from `config` + `actions`.
    """

    model_config = ConfigDict(frozen=True)

    config: GameConfig
    actions: list[ActionRecord] = Field(default_factory=list)
    final_state: GameState
    winner: Faction | None = None
    run: RunSummary = Field(default_factory=RunSummary)
    reasoning: dict[str, list[NodeOutputs]] = Field(default_factory=dict)
    """Each actor's private turns in order (belief/reasoning/output per turn). The
    X-ray replay (PR15) dequeues these per `(actor, action_type)` to sit private
    scheming beside the public record. Empty for non-LLM (random/mock) agents."""

    @property
    def history(self) -> list[Event]:
        """The full event history (visibility tags + event_ids intact)."""

        return self.final_state.history


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_transcript(
    config: GameConfig,
    run: RunResult,
    *,
    step_records: Mapping[str, Sequence[StepRecord]] | None = None,
) -> Transcript:
    """Fold a finished `RunResult` (PR7) and its `GameConfig` (PR8) into a record.

    `config.seed` / `config.voting_mode` must match how the game was played — they
    are what `replay_check` re-deals from. Applied actions are recorded bare (all
    replay needs); `step_records` (per actor, from each `LlmAgent`) is projected
    into the `reasoning` stream so the X-ray replay can show private deliberation.
    """

    reasoning: dict[str, list[NodeOutputs]] = {}
    if step_records is not None:
        for actor_id, steps in step_records.items():
            if steps:
                reasoning[actor_id] = [
                    NodeOutputs.from_step_record(s) for s in steps
                ]

    return Transcript(
        config=config,
        actions=[ActionRecord(action=a) for a in run.actions],
        final_state=run.final_state,
        winner=winner(run.final_state),
        run=RunSummary(
            tracing_session_id=run.tracing_session_id,
            retried_ok=run.retried_ok,
            fallbacks=run.fallbacks,
            api_retries=run.api_retries,
            aborted=run.aborted,
            abort_reason=run.abort_reason,
            cost_per_actor=dict(run.cost.per_actor),
            cursors=dict(run.cursors),
        ),
        reasoning=reasoning,
    )


# --------------------------------------------------------------------------- #
# Write / load
# --------------------------------------------------------------------------- #
def _timestamp() -> str:
    """A filesystem-safe UTC stamp, e.g. ``20260804T010203Z``."""

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_transcript(
    transcript: Transcript,
    directory: str | Path = "games",
    *,
    timestamp: str | None = None,
) -> Path:
    """Write `transcript` to ``{directory}/{ts}_{seed}_{mode}.json`` and return it.

    `games/` is git-ignored (PR1). `timestamp` is injectable so tests get a stable
    filename; otherwise a UTC stamp is generated.
    """

    ts = timestamp if timestamp is not None else _timestamp()
    cfg = transcript.config
    path = Path(directory) / f"{ts}_{cfg.seed}_{cfg.voting_mode.value}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcript.model_dump_json(indent=2))
    return path


def load_transcript(path: str | Path) -> Transcript:
    """Load a transcript from disk. The inverse of `write_transcript`."""

    return Transcript.model_validate_json(Path(path).read_text())


# --------------------------------------------------------------------------- #
# The self-proof
# --------------------------------------------------------------------------- #
def replay_check(transcript: Transcript) -> bool:
    """Re-apply the recorded actions through the engine; assert the state matches.

    Mirrors the orchestrator's loop (`resolve_automatic` → `next_requests` →
    `apply`) but feeds the recorded actions instead of calling agents. Returns
    `True` iff every recorded action was consumed and the reconstructed state
    equals `transcript.final_state` — the transcript proving itself. A completed
    game replays to `GAME_OVER`; an aborted game replays to its stopping point
    (its final recorded action).
    """

    cfg = transcript.config
    state = new_game(seed=cfg.seed, mode=cfg.voting_mode)
    actions = [rec.action for rec in transcript.actions]
    i = 0
    guard = 0
    while state.phase is not Phase.GAME_OVER:
        guard += 1
        if guard > _REPLAY_GUARD:
            return False
        advanced = resolve_automatic(state)
        if advanced is not state:
            state = advanced
            continue
        reqs = next_requests(state)
        if not reqs:
            continue
        # An agent action is required here. If the recorded actions are spent, the
        # game was aborted: this resolved-but-unapplied state is its stopping point.
        if i >= len(actions):
            break
        for _req in reqs:
            if i >= len(actions):
                break
            state = apply(state, actions[i])
            i += 1
            if state.phase is Phase.GAME_OVER:
                break

    return i == len(actions) and state == transcript.final_state
