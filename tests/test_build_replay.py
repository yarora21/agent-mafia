"""PR15 gate — the X-ray replay renders a real game, safely, with masks marked.

Offline: a dry-run game (real `LlmAgent`s + stub client) yields a transcript that
carries a reasoning stream, so the whole render path is exercised with no API. It
proves: the view model correlates reasoning to events; the embedded JSON is valid
and cannot break out of its <script> tag; and Mafia "mask" moments are flagged.
"""

from __future__ import annotations

import json
import re

from config import GameConfig
from engine.state import VotingMode
from scripts.build_replay import build_replay, build_view_model, escape_for_script, render_html
from scripts.play_llm import dry_run_factory, play_llm

_DATA_RE = re.compile(
    r'<script id="game-data" type="application/json">(.*?)</script>', re.S
)


async def _transcript() -> "object":
    config = GameConfig(seed=2, voting_mode=VotingMode.SIMULTANEOUS)
    transcript, _ = await play_llm(config, factory=dry_run_factory(), write=False)
    return transcript


# --------------------------------------------------------------------------- #
# View model
# --------------------------------------------------------------------------- #
async def test_view_model_correlates_and_marks_masks() -> None:
    vm = build_view_model(await _transcript())  # type: ignore[arg-type]

    assert vm["steps"], "no steps rendered"
    # Some agent-driven events carry their private reasoning.
    assert any(s["reasoning"] for s in vm["steps"])
    # Automatic events (game start/over, death, vote outcome) never do.
    for s in vm["steps"]:
        if s["type"] in {"game_start", "game_over", "death", "vote_outcome"}:
            assert s["reasoning"] is None
    # Every player's true role is exposed (this is the omniscient X-ray).
    assert {p["role"] for p in vm["players"]} <= {"mafia", "seer", "villager"}
    # Mafia speaking/voting in public with reasoning = a marked mask moment.
    masks = [s for s in vm["steps"] if s["is_mask"]]
    assert masks
    for s in masks:
        assert s["faction"] == "mafia" and s["visibility"] == "public"


# --------------------------------------------------------------------------- #
# Embedding safety
# --------------------------------------------------------------------------- #
async def test_embedded_json_is_valid_and_matches() -> None:
    transcript = await _transcript()
    vm = build_view_model(transcript)  # type: ignore[arg-type]
    html = build_replay(transcript)  # type: ignore[arg-type]

    m = _DATA_RE.search(html)
    assert m is not None
    # The \\uXXXX escapes are valid JSON, so the embedded blob parses directly.
    assert json.loads(m.group(1)) == vm
    # Exactly the two real template <script> tags — nothing leaked from the data.
    assert html.count("</script>") == 2


def test_escape_neutralizes_script_breakout() -> None:
    payload = 'evil </script><script>alert(1)</script>'
    escaped = escape_for_script(payload)
    assert "</script>" not in escaped
    assert "<script>" not in escaped
    assert "\\u003c/script\\u003e" in escaped


def test_render_html_keeps_hostile_message_inert() -> None:
    vm = {
        "meta": {"seed": 1, "mode": "simultaneous", "winner": "town",
                 "actions": 1, "fallbacks": 0, "total_cost_usd": 0.0},
        "players": [],
        "steps": [{"type": "speech", "message": "hi </script><script>x</script>",
                   "is_night": False, "day": 1, "phase": "day_discuss",
                   "reasoning": None, "is_mask": False, "actor": "p0"}],
    }
    html = render_html(vm)
    assert "hi </script>" not in html  # the hostile literal never appears raw
    assert html.count("</script>") == 2  # only the template's own tags
    m = _DATA_RE.search(html)
    assert m is not None and json.loads(m.group(1)) == vm
