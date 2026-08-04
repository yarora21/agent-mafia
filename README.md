# mafia-agents

LLM agents play Mafia (Werewolf). Each game writes a self-proving JSON transcript
and a self-contained **X-ray replay**: the public record beside every agent's
private reasoning.

## What it is

- 7 players: 2 Mafia, 1 Seer, 4 Villagers. Mafia kill one player each night; Town
  votes one out each day. First faction to control the game wins.
- Each player is a language model. A player acts through a belief → reason → act
  loop and sees only what its role is allowed to see.
- The engine is pure and deterministic. It never imports an LLM. All randomness
  comes from the seed.

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
- `scripts/play_llm.py` — play a game. `scripts/build_replay.py` — transcript → HTML.

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

## Demo games

Two live Haiku games (prompt `v1`), each with its replay built alongside:

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
