"""The referee — the game's liveness guarantee.

`engine.apply` trusts its input: it folds an *already-legal* action into a new
state. Agents are untrusted (an LLM may emit the wrong shape, target a corpse,
vote for itself, or return nothing at all), so something must stand between agent
output and `apply` and guarantee a legal move always exists. That is the referee.

Three pure functions, no I/O and no LLM:

- ``validate_action(state, request, action)`` — is this action legal for this
  request? Returns a human-readable *reason* when not (the orchestrator appends
  it to the retry prompt), or ``None`` when the action is legal.
- ``fallback_action(state, request)`` — a deterministic, seeded legal action to
  use when an agent has exhausted its retries. Target actions get a random legal
  target; free-text actions get a canned line.
- ``coerce_action(state, request, action)`` — the never-fails wrapper: return the
  action if legal, else the fallback. Guarantees ``apply`` gets a legal action.

The referee validates the *action* against the *request*; it trusts the request
(the engine's `next_requests` produced it and its `legal_targets`). Whose turn it
is, and retry/backoff/timeout policy, belong to the orchestrator, not here.
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
from engine.state import GameState, PlayerState

CANNED_MESSAGE = "I have nothing to add."
"""Fallback text for a SPEECH or MAFIA_CHAT the agent failed to produce."""

CANNED_NARRATION = "Dawn breaks, and the village finds one of its own gone."
"""Fallback text for a NARRATE the Narrator failed to produce."""

_TARGET_ACTIONS = frozenset(
    {ActionType.VOTE, ActionType.NIGHT_KILL, ActionType.INVESTIGATE}
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _find(state: GameState, player_id: str) -> PlayerState | None:
    return next((p for p in state.players if p.id == player_id), None)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate_target(
    state: GameState, request: ActionRequest, target_id: str
) -> str | None:
    """Legality of a target-based action, with a specific reason on failure.

    ``request.legal_targets`` is authoritative — the engine already computed the
    legal set. The extra probing here only produces a *better* reason for the
    retry prompt (dead / self / not-a-player) than a bare "not legal".
    """

    if target_id in request.legal_targets:
        return None
    if target_id == request.actor_id:
        return f"cannot target yourself ({target_id})"
    target = _find(state, target_id)
    if target is None:
        return f"{target_id!r} is not a player in this game"
    if not target.alive:
        return f"{target_id} is dead and cannot be targeted"
    return (
        f"{target_id} is not a legal target here; "
        f"legal targets are {sorted(request.legal_targets)}"
    )


def _validate_message(message: str) -> str | None:
    if not message.strip():
        return "message must not be empty"
    return None


def validate_action(
    state: GameState, request: ActionRequest, action: Action
) -> str | None:
    """Return a human-readable reason the action is illegal, or ``None`` if legal.

    Checks, in order: the action shape matches what was requested; the actor is
    the requested actor; and the payload is legal (a legal target for
    target-based actions, a non-empty message for free-text ones).
    """

    if action.type is not request.action_type:
        return (
            f"expected a {request.action_type.value} action, "
            f"got {action.type.value}"
        )
    if action.actor_id != request.actor_id:
        return (
            f"action actor {action.actor_id!r} does not match the "
            f"requested actor {request.actor_id!r}"
        )
    if isinstance(action, (Vote, NightKill, Investigate)):
        return _validate_target(state, request, action.target_id)
    if isinstance(action, (Speech, MafiaChat, Narration)):
        return _validate_message(action.message)
    return None  # pragma: no cover - the union is exhausted above


# --------------------------------------------------------------------------- #
# Fallbacks
# --------------------------------------------------------------------------- #
def _fallback_rng(state: GameState, request: ActionRequest) -> random.Random:
    """A deterministic RNG keyed on the game seed + this request's position.

    Seeding ``random.Random`` with a *string* is stable across processes (it is
    derived via SHA-512, not the salted builtin ``hash``), so the same state +
    request always yields the same fallback — reproducible in replay. The actor
    id is in the key so concurrent simultaneous-vote fallbacks differ per voter.
    """

    key = (
        f"{state.rng_seed}|{state.day_number}|{state.phase.value}"
        f"|{request.action_type.value}|{request.actor_id}|{len(state.history)}"
    )
    return random.Random(key)


def _pick_target(state: GameState, request: ActionRequest) -> str:
    targets = list(request.legal_targets)
    if not targets:
        # Unreachable for a well-formed target request (the engine only asks when
        # a legal target exists); recomputed here so a fallback is always possible.
        targets = [
            p.id for p in state.players if p.alive and p.id != request.actor_id
        ]
    return _fallback_rng(state, request).choice(sorted(targets))


def fallback_action(state: GameState, request: ActionRequest) -> Action:
    """A deterministic, always-legal action for a failed/absent agent response.

    Target actions get a seeded random legal target; free-text actions get a
    canned line. Deterministic: same ``(state, request)`` → same action.
    """

    at = request.action_type
    if at is ActionType.VOTE:
        return Vote(actor_id=request.actor_id, target_id=_pick_target(state, request))
    if at is ActionType.NIGHT_KILL:
        return NightKill(
            actor_id=request.actor_id, target_id=_pick_target(state, request)
        )
    if at is ActionType.INVESTIGATE:
        return Investigate(
            actor_id=request.actor_id, target_id=_pick_target(state, request)
        )
    if at is ActionType.SPEECH:
        return Speech(actor_id=request.actor_id, message=CANNED_MESSAGE)
    if at is ActionType.MAFIA_CHAT:
        return MafiaChat(actor_id=request.actor_id, message=CANNED_MESSAGE)
    if at is ActionType.NARRATE:
        return Narration(actor_id=request.actor_id, message=CANNED_NARRATION)
    raise ValueError(f"no fallback for action type {at}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Coercion
# --------------------------------------------------------------------------- #
def coerce_action(
    state: GameState, request: ActionRequest, action: Action | None
) -> Action:
    """Return ``action`` if legal, otherwise the deterministic fallback.

    Never raises for an agent failure: a ``None`` (malformed/no response) or any
    illegal action is replaced by ``fallback_action``, so the caller always has a
    legal action to feed ``apply`` and the game can never deadlock.
    """

    if action is not None and validate_action(state, request, action) is None:
        return action
    return fallback_action(state, request)
