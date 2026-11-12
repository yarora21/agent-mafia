"""The orchestrator — the async loop that wires agents to the pure engine.

This is the one seam where the outside world meets the engine. It lives OUTSIDE
`engine/` on purpose: it holds agent instances and awaits their (eventually
LLM-backed) `act` calls, so the engine can stay pure and import no LLM. The loop
itself is tiny:

    resolve_automatic -> next_requests -> observable_view -> agent.act
    -> coerce_action -> apply    (repeat until GAME_OVER)

It consumes the two boundaries built earlier: every agent is handed only its
`observable_view` (PR4), and every result — including a `None` from a misbehaving
agent — is passed through `referee.coerce_action` (PR5) so the game can never
stall. Simultaneous votes are fanned out with `asyncio.gather`: all living voters
are asked against the *same* pre-vote state, which is safe precisely because that
view never exposes in-flight ballots.

Reliability hardening (retries, timeouts, backoff, cost ceiling, mock agents) is
deliberately out of scope here — that is PR7. This PR assumes agents behave; the
random agent always does.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from agents import tracing
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from agents.interface import AgentInterface
from agents.random_agent import RandomAgent
from engine.actions import Action, ActionRequest
from engine.engine import apply, next_requests, resolve_automatic, winner
from engine.referee import coerce_action, validate_action
from engine.state import (
    NARRATOR_ID,
    NUM_PLAYERS,
    GameState,
    Phase,
    VotingMode,
    new_game,
)
from engine.visibility import ObservableState, observable_view

_GUARD_LIMIT = 1000
"""Hard cap on loop iterations; a well-formed game terminates far sooner."""


@dataclass
class ReliabilityConfig:
    """Tunables for the failure-handling machinery (PR8's `GameConfig` supersedes
    this later). The two failure classes are kept deliberately distinct.

    - Malformed / illegal (the agent responded, but the answer is invalid):
      `malformed_retries` re-solicits (a real LLM would get the validation reason
      appended), then falls back.
    - API failure / timeout (the agent raised or never returned): `api_retries`
      attempts with exponential backoff (`backoff_base * 2**attempt`), each call
      bounded by `timeout_seconds`, before falling back.
    """

    malformed_retries: int = 1
    api_retries: int = 3
    backoff_base: float = 0.0
    timeout_seconds: float = 5.0
    cost_per_call: float = 0.0
    cost_ceiling: float | None = None


@dataclass
class CostLedger:
    """Accumulated spend, keyed by actor id (the Narrator bills separately).

    PR7 uses a flat `cost_per_call`; PR8 swaps in real `MODEL_PRICING`. The ledger
    mechanism — charge, total, ceiling breach — is what matters here.
    """

    per_actor: dict[str, float] = field(default_factory=dict)

    def charge(self, actor_id: str, amount: float) -> None:
        self.per_actor[actor_id] = self.per_actor.get(actor_id, 0.0) + amount

    @property
    def total(self) -> float:
        return sum(self.per_actor.values())


@dataclass
class RunResult:
    """The outcome of one game run.

    `actions` is every agent-driven action in application order (the order the
    engine folded them in) — the foundation the transcript (PR13) builds on.
    `cursors` is each actor's last-seen `event_id`, the per-agent event cursor the
    `beliefs_only` memory strategy (PR9) will ride on. The reliability counters
    (`retried_ok`, `fallbacks`, `api_retries`) and `aborted`/`abort_reason` record
    what the failure machinery did, ready for the transcript.
    """

    final_state: GameState
    tracing_session_id: str | None = None
    actions: list[Action] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    cost: CostLedger = field(default_factory=CostLedger)
    retried_ok: int = 0
    fallbacks: int = 0
    api_retries: int = 0
    aborted: bool = False
    abort_reason: str | None = None


def build_random_agents(seed: int) -> dict[str, AgentInterface]:
    """One `RandomAgent` per player id (``p0``..) plus the Narrator.

    Each agent is seeded deterministically off `seed`, so a game wired from this
    map replays identically for a fixed `(seed, mode)`.
    """

    agents: dict[str, AgentInterface] = {
        f"p{i}": RandomAgent(seed=seed * 1000 + i) for i in range(NUM_PLAYERS)
    }
    agents[NARRATOR_ID] = RandomAgent(seed=seed * 1000 + NUM_PLAYERS)
    return agents


async def play_game(
    agents: Mapping[str, AgentInterface],
    *,
    seed: int,
    mode: VotingMode,
    reliability: ReliabilityConfig | None = None,
    cost_ledger: CostLedger | None = None,
    on_action: Callable[[Action, GameState], Awaitable[None]] | None = None,
) -> RunResult:
    """Play one full game to `GAME_OVER` (or a recorded abort), returning the run.

    `agents` must supply an entry for every player id and for `NARRATOR_ID`. Every
    agent call is hardened by `reliability`: malformed/illegal answers are retried
    then coerced to a legal fallback; outages/timeouts are retried with backoff
    then coerced; a cost-ceiling breach aborts the game and records why. So no
    agent — hanging, crashing, or lying — can stall or corrupt the game.

    `cost_ledger` lets the caller share one ledger with the agents (PR14): real
    LLM agents charge it through their own cost callbacks, and the ceiling check
    here reads the *same* object, so agent-reported spend actually gates the game.
    When omitted, a fresh ledger is used and only `reliability.cost_per_call` (the
    PR7 flat stopgap) charges it.
    """

    cfg = reliability or ReliabilityConfig()
    state = new_game(seed=seed, mode=mode)
    result = RunResult(
        final_state=state,
        tracing_session_id=uuid4().hex if tracing.enabled() else None,
    )
    if cost_ledger is not None:
        result.cost = cost_ledger
    result.cursors = {actor: -1 for actor in agents}

    async def call_with_backoff(
        agent: AgentInterface, view: ObservableState, req: ActionRequest
    ) -> tuple[Action | None, bool]:
        """Return (response, outage). `outage` is True once retries are exhausted
        on a raise/timeout; otherwise the agent's response (possibly None)."""

        attempt = 0
        while True:
            try:
                with tracing.observation(
                    "attempt-action", input={"actor_id": req.actor_id},
                    metadata={"api_attempt": attempt + 1},
                ) as attempt_span:
                    action = await asyncio.wait_for(
                        agent.act(view, req), cfg.timeout_seconds
                    )
                    attempt_span.update(output=action.model_dump(mode="json") if action else None)
                return action, False
            except Exception:  # outage or timeout — a non-response, not a bad answer
                if attempt >= cfg.api_retries:
                    return None, True
                result.api_retries += 1
                if cfg.backoff_base:
                    await asyncio.sleep(cfg.backoff_base * (2**attempt))
                attempt += 1

    async def _solicit(at_state: GameState, req: ActionRequest) -> Action:
        view = observable_view(at_state, req.actor_id)
        if view.events:
            result.cursors[req.actor_id] = view.events[-1].event_id
        agent = agents[req.actor_id]
        result.cost.charge(req.actor_id, cfg.cost_per_call)

        malformed_attempts = 0
        while True:
            action, outage = await call_with_backoff(agent, view, req)
            if outage:
                result.fallbacks += 1
                with tracing.observation("fallback-action", input={"reason": "api_retries_exhausted"}) as fallback:
                    action = coerce_action(at_state, req, None)
                    fallback.update(output=action.model_dump(mode="json"), level="WARNING")
                    return action
            legal = action is not None and validate_action(at_state, req, action) is None
            if legal:
                assert action is not None
                if malformed_attempts > 0:
                    result.retried_ok += 1
                return action
            with tracing.observation(
                "reject-action", input=action.model_dump(mode="json") if action else None,
            ) as rejected:
                rejected.update(level="WARNING", output={
                    "reason": validate_action(at_state, req, action) if action else "empty or malformed output",
                    "malformed_attempt": malformed_attempts + 1,
                })
            malformed_attempts += 1
            if malformed_attempts <= cfg.malformed_retries:
                continue  # re-solicit; a real LLM gets the validation reason back
            result.fallbacks += 1
            with tracing.observation("fallback-action", input={"reason": "malformed_retries_exhausted"}) as fallback:
                action = coerce_action(at_state, req, None)
                fallback.update(output=action.model_dump(mode="json"), level="WARNING")
                return action

    async def solicit(at_state: GameState, req: ActionRequest) -> Action:
        view = observable_view(at_state, req.actor_id)
        with tracing.player_turn(
            session_id=result.tracing_session_id,
            metadata={"actor_id": req.actor_id, "day": at_state.day_number,
                      "phase": at_state.phase.value, "seed": seed, "mode": mode.value,
                      "action_type": req.action_type.value},
            input={"request": req.model_dump(mode="json"), "view": view.model_dump(mode="json")},
        ) as turn:
            action = await _solicit(at_state, req)
            turn.update(output=action.model_dump(mode="json"))
            return action

    guard = 0
    while state.phase is not Phase.GAME_OVER:
        guard += 1
        if guard > _GUARD_LIMIT:
            raise RuntimeError("game failed to terminate within the guard limit")

        advanced = resolve_automatic(state)
        if advanced is not state:
            state = advanced
            continue

        reqs = next_requests(state)
        if not reqs:
            continue

        # One request in every phase except a simultaneous vote, where all living
        # voters are solicited concurrently against the same pre-vote state.
        solicit_state = state
        solicited = await asyncio.gather(
            *(solicit(solicit_state, req) for req in reqs)
        )

        if cfg.cost_ceiling is not None and result.cost.total > cfg.cost_ceiling:
            result.aborted = True
            result.abort_reason = (
                f"cost ceiling {cfg.cost_ceiling} exceeded "
                f"(spent {result.cost.total})"
            )
            break

        for req, action in zip(reqs, solicited):
            state = apply(state, action)
            result.actions.append(action)
            if on_action is not None:
                # Live spectator hook: emit each applied action + the state it
                # produced, so a caller can stream the game as it plays.
                await on_action(action, state)
            if state.phase is Phase.GAME_OVER:
                break

    result.final_state = state
    tracing.record_game_summary(result.tracing_session_id, {
        "winner": victor.value if (victor := winner(state)) else None,
        "aborted": result.aborted, "abort_reason": result.abort_reason,
        "actions": len(result.actions), "fallbacks": result.fallbacks,
        "api_retries": result.api_retries, "cost_usd": result.cost.total,
    })
    return result


async def play_random_game(
    seed: int,
    mode: VotingMode,
    reliability: ReliabilityConfig | None = None,
) -> RunResult:
    """Convenience: play a full game with random agents seeded off `seed`."""

    return await play_game(
        build_random_agents(seed), seed=seed, mode=mode, reliability=reliability
    )
