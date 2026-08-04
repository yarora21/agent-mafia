# MAFIA_AGENTS_SPEC.md

Project spec for **mafia-agents**: LLM agents that play Mafia (Werewolf), built for two goals:
1. A screen-recordable interview demo — a live game showing public chat vs. private agent reasoning side by side
2. One focused experiment: does sequential open voting vs. simultaneous voting change the Mafia win rate? (Section 5)

Philosophy: the game engine is deterministic and fully testable with zero LLM calls. All LLM behavior lives behind a narrow agent interface.

---

## 1. Tech stack

- Python 3.11+
- LangGraph for agent orchestration (per-agent subgraphs, game-level graph). HARD requirement — do not replace with plain async functions even though the per-agent graph is small; using LangGraph is itself a project goal.
- Anthropic API (default model configurable)
- Pydantic v2 for all game state, actions, and structured LLM outputs
- FastAPI + WebSocket for the demo UI backend (Phase 3)
- React + Tailwind frontend (Phase 3)
- pytest for engine tests

---

## 2. Game rules (v0 configuration)

- 7 players: 2 Mafia, 1 Seer, 4 Villagers
- Plus an 8th agent, the **Narrator**: not a player — never votes, can't be killed or investigated, doesn't count toward win conditions. Its sole job is delivering the death narration at the start of each Day (see Day phase). The Narrator receives ONLY the public view (victim identity + revealed role, public history) — it never sees the killer, so it cannot leak the killer's identity.
- Roles secretly assigned at game start. Mafia know each other. Nobody else knows anything.
- Alternating Night / Day phases, starting with Night 1.

### Night phase
- Mafia chat: while both Mafia are alive, they hold a private night chat (visibility MAFIA_ONLY) to discuss who to kill — 2 rounds, alternating, one short message each per round (same max length as speeches). The night's designated killer speaks first (they own the decision; their partner gets the last word before the kill). Skipped when only one Mafia remains (their private reasoning node covers solo deliberation).
- Seer: selects a living player OTHER than themselves, learns privately whether they are Mafia (boolean). The NIGHT_SEER phase occurs every night even when the Seer is dead — no agent is called and nothing resolves, but the phase always runs so the game's structure never reveals whether the Seer is alive.
- Mafia kill: one designated killer selects a living non-Mafia player to kill, informed by the chat. Designation starts with the lowest-seated Mafia on Night 1 and rotates among the LIVING Mafia each night. The designated killer's choice is final — no consensus mechanic, so the decision is always unambiguous.
- Phase order IS resolution order: NIGHT_MAFIA_CHAT → NIGHT_SEER → NIGHT_KILL, each phase fully resolving before the next begins. The Seer therefore always investigates the pre-kill state, and a Seer killed the same night has already received their result (recorded in their private history; moot for play, but the semantics are unambiguous).

### Day phase
- DAY_ANNOUNCE: the structural death event — victim identity + revealed role, emitted PUBLIC by the engine (v0 choice: revealed roles give agents hard evidence, easier to debug reasoning). Purely mechanical; no flavor.
- DAY_NARRATE: the Narrator delivers a short PUBLIC narration (max ~80 words) inventing HOW the victim was killed — dramatic flavor, zero mechanical content (in v0 the night kill is mandatory, so every Day opens with a death to narrate). Kept as a separate phase from DAY_ANNOUNCE: one is structural, one is flavor. Night deaths ONLY — vote eliminations get no narration (the ballot reveal is its own drama).
- Speaking order (used for both discussion and sequential voting): order the living players by seat number; day *d*'s first speaker is index `d mod len(living)` in that list; proceed cyclically through it. This rotates who opens with zero information, and stays well-defined when deaths leave gaps in seat numbers.
- Discussion: 2 rounds of speeches per day in speaking order; each speech is one public statement.
- Vote: `voting_mode` is a config option with two supported values — comparing Mafia win rate across the two modes is a first-class experiment (see Section 5):
  - `simultaneous`: all living players vote at once, no one seeing others' ballots while voting is open. After resolution, ALL individual ballots are revealed as PUBLIC events — one event per ballot, in seat order, followed by the outcome event. No bandwagon/copy-voting; ballot casting can be fanned out concurrently.
  - `sequential`: living players vote one at a time in the day's speaking order, each ballot becoming a PUBLIC event the moment it's cast — later voters see earlier votes. Tabletop-style bandwagon dynamics; Mafia can react strategically by voting late.
  - In both modes: legal targets are any LIVING player except yourself; no abstention. Plurality is eliminated; ties = no elimination; the eliminated player's role is revealed; all ballots end up in the public record, so Town always has full vote-pattern history — that's the core Town skill.

### Win conditions (checked after every death/elimination)
- Town wins: all Mafia dead.
- Mafia wins: living Mafia count >= living Town count.
- If the night kill ends the game: DAY_ANNOUNCE still emits the final death (with role), then the game goes straight to GAME_OVER with a result event — no narration, discussion, or vote.

