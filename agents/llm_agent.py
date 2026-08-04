"""PR11a — the model seam and per-turn records (no graph yet).

This is the plumbing the LLM agent (PR11b) is built on, kept separate so it
carries no LangGraph dependency and no network code of its own:

- `ModelClient` — the single seam through which a real model is reached. Its
  contract encodes the two failure classes the rest of the system already knows:
  a *malformed / empty* response is a first-class value (`None`), while an
  *outage* (transport error, timeout) is raised. The agent turns `None` into a
  retry (via the referee) and lets an exception propagate to the orchestrator's
  backoff path — it never retries the network itself.
- `TokenUsage` + `CostCallback` — how a call reports what it spent, so PR7's
  `CostLedger` can be charged and a cost ceiling enforced.
- `StepRecord` — an immutable snapshot of one agent turn (prompts, reasoning,
  output, belief delta, cost). The raw material PR13's transcript is built from.

The concrete `LlmAgent` and its belief→reason→act subgraph land in PR11b; a real
API-backed `ModelClient` lands in PR14. Nothing here calls a real model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypedDict, TypeVar, cast, runtime_checkable

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict

from agents.beliefs import (
    BeliefState,
    TargetOutput,
    TextOutput,
    output_schema_for,
    to_engine_action,
)
from agents.prompts import render_prompt
from config import DEFAULT_MODEL, MemoryStrategy, cost_usd
from engine.actions import Action, ActionRequest, ActionType
from engine.visibility import ObservableState

OutputT = TypeVar("OutputT", bound=BaseModel)


class TokenUsage(BaseModel):
    """Tokens billed for one model call; fed to `cost_usd` (PR8)."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


CostCallback = Callable[[float], None]
"""Called with the USD cost of a call so the orchestrator can charge its ledger."""


def noop_cost_callback(_usd: float) -> None:
    """Default sink when no ledger is wired (e.g. offline runs)."""


@runtime_checkable
class ModelClient(Protocol):
    """The seam a real language model sits behind.

    Two calls, one per output kind the graph needs: `complete_structured` for the
    action (validated into a PR9 output schema) and `complete_text` for free-form
    reasoning. Both return `(value, usage)`. A malformed or empty response yields
    a `None` value (never an exception); a genuine outage or timeout is *raised*,
    so the orchestrator's backoff path — not the agent — decides whether to retry.
    """

    async def complete_structured(
        self, *, system: str, user: str, schema: type[OutputT]
    ) -> tuple[OutputT | None, TokenUsage]: ...

    async def complete_text(
        self, *, system: str, user: str
    ) -> tuple[str | None, TokenUsage]: ...


class StepRecord(BaseModel):
    """One agent turn, captured for the transcript (PR13).

    Holds the exact prompts sent, the model's reasoning and parsed output, the
    engine `Action` it became (before coercion), the belief before/after, and the
    tokens and cost. `output`/`action` are `None` when the model produced nothing
    usable and the turn fell through to a fallback.
    """

    model_config = ConfigDict(frozen=True)

    actor_id: str
    action_type: ActionType
    system_prompt: str
    user_prompt: str
    reasoning: str | None = None
    output: TargetOutput | TextOutput | None = None
    action: Action | None = None
    belief_before: BeliefState
    belief_after: BeliefState
    usage: TokenUsage = TokenUsage()
    cost_usd: float = 0.0


# --------------------------------------------------------------------------- #
# PR11b — the LLM agent (belief → reason → act)
# --------------------------------------------------------------------------- #
class _TurnState(TypedDict, total=False):
    """The value threaded through one run of the subgraph."""

    view: ObservableState
    request: ActionRequest
    system: str
    base_user: str
    belief: BeliefState
    reasoning: str | None
    output: TargetOutput | TextOutput | None
    action: Action | None
    usage: TokenUsage


