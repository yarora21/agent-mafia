"""The pure loop contract, phase dispatch, and the night handlers (PR3a).

Three pure functions drive the whole game:

- ``next_requests(state)``   — who must act now, and for what.
- ``resolve_automatic(state)`` — advance any phase needing no agent input.
- ``apply(state, action)``   — fold one validated action into a new state.

Each is a thin dispatcher onto a per-phase handler. This slice implements the
three night handlers (mafia chat → seer → kill); day and game-over phases are
still stubs that raise until later slices fill them in.

No mutation, no side effects, no LLM: every function returns a new ``GameState``.
"""

from __future__ import annotations

from collections import Counter
from typing import Protocol

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
from engine.state import (
    NARRATOR_ID,
    Event,
    EventType,
    Faction,
    GameState,
    Phase,
    PlayerState,
    Role,
    Visibility,
    VotingMode,
    faction_of,
)

NIGHT_CHAT_ROUNDS = 2
"""Fixed mafia night-chat length (spec v0): 2 alternating rounds."""

DISCUSS_ROUNDS = 2
"""Fixed day-discussion length (spec v0): 2 rounds of speeches."""

_NIGHT_PHASES = frozenset(
    {Phase.NIGHT_MAFIA_CHAT, Phase.NIGHT_SEER, Phase.NIGHT_KILL}
)


# --------------------------------------------------------------------------- #
# Phase-advance machinery
# --------------------------------------------------------------------------- #
PHASE_CYCLE: tuple[Phase, ...] = (
    Phase.NIGHT_MAFIA_CHAT,
    Phase.NIGHT_SEER,
    Phase.NIGHT_KILL,
    Phase.DAY_ANNOUNCE,
    Phase.DAY_NARRATE,
    Phase.DAY_DISCUSS,
    Phase.DAY_VOTE,
)
"""The repeating night→day sequence. ``GAME_OVER`` is terminal and not in it —
it is entered by win checks (a later slice), never by linear advance."""


def next_phase(phase: Phase) -> Phase:
    """The phase that follows `phase` in the cycle.

    Wraps ``DAY_VOTE`` back to ``NIGHT_MAFIA_CHAT`` (the next day). Raises on
    ``GAME_OVER``, which has no successor.
    """

    try:
        idx = PHASE_CYCLE.index(phase)
    except ValueError:
        raise ValueError(f"{phase} is terminal and has no next phase") from None
    return PHASE_CYCLE[(idx + 1) % len(PHASE_CYCLE)]


def advance_phase(state: GameState) -> GameState:
    """Return a new state stepped to the next phase, resetting per-phase counters.

    Crossing ``DAY_VOTE`` → ``NIGHT_MAFIA_CHAT`` bumps the day number. Speech and
    voter bookkeeping reset on every phase change.
    """

    nxt = next_phase(state.phase)
    wrapped = state.phase is Phase.DAY_VOTE and nxt is Phase.NIGHT_MAFIA_CHAT
    return state.model_copy(
        update={
            "phase": nxt,
            "day_number": state.day_number + (1 if wrapped else 0),
            "speech_round": 0,
            "next_speaker": None,
            "next_voter": None,
            "ballots": {},
        }
    )


# --------------------------------------------------------------------------- #
# Pure state helpers
# --------------------------------------------------------------------------- #
def _living(state: GameState) -> list[PlayerState]:
    return [p for p in state.players if p.alive]


def _living_mafia(state: GameState) -> list[PlayerState]:
    return sorted(
        (p for p in state.players if p.alive and p.role is Role.MAFIA),
        key=lambda p: p.seat,
    )


def _living_seer(state: GameState) -> PlayerState | None:
    return next(
        (p for p in state.players if p.alive and p.role is Role.SEER), None
    )


def _player(state: GameState, player_id: str) -> PlayerState:
    return next(p for p in state.players if p.id == player_id)


def designated_killer(state: GameState) -> PlayerState | None:
    """The Mafia who owns tonight's kill.

    Lowest-seated living Mafia on Night 1, rotating among the living Mafia each
    subsequent night. ``None`` only if no Mafia are alive (a game-over case
    handled by a later slice).
    """

    mafia = _living_mafia(state)
    if not mafia:
        return None
    return mafia[(state.day_number - 1) % len(mafia)]


def _next_event_id(state: GameState) -> int:
    return state.history[-1].event_id + 1 if state.history else 0


