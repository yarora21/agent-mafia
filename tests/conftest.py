"""Shared test helpers for building deterministic game states."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from engine.state import (
    Event,
    EventType,
    GameState,
    Phase,
    PlayerState,
    Role,
    Visibility,
    VotingMode,
)

# A fixed roster used across engine tests, so seat/role assumptions are explicit:
#   p0 villager  p1 MAFIA  p2 SEER  p3 villager  p4 MAFIA  p5 villager  p6 villager
_ROSTER_ROLES: dict[int, Role] = {
    0: Role.VILLAGER,
    1: Role.MAFIA,
    2: Role.SEER,
    3: Role.VILLAGER,
    4: Role.MAFIA,
    5: Role.VILLAGER,
    6: Role.VILLAGER,
}


def make_state(
    phase: Phase,
    *,
    day_number: int = 1,
    dead: Iterable[str] = (),
    mode: VotingMode = VotingMode.SIMULTANEOUS,
) -> GameState:
    dead_set = set(dead)
    players = [
        PlayerState(
            id=f"p{seat}",
            seat=seat,
            role=role,
            alive=f"p{seat}" not in dead_set,
        )
        for seat, role in _ROSTER_ROLES.items()
    ]
    return GameState(
        players=players,
        phase=phase,
        voting_mode=mode,
        day_number=day_number,
    )


def after_night(
    *,
    kill: str = "p3",
    day_number: int = 1,
    also_dead: Iterable[str] = (),
    mode: VotingMode = VotingMode.SIMULTANEOUS,
) -> GameState:
    """A state parked at DAY_ANNOUNCE after a night that killed `kill`.

    Mirrors what the night handlers leave behind: the victim already dead and a
    MAFIA_ONLY NIGHT_KILL event recorded for the day.
    """

    dead = set(also_dead) | {kill}
    state = make_state(Phase.DAY_ANNOUNCE, day_number=day_number, dead=dead, mode=mode)
    kill_event = Event(
        event_id=0,
        type=EventType.NIGHT_KILL,
        visibility=Visibility.mafia_only(),
        day_number=day_number,
        phase=Phase.NIGHT_KILL,
        actor_id="p1",
        target_id=kill,
    )
    return state.model_copy(update={"history": [kill_event]})


@pytest.fixture(autouse=True)
def isolate_tracing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep offline tests independent of developer credentials and .env files."""
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
