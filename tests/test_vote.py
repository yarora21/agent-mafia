"""PR3b-ii gate: DAY_VOTE in both modes — staging, reveal timing, plurality, ties."""

from __future__ import annotations

from engine.actions import ActionType, Vote
from engine.engine import apply, next_requests
from engine.state import (
    Event,
    EventType,
    GameState,
    Phase,
    Role,
    VotingMode,
)
from tests.conftest import make_state


def _cast(state: GameState, votes: dict[str, str]) -> GameState:
    for voter, target in votes.items():
        state = apply(state, Vote(actor_id=voter, target_id=target))
    return state


def _vote_events(state: GameState) -> list[Event]:
    return [e for e in state.history if e.type is EventType.VOTE]


def _outcome(state: GameState) -> Event:
    return next(e for e in state.history if e.type is EventType.VOTE_OUTCOME)


# --- request shape (both modes) --------------------------------------------- #
def test_legal_targets_exclude_self() -> None:
    state = make_state(Phase.DAY_VOTE, mode=VotingMode.SIMULTANEOUS)
    reqs = next_requests(state)
    by_voter = {r.actor_id: r for r in reqs}
    assert by_voter["p0"].action_type is ActionType.VOTE
    assert "p0" not in by_voter["p0"].legal_targets
    assert set(by_voter["p0"].legal_targets) == {f"p{i}" for i in range(1, 7)}


# --- simultaneous ----------------------------------------------------------- #
def test_simultaneous_asks_all_living_at_once() -> None:
    state = make_state(Phase.DAY_VOTE, mode=VotingMode.SIMULTANEOUS)
    reqs = next_requests(state)
    assert {r.actor_id for r in reqs} == {f"p{i}" for i in range(7)}


def test_simultaneous_ballots_hidden_until_all_in() -> None:
    state = make_state(Phase.DAY_VOTE, mode=VotingMode.SIMULTANEOUS)
    # cast 3 of 7 — nothing revealed yet
    partial = _cast(state, {"p0": "p3", "p1": "p3", "p2": "p3"})
    assert _vote_events(partial) == []
    assert partial.phase is Phase.DAY_VOTE
    assert len(partial.ballots) == 3


def test_simultaneous_reveals_in_seat_order_then_outcome() -> None:
    state = make_state(Phase.DAY_VOTE, day_number=1, mode=VotingMode.SIMULTANEOUS)
    # p3 (villager) takes the plurality with 4 votes
    votes = {
        "p6": "p5",  # apply in non-seat order to prove reveal order is independent
        "p0": "p3",
        "p4": "p3",
        "p2": "p3",
        "p1": "p3",
        "p3": "p0",
        "p5": "p6",
    }
    end = _cast(state, votes)
    revealed = [(e.actor_id, e.target_id) for e in _vote_events(end)]
    assert [a for a, _ in revealed] == [f"p{i}" for i in range(7)]  # seat order
    outcome = _outcome(end)
    assert outcome.target_id == "p3"
    assert outcome.revealed_role is Role.VILLAGER
    assert not next(p for p in end.players if p.id == "p3").alive
    assert end.ballots == {}
    assert end.phase is Phase.NIGHT_MAFIA_CHAT  # wrapped to next night
    assert end.day_number == 2


def test_simultaneous_application_order_independent() -> None:
    state = make_state(Phase.DAY_VOTE, mode=VotingMode.SIMULTANEOUS)
    votes = {"p0": "p3", "p1": "p3", "p2": "p3", "p3": "p0", "p4": "p3", "p5": "p6", "p6": "p5"}
    a = _cast(state, votes)
    b = _cast(state, dict(reversed(list(votes.items()))))
    assert _outcome(a).target_id == _outcome(b).target_id == "p3"


def test_tie_produces_no_elimination() -> None:
    state = make_state(Phase.DAY_VOTE, mode=VotingMode.SIMULTANEOUS)
    # p3: 3 votes, p5: 3 votes, p6: 1 vote -> tie at the top
    votes = {
        "p0": "p3", "p1": "p3", "p2": "p3",
        "p3": "p5", "p4": "p5", "p6": "p5",
        "p5": "p6",
    }
    end = _cast(state, votes)
    outcome = _outcome(end)
    assert outcome.target_id is None
    assert outcome.revealed_role is None
    assert all(p.alive for p in end.players)  # nobody eliminated
    assert end.phase is Phase.NIGHT_MAFIA_CHAT


# --- sequential ------------------------------------------------------------- #
def test_sequential_asks_one_at_a_time_in_speaking_order() -> None:
    state = make_state(Phase.DAY_VOTE, day_number=1, mode=VotingMode.SEQUENTIAL)
    order = ["p1", "p2", "p3", "p4", "p5", "p6", "p0"]  # day 1, 7 living, start=1
    seen: list[str] = []
    while state.phase is Phase.DAY_VOTE:
        reqs = next_requests(state)
        assert len(reqs) == 1  # one voter at a time
        voter = reqs[0].actor_id
        seen.append(voter)
        state = apply(state, Vote(actor_id=voter, target_id="p3"))
    assert seen == order


def test_sequential_reveals_each_ballot_on_cast() -> None:
    state = make_state(Phase.DAY_VOTE, day_number=1, mode=VotingMode.SEQUENTIAL)
    # after the first ballot, it is already public (later voters can see it)
    first_voter = next_requests(state)[0].actor_id
    after_one = apply(state, Vote(actor_id=first_voter, target_id="p3"))
    votes = _vote_events(after_one)
    assert len(votes) == 1
    assert votes[0].actor_id == first_voter
    assert after_one.phase is Phase.DAY_VOTE  # not done yet


def test_sequential_plurality_eliminates_and_advances() -> None:
    state = make_state(Phase.DAY_VOTE, day_number=1, mode=VotingMode.SEQUENTIAL)
    while state.phase is Phase.DAY_VOTE:
        voter = next_requests(state)[0].actor_id
        # everyone piles on p5 (a villager)
        state = apply(state, Vote(actor_id=voter, target_id="p5"))
    assert len(_vote_events(state)) == 7  # all ballots public
    outcome = _outcome(state)
    assert outcome.target_id == "p5"
    assert not next(p for p in state.players if p.id == "p5").alive
    assert state.phase is Phase.NIGHT_MAFIA_CHAT
    assert state.day_number == 2