### Explicit v0 simplifications (future variants if wanted)
- No Doctor role
- Mafia night chat is fixed at 2 rounds
- Roles revealed on death
- Fixed speaking order (with rotating start seat)
- Ties = no elimination
- Mandatory night kill (real Mafia permits a no-kill; affects balance)

---

## 3. Architecture

```
mafia-agents/
├── engine/
│   ├── state.py          # GameState, PlayerState, Role, Phase (Pydantic)
│   ├── actions.py        # Action types: Speech, Vote, MafiaChat, NightKill, Investigate, Narration
│   ├── engine.py         # Pure state machine: apply(state, action) -> state
│   ├── visibility.py     # CRITICAL: computes each player's observable view
│   └── referee.py        # Game loop driver: whose turn, what action is legal
├── agents/
│   ├── interface.py      # AgentInterface protocol: observe -> act
│   ├── llm_agent.py      # LangGraph agent implementation
│   ├── beliefs.py        # Suspicion score tracking / belief state
│   ├── prompts.py        # All prompt templates, versioned
│   └── random_agent.py   # Baseline: random legal actions (for engine testing)
├── orchestrator.py       # Owns agent instances + per-agent event cursors; async loop wiring agents to the referee
├── experiments/
│   ├── runner.py         # Batch game runner (N games per voting mode)
│   └── analysis.py       # Mafia win % per voting mode
├── server/
│   ├── app.py            # FastAPI + WebSocket streaming of live games
│   └── replay.py         # Load stored games for replay in UI
├── ui/                   # React frontend (Phase 3)
├── tests/
└── games/                # Stored game transcripts (JSON, one file per game)
```

### 3.1 Engine (Phase 1) — pure, deterministic, no LLM

