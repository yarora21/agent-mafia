"""PR9b gate — the adapters map model output to legal engine actions, never raise.

Three properties, matching the plan's gate:
- the schemas round-trip through JSON;
- `to_engine_action` output passes `validate_action` for EVERY request type,
  over every request of many random games in both modes, with `actor_id`
  stamped from the request (never the model);
- a malformed dict yields a reason, not an exception (both adapters).
"""

from __future__ import annotations

import random

from agents.beliefs import (
    BeliefState,
    TargetOutput,
    TextOutput,
    output_schema_for,
    parse_belief_state,
    to_engine_action,
)
from engine.actions import (
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
from engine.referee import fallback_action, validate_action
from engine.state import GameState, Phase, Role, VotingMode, new_game

SEEDS = range(12)
MODES = (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL)
TARGET_TYPES = {ActionType.VOTE, ActionType.NIGHT_KILL, ActionType.INVESTIGATE}


# --------------------------------------------------------------------------- #
# Corpus — every (state, request) pair from random games
# --------------------------------------------------------------------------- #
def _iter_requests() -> list[tuple[GameState, ActionRequest]]:
    pairs: list[tuple[GameState, ActionRequest]] = []
    for mode in MODES:
        for seed in SEEDS:
            rng = random.Random(seed)
            state = new_game(seed=seed, mode=mode)
            guard = 0
            while state.phase is not Phase.GAME_OVER:
                guard += 1
                assert guard < 1000, "game failed to terminate"
                advanced = resolve_automatic(state)
                if advanced is not state:
                    state = advanced
                    continue
                reqs = next_requests(state)
                assert reqs
                req = rng.choice(reqs)
                pairs.append((state, req))
                state = apply(state, fallback_action(state, req))
    return pairs


REQUEST_CORPUS: list[tuple[GameState, ActionRequest]] = _iter_requests()


def _good_raw(req: ActionRequest) -> dict[str, object]:
    """A well-formed, legal model output dict for this request."""

    if req.action_type in TARGET_TYPES:
        return {"target_id": req.legal_targets[0]}
    return {"message": "i have a theory about this"}


def test_corpus_covers_all_action_types() -> None:
    kinds = {req.action_type for _, req in REQUEST_CORPUS}
    assert kinds == set(ActionType)


# --------------------------------------------------------------------------- #
# Schema round-trips
# --------------------------------------------------------------------------- #
def test_output_schemas_round_trip() -> None:
    tgt = TargetOutput(target_id="p3")
    assert TargetOutput.model_validate_json(tgt.model_dump_json()) == tgt
    txt = TextOutput(message="hello village")
    assert TextOutput.model_validate_json(txt.model_dump_json()) == txt


def test_belief_state_round_trips() -> None:
    belief = BeliefState(
        cursor=7,
        suspicions={"p2": 0.8, "p5": 0.1},
        confirmed_roles={"p4": Role.SEER},
        notes="p2 dodged the question",
    )
    assert BeliefState.model_validate_json(belief.model_dump_json()) == belief


def test_belief_state_defaults_are_empty() -> None:
    belief = BeliefState()
    assert belief.cursor is None
    assert belief.suspicions == {}
    assert belief.confirmed_roles == {}
    assert belief.notes == ""


# --------------------------------------------------------------------------- #
# output_schema_for
# --------------------------------------------------------------------------- #
def test_output_schema_for_matches_action_kind() -> None:
    for _, req in REQUEST_CORPUS:
        schema = output_schema_for(req)
        if req.action_type in TARGET_TYPES:
            assert schema is TargetOutput
        else:
            assert schema is TextOutput


# --------------------------------------------------------------------------- #
# to_engine_action — well-formed output is always a LEGAL action
# --------------------------------------------------------------------------- #
def test_good_output_maps_to_legal_action() -> None:
    expected = {
        ActionType.VOTE: Vote,
        ActionType.NIGHT_KILL: NightKill,
        ActionType.INVESTIGATE: Investigate,
        ActionType.SPEECH: Speech,
        ActionType.MAFIA_CHAT: MafiaChat,
        ActionType.NARRATE: Narration,
    }
    for state, req in REQUEST_CORPUS:
        action, reason = to_engine_action(_good_raw(req), req)
        assert reason is None
        assert action is not None
        # right concrete class, chosen from the request's action_type
        assert isinstance(action, expected[req.action_type])
        # actor stamped from the request; the referee agrees it is legal
        assert action.actor_id == req.actor_id
        assert validate_action(state, req, action) is None


def test_actor_id_comes_from_request_not_model() -> None:
    # A model that tries to spoof another actor is ignored: identity is stamped.
    _, req = REQUEST_CORPUS[0]
    raw = dict(_good_raw(req))
    raw["actor_id"] = "somebody-else"
    action, reason = to_engine_action(raw, req)
    assert reason is None
    assert action is not None
    assert action.actor_id == req.actor_id


def test_wellformed_but_illegal_target_is_left_for_referee() -> None:
    # The adapter builds a well-formed action; legality is NOT its job.
    target_req = next(
        req for _, req in REQUEST_CORPUS if req.action_type in TARGET_TYPES
    )
    state = next(s for s, r in REQUEST_CORPUS if r is target_req)
    action, reason = to_engine_action({"target_id": "ghost"}, target_req)
    assert reason is None
    assert action is not None  # well-formed...
    assert validate_action(state, target_req, action) is not None  # ...but illegal


# --------------------------------------------------------------------------- #
# to_engine_action — malformed output yields a reason, never an exception
# --------------------------------------------------------------------------- #
def test_malformed_target_output_returns_reason() -> None:
    target_req = next(
        req for _, req in REQUEST_CORPUS if req.action_type in TARGET_TYPES
    )
    action, reason = to_engine_action({"message": "wrong shape"}, target_req)
    assert action is None
    assert reason is not None and "TargetOutput" in reason


def test_malformed_text_output_returns_reason() -> None:
    text_req = next(
        req for _, req in REQUEST_CORPUS if req.action_type not in TARGET_TYPES
    )
    action, reason = to_engine_action({"target_id": "p1"}, text_req)
    assert action is None
    assert reason is not None and "TextOutput" in reason


def test_empty_dict_never_raises() -> None:
    for _, req in REQUEST_CORPUS:
        action, reason = to_engine_action({}, req)
        assert action is None
        assert reason is not None


# --------------------------------------------------------------------------- #
# parse_belief_state
# --------------------------------------------------------------------------- #
def test_parse_belief_state_good_dict() -> None:
    belief, reason = parse_belief_state(
        {"cursor": 3, "suspicions": {"p2": 0.5}, "notes": "hmm"}
    )
    assert reason is None
    assert belief is not None
    assert belief.cursor == 3
    assert belief.suspicions == {"p2": 0.5}


def test_parse_belief_state_malformed_returns_reason() -> None:
    belief, reason = parse_belief_state({"cursor": "not-an-int"})
    assert belief is None
    assert reason is not None and "BeliefState" in reason
