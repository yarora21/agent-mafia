"""PR10a — the rendering layer: game state → the exact text the model reads.

This is the *input* side of the LLM agent (PR9 was the output side). It is pure:
structured state in, strings out — no model calls, no I/O. It renders from only
three inputs — `ObservableState`, `ActionRequest`, and (optionally) `BeliefState`
— and **never** `GameState`. That is the tier-2 security boundary: PR4 proved the
view *object* holds no secrets; forbidding `GameState` here proves the *rendered
bytes* cannot either, because the omniscient state is not even reachable in this
file. PR10b's leak tests (a rendered-string scan + an AST test that this module
never references `GameState`) enforce it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agents.beliefs import BeliefState
from config import MemoryStrategy
from engine.actions import ActionRequest, ActionType
from engine.state import Event, EventType, NARRATOR_ID, Role
from engine.visibility import ObservableState

PROMPT_VERSION = "v1"  # v1: "fellow Mafia" line excludes the viewer (PR16)
"""Stamped into `GameConfig.prompt_version` so a transcript ties back to the
exact prompt wording that produced it. Bump on any change to the text below."""


class RenderedPrompt(BaseModel):
    """The two strings a Messages API call needs: `system` framing + `user` turn."""

    model_config = ConfigDict(frozen=True)

    system: str
    user: str


# --------------------------------------------------------------------------- #
# System prompts (the model's identity, one per role + narrator)
# --------------------------------------------------------------------------- #
_RULES = (
    "You are playing Mafia (Werewolf) with 7 players. The Mafia know each other "
    "and secretly kill one player each night; the Town (Villagers and one Seer) "
    "must vote out the Mafia by day. Speak in character and keep replies short."
)

_ROLE_SYSTEM: dict[Role, str] = {
    Role.MAFIA: (
        f"{_RULES}\n\nYou are MAFIA. Your goal is to eliminate the Town without "
        "being caught. At night you coordinate with your fellow Mafia and choose a "
        "victim. By day you blend in and misdirect suspicion."
    ),
    Role.SEER: (
        f"{_RULES}\n\nYou are the SEER, on the Town's side. Each night you may "
        "investigate one player and learn whether they are Mafia. Use what you "
        "learn to guide the town — but revealing yourself makes you a target."
    ),
    Role.VILLAGER: (
        f"{_RULES}\n\nYou are a VILLAGER, on the Town's side. You have no special "
        "power. Reason from what is said and vote to eliminate the Mafia."
    ),
}

_NARRATOR_SYSTEM = (
    "You are the NARRATOR of a game of Mafia (Werewolf). You are not a player and "
    "take no side. Describe events for the table in a short, evocative sentence."
)


def _system_prompt(view: ObservableState) -> str:
    if view.self_id == NARRATOR_ID or view.self_role is None:
        return _NARRATOR_SYSTEM
    return _ROLE_SYSTEM[view.self_role]


# --------------------------------------------------------------------------- #
# Situation rendering (from ObservableState only)
# --------------------------------------------------------------------------- #
def _roster_block(view: ObservableState) -> str:
    lines = []
    for entry in view.roster:
        status = "alive" if entry.alive else "dead"
        role = f", {entry.role.value}" if entry.role is not None else ""
        marker = " (you)" if entry.id == view.self_id else ""
        lines.append(f"  seat {entry.seat}: {entry.id} — {status}{role}{marker}")
    return "Players:\n" + "\n".join(lines)


def _event_line(e: Event) -> str:
    """One readable history line. Renders only fields the event carries."""

    if e.type is EventType.GAME_START:
        return "The game begins."
    if e.type is EventType.MAFIA_CHAT:
        return f"[Mafia] {e.actor_id}: {e.message}"
    if e.type is EventType.SEER_RESULT:
        verdict = "IS Mafia" if e.seer_result else "is NOT Mafia"
        return f"[Your investigation] {e.target_id} {verdict}."
    if e.type is EventType.NIGHT_KILL:
        return f"[Mafia] chose to kill {e.target_id}."
    if e.type is EventType.DEATH:
        role = f" (they were the {e.revealed_role.value})" if e.revealed_role else ""
        return f"{e.target_id} was found dead{role}."
    if e.type is EventType.NARRATION:
        return f"Narrator: {e.message}"
    if e.type is EventType.SPEECH:
        return f"{e.actor_id}: {e.message}"
    if e.type is EventType.VOTE:
        return f"{e.actor_id} voted for {e.target_id}."
    if e.type is EventType.VOTE_OUTCOME:
        if e.target_id is None:
            return "The vote was tied; no one was eliminated."
        role = f", revealed as the {e.revealed_role.value}" if e.revealed_role else ""
        return f"{e.target_id} was voted out{role}."
    if e.type is EventType.GAME_OVER:
        winner = e.winner.value if e.winner is not None else "unknown"
        return f"Game over — {winner} wins."
    return ""  # pragma: no cover - EventType is exhausted above


def _events_block(events: list[Event]) -> str:
    if not events:
        return "(nothing has happened yet)"
    return "\n".join(_event_line(e) for e in events)


def _beliefs_block(belief: BeliefState | None) -> str:
    if belief is None or (
        not belief.suspicions and not belief.confirmed_roles and not belief.notes
    ):
        return "Your notes so far: (none yet)."
    parts = ["Your private notes so far:"]
    if belief.confirmed_roles:
        confirmed = ", ".join(
            f"{pid}={role.value}" for pid, role in belief.confirmed_roles.items()
        )
        parts.append(f"  Confirmed: {confirmed}")
    if belief.suspicions:
        susp = ", ".join(f"{pid}={score:.2f}" for pid, score in belief.suspicions.items())
        parts.append(f"  Suspicion (0-1): {susp}")
    if belief.notes:
        parts.append(f"  Notes: {belief.notes}")
    return "\n".join(parts)


def _history_block(
    view: ObservableState,
    belief: BeliefState | None,
    strategy: MemoryStrategy,
    recent_events_count: int,
) -> str:
    """Render history per the memory strategy (spec 3.2)."""

    if strategy is MemoryStrategy.FULL_TRANSCRIPT:
        return "What has happened:\n" + _events_block(view.events)

    if strategy is MemoryStrategy.BELIEFS_ONLY:
        cursor = belief.cursor if belief is not None else None
        since = [e for e in view.events if cursor is None or e.event_id > cursor]
        return (
            _beliefs_block(belief)
            + "\n\nSince your last turn:\n"
            + _events_block(since)
        )

    # BELIEFS_PLUS_RECENT
    recent = view.events[-recent_events_count:] if recent_events_count > 0 else []
    return (
        _beliefs_block(belief)
        + "\n\nMost recent events:\n"
        + _events_block(recent)
    )


# --------------------------------------------------------------------------- #
# The action ask (from ActionRequest)
# --------------------------------------------------------------------------- #
_ACTION_INSTRUCTION: dict[ActionType, str] = {
    ActionType.SPEECH: "It is your turn to speak to the table. Say one short line.",
    ActionType.VOTE: "Vote for the player you want to eliminate.",
    ActionType.MAFIA_CHAT: "Talk privately with your fellow Mafia. Say one short line.",
    ActionType.NIGHT_KILL: "Choose the player the Mafia will kill tonight.",
    ActionType.INVESTIGATE: "Choose one player to investigate tonight.",
    ActionType.NARRATE: "Narrate what just happened in one short sentence.",
}

_TARGET_ACTIONS = {ActionType.VOTE, ActionType.NIGHT_KILL, ActionType.INVESTIGATE}


def _action_block(request: ActionRequest, retry_reason: str | None) -> str:
    parts = []
    if retry_reason is not None:
        parts.append(
            f"Your previous answer was rejected: {retry_reason} Try again."
        )
    parts.append(_ACTION_INSTRUCTION[request.action_type])
    if request.action_type in _TARGET_ACTIONS:
        targets = ", ".join(request.legal_targets) or "(none available)"
        parts.append(f"You must choose exactly one of these players: {targets}.")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# The one public entry point
# --------------------------------------------------------------------------- #
def render_prompt(
    view: ObservableState,
    request: ActionRequest,
    *,
    strategy: MemoryStrategy = MemoryStrategy.FULL_TRANSCRIPT,
    belief: BeliefState | None = None,
    recent_events_count: int = 20,
    retry_reason: str | None = None,
) -> RenderedPrompt:
    """Assemble the full prompt for one agent turn.

    Renders only from `view` / `request` / `belief`. `retry_reason` (the
    referee's reason from PR5) turns this into the retry variant. `strategy` and
    `recent_events_count` come from `GameConfig` (PR8).
    """

    seat = "" if view.self_seat is None else f", seat {view.self_seat}"
    header = f"You are {view.self_id}{seat}. It is day {view.day_number}, phase: {view.phase.value}."

    sections = [header, _roster_block(view)]
    allies = [mid for mid in view.known_mafia_ids if mid != view.self_id]
    if allies:
        sections.append(f"Your fellow Mafia: {', '.join(allies)}.")
    sections.append(_history_block(view, belief, strategy, recent_events_count))
    sections.append(_action_block(request, retry_reason))

    return RenderedPrompt(system=_system_prompt(view), user="\n\n".join(sections))
