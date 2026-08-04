# Phase 2 build plan — LLM agents

Turns the Phase 1 engine into a real game: seven LLM players plus the Narrator, producing a
valid transcript JSON per game. Companion to `MAFIA_AGENTS_SPEC.md` (Sections 3.2-3.4).

Guiding rule for sequencing: **stay off the API as long as possible.** Every part is verified
against mock/scripted agents first; the first real API call happens in Part H, when everything
around it already works.

---

## Where Phase 1 left us (what this plan builds on)

Verified working (71 tests passing):

- `engine/` — pure state machine. The loop contract is `next_requests(state)` /
  `resolve_automatic(state)` / `apply(state, action)`; simultaneous voting is the one case
  where `next_requests` returns multiple requests.
- `engine/visibility.py` — `observable_view(state, player_id) -> ObservableState` with
  `known_mafia_ids`, filtered `events` (event_ids preserved), roster with revealed-only roles.
  Narrator gets the public-only `_narrator_view`.
- `engine/referee.py` — `validate_action` returns a human-readable reason (written to be
  appended to a retry prompt); `fallback_action` is deterministic (seeded from game rng +
  event counter); `coerce_action` never fails.
- `orchestrator.py` — async loop, `asyncio.gather` fan-out for simultaneous votes, per-agent
  event cursors already tracked in `GameRun.cursors` (currently unused), `FallbackRecord`
  logging, `max_steps` liveness guard.
- `agents/interface.py` — `async def act(view, request) -> Action`, runtime-checkable Protocol.
- `agents/random_agent.py` — seeded, legal-move-only baseline.

Known gaps Phase 2 must fill (each is a Part below):

1. No retry loop — `_dispatch` coerces in one shot; the spec's "one retry with the validation
   error appended" belongs in the orchestrator (referee.py's docstring already says so).
2. `GameRun.cursors` is written but never read — the `beliefs_only` delta depends on it.
3. No cost accounting, no cost-ceiling abort.
4. No transcript writer; `games/` doesn't exist yet.
5. No `agents/beliefs.py`, `agents/prompts.py`, `agents/llm_agent.py`, no Narrator agent.
6. No API-failure path (backoff) distinct from malformed output.

---

## Part A — Dependencies, config, and secrets