`GameState` is immutable-style (return new state from `apply`). Includes:
- `players: list[PlayerState]` (id, seat, role, alive)
- `phase: Phase` (NIGHT_MAFIA_CHAT, NIGHT_SEER, NIGHT_KILL, DAY_ANNOUNCE, DAY_NARRATE, DAY_DISCUSS, DAY_VOTE, GAME_OVER) — enum order matches resolution order
- `day_number`, `speech_round`, `next_speaker`, `next_voter` (sequential voting mode only)
- `history: list[Event]` — every event tagged with a `Visibility` (PUBLIC, MAFIA_ONLY, PLAYER_ONLY(id)) and a monotonically increasing `event_id`. Event IDs are part of the engine/interface contract: `ObservableState` carries them, and `orchestrator.py` keeps a per-agent cursor (last event ID seen at the agent's previous act) so "events since last act" is computed by the orchestrator, not improvised agent-side — the `beliefs_only` memory strategy is built entirely on this delta. The orchestrator lives OUTSIDE `engine/` (it holds agent instances and makes LLM-adjacent calls); the engine stays pure.
- `rng_seed` — all randomness (role assignment) seeded for reproducibility

**visibility.py is the security boundary.** `observable_view(state, player_id) -> ObservableState` returns ONLY what that player may know. Its fields, explicitly: your own player id, seat, and role; the roster (every player's id, seat, alive/dead status, and role IF publicly revealed by death); current phase, day number, and voting mode; and your filtered event list (with event_ids): public events, your private events (seer results), plus Mafia identities and night chat iff you are Mafia. Agents NEVER receive raw GameState. Write property tests asserting no leakage (e.g., a Villager's view never contains any other player's role while alive, and Mafia chat never appears in a non-Mafia view). This is necessary but not sufficient: leakage can be reintroduced downstream in `prompts.py` (wrong field interpolated, a shared template listing roles). Add a second test tier that renders the FINAL prompt string for each agent/action and asserts no other player's role name or another player's seer results appear in it.

`referee.py` drives the loop: query legal actions for current phase, request action from the appropriate agent(s), validate, apply. Failure handling distinguishes two cases, both logged:
- **Malformed/illegal output** (LLM responded, but invalid): one retry with the validation error appended; second failure falls back — for target-based actions (VOTE, NIGHT_KILL, INVESTIGATE) a random legal target; for free-text actions a canned fallback (SPEECH/MAFIA_CHAT: "I have nothing to add."; NARRATE: a stock one-line narration).
- **API failure/timeout**: retry with exponential backoff (configurable attempts) before any fallback.

Dead players: a dead agent is never called again; its BeliefState is frozen into the transcript at time of death; a dead Mafia leaves the night chat (solo-Mafia case already specified above).

### 3.2 Agent interface (Phase 2)

```python
class AgentInterface(Protocol):
    async def act(self, view: ObservableState, request: ActionRequest) -> Action: ...
```

The interface is async from day one — LLM calls shouldn't block the event loop that streams the live game. The random agent satisfies it trivially; the engine stays pure either way.

`ActionRequest` says what's needed: SPEECH, VOTE, MAFIA_CHAT, NIGHT_KILL, INVESTIGATE, NARRATE — with the set of legal targets (for MAFIA_CHAT and NARRATE, a free-text message instead of a target). In sequential voting mode, ballots already cast are PUBLIC events in the voter's `ObservableState`; in simultaneous mode they don't exist yet, so nothing special is needed — visibility handles both modes with no extra machinery.

`LLMAgent` internals (LangGraph subgraph):
1. **Belief update node**: given new observations since last act, update a persistent `BeliefState`: per-player suspicion scores (0-100), notes, and claimed roles ledger. Structured output.
2. **Private reasoning node**: scratchpad reasoning about strategy for this specific action. Stored in the transcript as PRIVATE — this is the demo gold (public statement vs. private monologue).
3. **Action node**: produce the structured Action. Speeches have a max length (60 words) to keep games snappy and cheap — over-length speeches are TRUNCATED, not retried (retrying over verbosity burns money; the cap is cosmetic, not a validity rule).

BeliefState persists across the whole game per agent (in-memory dict keyed by player id; serialize into the game transcript for replay/inspection).

The Narrator is a degenerate agent: single action node, no belief update or private reasoning (nothing to strategize about). Pin it to the cheapest model (Haiku).

Memory design is a config flag (default: `beliefs_plus_recent` — used by both the demo and the voting experiment unless overridden):
- `full_transcript`: agent sees entire public history each call (baseline, expensive)
- `beliefs_only`: agent sees only its BeliefState + events since last action (cheap)
- `beliefs_plus_recent`: BeliefState + last N public events (default N=20)

### 3.3 Prompts

- All templates in `prompts.py`, each with a `PROMPT_VERSION` string that gets stamped into every game transcript (so any stored game can be tied back to the prompts that produced it).
- Role-specific system prompts: Mafia (deceive, deflect publicly; coordinate openly in night chat), Seer (extract value from investigations, decide when/whether to claim), Villager (find signal), Narrator (dramatic flavor only — explicitly instructed it does not know who the killer is and must never speculate about identity or motive in a way agents could read as evidence).
- Agents are told the full rules and the win condition. Keep personas out of v0 (persona injection is a fun later addition).

### 3.4 Game transcript format

One JSON file per game: config (models, memory strategy, prompt version, seed), full event history with visibility tags, every agent's private reasoning + belief snapshots at each action, final outcome, token/cost accounting per agent. This file is the single source of truth for UI replay.

---

## 4. Demo UI (Phase 3)

FastAPI serves: start game (config in request), WebSocket stream of events as they happen, replay endpoint for stored games.

React UI, three panels:
- **Left**: player circle/table — avatars, alive/dead, revealed roles, current speaker highlighted, live vote tallies during vote reveal
- **Center**: public chat — speeches and announcements in order, streaming in. Each Day opens with the Narrator's death narration, styled distinctly (italic/storybook) from player speeches. During night phases, the Mafia private chat renders here in a visually distinct style (spectator sees it; in-game, only Mafia do)
- **Right**: "X-ray panel" — select any player to see their private reasoning and suspicion scores updating live. THE demo moment is Mafia saying something innocent in public chat while their private monologue says the opposite.

Streaming pacing: insert small configurable delays between events during live demo mode so the recording is watchable (no wall-of-text dumps).

Also build replay mode reading from a stored game JSON — record the screen demo from a replay of a great game, not a live roll of the dice.

---

## 5. Voting-mode experiment (Phase 4)

The question: **does sequential open voting change the Mafia win rate vs. simultaneous voting?** Keep it simple — run games, count wins.

- `experiments/runner.py`: run N games per voting mode with asyncio concurrency (respect rate limits). Everything else held fixed: same model, same memory strategy, same prompt version.
- `experiments/analysis.py`: aggregates over the transcript JSONs in `games/` (no database needed at this scale). Output is a small table: games played, Mafia wins, Mafia win % per mode. No significance testing — just the percentages; more games = more trustworthy numbers.
- Cheap sanity check first: a batch of all-random-agent games per mode to confirm games complete and neither rule set is degenerate before spending LLM budget.

---

## 6. Build order

1. **Phase 1 — Engine** (no LLM): state, actions, visibility, referee. pytest coverage on: win conditions, phase transitions, visibility leakage properties, tie handling in BOTH voting modes. Random agents playing full games end-to-end in both modes.
2. **Phase 2 — LLM agents**: interface, belief tracking, prompts, one full LLM game runs and produces a valid transcript JSON. Manually read 2-3 transcripts and sanity-check reasoning quality before scaling.
3. **Phase 3 — UI**: replay mode first (deterministic to build against), then live mode.
4. **Phase 4 — Voting-mode experiment**: random-agent sanity batches, then the LLM comparison.
5. **Stretch**: Doctor role, persona injection, cross-provider model battles.

## 7. Definition of done (for the interview)

- One recorded 3-4 minute demo: replay of a dramatic game with the X-ray panel
- One result: Mafia win % over N games, simultaneous vs. sequential voting
- README with architecture diagram and the "why Mafia: verifiable ground truth" story

## 8. Engineering conventions

- Type hints everywhere; mypy clean
- No LLM calls in engine/ — enforce with an import-linter rule or code review discipline
- Every game reproducible from (config, seed, prompt version) up to LLM nondeterminism; store raw LLM outputs so replays never re-call the API
- Costs logged per call; abort a game that exceeds a configurable cost ceiling
