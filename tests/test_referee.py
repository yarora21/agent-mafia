"""PR5 gate — the referee: validation reasons, fallback determinism, coerce safety.

Three properties, matching the plan's gate:
- ``validate_action`` returns a specific reason for each illegal case and
  ``None`` for legal ones;
- ``fallback_action`` is deterministic (same state+request → same action) and
  ALWAYS legal, checked over every request of many random games in both modes;
- ``coerce_action`` never raises: a ``None`` or any illegal action becomes a
  legal fallback, while a legal action passes through untouched.
"""

from __future__ import annotations

import random

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
from engine.engine import apply, next_requests, resolve_automatic
from engine.referee import (
    CANNED_MESSAGE,
    CANNED_NARRATION,
    coerce_action,
    fallback_action,
    validate_action,
)
from engine.state import (
    NARRATOR_ID,
    GameState,
    Phase,
    VotingMode,
    new_game,
)
from tests.conftest import after_night, make_state

SEEDS = range(12)
MODES = (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL)


# --------------------------------------------------------------------------- #
# Request builders — real requests with authentic legal_targets
# --------------------------------------------------------------------------- #
def _first_request(state: GameState) -> ActionRequest:
    reqs = next_requests(state)
    assert reqs, f"no request at {state.phase}"
    return reqs[0]


def _narrate_request() -> tuple[GameState, ActionRequest]:
    state = resolve_automatic(after_night(kill="p3"))  # DAY_ANNOUNCE -> DAY_NARRATE
    assert state.phase is Phase.DAY_NARRATE
    return state, _first_request(state)


# --------------------------------------------------------------------------- #
# validate_action — legal actions
# --------------------------------------------------------------------------- #
def test_legal_actions_validate_none() -> None:
    vote_state = make_state(Phase.DAY_VOTE)
    vote_req = _first_request(vote_state)
    ok_vote = Vote(actor_id=vote_req.actor_id, target_id=vote_req.legal_targets[0])
    assert validate_action(vote_state, vote_req, ok_vote) is None

    seer_state = make_state(Phase.NIGHT_SEER)
    seer_req = _first_request(seer_state)
    ok_inv = Investigate(
        actor_id=seer_req.actor_id, target_id=seer_req.legal_targets[0]
    )
    assert validate_action(seer_state, seer_req, ok_inv) is None

    kill_state = make_state(Phase.NIGHT_KILL)
    kill_req = _first_request(kill_state)
    ok_kill = NightKill(
        actor_id=kill_req.actor_id, target_id=kill_req.legal_targets[0]
    )
    assert validate_action(kill_state, kill_req, ok_kill) is None

    speech_state = make_state(Phase.DAY_DISCUSS)
    speech_req = _first_request(speech_state)
    ok_speech = Speech(actor_id=speech_req.actor_id, message="I suspect p4.")
    assert validate_action(speech_state, speech_req, ok_speech) is None

    narr_state, narr_req = _narrate_request()
    ok_narr = Narration(actor_id=narr_req.actor_id, message="A grim dawn.")
    assert validate_action(narr_state, narr_req, ok_narr) is None


# --------------------------------------------------------------------------- #
# validate_action — a reason per illegal case
# --------------------------------------------------------------------------- #
def test_wrong_action_type_is_rejected() -> None:
    state = make_state(Phase.DAY_VOTE)
    req = _first_request(state)  # a VOTE request
    wrong = Speech(actor_id=req.actor_id, message="hi")
    reason = validate_action(state, req, wrong)
    assert reason is not None and "vote" in reason and "speech" in reason


def test_wrong_actor_is_rejected() -> None:
    state = make_state(Phase.DAY_VOTE)
    req = _first_request(state)
    impostor = "p6" if req.actor_id != "p6" else "p5"
    action = Vote(actor_id=impostor, target_id=req.legal_targets[0])
    reason = validate_action(state, req, action)
    assert reason is not None and "does not match" in reason


def test_self_target_is_rejected() -> None:
    state = make_state(Phase.DAY_VOTE)
    req = _first_request(state)
    action = Vote(actor_id=req.actor_id, target_id=req.actor_id)
    reason = validate_action(state, req, action)
    assert reason is not None and "yourself" in reason


def test_dead_target_is_rejected() -> None:
    state = make_state(Phase.DAY_VOTE, dead=["p3"])
    req = _first_request(state)
    assert req.actor_id != "p3"
    action = Vote(actor_id=req.actor_id, target_id="p3")
    reason = validate_action(state, req, action)
    assert reason is not None and "dead" in reason