**Build:**
- Add to `pyproject.toml`: `anthropic` (async client), `langgraph`, `langchain-anthropic`
  (LangGraph's Anthropic model binding). LangGraph is a HARD requirement per the spec — do not
  substitute plain async functions.
- `config.py` (top level): a Pydantic `GameConfig` — model id per role group (players vs.
  narrator), memory strategy (default `beliefs_plus_recent`, N=20), voting mode, seed,
  `PROMPT_VERSION` (stamped from prompts.py), cost ceiling (USD), retry/backoff settings.
  This object is what gets serialized into every transcript, so define it once here.
- API key via environment (`ANTHROPIC_API_KEY`); never in config files or transcripts.

**Gate:** `GameConfig` round-trips to JSON; mypy stays clean with new deps.

---

## Part B — Belief state and LLM output schemas (`agents/beliefs.py`)

The structured outputs the LLM will emit. Engine-side `Action` models already exist and stay
untouched — these are the *LLM-facing* schemas that map onto them.

**Build:**
- `BeliefState`: per-player entries keyed by player id — suspicion score (0-100), free-text
  note, claimed-role ledger (who has claimed what, and when). Plus a `last_event_id` field:
  the cursor value at the last update, serialized into transcripts.
- One output schema per ActionRequest type, minimal by design:
  - target actions → `{"target_id": str}` (actor_id comes from the request, never the LLM)
  - text actions → `{"message": str}`
  - belief update → the full revised `BeliefState`
- Adapter `to_engine_action(request, llm_output) -> Action` that stamps `actor_id` from the
  request. The LLM never chooses who it is — one less thing `validate_action` has to catch.

**Gate (no API):** unit tests — schemas round-trip JSON; adapter output passes
`referee.validate_action` for every request type; a malformed dict surfaces as a validation
reason, not an exception.

---

## Part C — Orchestrator upgrades, tested entirely on mocks

All the reliability machinery, built against scripted agents before any LLM exists.

**Build:**
- `agents/mock_agent.py` — `ScriptedAgent` for tests: configurable to return a fixed action,
  an illegal action, garbage, raise, or hang (asyncio.sleep past the timeout). RandomAgent
  can't misbehave; tests need an agent that can.
- Retry loop in `_dispatch`: on a validation failure, re-call the agent once with the reason
  string attached to the request (extend `ActionRequest` with `retry_reason: Optional[str]`),
  then `coerce_action` as today. Distinguish outcomes in the run record: `clean`, `retried_ok`,
  `malformed_fallback`.
- API-failure path, kept separate from malformed: per-call timeout + exponential backoff
  (bounded attempts, from `GameConfig`) around the agent call. Record as `api_failure`, never
  as malformed — an outage says nothing about agent quality.
- Cost accounting: a `CostLedger` on the run — per-agent token in/out and USD, fed by the LLM
  agent (Part E) via a callback the orchestrator owns. Cost-ceiling check after each dispatch;
  on breach, stop the game and mark the run `aborted=True` (recorded, never silently dropped).
- Wire the cursors: `_dispatch` passes `since_event_id` (the agent's cursor) alongside the
  view, so agents can compute "events since my last act" without inventing their own tracking.

**Gate (no API):** mock games — a hanging agent triggers timeout→fallback and the game
completes; an illegal-then-legal agent lands in `retried_ok`; a cost ledger breach aborts and
records; cursor values match the last event_id each agent actually saw (assert against
history). All existing 71 tests still pass.

---

## Part D — Prompts and the rendered-prompt leak tests (`agents/prompts.py`)

**Build:**
- `PROMPT_VERSION` string; bumped on every template change, stamped into transcripts.
- Three template layers:
  1. Role system prompts (Mafia / Seer / Villager / Narrator) — full rules + win condition.
     Mafia: deceive and deflect publicly, coordinate openly in night chat. Narrator: flavor
     only; explicitly told it does not know the killer and must not speculate about identity.
  2. Per-action instruction blocks — legal targets interpolated from the `ActionRequest`;
     the retry variant appends `retry_reason`.
  3. Memory assembly per strategy: `full_transcript` (all visible events),
     `beliefs_only` (BeliefState + events after `since_event_id`),
     `beliefs_plus_recent` (BeliefState + last 20 public events).
- Everything renders from `ObservableState` + `ActionRequest` + `BeliefState` ONLY — no
  function in prompts.py may take a `GameState`. Enforce with the same import discipline as
  engine/ (prompts.py imports from visibility/actions, never state internals).

**Gate — the tier-2 leak tests (no API), added to `tests/test_prompt_leakage.py`:**
render the final prompt string for every (role × action type × memory strategy) against
mid-game states from real random-agent games, and assert: no other player's unrevealed role
name appears; no seer result string for another player appears; a Villager's prompt never
contains "MAFIA_ONLY" content. These run in CI on every prompt edit, forever — the prompt is
the boundary the model actually sees (see `DESIGN_DEEP_DIVES.md` §1).

---

## Part E — The LLM agent (`agents/llm_agent.py`)

The LangGraph per-agent subgraph, in the spec's three nodes:

1. **Belief update node** — input: current `BeliefState` + events since `since_event_id`;
   output: revised `BeliefState` (structured). Skipped when the delta is empty (saves a call
   on back-to-back actions like chat rounds).
2. **Private reasoning node** — free-text scratchpad for this action. Persisted PRIVATE in
   the transcript; this is the X-ray panel's content, so it must be stored even though no
   downstream node consumes it mechanically.
3. **Action node** — emits the Part B output schema; tight `max_tokens` (speech cap is
   enforced by engine truncation, so the token cap is a latency/cost bound, not correctness).

**Build details:**
- `AsyncAnthropic` via LangGraph's model binding; `ainvoke` throughout. Exceptions and
  timeouts propagate to the orchestrator's Part C handling — the agent itself stays simple.
- BeliefState lives on the agent instance (per-game, per-agent), serialized into the
  transcript at every action (spec 3.4).
- Model id from `GameConfig`; per-call token usage reported to the orchestrator's CostLedger.

**Gate (no API):** unit tests with a stubbed model client — node order is belief → reason →
act; empty delta skips node 1; beliefs persist across calls; a parse failure from the stub
surfaces as `None`/invalid to `coerce_action` (feeding Part C's retry), not as a crash.

---

## Part F — The Narrator agent (`agents/narrator_agent.py`)

A separate class, NOT a special-cased LLMAgent: single action node, no beliefs, no reasoning
node. Sharing the class invites accidentally giving it memory or a scratchpad. Pinned to the
cheapest model (Haiku) via its own `GameConfig` field; its cost is logged under a separate
ledger key so player-cost numbers stay clean.

**Gate (no API):** stubbed-model test — produces a `Narration`, never receives non-public
events (assert its view came from `_narrator_view`).

---

## Part G — Transcript writer (`transcript.py` + `games/`)

One JSON file per game; the single source of truth for the Phase 3 replay UI and the Phase 4
experiment analysis, so its shape is a contract.

**Build:**
- A Pydantic `Transcript` model: `GameConfig` (incl. `PROMPT_VERSION`, seed, voting mode),
  full event history (visibility tags + event_ids intact), per-action records — actor, the
  raw LLM outputs for all three nodes (belief/reasoning/action), retries/fallbacks, token
  usage — final state summary, winner, `GameRun` metadata (fallback records, cost ledger,
  aborted flag).
- Raw LLM outputs stored verbatim so replays never re-call the API (spec §8).
- Writer hooks into the orchestrator run; filename `games/{timestamp}_{seed}_{mode}.json`.
- A loader + `replay_check(transcript)` helper: re-apply the recorded actions through the
  engine and assert the final state matches — the transcript proves itself.

**Gate (no API):** a mock game writes a schema-valid transcript; `replay_check` passes;
loading and re-serializing is byte-stable.

---

## Part H — First real games (the API switch-on)

Cheapest first, each step a checkpoint:

1. **Single-call smoke**: one Haiku agent, one SPEECH request against a mid-game view.
   Verifies auth, structured output parsing, token/cost logging against a real response.
2. **All-Haiku full game**, simultaneous mode, via a new `scripts/play_llm.py` (mirrors
   `scripts/play.py`). Confirm: completes, transcript valid, cost ceiling respected. Record
   wall-clock and USD — these numbers calibrate the Phase 4 experiment budget.
3. **Same seed, sequential mode** — second voting mode end-to-end with real agents.
4. Spot-check the run record: if `malformed_fallback` is more than occasional, that's a
   prompt/schema bug to fix before proceeding — fallback-heavy games are not real games.

**Gate:** both modes complete on Haiku with valid transcripts and sane fallback counts.

---

## Part I — The quality pass (the spec's explicit exit gate)

Manually read 2-3 full transcripts before calling Phase 2 done. Read for:

- **Coherence** — speeches respond to the actual conversation; votes follow stated suspicion.
- **Role fidelity** — Mafia deceives rather than confesses; the Seer uses its results and
  makes a sane claim decision; night chat actually coordinates the kill target.
- **The demo moment** — visible daylight between a Mafia agent's public statement and its
  private reasoning. If public and private text read the same, the X-ray panel has nothing
  to show; that is a prompt problem and it gets fixed NOW, not in Phase 3.
- **Belief sanity** — suspicion scores move in response to hard evidence (revealed roles,
  vote patterns), not randomly.

Iterate prompts; bump `PROMPT_VERSION` every change; keep the leak tests green. Expect this
loop to be most of Phase 2's calendar time — everything before it is plumbing.

---

## Definition of done

- [ ] One full LLM game per voting mode completes on Haiku with a schema-valid transcript
- [ ] `replay_check` passes on those transcripts (replays never re-call the API)
- [ ] Rendered-prompt leak tests in CI and green
- [ ] Retry / malformed-fallback / api-failure / abort paths demonstrated via mocks, and
      their counts visible in real-game run records
- [ ] Cost ledger produces per-agent USD numbers; a deliberate low ceiling aborts cleanly
- [ ] 2-3 transcripts manually reviewed and signed off, including at least one clear
      public-vs-private Mafia contrast worth screen-recording in Phase 3

## Sequencing

```
A (deps/config) ──┬── B (schemas/beliefs) ──┬── E (LLM agent) ──┬── H (real games) ── I (quality)
                  ├── C (orchestrator+mocks)┤                   │
                  └── D (prompts+leak tests)┴── F (narrator) ───┤
                                                G (transcript) ─┘
```

B, C, D are independent after A and can be built in any order (or in parallel). E needs B+D;
F needs D; G needs C. H needs everything; I is the exit gate.

## Risks worth naming

- **Structured-output flakiness on Haiku** is the most likely source of malformed fallbacks —
  keep schemas minimal (Part B) and let Part C's retry absorb the residue.
- **Prompt-quality iteration (Part I) is unbounded** — timebox it; the goal is a good game,
  not a perfect one, and the demo records from the best replay anyway.
- **LangGraph ceremony**: the graph is small and fixed; resist adding nodes/edges beyond the
  spec's three. LangGraph is here as a requirement, not an invitation.
