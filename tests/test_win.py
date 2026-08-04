"""PR3c gate: win conditions, GAME_OVER transitions, and full-game termination."""

from __future__ import annotations

from engine.actions import (
    Action,
    ActionType,
    Investigate,
    MafiaChat,
    Narration,
    NightKill,
    Speech,
    Vote,
)
from engine.engine import (
    apply,
    next_requests,
    resolve_automatic,
    winner,
)
from engine.state import (
    NARRATOR_ID,
    EventType,
    Faction,
    GameState,
    Phase,
    VotingMode,
    new_game,
)
from tests.conftest import make_state


# --- winner() predicate ----------------------------------------------------- #
def test_no_winner_at_full_roster() -> None:
    assert winner(make_state(Phase.DAY_DISCUSS)) is None


def test_town_wins_when_all_mafia_dead() -> None:
    # kill both mafia p1, p4
    state = make_state(Phase.DAY_DISCUSS, dead=["p1", "p4"])
    assert winner(state) is Faction.TOWN


def test_mafia_wins_at_parity() -> None:
    # living: p1(mafia), p4(mafia), p6(town) -> 2 mafia >= 1 town
    state = make_state(Phase.DAY_DISCUSS, dead=["p0", "p2", "p3", "p5"])
    assert winner(state) is Faction.MAFIA


# --- game-ending night kill ------------------------------------------------- #
def test_night_kill_that_ends_game_announces_then_game_over() -> None:
    # living before kill: p1,p4 (mafia), p2,p6 (town). Killer p1 takes p6 ->
    # 2 mafia vs 1 town -> Mafia win. Death still announced, then GAME_OVER.
    state = make_state(Phase.NIGHT_KILL, day_number=1, dead=["p0", "p3", "p5"])
    killer = next_requests(state)[0].actor_id
    after_kill = apply(state, NightKill(actor_id=killer, target_id="p6"))
    assert after_kill.phase is Phase.DAY_ANNOUNCE  # not short-circuited yet

    end = resolve_automatic(after_kill)  # the announce
    types = [e.type for e in end.history]
    assert EventType.DEATH in types  # final death still announced
    assert types[-1] is EventType.GAME_OVER
    assert end.history[-1].winner is Faction.MAFIA
    assert end.phase is Phase.GAME_OVER
    # no narration / speech / vote after a game-ending night kill
    assert EventType.NARRATION not in types
    assert EventType.SPEECH not in types
    assert EventType.VOTE not in types


# --- game-ending vote ------------------------------------------------------- #
def test_voting_out_last_mafia_is_a_town_win() -> None:
    # one mafia already dead (p4); everyone votes out the last mafia p1.
    state = make_state(
        Phase.DAY_VOTE, day_number=1, dead=["p4"], mode=VotingMode.SIMULTANEOUS
    )
    living = [p.id for p in state.players if p.alive]
    votes = {v: ("p1" if v != "p1" else "p0") for v in living}
    for voter, target in votes.items():
        state = apply(state, Vote(actor_id=voter, target_id=target))
    assert state.phase is Phase.GAME_OVER
    outcome = state.history[-1]
    assert outcome.type is EventType.GAME_OVER
    assert outcome.winner is Faction.TOWN


def test_game_over_is_terminal() -> None:
    state = make_state(Phase.GAME_OVER)
    assert next_requests(state) == []
    assert resolve_automatic(state) is state


# --- full game to termination ----------------------------------------------- #
def _script_action(state: GameState) -> Action:
    """A deterministic strategy good enough to drive a game to completion:
    town always votes the lowest-seated living player they can see."""

    req = next_requests(state)[0]
    living = [p for p in state.players if p.alive]
    kind = req.action_type
    if kind is ActionType.MAFIA_CHAT:
        return MafiaChat(actor_id=req.actor_id, message="plan")
    if kind is ActionType.INVESTIGATE:
        return Investigate(actor_id=req.actor_id, target_id=req.legal_targets[0])
    if kind is ActionType.NIGHT_KILL:
        return NightKill(actor_id=req.actor_id, target_id=req.legal_targets[0])
    if kind is ActionType.NARRATE:
        return Narration(actor_id=NARRATOR_ID, message="dawn")
    if kind is ActionType.SPEECH:
        return Speech(actor_id=req.actor_id, message="hmm")
    return Vote(actor_id=req.actor_id, target_id=req.legal_targets[0])


def _play_to_end(seed: int, mode: VotingMode) -> GameState:
    state = new_game(seed=seed, mode=mode)
    guard = 0
    while state.phase is not Phase.GAME_OVER:
        guard += 1
        assert guard < 500, "game failed to terminate"
        state = resolve_automatic(state)
        if state.phase is Phase.GAME_OVER:
            break
        reqs = next_requests(state)
        if not reqs:
            continue  # resolve_automatic advanced a no-input phase
        state = apply(state, _script_action(state))
    return state


def test_scripted_games_terminate_both_modes() -> None:
    for mode in (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL):
        for seed in range(6):
            end = _play_to_end(seed, mode)
            assert end.phase is Phase.GAME_OVER
            assert end.history[-1].type is EventType.GAME_OVER
            assert end.history[-1].winner in (Faction.TOWN, Faction.MAFIA)


def test_town_win_is_reachable() -> None:
    # Directed: at least one seed drives the scripted game to a Town win.
    outcomes = {
        _play_to_end(seed, VotingMode.SEQUENTIAL).history[-1].winner
        for seed in range(20)
    }
    assert Faction.TOWN in outcomes
