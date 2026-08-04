"""PR14a gate — the AnthropicModelClient honours the ModelClient contract, offline.

No network: a `_FakeChatModel` stands in for `ChatAnthropic` via the client's
`llm=` injection seam. It proves the two failure classes map correctly —
- a validating structured reply → the parsed schema instance,
- a non-validating reply → `None` (never an exception, thanks to include_raw),
- a transport error → *raised* (outage), not swallowed —
plus token-usage extraction and text flattening. The orchestrator's shared-ledger
wiring (agent-reported spend gates the game) is exercised too.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from agents.anthropic_client import AnthropicModelClient
from agents.beliefs import TargetOutput, TextOutput
from agents.interface import AgentInterface
from agents.llm_agent import ModelClient, TokenUsage
from engine.actions import Action, ActionRequest
from engine.state import NARRATOR_ID, NUM_PLAYERS, VotingMode
from engine.visibility import ObservableState
from orchestrator import CostLedger, ReliabilityConfig, play_game

_USAGE_META = {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}


# --------------------------------------------------------------------------- #
# A fake chat model (no network) implementing the two surfaces the client uses
# --------------------------------------------------------------------------- #
class _FakeStructured:
    def __init__(self, parsed: BaseModel | None, *, raise_outage: bool) -> None:
        self._parsed = parsed
        self._raise = raise_outage

    async def ainvoke(self, _messages: list[BaseMessage]) -> dict[str, Any]:
        if self._raise:
            raise RuntimeError("simulated API outage")
        raw = AIMessage(content="raw", usage_metadata=_USAGE_META)
        return {"raw": raw, "parsed": self._parsed, "parsing_error": None}


class _FakeChatModel:
    """Enough of a BaseChatModel for the client: structured + text, both async."""

    def __init__(
        self,
        *,
        parsed: BaseModel | None = None,
        text: str | list[Any] = "some reasoning",
        raise_outage: bool = False,
    ) -> None:
        self._parsed = parsed
        self._text = text
        self._raise = raise_outage

    def with_structured_output(
        self, _schema: type[BaseModel], *, include_raw: bool = False
    ) -> _FakeStructured:
        assert include_raw is True  # the client must not let a parse error raise
        return _FakeStructured(self._parsed, raise_outage=self._raise)

    async def ainvoke(self, _messages: list[BaseMessage]) -> AIMessage:
        if self._raise:
            raise RuntimeError("simulated API outage")
        return AIMessage(content=self._text, usage_metadata=_USAGE_META)


def _client(**kw: Any) -> AnthropicModelClient:
    return AnthropicModelClient(llm=cast(BaseChatModel, _FakeChatModel(**kw)))


# --------------------------------------------------------------------------- #
# Contract tests
# --------------------------------------------------------------------------- #
def test_is_model_client() -> None:
    assert isinstance(_client(), ModelClient)


async def test_structured_parses_and_reports_usage() -> None:
    parsed = TargetOutput(target_id="p3")
    out, usage = await _client(parsed=parsed).complete_structured(
        system="s", user="u", schema=TargetOutput
    )
    assert out == parsed
    assert usage == TokenUsage(input_tokens=12, output_tokens=7)


async def test_structured_malformed_is_none_not_exception() -> None:
    out, usage = await _client(parsed=None).complete_structured(
        system="s", user="u", schema=TextOutput
    )
    assert out is None  # include_raw routed the parse failure to None
    assert usage == TokenUsage(input_tokens=12, output_tokens=7)


async def test_structured_outage_propagates() -> None:
    raised = False
    try:
        await _client(raise_outage=True).complete_structured(
            system="s", user="u", schema=TargetOutput
        )
    except RuntimeError:
        raised = True
    assert raised


async def test_text_completes_and_reports_usage() -> None:
    text, usage = await _client(text="I think p2 is Mafia.").complete_text(
        system="s", user="u"
    )
    assert text == "I think p2 is Mafia."
    assert usage == TokenUsage(input_tokens=12, output_tokens=7)


async def test_text_flattens_content_blocks() -> None:
    blocks = [{"type": "text", "text": "part-a "}, {"type": "text", "text": "part-b"}]
    text, _ = await _client(text=blocks).complete_text(system="s", user="u")
    assert text == "part-a part-b"


async def test_text_empty_is_none() -> None:
    text, _ = await _client(text="   ").complete_text(system="s", user="u")
    assert text is None  # whitespace-only is "nothing usable"


async def test_text_outage_propagates() -> None:
    raised = False
    try:
        await _client(raise_outage=True).complete_text(system="s", user="u")
    except RuntimeError:
        raised = True
    assert raised


# --------------------------------------------------------------------------- #
# Orchestrator shared-ledger wiring: agent-reported spend gates the game
# --------------------------------------------------------------------------- #
class _ChargingAgent:
    """A trivial agent that charges a shared ledger on every turn, then no-ops."""

    def __init__(self, ledger: CostLedger, amount: float) -> None:
        self._ledger = ledger
        self._amount = amount

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        self._ledger.charge(request.actor_id, self._amount)
        return None  # illegal-ish → referee coerces a legal fallback; game proceeds


async def test_shared_ledger_gates_the_game() -> None:
    ledger = CostLedger()
    agents: dict[str, AgentInterface] = {
        f"p{i}": _ChargingAgent(ledger, 1.0) for i in range(NUM_PLAYERS)
    }
    agents[NARRATOR_ID] = _ChargingAgent(ledger, 1.0)

    cfg = ReliabilityConfig(
        malformed_retries=0, api_retries=0, timeout_seconds=1.0, cost_ceiling=3.0
    )
    result = await play_game(
        agents, seed=1, mode=VotingMode.SIMULTANEOUS, reliability=cfg, cost_ledger=ledger
    )

    assert result.cost is ledger  # the orchestrator used the injected ledger
    assert result.aborted is True  # agent-reported spend crossed the ceiling
    assert result.abort_reason is not None and "cost ceiling" in result.abort_reason
    assert ledger.total > 3.0


def test_asyncio_smoke() -> None:
    # A plain (non-pytest-asyncio) sanity call, proving the client runs standalone.
    out, _ = asyncio.run(
        _client(parsed=TextOutput(message="hi")).complete_structured(
            system="s", user="u", schema=TextOutput
        )
    )
    assert isinstance(out, TextOutput)
