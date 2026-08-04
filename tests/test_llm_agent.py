"""PR11b gate — the LLM agent's belief→reason→act logic, against a stub model.

No real API (that is PR14). A `_StubClient` records its calls and returns
canned outputs, so every property is deterministic and instant:
- node order is belief → reason → act;
- an empty event delta skips the belief node;
- beliefs persist across turns;
- a malformed action becomes `None` (the orchestrator's retry path);
- a model exception propagates (no internal retry);
- cost is reported per call and captured in the `StepRecord`.
"""

from __future__ import annotations

from typing import TypeVar, cast

from pydantic import BaseModel

from agents.beliefs import BeliefState, TargetOutput, TextOutput
from agents.interface import AgentInterface
from agents.llm_agent import LlmAgent, TokenUsage
from agents.mock_agent import SimulatedOutage
from config import DEFAULT_MODEL, cost_usd
from engine.actions import ActionRequest, ActionType
from engine.engine import apply, next_requests, resolve_automatic
from engine.referee import fallback_action
from engine.state import GameState, Phase, VotingMode, new_game
from engine.visibility import ObservableState, observable_view

TARGET_TYPES = {ActionType.VOTE, ActionType.NIGHT_KILL, ActionType.INVESTIGATE}
_USAGE = TokenUsage(input_tokens=100, output_tokens=50)
_S = TypeVar("_S", bound=BaseModel)


# --------------------------------------------------------------------------- #
# Stub model client
# --------------------------------------------------------------------------- #
class _StubClient:
    """A ModelClient that records calls and returns canned values."""

    def __init__(
        self,
        *,
        belief_out: BeliefState | None,
        reasoning: str | None,
        action_out: TargetOutput | TextOutput | None,
        raise_on: str | None = None,  # "belief" | "reason" | "act"
    ) -> None:
        self.belief_out = belief_out
        self.reasoning = reasoning
        self.action_out = action_out
        self.raise_on = raise_on
        self.calls: list[str] = []

    async def complete_structured(
        self, *, system: str, user: str, schema: type[_S]
    ) -> tuple[_S | None, TokenUsage]:
        if schema is BeliefState:
            self.calls.append("belief")
            if self.raise_on == "belief":
                raise SimulatedOutage("belief outage")
            return cast("_S | None", self.belief_out), _USAGE
        self.calls.append("act")
        if self.raise_on == "act":
            raise SimulatedOutage("act outage")
        return cast("_S | None", self.action_out), _USAGE

    async def complete_text(
        self, *, system: str, user: str
    ) -> tuple[str | None, TokenUsage]:
        self.calls.append("reason")
        if self.raise_on == "reason":
            raise SimulatedOutage("reason outage")
        return self.reasoning, _USAGE


# --------------------------------------------------------------------------- #
# Fixtures: a real (view, request) with a matching canned output
# --------------------------------------------------------------------------- #
def _turn_with_events() -> tuple[GameState, ObservableState, ActionRequest]:
    """Drive to the first turn whose acting player already sees ≥1 event.

    A fresh game has an empty history, so turn one has an empty delta by design
    (the belief node correctly skips). To exercise belief→reason→act we need a
    state where the actor's view is non-empty.
    """

    state = new_game(seed=3, mode=VotingMode.SEQUENTIAL)
    for _ in range(1000):
        state = resolve_automatic(state)
        req = next_requests(state)[0]
        view = observable_view(state, req.actor_id)
        if view.events:
            return state, view, req
        state = apply(state, fallback_action(state, req))
    raise AssertionError("no turn with events found")


def _canned_output(req: ActionRequest) -> TargetOutput | TextOutput:
    if req.action_type in TARGET_TYPES:
        return TargetOutput(target_id=req.legal_targets[0])
    return TextOutput(message="a short line")


def _full_stub(req: ActionRequest, **kw: object) -> _StubClient:
    return _StubClient(
        belief_out=BeliefState(notes="updated", suspicions={"p2": 0.7}),
        reasoning="I will play it safe.",
        action_out=_canned_output(req),
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_is_agent_interface() -> None:
    agent = LlmAgent(_full_stub(_turn_with_events()[2]))
    assert isinstance(agent, AgentInterface)


async def test_node_order_belief_reason_act() -> None:
    _, view, req = _turn_with_events()
    stub = _full_stub(req)
    agent = LlmAgent(stub)
    action = await agent.act(view, req)
    assert stub.calls == ["belief", "reason", "act"]
    assert action is not None
    assert action.actor_id == req.actor_id


async def test_empty_delta_skips_belief_node() -> None:
    _, view, req = _turn_with_events()
    stub = _full_stub(req)
    agent = LlmAgent(stub)
    # Pretend the agent has already seen every event: cursor past the last id.
    agent._belief = BeliefState(cursor=10**9)
    await agent.act(view, req)
    assert stub.calls == ["reason", "act"]  # belief node bypassed


async def test_beliefs_persist_across_turns() -> None:
    state, view, req = _turn_with_events()
    stub = _full_stub(req)
    agent = LlmAgent(stub)

    await agent.act(view, req)
    after_turn1 = agent.belief
    assert after_turn1.notes == "updated"
    assert after_turn1.cursor is not None  # cursor advanced to newest event

    # Drive one legal action, take the next turn; belief must carry over.
    state = apply(state, fallback_action(state, req))
    state = resolve_automatic(state)
    req2 = next_requests(state)[0]
    view2 = observable_view(state, req2.actor_id)
    stub.action_out = _canned_output(req2)
    await agent.act(view2, req2)

    assert agent.step_records[1].belief_before == after_turn1


async def test_malformed_action_returns_none() -> None:
    _, view, req = _turn_with_events()
    stub = _full_stub(req)
    stub.action_out = None  # model produced nothing usable for the action
    agent = LlmAgent(stub)
    action = await agent.act(view, req)
    assert action is None
    assert stub.calls == ["belief", "reason", "act"]  # all nodes still ran
    assert agent.step_records[0].action is None


async def test_model_exception_propagates() -> None:
    _, view, req = _turn_with_events()
    stub = _full_stub(req, raise_on="reason")
    agent = LlmAgent(stub)
    raised = False
    try:
        await agent.act(view, req)
    except SimulatedOutage:
        raised = True
    assert raised  # no internal retry, no swallowing


async def test_cost_reported_and_recorded() -> None:
    _, view, req = _turn_with_events()
    stub = _full_stub(req)
    charged: list[float] = []
    agent = LlmAgent(stub, cost_callback=charged.append)
    await agent.act(view, req)

    per_call = cost_usd(DEFAULT_MODEL, _USAGE.input_tokens, _USAGE.output_tokens)
    assert len(charged) == 3  # one per node
    assert sum(charged) == cost_usd(DEFAULT_MODEL, 300, 150)  # 3× tokens, linear
    record = agent.step_records[0]
    assert record.cost_usd == sum(charged)
    assert record.usage == TokenUsage(input_tokens=300, output_tokens=150)
    assert per_call > 0


async def test_step_record_is_populated() -> None:
    _, view, req = _turn_with_events()
    agent = LlmAgent(_full_stub(req))
    await agent.act(view, req)
    rec = agent.step_records[0]
    assert rec.actor_id == req.actor_id
    assert rec.action_type is req.action_type
    assert rec.system_prompt and rec.user_prompt
    assert rec.reasoning == "I will play it safe."
    assert rec.action is not None
    assert rec.belief_after.notes == "updated"