def _emit(
    state: GameState,
    *,
    type: EventType,
    visibility: Visibility,
    actor_id: str | None = None,
    target_id: str | None = None,
    message: str | None = None,
    seer_result: bool | None = None,
    revealed_role: Role | None = None,
    winner: Faction | None = None,
) -> Event:
    """Build an event stamped with the current day/phase and the next event id."""

    return Event(
        event_id=_next_event_id(state),
        type=type,
        visibility=visibility,
        day_number=state.day_number,
        phase=state.phase,
        actor_id=actor_id,
        target_id=target_id,
        message=message,
        seer_result=seer_result,
        revealed_role=revealed_role,
        winner=winner,
    )


def _speaking_order(state: GameState) -> list[PlayerState]:
    """Living players in speaking order for the current day.

    Living players by seat, rotated so day ``d``'s first speaker is index
    ``d mod len(living)`` — this rotates who opens with zero information and
    stays well-defined when deaths leave gaps in seat numbers. Used by both
    discussion and sequential voting.
    """

    living = sorted(_living(state), key=lambda p: p.seat)
    start = state.day_number % len(living)
    return living[start:] + living[:start]


def _append_event(state: GameState, event: Event) -> GameState:
    return state.model_copy(update={"history": [*state.history, event]})


def _kill_player(state: GameState, player_id: str) -> GameState:
    players = [
        p.model_copy(update={"alive": False}) if p.id == player_id else p
        for p in state.players
    ]
    return state.model_copy(update={"players": players})


# --------------------------------------------------------------------------- #
# Night handlers
# --------------------------------------------------------------------------- #
class _MafiaChatHandler:
    """NIGHT_MAFIA_CHAT: 2 alternating rounds, designated killer first.

    Skipped entirely when fewer than two Mafia are alive (the solo Mafia's
    private reasoning covers deliberation). Progress is derived from the count
    of chat events this night — no extra state field needed.
    """

    def _speaker_order(self, state: GameState) -> list[PlayerState]:
        killer = designated_killer(state)
        assert killer is not None  # chat only runs with two living mafia
        partner = next(p for p in _living_mafia(state) if p.id != killer.id)
        order: list[PlayerState] = []
        for _ in range(NIGHT_CHAT_ROUNDS):
            order.extend((killer, partner))
        return order

    def _messages_so_far(self, state: GameState) -> int:
        return sum(
            1
            for e in state.history
            if e.type is EventType.MAFIA_CHAT and e.day_number == state.day_number
        )

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        if len(_living_mafia(state)) < 2:
            return []
        order = self._speaker_order(state)
        count = self._messages_so_far(state)
        if count >= len(order):
            return []
        return [
            ActionRequest(
                action_type=ActionType.MAFIA_CHAT, actor_id=order[count].id
            )
        ]

    def resolve_automatic(self, state: GameState) -> GameState:
        if len(_living_mafia(state)) < 2:
            return advance_phase(state)
        return state

    def apply(self, state: GameState, action: Action) -> GameState:
        if not isinstance(action, MafiaChat):
            raise TypeError(
                f"NIGHT_MAFIA_CHAT expects MafiaChat, got {type(action).__name__}"
            )
        event = _emit(
            state,
            type=EventType.MAFIA_CHAT,
            visibility=Visibility.mafia_only(),
            actor_id=action.actor_id,
            message=action.message,
        )
        advanced = _append_event(state, event)
        total = NIGHT_CHAT_ROUNDS * len(_living_mafia(state))
        if self._messages_so_far(advanced) >= total:
            return advance_phase(advanced)
        return advanced


class _SeerHandler:
    """NIGHT_SEER: the living Seer learns whether a target is Mafia.

    The phase runs every night even with no living Seer — then it resolves
    automatically with no event, so the game's structure never reveals whether
    the Seer is alive.
    """

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        seer = _living_seer(state)
        if seer is None:
            return []
        targets = [p.id for p in _living(state) if p.id != seer.id]
        return [
            ActionRequest(
                action_type=ActionType.INVESTIGATE,
                actor_id=seer.id,
                legal_targets=targets,
            )
        ]

    def resolve_automatic(self, state: GameState) -> GameState:
        if _living_seer(state) is None:
            return advance_phase(state)
        return state

    def apply(self, state: GameState, action: Action) -> GameState:
        if not isinstance(action, Investigate):
            raise TypeError(
                f"NIGHT_SEER expects Investigate, got {type(action).__name__}"
            )
        target = _player(state, action.target_id)
        event = _emit(
            state,
            type=EventType.SEER_RESULT,
            visibility=Visibility.player_only(action.actor_id),
            actor_id=action.actor_id,
            target_id=action.target_id,
            seer_result=target.role is Role.MAFIA,
        )
        return advance_phase(_append_event(state, event))


