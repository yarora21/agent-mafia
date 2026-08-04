"""Scriptable mock agents — the test instrument for the reliability machinery.

`ScriptedAgent` fails on command, deterministically, so every failure path in the
orchestrator (PR7) can be exercised with no real API: a legal move, an illegal
move, a raised outage, or an indefinite hang, chosen per `act` call from a script
(falling back to a default once the script runs out). Legal moves are delegated to
a `RandomAgent`, so a scripted agent is a drop-in `AgentInterface`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from enum import Enum

from agents.random_agent import RandomAgent
from engine.actions import Action, ActionRequest, Vote
from engine.visibility import ObservableState


class ScriptBehavior(str, Enum):
    """One scripted outcome for a single `act` call."""

    LEGAL = "legal"  # a real, legal random move
    ILLEGAL = "illegal"  # a well-formed but illegal action (malformed path)
    RAISE = "raise"  # raise an outage (api_failure path)
    HANG = "hang"  # never return in time (timeout path)


class SimulatedOutage(RuntimeError):
    """Raised by a scripted RAISE to stand in for an API outage."""


class ScriptedAgent:
    """An agent whose every move is dictated by a script, for reliability tests."""

    def __init__(
        self,
        script: Sequence[ScriptBehavior] = (),
        *,
        default: ScriptBehavior = ScriptBehavior.LEGAL,
        seed: int = 0,
        hang_seconds: float = 3600.0,
    ) -> None:
        self._script = list(script)
        self._default = default
        self._index = 0
        self._hang_seconds = hang_seconds
        self._legal = RandomAgent(seed=seed)
        self._rng = random.Random(seed)

    def _next_behavior(self) -> ScriptBehavior:
        if self._index < len(self._script):
            behavior = self._script[self._index]
        else:
            behavior = self._default
        self._index += 1
        return behavior

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        behavior = self._next_behavior()
        if behavior is ScriptBehavior.RAISE:
            raise SimulatedOutage(f"simulated outage for {request.actor_id}")
        if behavior is ScriptBehavior.HANG:
            await asyncio.sleep(self._hang_seconds)  # cancelled by the timeout
        if behavior is ScriptBehavior.ILLEGAL:
            # A vote for a non-existent target: wrong type for non-vote requests,
            # an illegal target for vote requests — illegal in every phase.
            return Vote(actor_id=request.actor_id, target_id="ghost")
        return await self._legal.act(view, request)