def test_unknown_target_is_rejected() -> None:
    state = make_state(Phase.DAY_VOTE)
    req = _first_request(state)
    action = Vote(actor_id=req.actor_id, target_id="ghost")
    reason = validate_action(state, req, action)
    assert reason is not None and "not a player" in reason


def test_mafia_cannot_night_kill_mafia() -> None:
    # p1 and p4 are Mafia in the fixed roster; p1 is the Night-1 killer.
    state = make_state(Phase.NIGHT_KILL)
    req = _first_request(state)
    assert req.actor_id == "p1"
    action = NightKill(actor_id="p1", target_id="p4")  # p4 is the ally
    reason = validate_action(state, req, action)
    assert reason is not None and "not a legal target" in reason


def test_empty_message_is_rejected() -> None:
    state = make_state(Phase.DAY_DISCUSS)
    req = _first_request(state)
    action = Speech(actor_id=req.actor_id, message="   ")
    reason = validate_action(state, req, action)
    assert reason is not None and "empty" in reason


# --------------------------------------------------------------------------- #
# fallback_action — canned text
# --------------------------------------------------------------------------- #
def test_fallback_free_text_is_canned() -> None:
    chat_req = _first_request(make_state(Phase.NIGHT_MAFIA_CHAT))
    fb_chat = fallback_action(make_state(Phase.NIGHT_MAFIA_CHAT), chat_req)
    assert isinstance(fb_chat, MafiaChat) and fb_chat.message == CANNED_MESSAGE

    speech_req = _first_request(make_state(Phase.DAY_DISCUSS))
    fb_speech = fallback_action(make_state(Phase.DAY_DISCUSS), speech_req)
    assert isinstance(fb_speech, Speech) and fb_speech.message == CANNED_MESSAGE

    narr_state, narr_req = _narrate_request()
    fb_narr = fallback_action(narr_state, narr_req)
    assert isinstance(fb_narr, Narration) and fb_narr.message == CANNED_NARRATION
    assert fb_narr.actor_id == NARRATOR_ID


# --------------------------------------------------------------------------- #
# Property sweep — every request of many random games, both modes
# --------------------------------------------------------------------------- #
def _iter_requests() -> list[tuple[GameState, ActionRequest]]:
    """Walk random games and collect every (state, request) pair encountered."""

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
                # Drive the game forward with a legal (fallback) action.
                state = apply(state, fallback_action(state, req))
    return pairs


REQUEST_CORPUS: list[tuple[GameState, ActionRequest]] = _iter_requests()


def test_corpus_is_nontrivial() -> None:
    assert len(REQUEST_CORPUS) > 200
    kinds = {req.action_type for _, req in REQUEST_CORPUS}
    assert kinds == set(ActionType)  # every action type exercised


def test_fallback_is_always_legal() -> None:
    for state, req in REQUEST_CORPUS:
        fb = fallback_action(state, req)
        assert validate_action(state, req, fb) is None, (
            f"fallback illegal at {state.phase} for {req.action_type}"
        )


def test_fallback_is_deterministic() -> None:
    for state, req in REQUEST_CORPUS:
        first = fallback_action(state, req)
        again = fallback_action(state, req)
        # A structurally-identical but distinct state object yields the same
        # fallback (seeding is on values, not object identity / salted hash).
        clone = state.model_copy(deep=True)
        on_clone = fallback_action(clone, req)
        assert first == again == on_clone


def _wrong_type_action(req: ActionRequest) -> Action:
    """An action whose type never matches the request — always illegal."""

    if req.action_type is ActionType.SPEECH:
        return Vote(actor_id=req.actor_id, target_id="p0")
    return Speech(actor_id=req.actor_id, message="wrong shape")


def test_coerce_never_raises_and_is_always_legal() -> None:
    for state, req in REQUEST_CORPUS:
        # None (malformed / no response) -> a legal fallback.
        coerced_none = coerce_action(state, req, None)
        assert validate_action(state, req, coerced_none) is None

        # An illegal action -> the fallback, never the illegal action itself.
        illegal = _wrong_type_action(req)
        coerced_bad = coerce_action(state, req, illegal)
        assert validate_action(state, req, coerced_bad) is None
        assert coerced_bad != illegal


def test_coerce_passes_legal_action_through() -> None:
    state = make_state(Phase.DAY_VOTE)
    req = _first_request(state)
    good = Vote(actor_id=req.actor_id, target_id=req.legal_targets[0])
    assert coerce_action(state, req, good) is good
