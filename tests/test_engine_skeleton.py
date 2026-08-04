"""PR3a-i gate: dispatch routing and the phase-advance machinery."""

import pytest

from engine.actions import Speech
from engine.engine import (
    PHASE_CYCLE,
    advance_phase,
    apply,
    next_phase,
    next_requests,
    resolve_automatic,
)
from engine.state import Phase
from tests.conftest import make_state


# --- dispatch --------------------------------------------------------------- #
def test_every_phase_resolves_to_a_handler() -> None:
    # Dispatch never raises KeyError; every phase returns a request list.
    for phase in Phase:
        assert isinstance(next_requests(make_state(phase)), list)


def test_game_over_is_terminal() -> None:
    state = make_state(Phase.GAME_OVER)
    assert next_requests(state) == []
    assert resolve_automatic(state) is state
    with pytest.raises(RuntimeError, match="game is over"):
        apply(state, Speech(actor_id="p0", message="x"))


# --- next_phase ------------------------------------------------------------- #
def test_next_phase_walks_the_cycle() -> None:
    expected = {
        Phase.NIGHT_MAFIA_CHAT: Phase.NIGHT_SEER,
        Phase.NIGHT_SEER: Phase.NIGHT_KILL,
        Phase.NIGHT_KILL: Phase.DAY_ANNOUNCE,
        Phase.DAY_ANNOUNCE: Phase.DAY_NARRATE,
        Phase.DAY_NARRATE: Phase.DAY_DISCUSS,
        Phase.DAY_DISCUSS: Phase.DAY_VOTE,
        Phase.DAY_VOTE: Phase.NIGHT_MAFIA_CHAT,
    }
    for phase, nxt in expected.items():
        assert next_phase(phase) is nxt


def test_cycle_covers_all_but_game_over() -> None:
    assert set(PHASE_CYCLE) == set(Phase) - {Phase.GAME_OVER}


def test_game_over_has_no_successor() -> None:
    with pytest.raises(ValueError, match="terminal"):
        next_phase(Phase.GAME_OVER)


# --- advance_phase ---------------------------------------------------------- #
def test_advance_steps_and_resets_counters() -> None:
    state = make_state(Phase.NIGHT_MAFIA_CHAT).model_copy(
        update={"speech_round": 1, "next_speaker": "p3"}
    )
    nxt = advance_phase(state)
    assert nxt.phase is Phase.NIGHT_SEER
    assert nxt.day_number == 1
    assert nxt.speech_round == 0
    assert nxt.next_speaker is None


def test_advance_wraps_and_increments_day() -> None:
    state = make_state(Phase.DAY_VOTE).model_copy(update={"next_voter": "p4"})
    nxt = advance_phase(state)
    assert nxt.phase is Phase.NIGHT_MAFIA_CHAT
    assert nxt.day_number == 2
    assert nxt.next_voter is None


def test_advance_is_pure() -> None:
    state = make_state(Phase.NIGHT_SEER)
    _ = advance_phase(state)
    assert state.phase is Phase.NIGHT_SEER
