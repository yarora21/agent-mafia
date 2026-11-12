# mafia-agents

**▶ Play / watch live: https://mafia-xray.fly.dev**

LLM agents play Mafia (Werewolf). Each game writes a self-proving JSON transcript
and a self-contained **X-ray replay**: the public record beside every agent's
private reasoning. A small web app (`webapp.py`) plays games on demand and streams
them **live** — you watch each move appear next to the reasoning that produced it.

## What it is

- 8 players: 2 Mafia, 1 Seer, 5 Villagers. Mafia kill one player each night; Town
  votes one out each day. First faction to control the game wins.
- Each player is a language model. A player acts through a belief → reason → act
  loop and sees only what its role is allowed to see.
- The engine is pure and deterministic. It never imports an LLM. All randomness
  comes from the seed.
- Seeds set starting roles for each player. The LLM outputs can (and will) make the same seed produce different outcomes.

## Layout

- `engine/` — the pure core. State machine (`engine.py`), immutable domain types
  (`state.py`, `actions.py`), the **visibility** choke point (`visibility.py`),
  and the **referee** that keeps the game live (`referee.py`).
- `agents/` — the LLM side. `llm_agent.py` (the player), `narrator_agent.py`
  (death flavor), `prompts.py` (the exact bytes a model sees), `beliefs.py`
  (schemas + adapters), `anthropic_client.py` (the API seam).
- `config.py` — `GameConfig`, model pricing, cost helpers. The API key comes from
  the environment, never a config field.
- `orchestrator.py` — the async play loop, retries, timeouts, and the cost ledger.
- `transcript.py` — writes the JSON record and `replay_check` (re-applies every
  recorded action through the engine and asserts the state matches).
- `scripts/play_llm.py` — play a game. `scripts/build_replay.py` — transcript → HTML
  (also drives the live board via a shared `build_steps`).
- `webapp.py` — the FastAPI service: play games on demand, watch them live, and
  browse past replays. `Dockerfile` + `fly.toml` deploy it on Fly.io.

## Two security boundaries

- **Visibility (data).** `engine/visibility.py` is the only place agent-visible
  state is produced. Property tests scan every intermediate state of many random
  games: no unrevealed role, Mafia chat, or Seer result ever leaks.
- **Prompts (bytes).** `agents/prompts.py` renders only from the observable view,
  never from `GameState` (proven by an AST test). Leak tests scan the final prompt
  string for every viewer × role × action × memory strategy.

## Run it

Install (Python 3.10+):

```
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-...      # only needed for live games
```

Play:

```
python -m scripts.play_llm --dry-run                       # offline stub, zero spend
python -m scripts.play_llm --smoke                         # one live call, auth/parse check
python -m scripts.play_llm --seed 0 --mode simultaneous --ceiling 2.0   # full live game
```

- `--ceiling` caps spend in USD; the game aborts (and records the abort) if the
  ledger passes it.
- `--mode` is `simultaneous` (ballots revealed together) or `sequential`
  (revealed on cast).
- Each run writes `games/{timestamp}_{seed}_{mode}.json` and runs `replay_check`.

Build the X-ray replay:

```
python -m scripts.build_replay games/<transcript>.json     # writes the .html beside it
```

Open the `.html` in a browser. It is fully self-contained (all CSS/JS inline).
Step, play, or scrub the timeline; the roster shows every true role (an omniscient
X-ray), and Mafia "mask" moments — an innocent public line beside private scheming
— are flagged.

## Web app (live X-ray)

`webapp.py` is a small FastAPI service that plays games on demand and streams them
**live** — the same X-ray view as the saved replay, but unfolding in real time.

- The homepage's "Play a game" button starts a game and redirects to
  `/watch/{job_id}`.
- `/watch/{job_id}` is the replay page in **live mode**: it opens a Server-Sent
  Events feed (`/play/stream/{job_id}`) and appends each step as the game plays,
  auto-following the newest turn. When the game ends it links to the full,
  scrubbable replay. One renderer (`build_replay`) drives both surfaces, so the
  live board and the saved replay look identical.
- Because it is public and every game spends real money, hard guards sit in front
  of every game — a per-game cost ceiling, a global daily cap, and per-IP rate
  limiting — all tunable by environment (see the top of `webapp.py`).

Run locally:

