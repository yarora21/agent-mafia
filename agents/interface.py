"""The agent contract — the single narrow seam every player implements.

An agent is anything that, shown what a player may legally know
(`ObservableState`) and what is being asked of it (`ActionRequest`), produces an
`Action`. That is the whole interface. The engine never sees an agent; the
orchestrator (PR6b) holds agent instances and calls `act`, then routes the result
through the referee and `apply`.

Two deliberate choices:

- **Async from day one.** Real LLM calls must not block the event loop that will
  later stream a live game and fan out simultaneous votes. The random agent
  satisfies this trivially (no `await` needed); the engine stays pure regardless.
- **`act` returns `Action | None`.** `None` means "I produced nothing usable"
  (a malformed or missing response). The orchestrator treats `None` exactly like
  an illegal action and lets `referee.coerce_action` substitute a legal fallback,
  so a misbehaving agent can never stall the game. Baking `Optional` in now avoids
  an interface break when the LLM agent (PR11) needs to signal malformed output.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from engine.actions import Action, ActionRequest
from engine.visibility import ObservableState


@runtime_checkable
class AgentInterface(Protocol):
    """What every player (and the Narrator) must implement to be driven.

    Implementations must not mutate `view` or `request` (both are frozen) and
    should never raise for ordinary "I can't answer" cases — return `None`
    instead. A raised exception is reserved for genuine outages (e.g. an API
    failure), which the orchestrator handles distinctly from a `None` result.
    """

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None: ...
