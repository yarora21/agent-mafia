"""Exercise real Langfuse/LangChain instrumentation with a local OTel exporter."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
import json
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents import tracing
from agents.anthropic_client import AnthropicModelClient
from agents.beliefs import BeliefState
from agents.llm_agent import LlmAgent
from config import GameConfig
from scripts.play_llm import dry_run_factory, play_llm
from tests.test_llm_agent import _canned_output, _turn_with_events


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Langfuse, InMemorySpanExporter]]:
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    key = "pk-lf-test-" + uuid4().hex
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", key)
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=key, secret_key="sk-lf-offline-test", base_url="http://localhost:1",
        tracer_provider=TracerProvider(), span_exporter=exporter,
        mask_otel_spans=tracing.mask_spans,
    )
    monkeypatch.setattr(tracing, "_client", client)
    yield client, exporter
    client.shutdown()


class ToolChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": "claude-haiku-4-5"}


def reply(output: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": type(output).__name__, "args": output.model_dump(), "id": uuid4().hex,
    }], usage_metadata={"input_tokens": 12, "output_tokens": 7, "total_tokens": 19})


async def test_graph_generations_have_exact_inputs_usage_and_parents(exported: Any) -> None:
    client, exporter = exported
    _, view, req = _turn_with_events()
    model = ToolChatModel(responses=[
        reply(BeliefState(notes="test notes")),
        AIMessage(content="private reason", usage_metadata={
            "input_tokens": 12, "output_tokens": 7, "total_tokens": 19}),
        reply(_canned_output(req)),
    ])
    agent = LlmAgent(AnthropicModelClient(llm=model))
    with tracing.player_turn(session_id="game-a", metadata={"actor_id": req.actor_id}, input="turn") as turn:
        action = await agent.act(view, req)
        assert action is not None
        turn.update(output=action.model_dump())
    client.flush()
    spans = exporter.get_finished_spans()
    generations = [s for s in spans if s.attributes.get("langfuse.observation.type") == "generation"]
    assert len(generations) == 3  # No duplicate callbacks/generations.
    assert all(s.parent is not None for s in generations)
    inputs = [str(s.attributes.get("langfuse.observation.input")) for s in generations]
    assert any("update your private notes" in i for i in inputs)
    assert any("Think through your move" in i for i in inputs)
    assert any("private reason" in i and "Now produce your action" in i for i in inputs)
    for span in generations:
        assert span.attributes.get("langfuse.observation.model.name") == "claude-haiku-4-5"
        usage = json.loads(str(span.attributes["langfuse.observation.usage_details"]))
        assert usage["input"] == 12 and usage["output"] == 7
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1 and roots[0].name == "play-player-turn"
    assert not tracing.callback_config()


async def test_concurrent_turns_are_separate_traces_in_same_session(exported: Any) -> None:
    client, exporter = exported

    async def run(actor: str) -> None:
        with tracing.player_turn(session_id="same-game", metadata={"actor_id": actor}, input=actor):
            await asyncio.sleep(0)
            with tracing.observation("attempt-action"):
                await asyncio.sleep(0)

    await asyncio.gather(run("p1"), run("p2"))
    client.flush()
    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 2
    assert len({s.context.trace_id for s in roots}) == 2
    for root in roots:
        assert root.attributes["session.id"] == "same-game"
        child = next(s for s in spans if s.parent and s.parent.span_id == root.context.span_id)
        assert child.context.trace_id == root.context.trace_id


async def test_full_game_records_fallbacks_and_session(exported: Any) -> None:
    client, exporter = exported
    transcript, run = await play_llm(GameConfig(seed=0), factory=dry_run_factory())
    client.flush()
    spans = exporter.get_finished_spans()
    assert run.tracing_session_id and transcript.run.tracing_session_id == run.tracing_session_id
    assert run.fallbacks > 0
    assert sum(s.name == "fallback-action" for s in spans) == run.fallbacks
    assert any(s.name == "reject-action" for s in spans)
    assert all(s.attributes.get("session.id") == run.tracing_session_id for s in spans)


def test_masking_and_exception_capture(exported: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, exporter = exported
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-from-env")
    with pytest.raises(RuntimeError):
        with tracing.observation("failed-call", input="secret-from-env sk-ant-api03-secretvalue"):
            raise RuntimeError("simulated failure")
    client.flush()
    span = exporter.get_finished_spans()[0]
    attributes = str(dict(span.attributes))
    assert "secret-from-env" not in attributes
    assert "sk-ant-api03-secretvalue" not in attributes
    assert "[REDACTED]" in attributes
    assert span.attributes["langfuse.observation.level"] == "ERROR"


def test_disabled_and_missing_config_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    tracing.configure_tracing()
    assert not tracing.enabled()
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    tracing.configure_tracing()
    assert not tracing.enabled()
    with tracing.player_turn(session_id=None, metadata={}, input="test") as turn:
        turn.update(output="test")
        assert not tracing.callback_config()


@pytest.mark.parametrize("timeout", [False, True])
async def test_outages_and_timeouts_preserve_attempts_and_fallbacks(exported: Any, timeout: bool) -> None:
    from agents.interface import AgentInterface
    from engine.actions import Action, ActionRequest
    from engine.state import NARRATOR_ID, VotingMode
    from engine.visibility import ObservableState
    from orchestrator import ReliabilityConfig, build_random_agents, play_game

    class FailingAgent:
        async def act(self, view: ObservableState, request: ActionRequest) -> Action | None:
            if timeout:
                await asyncio.sleep(1)
                return None
            raise RuntimeError("simulated outage")

    client, exporter = exported
    agents: dict[str, AgentInterface] = build_random_agents(0)
    agents[NARRATOR_ID] = FailingAgent()
    run = await play_game(agents, seed=0, mode=VotingMode.SIMULTANEOUS,
                          reliability=ReliabilityConfig(api_retries=1, timeout_seconds=0.01))
    client.flush()
    spans = exporter.get_finished_spans()
    errors = [s for s in spans if s.name == "attempt-action"
              and s.attributes.get("langfuse.observation.level") == "ERROR"]
    assert run.api_retries > 0 and run.fallbacks > 0
    assert len(errors) == 2 * run.api_retries
    assert all(s.parent is not None for s in errors)
    assert not tracing.callback_config()