```
pip install -e ".[web]"
export ANTHROPIC_API_KEY=sk-...
uvicorn webapp:app --reload        # http://localhost:8000
```

Deploy (Fly.io): `Dockerfile`, `fly.toml`, and a persistent volume for `games/`
are included. `fly deploy` builds and ships it; the API key is a Fly **secret**
(`fly secrets set ANTHROPIC_API_KEY=...`), never baked into the image.

## Demo games

Two live Haiku games (prompt `v1`, from the earlier 7-player setup), each with its
replay built alongside:

- `games/20260804T142046Z_0_simultaneous.*` — **short & punchy.** Mafia win in one
  day. The Seer (p1) confirms a Mafia but pushes too early, looks like scum, and
  is mislynched. 30 actions, ~$0.20.
- `games/20260804T142752Z_1_sequential.*` — **town eats itself.** Mafia kill the
  Seer N1, Town correctly lynches one Mafia, then eats an innocent while the real
  Mafia hides. Mafia win. 40 actions, ~$0.31.

## Gate

```
python -m pytest -q
python -m mypy --strict .
```

Both must pass. The demo goal is one good, demoable game plus the X-ray replay —
not a batch stats experiment. See `bugs.md` for known quirks.

## Langfuse tracing

The application uses Langfuse Python SDK **5.2.0**, with the native LangChain /
LangGraph callback integration. Tracing is **off by default**. Configure it:

1. Install dependencies with `pip install -e ".[dev,web]"`.
2. Create a free [Langfuse Cloud](https://langfuse.com/cloud) project (or use your
   own Langfuse server), then create project API keys under Settings → API Keys.
3. Copy `.env.example` to `.env`, fill in the public/secret keys, set
   `LANGFUSE_BASE_URL` to the project's region, and set
   `LANGFUSE_TRACING_ENABLED=true`. Do not commit `.env` or paste keys into chat.
4. Run `python -m scripts.play_llm --dry-run --no-write` to check graph traces
   without spending model tokens. Dry runs have **no model generation spans**.
5. Run `python -m scripts.play_llm --smoke` to verify one real Anthropic generation
   (billed by Anthropic), then run a game normally. The web app uses the same
   tracing paths; restart it after changing environment variables.

The CLI and web startup load the project `.env` without overriding shell values.
Library callers should call `agents.tracing.configure_tracing()` before running
and `flush_tracing()` before exiting. CLI exit and web shutdown flush automatically.
Missing configuration leaves tracing disabled; export errors do not change game
results. Cloud delivery still depends on correct credentials and connectivity.

In Langfuse, open **Sessions** and find the session ID printed by the CLI and saved
as `run.tracing_session_id` in the transcript. Each player turn is a separate
`play-player-turn` agent trace. It contains API attempts, the `decide-action`
graph, belief/reason/act nodes, and model generations with final messages,
responses, model name, token usage, and Langfuse's model-based cost calculation.
The narrator's model calls are captured too. Rejected actions and referee
fallbacks are explicit observations. `finish-game` records the winner, abort
reason, retry/fallback totals, and the application's cost ledger. Langfuse's cost
estimate can differ from the local pricing table in `config.py`.

Filter by actor, day, phase, action type, seed, voting mode, model, prompt version,
or memory strategy. Parallel votes have separate trace IDs and task-local
callbacks. Partial failed attempts remain visible even without a completed
`StepRecord`. The game engine has no tracing dependency.

Traces intentionally include private game notes, role-visible state, and prompts
for debugging. They go to your configured Langfuse project, not to other agents.
Treat project access as access to game spoilers. Export-time masking redacts
configured API keys and common API-key patterns from span attributes; it is not
a general PII scrubber. No HTTP request, player IP, API client, or environment
object is passed as trace input. Set `LANGFUSE_TRACING_ENABLED=false` and restart
to stop export. Existing JSON transcripts and replay behavior remain available.

Offline instrumentation checks use the **real SDK and a local OpenTelemetry
exporter**, with no cloud or model calls:

```
python -m pytest tests/test_tracing.py -q
```

References: [LangChain integration](https://langfuse.com/integrations/frameworks/langchain),
[trace best practices](https://langfuse.com/docs/observability/best-practices), and
[export-time masking](https://langfuse.com/docs/observability/features/masking).
