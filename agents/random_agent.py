"""The random agent — the simplest thing that satisfies `AgentInterface`.

It plays legal but strategy-free moves: a uniformly random legal target for
target actions, and a short filler line for free-text ones. Its jobs are to prove
the interface is real, to serve as the orchestrator's end-to-end driver before any
LLM exists, and to act as a baseline opponent.

Reproducibility: each instance owns a seeded `random.Random`, so a game wired from
fixed per-agent seeds replays identically. The agent never blocks (no real I/O),
so `act` is async only to fit the interface.
"""

from __future__ import annotations

import random

from engine.actions import (
    Action,
    ActionRequest,
    ActionType,
    Investigate,
    MafiaChat,
    Narration,
    NightKill,
    Speech,
    Vote,
)
from engine.visibility import ObservableState

_FILLER_LINES: tuple[str, ...] = (
    "I'm not sure who to trust yet.",
    "Something feels off about that last vote.",
    "Let's hear what everyone else thinks.",
    "I'll keep my eyes open tonight.",
    "No strong reads from me so far.",
)

_FILLER_NARRATION: tuple[str, ...] = (
    "A cold wind carried a scream through the empty streets.",
    "They were found at dawn, the door still bolted from within.",
    "No one heard a thing until the morning light revealed the loss.",
)


class RandomAgent:
    """A player that picks uniformly at random among legal moves."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        at = request.action_type
        if at in (ActionType.VOTE, ActionType.NIGHT_KILL, ActionType.INVESTIGATE):
            if not request.legal_targets:
                return None  # no legal target; let the orchestrator coerce
            target = self._rng.choice(request.legal_targets)
            if at is ActionType.VOTE:
                return Vote(actor_id=request.actor_id, target_id=target)
            if at is ActionType.NIGHT_KILL:
                return NightKill(actor_id=request.actor_id, target_id=target)
            return Investigate(actor_id=request.actor_id, target_id=target)
        if at is ActionType.NARRATE:
            return Narration(
                actor_id=request.actor_id,
                message=self._rng.choice(_FILLER_NARRATION),
            )
        message = self._rng.choice(_FILLER_LINES)
        if at is ActionType.MAFIA_CHAT:
            return MafiaChat(actor_id=request.actor_id, message=message)
        return Speech(actor_id=request.actor_id, message=message)
