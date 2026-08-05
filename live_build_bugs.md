# Live Build — Bugs & Issues

Running log of bugs, gotchas, and surprises hit while building the live X-ray
feature (see `live_plan.md`). Newest at the top.

Format per entry:
- **[date] Title** — what happened / root cause / fix.

---

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
