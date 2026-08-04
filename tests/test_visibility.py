"""PR4 gate — tier-1 leak tests for the visibility boundary.

The strategy: drive many random games to completion in BOTH voting modes with a
seeded RNG, snapshotting EVERY intermediate state (including the partial-ballot
states of an open simultaneous vote and the one-phase gap where a night victim
reads dead before its role is revealed). Then, for every snapshot, build the view
of every player AND the Narrator and assert the boundary leaks nothing:

- no living/unrevealed role appears in any roster (not even the viewer's own);
- MAFIA_ONLY events (identities + night chat) reach only Mafia;
- PLAYER_ONLY events (seer results) reach only their owner;
- Mafia know their allies; Town's `known_mafia_ids` is empty;
- the Narrator sees public-only;
- `GameState.ballots` is never observable, and an open simultaneous vote exposes
  no in-flight ballots.

Because these run over thousands of states, a real leak fails loudly and points
at the exact state.
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
from engine.engine import apply, next_requests, resolve_automatic
from engine.state import (
    NARRATOR_ID,
    EventType,
    GameState,
    Phase,
    Role,
    VisibilityKind,
    VotingMode,
    new_game,
)
from engine.visibility import (
    ObservableState,
    known_mafia_ids,
    narrator_view,
    observable_view,
)

SEEDS = range(15)
MODES = (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL)


# --------------------------------------------------------------------------- #
# Random game driver — collects every intermediate state
# --------------------------------------------------------------------------- #
def _action_for(req: ActionRequest, rng: random.Random) -> Action:
    kind = req.action_type
    if kind is ActionType.MAFIA_CHAT:
        return MafiaChat(actor_id=req.actor_id, message="...")
    if kind is ActionType.INVESTIGATE:
        return Investigate(actor_id=req.actor_id, target_id=rng.choice(req.legal_targets))
    if kind is ActionType.NIGHT_KILL:
        return NightKill(actor_id=req.actor_id, target_id=rng.choice(req.legal_targets))
    if kind is ActionType.NARRATE:
        return Narration(actor_id=NARRATOR_ID, message="dawn")
    if kind is ActionType.SPEECH:
        return Speech(actor_id=req.actor_id, message="hmm")
    return Vote(actor_id=req.actor_id, target_id=rng.choice(req.legal_targets))


def _play_collecting(seed: int, mode: VotingMode) -> list[GameState]:
    """Play one random game, returning a snapshot of every state it passes through."""

    rng = random.Random(seed)
    states: list[GameState] = []
    state = new_game(seed=seed, mode=mode)
    guard = 0
    while True:
        states.append(state)
        if state.phase is Phase.GAME_OVER:
            break
        guard += 1
        assert guard < 1000, "game failed to terminate"
        advanced = resolve_automatic(state)
        if advanced is not state:
            state = advanced
            continue
        reqs = next_requests(state)
        assert reqs, f"phase {state.phase} stalled: no auto-advance and no requests"
        state = apply(state, _action_for(rng.choice(reqs), rng))
    return states


def _all_states() -> list[GameState]:
    states: list[GameState] = []
    for mode in MODES:
        for seed in SEEDS:
            states.extend(_play_collecting(seed, mode))
    return states


# Computed once; every leak test scans the same corpus of intermediate states.
ALL_STATES: list[GameState] = _all_states()


def _viewers(state: GameState) -> list[str]:
    return [p.id for p in state.players] + [NARRATOR_ID]


def _public_reveals(state: GameState) -> dict[str, Role]:
    """Independently recomputed reveal map — the test's own oracle, not the SUT's."""

    reveals: dict[str, Role] = {}
    for e in state.history:
        if (
            e.visibility.kind is VisibilityKind.PUBLIC
            and e.revealed_role is not None
            and e.target_id is not None
        ):
            reveals[e.target_id] = e.revealed_role
    return reveals


# --------------------------------------------------------------------------- #
# Sanity: the corpus really is big and varied
# --------------------------------------------------------------------------- #
def test_corpus_is_nontrivial() -> None:
    assert len(ALL_STATES) > 500
    # Every phase (except maybe none) shows up across the corpus.
    phases = {s.phase for s in ALL_STATES}
    assert Phase.DAY_VOTE in phases
    assert Phase.GAME_OVER in phases


# --------------------------------------------------------------------------- #
# Structural: the view type cannot carry ballots at all
# --------------------------------------------------------------------------- #
def test_observable_state_has_no_ballots_field() -> None:
    assert "ballots" not in ObservableState.model_fields


def test_observable_view_rejects_unknown_id() -> None:
    state = new_game(seed=0, mode=VotingMode.SIMULTANEOUS)
    try:
        observable_view(state, "nobody")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for a non-player id")


# --------------------------------------------------------------------------- #
# Master invariant: every visible event is legally visible to that viewer
# --------------------------------------------------------------------------- #
def test_every_visible_event_is_permitted() -> None:
    for state in ALL_STATES:
        mafia_ids = {p.id for p in state.players if p.role is Role.MAFIA}
        for viewer in _viewers(state):
            view = observable_view(state, viewer)
            is_mafia = viewer in mafia_ids
            for e in view.events:
                kind = e.visibility.kind
                if kind is VisibilityKind.PUBLIC:
                    continue
                if kind is VisibilityKind.MAFIA_ONLY:
                    assert is_mafia, f"MAFIA_ONLY {e.type} leaked to {viewer}"
                elif kind is VisibilityKind.PLAYER_ONLY:
                    assert e.visibility.player_id == viewer, (
                        f"PLAYER_ONLY {e.type} leaked to {viewer}"
                    )
                else:  # pragma: no cover - defensive
                    raise AssertionError(f"unknown visibility {kind}")


def test_all_public_events_reach_everyone() -> None:
    """Nobody is starved of public information (event_ids preserved, order kept)."""

    for state in ALL_STATES:
        public_ids = [
            e.event_id
            for e in state.history
            if e.visibility.kind is VisibilityKind.PUBLIC
        ]
        for viewer in _viewers(state):
            view = observable_view(state, viewer)
            seen = [e.event_id for e in view.events]
            assert seen == sorted(seen), "event order/ids not preserved"
            assert set(public_ids) <= set(seen)


# --------------------------------------------------------------------------- #
# No role leak: rosters only ever show publicly revealed roles
# --------------------------------------------------------------------------- #
def test_no_unrevealed_role_leaks_in_roster() -> None:
    for state in ALL_STATES:
        reveals = _public_reveals(state)
        for viewer in _viewers(state):
            view = observable_view(state, viewer)
            for entry in view.roster:
                if entry.role is not None:
                    assert entry.id in reveals and reveals[entry.id] == entry.role, (
                        f"roster leaked {entry.id}'s role to {viewer}"
                    )


def test_villager_sees_no_other_living_role() -> None:
    """The canonical leak case: a living Villager, no other living player's role."""

    for state in ALL_STATES:
        for p in state.players:
            if p.role is not Role.VILLAGER or not p.alive:
                continue
            view = observable_view(state, p.id)
            reveals = _public_reveals(state)
            for entry in view.roster:
                if entry.id == p.id:
                    continue
                # any role shown must be a dead, publicly-revealed player
                if entry.role is not None:
                    assert entry.id in reveals
            assert view.known_mafia_ids == []


