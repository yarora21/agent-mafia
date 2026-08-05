"""PR2 gate: the deterministic deal."""

from collections import Counter

from engine.state import (
    NUM_PLAYERS,
    Phase,
    Role,
    VotingMode,
    new_game,
)


def test_deal_shape() -> None:
    state = new_game(seed=1, mode=VotingMode.SIMULTANEOUS)
    assert len(state.players) == NUM_PLAYERS
    assert [p.seat for p in state.players] == list(range(NUM_PLAYERS))
    assert [p.id for p in state.players] == [f"p{i}" for i in range(NUM_PLAYERS)]
    assert all(p.alive for p in state.players)


def test_role_counts() -> None:
    state = new_game(seed=123, mode=VotingMode.SIMULTANEOUS)
    counts = Counter(p.role for p in state.players)
    assert counts[Role.MAFIA] == 2
    assert counts[Role.SEER] == 1
    assert counts[Role.VILLAGER] == 5


def test_starting_position() -> None:
    state = new_game(seed=1, mode=VotingMode.SEQUENTIAL)
    assert state.phase is Phase.NIGHT_MAFIA_CHAT
    assert state.day_number == 1
    assert state.voting_mode is VotingMode.SEQUENTIAL
    assert state.history == []
    assert state.rng_seed == 1


def test_deterministic_from_seed() -> None:
    a = new_game(seed=777, mode=VotingMode.SIMULTANEOUS)
    b = new_game(seed=777, mode=VotingMode.SIMULTANEOUS)
    assert a == b
    assert a.model_dump_json() == b.model_dump_json()


def test_different_seeds_differ() -> None:
    # Not guaranteed for every pair, but these two seeds deal differently.
    a = new_game(seed=1, mode=VotingMode.SIMULTANEOUS)
    b = new_game(seed=2, mode=VotingMode.SIMULTANEOUS)
    assert [p.role for p in a.players] != [p.role for p in b.players]


def test_mode_recorded_not_dealt() -> None:
    # Voting mode must not perturb the role deal for a fixed seed.
    sim = new_game(seed=55, mode=VotingMode.SIMULTANEOUS)
    seq = new_game(seed=55, mode=VotingMode.SEQUENTIAL)
    assert [p.role for p in sim.players] == [p.role for p in seq.players]