class _KillHandler:
    """NIGHT_KILL: the designated killer eliminates a living non-Mafia.

    The kill resolves into state and is recorded as a MAFIA_ONLY event; the
    public death reveal is a day concern (DAY_ANNOUNCE, a later slice).
    """

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        killer = designated_killer(state)
        if killer is None:
            return []
        targets = [p.id for p in _living(state) if p.role is not Role.MAFIA]
        return [
            ActionRequest(
                action_type=ActionType.NIGHT_KILL,
                actor_id=killer.id,
                legal_targets=targets,
            )
        ]

    def resolve_automatic(self, state: GameState) -> GameState:
        return state

    def apply(self, state: GameState, action: Action) -> GameState:
        if not isinstance(action, NightKill):
            raise TypeError(
                f"NIGHT_KILL expects NightKill, got {type(action).__name__}"
            )
        event = _emit(
            state,
            type=EventType.NIGHT_KILL,
            visibility=Visibility.mafia_only(),
            actor_id=action.actor_id,
            target_id=action.target_id,
        )
        killed = _kill_player(_append_event(state, event), action.target_id)
        return advance_phase(killed)


# --------------------------------------------------------------------------- #
# Day handlers (talking phases)
# --------------------------------------------------------------------------- #
def _nights_kill_event(state: GameState) -> Event | None:
    """The NIGHT_KILL recorded for the current day's preceding night, if any."""

    return next(
        (
            e
            for e in reversed(state.history)
            if e.type is EventType.NIGHT_KILL and e.day_number == state.day_number
        ),
        None,
    )


class _AnnounceHandler:
    """DAY_ANNOUNCE: the structural death reveal.

    Purely mechanical, no agent. Publicizes the night's victim (identity +
    revealed role) as a PUBLIC event, then advances. The victim is already dead
    (flipped at NIGHT_KILL); this only makes the death public knowledge. In v0
    the night kill is mandatory, so every day opens with a death to announce.
    """

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        return []

    def resolve_automatic(self, state: GameState) -> GameState:
        kill = _nights_kill_event(state)
        if kill is not None and kill.target_id is not None:
            victim = _player(state, kill.target_id)
            state = _append_event(
                state,
                _emit(
                    state,
                    type=EventType.DEATH,
                    visibility=Visibility.public(),
                    target_id=victim.id,
                    revealed_role=victim.role,
                ),
            )
        # If the night kill ended the game, the death is announced above and we
        # go straight to GAME_OVER — no narration, discussion, or vote.
        result = winner(state)
        if result is not None:
            return _to_game_over(state, result)
        return advance_phase(state)

    def apply(self, state: GameState, action: Action) -> GameState:
        raise TypeError("DAY_ANNOUNCE takes no agent action")


class _NarrateHandler:
    """DAY_NARRATE: the Narrator delivers PUBLIC death flavor.

    A single agent action from the Narrator (not a player). Zero mechanical
    content — kept separate from DAY_ANNOUNCE so structural vs. flavor stay
    distinct. Night deaths only; vote eliminations get no narration.
    """

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        return [
            ActionRequest(action_type=ActionType.NARRATE, actor_id=NARRATOR_ID)
        ]

    def resolve_automatic(self, state: GameState) -> GameState:
        return state

    def apply(self, state: GameState, action: Action) -> GameState:
        if not isinstance(action, Narration):
            raise TypeError(
                f"DAY_NARRATE expects Narration, got {type(action).__name__}"
            )
        event = _emit(
            state,
            type=EventType.NARRATION,
            visibility=Visibility.public(),
            actor_id=NARRATOR_ID,
            message=action.message,
        )
        return advance_phase(_append_event(state, event))


