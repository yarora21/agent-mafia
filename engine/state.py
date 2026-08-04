"""Immutable domain types for the Mafia game and the deterministic deal.

This module is pure data plus one constructor (`new_game`). It encodes *what a
game is made of* — roles, phases, players, events, and the starting deal — but
no rules about how those pieces interact. Whose turn it is, what is legal, and
how state advances all live in later engine modules (state machine, referee).
"""

from __future__ import annotations

import random
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Role(str, Enum):
    """A player's secret role. The Narrator is not a role — it is not a player."""

    MAFIA = "mafia"
    SEER = "seer"
    VILLAGER = "villager"


class Faction(str, Enum):
    """Win-condition alignment. Seer and Villager are both Town."""

    MAFIA = "mafia"
    TOWN = "town"


_FACTION_OF: dict[Role, Faction] = {
    Role.MAFIA: Faction.MAFIA,
    Role.SEER: Faction.TOWN,
    Role.VILLAGER: Faction.TOWN,
}


def faction_of(role: Role) -> Faction:
    """The faction a role belongs to. A pure lookup, not a game rule."""

    return _FACTION_OF[role]


class Phase(str, Enum):
    """Game phases, declared in resolution order (spec 3.1).

    Each night phase fully resolves before the next begins; the day mirrors it.
    Declaration order here is the canonical resolution order the state machine
    advances through.
    """

    NIGHT_MAFIA_CHAT = "night_mafia_chat"
    NIGHT_SEER = "night_seer"
    NIGHT_KILL = "night_kill"
    DAY_ANNOUNCE = "day_announce"
    DAY_NARRATE = "day_narrate"
    DAY_DISCUSS = "day_discuss"
    DAY_VOTE = "day_vote"
    GAME_OVER = "game_over"


class VotingMode(str, Enum):
    """The two vote resolutions compared by the Section 5 experiment."""

    SIMULTANEOUS = "simultaneous"
    SEQUENTIAL = "sequential"


class VisibilityKind(str, Enum):
    """Who may observe an event."""

    PUBLIC = "public"
    MAFIA_ONLY = "mafia_only"
    PLAYER_ONLY = "player_only"


class EventType(str, Enum):
    """The kinds of history entries the engine emits."""

    GAME_START = "game_start"
    MAFIA_CHAT = "mafia_chat"
    SEER_RESULT = "seer_result"
    NIGHT_KILL = "night_kill"
    DEATH = "death"
    NARRATION = "narration"
    SPEECH = "speech"
    VOTE = "vote"
    VOTE_OUTCOME = "vote_outcome"
    GAME_OVER = "game_over"


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #
class Visibility(BaseModel):
    """The tag every event carries; the choke point PR4's boundary keys off.

    PUBLIC       — everyone sees it.
    MAFIA_ONLY   — only living/known Mafia see it (night chat).
    PLAYER_ONLY  — only `player_id` sees it (a Seer result).
    """

    model_config = ConfigDict(frozen=True)

    kind: VisibilityKind
    player_id: str | None = None

    @classmethod
    def public(cls) -> Visibility:
        return cls(kind=VisibilityKind.PUBLIC)

    @classmethod
    def mafia_only(cls) -> Visibility:
        return cls(kind=VisibilityKind.MAFIA_ONLY)

    @classmethod
    def player_only(cls, player_id: str) -> Visibility:
        return cls(kind=VisibilityKind.PLAYER_ONLY, player_id=player_id)


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #
class Event(BaseModel):
    """One entry in the game history.

    `event_id` is a monotonically increasing integer and is part of the
    engine/interface contract: agent event cursors ride on it. Payload fields
    are optional and populated per `type`; the state machine (PR3) decides which
    apply to which event.
    """

    model_config = ConfigDict(frozen=True)

    event_id: int
    type: EventType
    visibility: Visibility
    day_number: int
    phase: Phase
    actor_id: str | None = None
    target_id: str | None = None
    message: str | None = None
    revealed_role: Role | None = None
    seer_result: bool | None = None
    winner: Faction | None = None


# --------------------------------------------------------------------------- #
# Players and game state
# --------------------------------------------------------------------------- #
class PlayerState(BaseModel):
    """A single player. Role is secret; `alive` flips on death."""

    model_config = ConfigDict(frozen=True)

    id: str
    seat: int
    role: Role
    alive: bool = True


class GameState(BaseModel):
    """The full, authoritative game state.

    Immutable-style: never mutated in place. The state machine returns a new
    `GameState` from each step (via `model_copy`). Agents never receive this —
    they see the filtered `ObservableState` produced by the visibility boundary.
    """

    model_config = ConfigDict(frozen=True)

    players: list[PlayerState]
    phase: Phase
    voting_mode: VotingMode
    day_number: int = 1
    speech_round: int = 0
    next_speaker: str | None = None
    next_voter: str | None = None
    ballots: dict[str, str] = Field(default_factory=dict)
    history: list[Event] = Field(default_factory=list)
    rng_seed: int = 0


# --------------------------------------------------------------------------- #
# The deterministic deal
# --------------------------------------------------------------------------- #
NARRATOR_ID = "narrator"
"""Actor id for the Narrator. Not a player — never in `players`, never votes,
can't be killed or investigated, doesn't count toward win conditions."""

NUM_PLAYERS = 7
ROLE_DEAL: tuple[Role, ...] = (
    Role.MAFIA,
    Role.MAFIA,
    Role.SEER,
    Role.VILLAGER,
    Role.VILLAGER,
    Role.VILLAGER,
    Role.VILLAGER,
)


def new_game(seed: int, mode: VotingMode) -> GameState:
    """Deal a fresh game reproducibly from `(seed, mode)`.

    Seats are 0..6 with ids ``p0``..``p6``; the seven roles are shuffled with a
    seeded RNG and assigned to seats. Same inputs always produce an identical
    starting state. No history yet — the game opens on Night 1, mafia chat.
    """

    rng = random.Random(seed)
    roles = list(ROLE_DEAL)
    rng.shuffle(roles)
    players = [
        PlayerState(id=f"p{seat}", seat=seat, role=role)
        for seat, role in enumerate(roles)
    ]
    return GameState(
        players=players,
        phase=Phase.NIGHT_MAFIA_CHAT,
        voting_mode=mode,
        day_number=1,
        rng_seed=seed,
    )