def test_self_role_is_own_true_role() -> None:
    for state in ALL_STATES:
        for p in state.players:
            view = observable_view(state, p.id)
            assert view.self_role is p.role
            assert view.self_seat == p.seat


# --------------------------------------------------------------------------- #
# Mafia chat invisible to non-mafia; mafia know their allies
# --------------------------------------------------------------------------- #
def test_mafia_chat_hidden_from_non_mafia() -> None:
    for state in ALL_STATES:
        mafia_ids = {p.id for p in state.players if p.role is Role.MAFIA}
        for viewer in _viewers(state):
            if viewer in mafia_ids:
                continue
            view = observable_view(state, viewer)
            assert not any(e.type is EventType.MAFIA_CHAT for e in view.events)
            assert not any(
                e.visibility.kind is VisibilityKind.MAFIA_ONLY for e in view.events
            )


def test_mafia_know_allies_town_does_not() -> None:
    for state in ALL_STATES:
        mafia_ids = sorted(
            (p.id for p in state.players if p.role is Role.MAFIA),
            key=lambda pid: next(pp.seat for pp in state.players if pp.id == pid),
        )
        for p in state.players:
            got = known_mafia_ids(state, p.id)
            if p.role is Role.MAFIA:
                assert got == mafia_ids
                assert p.id in got  # a Mafia knows itself is Mafia
            else:
                assert got == []


# --------------------------------------------------------------------------- #
# Seer results private to the seer
# --------------------------------------------------------------------------- #
def test_seer_results_private() -> None:
    for state in ALL_STATES:
        for viewer in _viewers(state):
            view = observable_view(state, viewer)
            for e in view.events:
                if e.type is EventType.SEER_RESULT:
                    assert e.visibility.player_id == viewer, (
                        f"seer result for {e.visibility.player_id} leaked to {viewer}"
                    )


# --------------------------------------------------------------------------- #
# Ballots never leak — no field, and no in-flight simultaneous votes surface
# --------------------------------------------------------------------------- #
def test_open_simultaneous_vote_leaks_no_ballots() -> None:
    saw_open_vote = False
    for state in ALL_STATES:
        if not (
            state.voting_mode is VotingMode.SIMULTANEOUS
            and state.phase is Phase.DAY_VOTE
        ):
            continue
        if state.ballots:
            saw_open_vote = True  # a genuinely in-flight simultaneous vote
        for viewer in _viewers(state):
            view = observable_view(state, viewer)
            # No VOTE event for the current day exists yet — ballots are staged
            # silently and only revealed at resolution, which advances the phase.
            assert not any(
                e.type is EventType.VOTE and e.day_number == state.day_number
                for e in view.events
            )
    assert saw_open_vote, "corpus never exercised a partially-cast simultaneous vote"


# --------------------------------------------------------------------------- #
# Narrator sees public-only
# --------------------------------------------------------------------------- #
def test_narrator_view_is_public_only() -> None:
    for state in ALL_STATES:
        view = narrator_view(state)
        assert view is not None
        assert view.self_role is None
        assert view.self_seat is None
        assert view.known_mafia_ids == []
        assert all(
            e.visibility.kind is VisibilityKind.PUBLIC for e in view.events
        )
        # never any private role beyond public reveals
        reveals = _public_reveals(state)
        for entry in view.roster:
            if entry.role is not None:
                assert entry.id in reveals


def test_observable_view_routes_narrator_id() -> None:
    state = new_game(seed=1, mode=VotingMode.SEQUENTIAL)
    assert observable_view(state, NARRATOR_ID) == narrator_view(state)
