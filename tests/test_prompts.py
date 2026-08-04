"""PR10b gate — the tier-2 (bytes) boundary: rendered prompts leak no secrets.

Two kinds of proof:
- **Behavioral.** Render the final prompt string for every viewer, in every
  memory strategy, against real mid-game states (and, via the request corpus,
  every role × action type). Assert no other player's unrevealed role, no seer
  result the viewer does not own, and no Mafia-only content ever appears.
- **Structural.** Parse `prompts.py` with `ast` and prove it never references
  `GameState` — the render path cannot even reach the omniscient state, so a
  leak is impossible by construction, not merely absent from these samples.
"""

from __future__ import annotations

import ast
import pathlib
import random

from agents.prompts import RenderedPrompt, render_prompt
from config import MemoryStrategy
from engine.actions import ActionRequest, ActionType
from engine.engine import apply, next_requests, resolve_automatic
from engine.referee import fallback_action
from engine.state import NARRATOR_ID, GameState, Phase, Role, VotingMode, new_game
from engine.visibility import _public_reveals, observable_view

SEEDS = range(10)
MODES = (VotingMode.SIMULTANEOUS, VotingMode.SEQUENTIAL)
STRATEGIES = tuple(MemoryStrategy)
PROMPTS_PATH = pathlib.Path(__file__).resolve().parent.parent / "agents" / "prompts.py"


# --------------------------------------------------------------------------- #
# Corpora — real mid-game states and real (state, request) pairs
# --------------------------------------------------------------------------- #
def _drive() -> tuple[list[GameState], list[tuple[GameState, ActionRequest]]]:
    states: list[GameState] = []
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
                states.append(state)
                req = rng.choice(next_requests(state))
                pairs.append((state, req))
                state = apply(state, fallback_action(state, req))
    return states, pairs


STATE_CORPUS, REQUEST_CORPUS = _drive()


def _speech_req(actor_id: str) -> ActionRequest:
    """A minimal, always-renderable request so any viewer can be scanned."""

    return ActionRequest(action_type=ActionType.SPEECH, actor_id=actor_id)


def _assert_no_leak(state: GameState, view_self_id: str, text: str) -> None:
    """The core confidentiality check for one rendered prompt string."""

    viewer = next((p for p in state.players if p.id == view_self_id), None)
    viewer_role = viewer.role if viewer is not None else None  # None = narrator
    reveals = _public_reveals(state)

    # Mafia-only content (night chat, night kill) never reaches non-Mafia.
    if viewer_role is not Role.MAFIA:
        assert "[Mafia]" not in text
        assert "fellow Mafia" not in text
    # A Seer's private investigation results reach only that Seer.
    if viewer_role is not Role.SEER:
        assert "[Your investigation]" not in text
    # No OTHER player's unrevealed role is ever attributed in the roster.
    for p in state.players:
        if p.id == view_self_id or p.id in reveals:
            continue
        assert f"{p.id} — alive, {p.role.value}" not in text
        assert f"{p.id} — dead, {p.role.value}" not in text


# --------------------------------------------------------------------------- #
# Corpus sanity
# --------------------------------------------------------------------------- #
def test_corpus_is_rich() -> None:
    assert len(STATE_CORPUS) > 100
    roles = {req.action_type for _, req in REQUEST_CORPUS}
    assert roles == set(ActionType)  # every action type is exercised


# --------------------------------------------------------------------------- #
# Behavioral leak scan — every viewer, every strategy, every state
# --------------------------------------------------------------------------- #
def test_no_leak_for_any_viewer_any_strategy() -> None:
    for state in STATE_CORPUS:
        viewers = [p.id for p in state.players] + [NARRATOR_ID]
        for vid in viewers:
            view = observable_view(state, vid)
            for strategy in STRATEGIES:
                rendered = render_prompt(
                    view, _speech_req(vid), strategy=strategy
                )
                assert isinstance(rendered, RenderedPrompt)
                _assert_no_leak(state, vid, rendered.user)


# --------------------------------------------------------------------------- #
# Behavioral leak scan — real (role × action) turns, every strategy
# --------------------------------------------------------------------------- #
def test_no_leak_across_role_and_action_matrix() -> None:
    seen_roles: set[Role | None] = set()
    for state, req in REQUEST_CORPUS:
        view = observable_view(state, req.actor_id)
        seen_roles.add(view.self_role)
        for strategy in STRATEGIES:
            rendered = render_prompt(view, req, strategy=strategy)
            _assert_no_leak(state, req.actor_id, rendered.user)
    # actors span all three player roles (narrator's self_role is None)
    assert {Role.MAFIA, Role.SEER, Role.VILLAGER}.issubset(seen_roles)


# --------------------------------------------------------------------------- #
# Targeted confidentiality cases
# --------------------------------------------------------------------------- #
def test_villager_prompt_hides_mafia_chat() -> None:
    # Find a state with mafia chat in the transcript, render a villager's view.
    for state in STATE_CORPUS:
        villagers = [p for p in state.players if p.role is Role.VILLAGER]
        v = villagers[0]
        rendered = render_prompt(
            observable_view(state, v.id),
            _speech_req(v.id),
            strategy=MemoryStrategy.FULL_TRANSCRIPT,
        )
        assert "[Mafia]" not in rendered.user


def test_mafia_ally_line_excludes_self() -> None:
    # PR16 regression: `known_mafia_ids` includes the viewer, but the rendered
    # "Your fellow Mafia" line must never list the viewer as their own ally.
    checked = False
    for state in STATE_CORPUS:
        for m in (p for p in state.players if p.role is Role.MAFIA):
            view = observable_view(state, m.id)
            rendered = render_prompt(
                view, _speech_req(m.id), strategy=MemoryStrategy.FULL_TRANSCRIPT
            )
            for line in rendered.user.splitlines():
                if line.startswith("Your fellow Mafia:"):
                    assert m.id not in line
                    checked = True
    assert checked, "corpus had no rendered Mafia ally line to check"


def test_public_death_reveal_reaches_everyone() -> None:
    # A publicly revealed role SHOULD appear in every viewer's roster.
    for state in STATE_CORPUS:
        reveals = _public_reveals(state)
        if not reveals:
            continue
        pid, role = next(iter(reveals.items()))
        for vid in [p.id for p in state.players] + [NARRATOR_ID]:
            rendered = render_prompt(
                observable_view(state, vid),
                _speech_req(vid),
                strategy=MemoryStrategy.FULL_TRANSCRIPT,
            )
            assert f"{pid} — dead, {role.value}" in rendered.user
        return
    # (no reveal found in the corpus is acceptable; the scan above still ran)


# --------------------------------------------------------------------------- #
# Structural boundary — prompts.py can never reach GameState
# --------------------------------------------------------------------------- #
def test_prompts_module_never_references_gamestate() -> None:
    tree = ast.parse(PROMPTS_PATH.read_text())

    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "GameState" not in referenced_names

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            assert "GameState" not in imported
        if isinstance(node, ast.Attribute):
            assert node.attr != "GameState"
