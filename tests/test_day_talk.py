"""PR3b-i gate: the talking day — announce, narrate, discuss."""

from __future__ import annotations

from engine.actions import ActionType, Narration, Speech
from engine.engine import (
    apply,
    next_requests,
    resolve_automatic,
)
from engine.state import (
    NARRATOR_ID,
    EventType,
    GameState,
    Phase,
    Role,
    VisibilityKind,
)
from tests.conftest import after_night, make_state


# --- DAY_ANNOUNCE ----------------------------------------------------------- #
def test_announce_is_automatic_no_agent() -> None:
    state = after_night(kill="p3")
    assert next_requests(state) == []


def test_announce_reveals_victim_publicly_and_advances() -> None:
    state = after_night(kill="p3", day_number=1)  # p3 is a villager
    nxt = resolve_automatic(state)
    death = nxt.history[-1]
    assert death.type is EventType.DEATH
    assert death.visibility.kind is VisibilityKind.PUBLIC
    assert death.target_id == "p3"
    assert death.revealed_role is Role.VILLAGER
    assert nxt.phase is Phase.DAY_NARRATE


def test_announce_reveals_mafia_role_if_mafia_killed() -> None:
    # Mafia never kill their own at night, but the reveal machinery is role-agnostic.
    state = after_night(kill="p1", day_number=1)  # p1 is mafia in the roster
    nxt = resolve_automatic(state)
    assert nxt.history[-1].revealed_role is Role.MAFIA


# --- DAY_NARRATE ------------------------------------------------------------ #
def test_narrate_requests_the_narrator() -> None:
    state = make_state(Phase.DAY_NARRATE)
    req = next_requests(state)[0]
    assert req.action_type is ActionType.NARRATE
    assert req.actor_id == NARRATOR_ID
    assert req.legal_targets == []


def test_narration_is_public_and_advances_to_discuss() -> None:
    state = make_state(Phase.DAY_NARRATE)
    nxt = apply(state, Narration(actor_id=NARRATOR_ID, message="A cold dawn broke."))
    narr = nxt.history[-1]
    assert narr.type is EventType.NARRATION
    assert narr.visibility.kind is VisibilityKind.PUBLIC
    assert narr.actor_id == NARRATOR_ID
    assert nxt.phase is Phase.DAY_DISCUSS


# --- DAY_DISCUSS ------------------------------------------------------------ #
def _run_discussion(state: GameState) -> tuple[GameState, list[str]]:
    speakers: list[str] = []
    while state.phase is Phase.DAY_DISCUSS:
        req = next_requests(state)[0]
        speakers.append(req.actor_id)
        state = apply(state, Speech(actor_id=req.actor_id, message="my thoughts"))
    return state, speakers


def test_discussion_two_rounds_in_rotated_order() -> None:
    # Day 1, everyone alive (7 living): start index = 1 % 7 = 1 -> opens on p1.
    state = make_state(Phase.DAY_DISCUSS, day_number=1)
    end, speakers = _run_discussion(state)
    order = ["p1", "p2", "p3", "p4", "p5", "p6", "p0"]
    assert speakers == order + order  # two rounds
    assert end.phase is Phase.DAY_VOTE


def test_discussion_only_living_speak_and_rotation_uses_living_count() -> None:
    # p3 dead -> 6 living [p0,p1,p2,p4,p5,p6]; day 1 start = 1 % 6 = 1 -> opens p1.
    state = make_state(Phase.DAY_DISCUSS, day_number=1, dead=["p3"])
    _end, speakers = _run_discussion(state)
    order = ["p1", "p2", "p4", "p5", "p6", "p0"]
    assert speakers == order + order
    assert "p3" not in speakers


def test_discussion_speeches_are_public() -> None:
    state = make_state(Phase.DAY_DISCUSS, day_number=1)
    end, _ = _run_discussion(state)
    speeches = [e for e in end.history if e.type is EventType.SPEECH]
    assert len(speeches) == 14  # 2 rounds * 7 living
    assert all(e.visibility.kind is VisibilityKind.PUBLIC for e in speeches)


# --- integration across the talking day ------------------------------------- #
def test_talking_day_runs_announce_to_vote() -> None:
    state = after_night(kill="p3", day_number=1)
    # announce (auto)
    state = resolve_automatic(state)
    assert state.phase is Phase.DAY_NARRATE
    # narrate (agent)
    state = apply(state, Narration(actor_id=NARRATOR_ID, message="grim"))
    assert state.phase is Phase.DAY_DISCUSS
    # discuss (agents)
    state, _ = _run_discussion(state)
    assert state.phase is Phase.DAY_VOTE
    kinds = {e.type for e in state.history}
    assert {EventType.DEATH, EventType.NARRATION, EventType.SPEECH} <= kinds
