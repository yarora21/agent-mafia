"""PR2 gate: construction + JSON round-trip for the domain types."""

from pydantic import TypeAdapter

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
    Event,
    EventType,
    Faction,
    GameState,
    Phase,
    PlayerState,
    Role,
    Visibility,
    VisibilityKind,
    VotingMode,
    faction_of,
    new_game,
)

_action_adapter: TypeAdapter[Action] = TypeAdapter(Action)


def test_faction_of() -> None:
    assert faction_of(Role.MAFIA) is Faction.MAFIA
    assert faction_of(Role.SEER) is Faction.TOWN
    assert faction_of(Role.VILLAGER) is Faction.TOWN


def test_visibility_constructors() -> None:
    assert Visibility.public().kind is VisibilityKind.PUBLIC
    assert Visibility.mafia_only().kind is VisibilityKind.MAFIA_ONLY
    priv = Visibility.player_only("p3")
    assert priv.kind is VisibilityKind.PLAYER_ONLY
    assert priv.player_id == "p3"


def test_visibility_round_trip() -> None:
    for vis in (Visibility.public(), Visibility.mafia_only(), Visibility.player_only("p1")):
        assert Visibility.model_validate_json(vis.model_dump_json()) == vis


def test_event_round_trip() -> None:
    event = Event(
        event_id=7,
        type=EventType.SEER_RESULT,
        visibility=Visibility.player_only("p2"),
        day_number=1,
        phase=Phase.NIGHT_SEER,
        actor_id="p2",
        target_id="p5",
        seer_result=True,
    )
    assert Event.model_validate_json(event.model_dump_json()) == event


def test_game_state_round_trip() -> None:
    state = new_game(seed=42, mode=VotingMode.SEQUENTIAL)
    assert GameState.model_validate_json(state.model_dump_json()) == state


def test_action_union_round_trip() -> None:
    actions: list[Action] = [
        Speech(actor_id="p0", message="hello"),
        Vote(actor_id="p1", target_id="p2"),
        MafiaChat(actor_id="p3", message="kill p4"),
        NightKill(actor_id="p3", target_id="p4"),
        Investigate(actor_id="p5", target_id="p0"),
        Narration(actor_id="narrator", message="a grim dawn"),
    ]
    for action in actions:
        dumped = _action_adapter.dump_json(action)
        restored = _action_adapter.validate_json(dumped)
        assert restored == action
        assert restored.type == action.type


def test_action_request_defaults() -> None:
    req = ActionRequest(action_type=ActionType.SPEECH, actor_id="p0")
    assert req.legal_targets == []
    req2 = ActionRequest(
        action_type=ActionType.VOTE, actor_id="p0", legal_targets=["p1", "p2"]
    )
    assert ActionRequest.model_validate_json(req2.model_dump_json()) == req2


def test_models_are_frozen() -> None:
    player = PlayerState(id="p0", seat=0, role=Role.VILLAGER)
    import pytest

    with pytest.raises(Exception):
        player.alive = False
