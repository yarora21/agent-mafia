"""PR6b gate — the orchestrator wires agents to the engine end to end.

Claims proven here:
- random games COMPLETE in both voting modes (reach GAME_OVER with a winner);
- runs are REPRODUCIBLE from a seed (same (seed, mode) -> identical final state
  and identical applied-action sequence);
- the orchestrator's use of `coerce_action` gives LIVENESS: even agents that
  always return `None` or an illegal action can't stall the game;
- the `asyncio.gather` fan-out for simultaneous votes is BLIND (no voter sees a
  current-day ballot while voting is open), whereas sequential voters can see
  earlier ballots — the one observable mode difference;
- per-agent event cursors stay within what each actor may legally have seen.
"""

from __future__ import annotations

from engine.actions import Action, ActionRequest, ActionType, Vote
from engine.state import (
    NARRATOR_ID,
    NUM_PLAYERS,
    EventType,
    Faction,
    Phase,
    VotingMode,
)
from engine.visibility import ObservableState, observable_view
from agents.interface import AgentInterface
from orchestrator import (
    RunResult,
    build_random_agents,
    play_game,
    play_random_game,
)

SEEDS = range(8)
MODES = (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL)


def _assert_finished(result: RunResult) -> None:
    state = result.final_state
    assert state.phase is Phase.GAME_OVER
    assert state.history[-1].type is EventType.GAME_OVER
    assert state.history[-1].winner in (Faction.TOWN, Faction.MAFIA)


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #
async def test_random_games_complete_both_modes() -> None:
    for mode in MODES:
        for seed in SEEDS:
            _assert_finished(await play_random_game(seed, mode))


async def test_a_completed_game_records_actions() -> None:
    result = await play_random_game(0, VotingMode.SEQUENTIAL)
    kinds = {a.type for a in result.actions}
    # A full game exercises the day-talking and voting action types at least.
    assert ActionType.SPEECH in kinds
    assert ActionType.VOTE in kinds


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #
async def test_runs_are_reproducible_from_seed() -> None:
    for mode in MODES:
        for seed in SEEDS:
            a = await play_random_game(seed, mode)
            b = await play_random_game(seed, mode)
            assert a.final_state == b.final_state
            assert a.actions == b.actions
            assert a.cursors == b.cursors


async def test_different_seeds_diverge() -> None:
    # Not a hard guarantee for every pair, but across a range the histories differ.
    finals = {
        (await play_random_game(seed, VotingMode.SEQUENTIAL)).final_state.history[-1]
        for seed in range(12)
    }
    assert len(finals) > 1


# --------------------------------------------------------------------------- #
# Liveness under misbehaving agents (coerce_action doing its job)
# --------------------------------------------------------------------------- #
class _NoneAgent:
    """Always fails to produce a usable action."""

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        return None


class _IllegalAgent:
    """Always returns an action that is illegal for any request.

    A Vote for a non-existent target is wrong-type for non-vote requests and an
    illegal target for vote requests — illegal in every phase.
    """

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        return Vote(actor_id=request.actor_id, target_id="ghost")


async def test_none_returning_agents_still_finish() -> None:
    agents = {f"p{i}": _NoneAgent() for i in range(NUM_PLAYERS)}
    agents[NARRATOR_ID] = _NoneAgent()
    for mode in MODES:
        _assert_finished(await play_game(agents, seed=3, mode=mode))


async def test_illegal_returning_agents_still_finish() -> None:
    agents = {f"p{i}": _IllegalAgent() for i in range(NUM_PLAYERS)}
    agents[NARRATOR_ID] = _IllegalAgent()
    for mode in MODES:
        _assert_finished(await play_game(agents, seed=5, mode=mode))


# --------------------------------------------------------------------------- #
# The simultaneous vs sequential difference, observed through what agents saw
# --------------------------------------------------------------------------- #
class _RecordingAgent:
    """Wraps a real agent, logging every (request, view) it is handed."""

    def __init__(
        self,
        inner: AgentInterface,
        log: list[tuple[ActionRequest, ObservableState]],
    ) -> None:
        self._inner = inner
        self._log = log

    async def act(
        self, view: ObservableState, request: ActionRequest
    ) -> Action | None:
        self._log.append((request, view))
        return await self._inner.act(view, request)


async def _play_recording(
    seed: int, mode: VotingMode
) -> list[tuple[ActionRequest, ObservableState]]:
    log: list[tuple[ActionRequest, ObservableState]] = []
    base = build_random_agents(seed)
    agents = {aid: _RecordingAgent(inner, log) for aid, inner in base.items()}
    await play_game(agents, seed=seed, mode=mode)
    return log


async def test_simultaneous_votes_are_blind() -> None:
    for seed in SEEDS:
        log = await _play_recording(seed, VotingMode.SIMULTANEOUS)
        vote_views = [
            view for req, view in log if req.action_type is ActionType.VOTE
        ]
        assert vote_views, "expected some vote requests"
        for view in vote_views:
            # No ballot for the current day is visible while voting is open.
            assert not any(
                e.type is EventType.VOTE and e.day_number == view.day_number
                for e in view.events
            )


async def test_sequential_voters_can_see_earlier_ballots() -> None:
    saw_prior_ballot = False
    for seed in SEEDS:
        log = await _play_recording(seed, VotingMode.SEQUENTIAL)
        for req, view in log:
            if req.action_type is not ActionType.VOTE:
                continue
            if any(
                e.type is EventType.VOTE and e.day_number == view.day_number
                for e in view.events
            ):
                saw_prior_ballot = True
    assert saw_prior_ballot, "a later sequential voter should see earlier ballots"


# --------------------------------------------------------------------------- #
# Per-agent cursors
# --------------------------------------------------------------------------- #
async def test_cursors_stay_within_what_each_actor_could_see() -> None:
    for mode in MODES:
        result = await play_random_game(4, mode)
        final = result.final_state
        for player in final.players:
            cursor = result.cursors[player.id]
            visible = observable_view(final, player.id).events
            highest_visible = visible[-1].event_id if visible else -1
            assert -1 <= cursor <= highest_visible
