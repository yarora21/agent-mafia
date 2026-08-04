"""PR12 — the Narrator agent: death flavor, and nothing else.

A deliberately degenerate `AgentInterface`. Unlike `LlmAgent` it is a *separate
class*, not a configured subclass — sharing code would invite accidentally giving
the Narrator a belief scratchpad or a reasoning node it must never have. It is one
model call: render the public-only view, ask for a line, wrap it as a `Narration`.

Two invariants it exists to keep:

- **Public-only sight.** It is only ever driven from `narrator_view` (PR4), which
  filters `GameState` to public events. It never sees the killer, the mafia chat,
  or a seer result, so it cannot leak them into the flavor it invents. PR12's gate
  scans its rendered prompt bytes to prove this.
- **No memory, no reasoning.** No `BeliefState`, no reason node — there is nothing
  for a non-player to strategize about. It is pinned to the cheapest model
  (`NARRATOR_MODEL`) and its cost is charged under the separate `NARRATOR_ID`
  ledger key so player-cost numbers stay clean.

Failure follows the system's two classes: a malformed / empty model reply becomes
`None` (the orchestrator retries, then the referee coerces `CANNED_NARRATION`); an
outage is raised by the `ModelClient` and propagates — the Narrator never retries
the network itself.
"""

from __future__ import annotations

from agents.beliefs import TextOutput, to_engine_action
from agents.llm_agent import CostCallback, ModelClient, noop_cost_callback
from agents.prompts import render_prompt
from config import NARRATOR_MODEL, cost_usd
from engine.actions import Action, ActionRequest
from engine.visibility import ObservableState


class NarratorAgent:
    """A one-call `AgentInterface` (PR6) that produces death narration.

    `act` renders the public-only view, asks the model for a single line via the
    structured `TextOutput` schema, and stamps it into a `Narration` (PR9's
    `to_engine_action`, which takes `actor_id` from the request — never the model).
    No belief update, no reasoning: a Narrator turn is exactly one model call.
    """

    def __init__(
        self,
        client: ModelClient,
        *,
        model: str = NARRATOR_MODEL,
        cost_callback: CostCallback = noop_cost_callback,
    ) -> None:
        self._client = client
        self._model = model
        self._cost_callback = cost_callback

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        rendered = render_prompt(view, request)
        # An outage raised by the ModelClient propagates by design (no retry here).
        output, usage = await self._client.complete_structured(
            system=rendered.system, user=rendered.user, schema=TextOutput
        )
        self._cost_callback(
            cost_usd(self._model, usage.input_tokens, usage.output_tokens)
        )
        if output is None:
            return None  # malformed → orchestrator retry → referee CANNED_NARRATION
        action, _reason = to_engine_action(output.model_dump(), request)
        return action
