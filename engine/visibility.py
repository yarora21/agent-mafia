"""The visibility boundary — the one choke point for agent-visible state.

`observable_view(state, player_id)` turns the omniscient, authoritative
`GameState` into an `ObservableState` holding *only* what that player is allowed
to know. Agents (and the Narrator) NEVER receive a raw `GameState`; every path
that shows a model anything routes through here. This module is the game's
reference monitor: small enough to read in one sitting, always invoked, and the
single place the leak tests point at.

What a view exposes (spec 3.6):
    - the viewer's own id, seat, and role;
    - the roster: every player's id, seat, alive/dead — and role ONLY where a
      public event has revealed it (death or vote-out);
    - current phase, day number, voting mode;
    - the viewer's filtered events (event_ids preserved): PUBLIC events, the
      viewer's own PLAYER_ONLY events (seer results), and MAFIA_ONLY events
      (identities + night chat) iff the viewer is Mafia;
    - `known_mafia_ids`: the Mafia roster iff the viewer is Mafia, else empty.

What a view NEVER exposes:
    - `GameState.ballots`. That dict stages in-flight simultaneous votes; it has
      no field here at all. Ballots reach a viewer only as PUBLIC `VOTE` events
      emitted AFTER a vote closes, so an open simultaneous vote leaks nothing.
    - any living player's unrevealed role (other than the viewer's own).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from engine.state import (
    NARRATOR_ID,
    Event,
    GameState,
    Phase,
    PlayerState,
    Role,
    VisibilityKind,
    VotingMode,
)


# --------------------------------------------------------------------------- #
# The agent-facing view types
# --------------------------------------------------------------------------- #
class RosterEntry(BaseModel):
    """One player as seen from a view: identity + liveness, role only if public.

    `role` is populated ONLY when a public event (a death reveal or a vote-out)
    has already made that player's role common knowledge. A living, unrevealed
    player's role is always `None` here — including allies of a Mafia viewer,
    whose identities travel via `ObservableState.known_mafia_ids`, not this field.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    seat: int
    alive: bool
    role: Role | None = None


class ObservableState(BaseModel):
    """Everything a single viewer may legally know. The only state agents see.

    Deliberately has no `ballots` field: in-flight votes are never observable.
    The Narrator (not a player) gets `self_seat=None`, `self_role=None`, an empty
    `known_mafia_ids`, and a public-only event list.
    """

    model_config = ConfigDict(frozen=True)

    self_id: str
    self_seat: int | None
    self_role: Role | None
    phase: Phase
    day_number: int
    voting_mode: VotingMode
    roster: list[RosterEntry]
    events: list[Event]
    known_mafia_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _find(state: GameState, player_id: str) -> PlayerState | None:
    return next((p for p in state.players if p.id == player_id), None)


def _public_reveals(state: GameState) -> dict[str, Role]:
    """Map of player_id -> role for every publicly revealed role so far.

    A role becomes common knowledge only through a PUBLIC event carrying a
    `revealed_role` (DEATH at dawn, VOTE_OUTCOME after an elimination). The
    `alive` flag is deliberately NOT consulted: a night victim reads dead one
    phase before its DEATH event, and its role must stay hidden until that event.
    """

    reveals: dict[str, Role] = {}
    for event in state.history:
        if (
            event.visibility.kind is VisibilityKind.PUBLIC
            and event.revealed_role is not None
            and event.target_id is not None
        ):
            reveals[event.target_id] = event.revealed_role
    return reveals


def known_mafia_ids(state: GameState, player_id: str) -> list[str]:
    """The Mafia roster the viewer knows: all Mafia ids (by seat) if the viewer
    is Mafia, else an empty list. Non-Mafia never learn allies from this field.

    Includes the viewer and dead Mafia — faction identity does not change on
    death. A Seer's investigation result is NOT surfaced here; it rides on the
    viewer's private SEER_RESULT event instead.
    """

    viewer = _find(state, player_id)
    if viewer is None or viewer.role is not Role.MAFIA:
        return []
    return [
        p.id
        for p in sorted(state.players, key=lambda p: p.seat)
        if p.role is Role.MAFIA
    ]


def _can_see(event: Event, viewer_id: str, viewer_role: Role | None) -> bool:
    """Whether one event is visible to a viewer with the given role.

    PUBLIC      -> everyone (including the Narrator, whose role is None).
    MAFIA_ONLY  -> only a Mafia viewer.
    PLAYER_ONLY -> only the tagged player.
    """

    kind = event.visibility.kind
    if kind is VisibilityKind.PUBLIC:
        return True
    if kind is VisibilityKind.MAFIA_ONLY:
        return viewer_role is Role.MAFIA
    if kind is VisibilityKind.PLAYER_ONLY:
        return event.visibility.player_id == viewer_id
    return False


def _roster(state: GameState) -> list[RosterEntry]:
    """The roster in seat order, roles filled in only where publicly revealed."""

    reveals = _public_reveals(state)
    return [
        RosterEntry(
            id=p.id,
            seat=p.seat,
            alive=p.alive,
            role=reveals.get(p.id),
        )
        for p in sorted(state.players, key=lambda p: p.seat)
    ]


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #
def narrator_view(state: GameState) -> ObservableState:
    """The Narrator's public-only view.

    The Narrator is not a player: it has no seat, no role, no allies, and sees
    ONLY PUBLIC events — so it can never leak the killer's identity or any
    private role. Used by DAY_NARRATE.
    """

    events = [e for e in state.history if _can_see(e, NARRATOR_ID, None)]
    return ObservableState(
        self_id=NARRATOR_ID,
        self_seat=None,
        self_role=None,
        phase=state.phase,
        day_number=state.day_number,
        voting_mode=state.voting_mode,
        roster=_roster(state),
        events=events,
        known_mafia_ids=[],
    )


def observable_view(state: GameState, player_id: str) -> ObservableState:
    """Return only what `player_id` may legally know about `state`.

    This is the security boundary: agents receive the result, never `state`
    itself. Raises `KeyError` for an id that is neither a player nor the
    Narrator, so a typo fails loudly instead of silently disclosing nothing.
    """

    if player_id == NARRATOR_ID:
        return narrator_view(state)

    viewer = _find(state, player_id)
    if viewer is None:
        raise KeyError(f"{player_id!r} is not a player or the Narrator")

    events = [e for e in state.history if _can_see(e, player_id, viewer.role)]
    return ObservableState(
        self_id=viewer.id,
        self_seat=viewer.seat,
        self_role=viewer.role,
        phase=state.phase,
        day_number=state.day_number,
        voting_mode=state.voting_mode,
        roster=_roster(state),
        events=events,
        known_mafia_ids=known_mafia_ids(state, player_id),
    )
