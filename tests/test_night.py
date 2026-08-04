"""PR3a-ii gate: the three night handlers (mafia chat → seer → kill)."""

from __future__ import annotations

from engine.actions import (
    Action,
    ActionType,
    Investigate,
    MafiaChat,
    NightKill,
)
from engine.engine import (
    apply,
    designated_killer,
    next_requests,
    resolve_automatic,
)
from engine.state import (
    EventType,
    GameState,
    Phase,
    VisibilityKind,
)
from tests.conftest import make_state

_NIGHT = {Phase.NIGHT_MAFIA_CHAT, Phase.NIGHT_SEER, Phase.NIGHT_KILL}


def play_night(
    state: GameState, *, seer_target: str, kill_target: str
) -> GameState:
    """Drive one night to the DAY_ANNOUNCE boundary with fixed choices."""

    guard = 0
    while state.phase in _NIGHT:
        guard += 1
        assert guard < 50, "night failed to terminate"
        state = resolve_automatic(state)
        if state.phase not in _NIGHT:
            break
        reqs = next_requests(state)
        if not reqs:
            state = resolve_automatic(state)
            continue
        req = reqs[0]
        action: Action
        if req.action_type is ActionType.MAFIA_CHAT:
            action = MafiaChat(actor_id=req.actor_id, message="...")
        elif req.action_type is ActionType.INVESTIGATE:
            action = Investigate(actor_id=req.actor_id, target_id=seer_target)
        else:  # NIGHT_KILL
            action = NightKill(actor_id=req.actor_id, target_id=kill_target)
        state = apply(state, action)
    return state


# --- designated killer / rotation ------------------------------------------ #
def test_killer_is_lowest_seat_on_night_one() -> None:
    killer = designated_killer(make_state(Phase.NIGHT_KILL, day_number=1))
    assert killer is not None and killer.id == "p1"


def test_killer_rotates_second_night() -> None:
    killer = designated_killer(make_state(Phase.NIGHT_KILL, day_number=2))
    assert killer is not None and killer.id == "p4"


def test_sole_mafia_is_always_the_killer() -> None:
    state = make_state(Phase.NIGHT_KILL, day_number=2, dead=["p1"])
    killer = designated_killer(state)
    assert killer is not None and killer.id == "p4"


# --- mafia chat ------------------------------------------------------------- #
def test_chat_order_is_killer_partner_twice() -> None:
    state = make_state(Phase.NIGHT_MAFIA_CHAT, day_number=1)
    speakers: list[str] = []
    while state.phase is Phase.NIGHT_MAFIA_CHAT:
        reqs = next_requests(state)
        req = reqs[0]
        speakers.append(req.actor_id)
        state = apply(state, MafiaChat(actor_id=req.actor_id, message="hi"))
    assert speakers == ["p1", "p4", "p1", "p4"]
    assert state.phase is Phase.NIGHT_SEER


def test_chat_events_are_mafia_only() -> None:
    state = make_state(Phase.NIGHT_MAFIA_CHAT, day_number=1)
    while state.phase is Phase.NIGHT_MAFIA_CHAT:
        req = next_requests(state)[0]
        state = apply(state, MafiaChat(actor_id=req.actor_id, message="hi"))
    chat = [e for e in state.history if e.type is EventType.MAFIA_CHAT]
    assert len(chat) == 4
    assert all(e.visibility.kind is VisibilityKind.MAFIA_ONLY for e in chat)


def test_chat_skipped_with_one_mafia() -> None:
    state = make_state(Phase.NIGHT_MAFIA_CHAT, day_number=1, dead=["p1"])
    assert next_requests(state) == []
    resolved = resolve_automatic(state)
    assert resolved.phase is Phase.NIGHT_SEER
    assert resolved.history == []  # no chat events emitted


# --- seer ------------------------------------------------------------------- #
def test_seer_request_excludes_self_and_dead() -> None:
    state = make_state(Phase.NIGHT_SEER, dead=["p6"])
    req = next_requests(state)[0]
    assert req.action_type is ActionType.INVESTIGATE
    assert req.actor_id == "p2"
    assert "p2" not in req.legal_targets
    assert "p6" not in req.legal_targets


def test_seer_result_true_on_mafia() -> None:
    state = make_state(Phase.NIGHT_SEER)
    nxt = apply(state, Investigate(actor_id="p2", target_id="p1"))
    result = nxt.history[-1]
    assert result.type is EventType.SEER_RESULT
    assert result.seer_result is True
    assert result.visibility.kind is VisibilityKind.PLAYER_ONLY
    assert result.visibility.player_id == "p2"
    assert nxt.phase is Phase.NIGHT_KILL


def test_seer_result_false_on_townsperson() -> None:
    state = make_state(Phase.NIGHT_SEER)
    nxt = apply(state, Investigate(actor_id="p2", target_id="p0"))
    assert nxt.history[-1].seer_result is False


def test_dead_seer_phase_is_a_silent_noop() -> None:
    state = make_state(Phase.NIGHT_SEER, dead=["p2"])
    assert next_requests(state) == []
    resolved = resolve_automatic(state)
    assert resolved.phase is Phase.NIGHT_KILL
    assert resolved.history == []


# --- kill ------------------------------------------------------------------- #
def test_kill_targets_are_living_non_mafia() -> None:
    state = make_state(Phase.NIGHT_KILL, day_number=1)
    req = next_requests(state)[0]
    assert req.actor_id == "p1"
    assert set(req.legal_targets) == {"p0", "p2", "p3", "p5", "p6"}  # no mafia


def test_kill_flips_victim_and_records_private_event() -> None:
    state = make_state(Phase.NIGHT_KILL, day_number=1)
    nxt = apply(state, NightKill(actor_id="p1", target_id="p3"))
    assert not next(p for p in nxt.players if p.id == "p3").alive
    kill_event = nxt.history[-1]
    assert kill_event.type is EventType.NIGHT_KILL
    assert kill_event.visibility.kind is VisibilityKind.MAFIA_ONLY
    assert kill_event.target_id == "p3"
    assert nxt.phase is Phase.DAY_ANNOUNCE  # parked at the day boundary


# --- full night integration ------------------------------------------------- #
def test_full_night_parks_at_day_announce() -> None:
    state = make_state(Phase.NIGHT_MAFIA_CHAT, day_number=1)
    end = play_night(state, seer_target="p1", kill_target="p3")
    assert end.phase is Phase.DAY_ANNOUNCE
    types = [e.type for e in end.history]
    assert types == [
        EventType.MAFIA_CHAT,
        EventType.MAFIA_CHAT,
        EventType.MAFIA_CHAT,
        EventType.MAFIA_CHAT,
        EventType.SEER_RESULT,
        EventType.NIGHT_KILL,
    ]
    # Event ids are monotonic and gapless from zero.
    assert [e.event_id for e in end.history] == list(range(len(end.history)))


def test_seer_investigates_pre_kill_state() -> None:
    # With the Seer's own target killed the same night, the result was still
    # recorded (order is seer-before-kill).
    state = make_state(Phase.NIGHT_MAFIA_CHAT, day_number=1)
    end = play_night(state, seer_target="p3", kill_target="p3")
    seer_events = [e for e in end.history if e.type is EventType.SEER_RESULT]
    assert len(seer_events) == 1
    assert seer_events[0].target_id == "p3"