class LlmAgent:
    """A language model as a drop-in `AgentInterface` (PR6).

    One turn is a three-node subgraph — **belief → reason → act** — run against a
    `ModelClient` seam (PR11a). The belief node folds new events into a persistent
    `BeliefState`; it is skipped when nothing is new. The reason node deliberates;
    its output feeds the act node, which emits the engine `Action`. Failures follow
    the system's two classes: a malformed model reply becomes `None` (the
    orchestrator retries then the referee coerces a fallback); an outage is raised
    from the `ModelClient` and propagates — the agent never retries the network.
    """

    def __init__(
        self,
        client: ModelClient,
        *,
        model: str = DEFAULT_MODEL,
        memory_strategy: MemoryStrategy = MemoryStrategy.FULL_TRANSCRIPT,
        recent_events_count: int = 20,
        cost_callback: CostCallback = noop_cost_callback,
    ) -> None:
        self._client = client
        self._model = model
        self._strategy = memory_strategy
        self._recent = recent_events_count
        self._cost_callback = cost_callback
        self._belief = BeliefState()
        self.step_records: list[StepRecord] = []
        self._graph = self._build_graph()

    # -- properties -------------------------------------------------------- #
    @property
    def belief(self) -> BeliefState:
        """The agent's current, persisted beliefs (updated after each turn)."""

        return self._belief

    # -- cost -------------------------------------------------------------- #
    def _charge(self, usage: TokenUsage) -> None:
        cost = cost_usd(self._model, usage.input_tokens, usage.output_tokens)
        self._cost_callback(cost)

    # -- graph ------------------------------------------------------------- #
    def _build_graph(self) -> Any:
        g = StateGraph(_TurnState)
        g.add_node("belief", self._belief_node)
        g.add_node("reason", self._reason_node)
        g.add_node("act", self._act_node)
        # Empty-delta skip: no new events → bypass the belief node entirely.
        g.add_conditional_edges(
            START, self._route_start, {"belief": "belief", "reason": "reason"}
        )
        g.add_edge("belief", "reason")
        g.add_edge("reason", "act")
        g.add_edge("act", END)
        return g.compile()

    def _route_start(self, state: _TurnState) -> str:
        return "belief" if self._new_events(state) else "reason"

    @staticmethod
    def _new_events(state: _TurnState) -> bool:
        cursor = state["belief"].cursor
        events = state["view"].events
        return any(cursor is None or e.event_id > cursor for e in events)

    @staticmethod
    def _latest_event_id(view: ObservableState, fallback: int | None) -> int | None:
        return view.events[-1].event_id if view.events else fallback

    async def _belief_node(self, state: _TurnState) -> _TurnState:
        belief_in = state["belief"]
        prompt = (
            f"{state['base_user']}\n\nBefore acting, update your private notes to "
            "reflect anything new. Return your complete updated belief state."
        )
        updated, usage = await self._client.complete_structured(
            system=state["system"], user=prompt, schema=BeliefState
        )
        self._charge(usage)
        latest = self._latest_event_id(state["view"], belief_in.cursor)
        base = updated if updated is not None else belief_in
        belief = base.model_copy(update={"cursor": latest})
        return {"belief": belief, "usage": state.get("usage", TokenUsage()) + usage}

    async def _reason_node(self, state: _TurnState) -> _TurnState:
        prompt = (
            f"{state['base_user']}\n\nThink through your move privately given your "
            "notes. Respond with your reasoning only — do not state an action yet."
        )
        reasoning, usage = await self._client.complete_text(
            system=state["system"], user=prompt
        )
        self._charge(usage)
        return {
            "reasoning": reasoning,
            "usage": state.get("usage", TokenUsage()) + usage,
        }

    async def _act_node(self, state: _TurnState) -> _TurnState:
        request = state["request"]
        reasoning = state.get("reasoning") or "(no reasoning recorded)"
        prompt = (
            f"{state['base_user']}\n\nYour private reasoning: {reasoning}\n\n"
            "Now produce your action."
        )
        schema = output_schema_for(request)
        output, usage = await self._client.complete_structured(
            system=state["system"], user=prompt, schema=schema
        )
        self._charge(usage)
        action: Action | None = None
        if output is not None:
            action, _reason = to_engine_action(output.model_dump(), request)
        return {
            "output": output,
            "action": action,
            "usage": state.get("usage", TokenUsage()) + usage,
        }

    # -- AgentInterface ---------------------------------------------------- #
    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        rendered = render_prompt(
            view,
            request,
            strategy=self._strategy,
            belief=self._belief,
            recent_events_count=self._recent,
        )
        belief_before = self._belief
        initial: _TurnState = {
            "view": view,
            "request": request,
            "system": rendered.system,
            "base_user": rendered.user,
            "belief": belief_before,
            "reasoning": None,
            "output": None,
            "action": None,
            "usage": TokenUsage(),
        }
        # An outage raised by the ModelClient propagates here by design.
        final = cast(_TurnState, await self._graph.ainvoke(initial))

        self._belief = final.get("belief", belief_before)
        usage = final.get("usage", TokenUsage())
        self.step_records.append(
            StepRecord(
                actor_id=request.actor_id,
                action_type=request.action_type,
                system_prompt=rendered.system,
                user_prompt=rendered.user,
                reasoning=final.get("reasoning"),
                output=final.get("output"),
                action=final.get("action"),
                belief_before=belief_before,
                belief_after=self._belief,
                usage=usage,
                cost_usd=cost_usd(
                    self._model, usage.input_tokens, usage.output_tokens
                ),
            )
        )
        return final.get("action")
