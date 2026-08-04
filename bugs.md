# Bugs & known quirks

Log from the PR16 transcript-quality pass. Each entry: what, where, status.

## Fixed

### 1. Mafia prompt listed the viewer among its own allies
- **What.** The "Your fellow Mafia" line rendered `known_mafia_ids` verbatim.
  That list intentionally includes the viewer (correct for the data layer), so
  p2 was told *"Your fellow Mafia: p2, p3."*
- **Where.** `agents/prompts.py` (`render_prompt`).
- **Impact.** A model noticed the contradiction and spent a whole turn on it
  (v0 game 0, p2's second Mafia-chat turn). Also mislabeled the solo-Mafia case.
- **Fix.** Exclude `view.self_id` from the ally line; bump `PROMPT_VERSION`
  v0 → v1. Regression test: `tests/test_prompts.py::test_mafia_ally_line_excludes_self`.
  Demo transcripts regenerated on v1.

## Known quirks (not code bugs)

### 2. Literal `\n` inside belief notes
- **What.** Some belief `notes` contain a literal backslash-n instead of a real
  newline (e.g. v0 game 1, p6). This is the model emitting `\n` as text, not a
  serialization fault — the JSON is faithful to what the model returned.
- **Status.** Left as-is; the transcript records true model output. Would only
  change with post-processing of model text, which we deliberately avoid.

### 3. Superseded v0 demo transcripts
- **What.** The pre-PR16 demo games (`20260804T134443Z_0_*`,
  `20260804T140911Z_1_*`) were generated on prompt `v0`, before fix #1. They do
  not match the current prompt code and cannot be reproduced (LLM output is
  nondeterministic).
- **Status.** Superseded by the v1 games. Kept pending a decision on removal.