class _DiscussHandler:
    """DAY_DISCUSS: 2 rounds of PUBLIC speeches in speaking order.

    Each living player speaks once per round. Progress is derived from the count
    of speeches this day — the count doubles as the cursor into the (rotated)
    speaking order.
    """

    def _speeches_so_far(self, state: GameState) -> int:
        return sum(
            1
            for e in state.history
            if e.type is EventType.SPEECH and e.day_number == state.day_number
        )

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        order = _speaking_order(state)
        total = DISCUSS_ROUNDS * len(order)
        count = self._speeches_so_far(state)
        if count >= total:
            return []
        speaker = order[count % len(order)]
        return [
            ActionRequest(action_type=ActionType.SPEECH, actor_id=speaker.id)
        ]

    def resolve_automatic(self, state: GameState) -> GameState:
        return state

    def apply(self, state: GameState, action: Action) -> GameState:
        if not isinstance(action, Speech):
            raise TypeError(
                f"DAY_DISCUSS expects Speech, got {type(action).__name__}"
            )
        event = _emit(
            state,
            type=EventType.SPEECH,
            visibility=Visibility.public(),
            actor_id=action.actor_id,
            message=action.message,
        )
        advanced = _append_event(state, event)
        total = DISCUSS_ROUNDS * len(_speaking_order(state))
        if self._speeches_so_far(advanced) >= total:
            return advance_phase(advanced)
        return advanced


# --------------------------------------------------------------------------- #
# Voting
# --------------------------------------------------------------------------- #
def _plurality(ballots: dict[str, str]) -> str | None:
    """The unique most-voted target, or ``None`` on a tie / no ballots."""

    if not ballots:
        return None
    tally = Counter(ballots.values())
    top = max(tally.values())
    leaders = [pid for pid, count in tally.items() if count == top]
    return leaders[0] if len(leaders) == 1 else None


def _resolve_election(state: GameState, *, emit_ballots: bool) -> GameState:
    """Tally staged ballots, reveal, eliminate the plurality, advance.

    ``emit_ballots`` is True for simultaneous mode (ballots were staged silently
    and are now revealed PUBLIC in seat order); False for sequential mode (each
    ballot was already emitted the moment it was cast). Ties → no elimination.
    The eliminated player's role is revealed. All ballots end in the public
    record either way.
    """

    if emit_ballots:
        for voter in sorted(_living(state), key=lambda p: p.seat):
            state = _append_event(
                state,
                _emit(
                    state,
                    type=EventType.VOTE,
                    visibility=Visibility.public(),
                    actor_id=voter.id,
                    target_id=state.ballots[voter.id],
                ),
            )

    eliminated = _plurality(state.ballots)
    if eliminated is not None:
        state = _append_event(
            state,
            _emit(
                state,
                type=EventType.VOTE_OUTCOME,
                visibility=Visibility.public(),
                target_id=eliminated,
                revealed_role=_player(state, eliminated).role,
            ),
        )
        state = _kill_player(state, eliminated)
    else:
        state = _append_event(
            state,
            _emit(
                state,
                type=EventType.VOTE_OUTCOME,
                visibility=Visibility.public(),
                message="No elimination (tie).",
            ),
        )

    state = state.model_copy(update={"ballots": {}})
    result = winner(state)
    if result is not None:
        return _to_game_over(state, result)
    return advance_phase(state)


class _VoteHandler:
    """DAY_VOTE for both voting modes.

    - ``simultaneous``: every living player is asked at once; ballots stage
      silently, then reveal PUBLIC in seat order followed by the outcome. No
      bandwagon — nobody sees a ballot while voting is open.
    - ``sequential``: one voter at a time in the day's speaking order; each
      ballot becomes PUBLIC on cast, so later voters see earlier votes.

    In both: legal targets are living players except yourself; no abstention.
    """

    def _vote_request(self, state: GameState, voter: PlayerState) -> ActionRequest:
        targets = [p.id for p in _living(state) if p.id != voter.id]
        return ActionRequest(
            action_type=ActionType.VOTE,
            actor_id=voter.id,
            legal_targets=targets,
        )

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        if state.voting_mode is VotingMode.SIMULTANEOUS:
            pending = [
                p
                for p in sorted(_living(state), key=lambda p: p.seat)
                if p.id not in state.ballots
            ]
            return [self._vote_request(state, p) for p in pending]

        order = _speaking_order(state)
        count = len(state.ballots)
        if count >= len(order):
            return []
        return [self._vote_request(state, order[count])]

    def resolve_automatic(self, state: GameState) -> GameState:
        return state

    def apply(self, state: GameState, action: Action) -> GameState:
        if not isinstance(action, Vote):
            raise TypeError(
                f"DAY_VOTE expects Vote, got {type(action).__name__}"
            )
        state = state.model_copy(
            update={"ballots": {**state.ballots, action.actor_id: action.target_id}}
        )
        if state.voting_mode is VotingMode.SEQUENTIAL:
            state = _append_event(
                state,
                _emit(
                    state,
                    type=EventType.VOTE,
                    visibility=Visibility.public(),
                    actor_id=action.actor_id,
                    target_id=action.target_id,
                ),
            )
        if len(state.ballots) >= len(_living(state)):
            return _resolve_election(
                state, emit_ballots=state.voting_mode is VotingMode.SIMULTANEOUS
            )
        return state


