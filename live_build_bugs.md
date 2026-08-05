# Live Build — Bugs & Issues

Running log of bugs, gotchas, and surprises hit while building the live X-ray
feature (see `live_plan.md`). Newest at the top.

Format per entry:
- **[date] Title** — what happened / root cause / fix.

---

## Add-a-villager (7 → 8 players)

- **[2026-08-05] Seed-coupled reliability test broke on the deal change** —
  `test_illegal_then_legal_is_retried_ok` scripted **p0** to answer illegally
  once and asserted `retried_ok >= 1`. It relied on seed 7's *7-player* deal
  making p0 act. After adding a villager, seed 7's 8-player deal makes p0 a
  villager who dies Night 1 and never acts, so the retry never fires →
  `assert 0 >= 1`. Root cause: the test was coupled to a specific seed's deal.
  Fixed by switching to **seed 3**, where p0 is dealt Mafia (acts first in
  Night-1 chat), and documenting *why* in the test so it won't silently re-break.
  All 182 tests pass; mypy clean.

## Slice 1

- **[2026-08-05] `GameState` has no `day` attribute** — the `_on_action` payload
  first read `at_state.day`; the field is `day_number`. Caught before running
  (grep of `engine/state.py`), fixed to `at_state.day_number`. Offline dry-run
  smoke then emitted 51 action events cleanly; mypy clean on `orchestrator.py` +
  `scripts/play_llm.py`.

---

## Pre-existing (fixed before Slice 1)

- **[2026-08-05] `AttributeError: 'RunResult' object has no attribute 'total_cost_usd'`**
  — `webapp.py` read `run.total_cost_usd`, but the `run` from `play_llm` is a
  `RunResult` (has `cost: CostLedger`, no total). The cost total lives on
  `transcript.run` (a `RunSummary`). Fixed by reading
  `transcript.run.total_cost_usd`. Crashed *every* game before the transcript
  write step, so no games were saved and none counted against the daily cap.