# --------------------------------------------------------------------------- #
# Win conditions + game over
# --------------------------------------------------------------------------- #
def winner(state: GameState) -> Faction | None:
    """The winning faction if the game is decided, else ``None``.

    Checked after every death/elimination:
    - Town wins when all Mafia are dead.
    - Mafia wins when living Mafia >= living Town.
    """

    mafia = sum(1 for p in _living(state) if faction_of(p.role) is Faction.MAFIA)
    town = sum(1 for p in _living(state) if faction_of(p.role) is Faction.TOWN)
    if mafia == 0:
        return Faction.TOWN
    if mafia >= town:
        return Faction.MAFIA
    return None


def _to_game_over(state: GameState, result: Faction) -> GameState:
    """Emit the PUBLIC result event and park the game in GAME_OVER (terminal)."""

    event = _emit(
        state,
        type=EventType.GAME_OVER,
        visibility=Visibility.public(),
        winner=result,
    )
    return _append_event(state, event).model_copy(
        update={"phase": Phase.GAME_OVER}
    )


class _GameOverHandler:
    """GAME_OVER: terminal. No agent acts; no action can be applied."""

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        return []

    def resolve_automatic(self, state: GameState) -> GameState:
        return state

    def apply(self, state: GameState, action: Action) -> GameState:
        raise RuntimeError("game is over; no actions can be applied")


# --------------------------------------------------------------------------- #
# Per-phase handler contract + dispatch
# --------------------------------------------------------------------------- #
class PhaseHandler(Protocol):
    """What every phase must implement to plug into the loop."""

    def next_requests(self, state: GameState) -> list[ActionRequest]: ...

    def resolve_automatic(self, state: GameState) -> GameState: ...

    def apply(self, state: GameState, action: Action) -> GameState: ...


class _UnimplementedPhase:
    """Stub handler: routes correctly but has no behavior yet.

    Present so dispatch is fully wired and every phase resolves to a handler.
    Replaced phase-by-phase in later slices (day + game-over).
    """

    def __init__(self, phase: Phase) -> None:
        self._phase = phase

    def next_requests(self, state: GameState) -> list[ActionRequest]:
        raise NotImplementedError(f"next_requests not implemented for {self._phase}")

    def resolve_automatic(self, state: GameState) -> GameState:
        raise NotImplementedError(
            f"resolve_automatic not implemented for {self._phase}"
        )

    def apply(self, state: GameState, action: Action) -> GameState:
        raise NotImplementedError(f"apply not implemented for {self._phase}")


_HANDLERS: dict[Phase, PhaseHandler] = {
    phase: _UnimplementedPhase(phase) for phase in Phase
}
_HANDLERS[Phase.NIGHT_MAFIA_CHAT] = _MafiaChatHandler()
_HANDLERS[Phase.NIGHT_SEER] = _SeerHandler()
_HANDLERS[Phase.NIGHT_KILL] = _KillHandler()
_HANDLERS[Phase.DAY_ANNOUNCE] = _AnnounceHandler()
_HANDLERS[Phase.DAY_NARRATE] = _NarrateHandler()
_HANDLERS[Phase.DAY_DISCUSS] = _DiscussHandler()
_HANDLERS[Phase.DAY_VOTE] = _VoteHandler()
_HANDLERS[Phase.GAME_OVER] = _GameOverHandler()


def _handler_for(phase: Phase) -> PhaseHandler:
    try:
        return _HANDLERS[phase]
    except KeyError:
        raise KeyError(f"no handler registered for {phase}") from None


# --------------------------------------------------------------------------- #
# The loop contract
# --------------------------------------------------------------------------- #
def next_requests(state: GameState) -> list[ActionRequest]:
    """Agent asks pending in the current phase (empty if none)."""

    return _handler_for(state.phase).next_requests(state)


def resolve_automatic(state: GameState) -> GameState:
    """Advance the current phase if it needs no agent input; else return as-is."""

    return _handler_for(state.phase).resolve_automatic(state)


def apply(state: GameState, action: Action) -> GameState:
    """Fold one already-validated action into a new state."""

    return _handler_for(state.phase).apply(state, action)
