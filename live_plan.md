# Live X-Ray — Build Plan

Make a game feel **live**: spectators watch it unfold in real time (each agent's
public move *and* private reasoning appearing as the game plays), instead of only
opening a finished replay. Users can also browse past games.

## Two product decisions (locked)
- **Full X-ray live** — the live view shows public moves *and* private reasoning.
  This removes the public/private visibility-filtering risk: the spectator is
  meant to see everything (a "director's commentary" view).
- **View past transcripts** — the homepage already lists finished games and links
  each to its full X-ray replay (`build_replay`). This was blocked only by the
  cost bug (games crashed before the write step); now fixed, completed games save
  and appear automatically. Remaining work is polish.

## Core mechanism
The game loop moves one action at a time in `orchestrator.py` (`apply(state,
action)`). That is the single streaming seam. Each `LlmAgent` appends a
`StepRecord` (with `.reasoning`) during `act()`, and the orchestrator holds the
`agents` map — so at apply-time we can pull the acting agent's latest reasoning
and emit the full X-ray event.

## Transport
Server-Sent Events (SSE), not WebSocket — one-way server→browser,
auto-reconnecting, trivial in FastAPI (`StreamingResponse`). Each `Job` keeps its
emitted events in memory; the SSE endpoint replays buffered events then tails new
ones. This matches the single-machine design (`MAX_CONCURRENT_GAMES=1`) and gives
late-join, reconnect, and multi-viewer for free.

## Slices

### Slice 1 — live pipe, full X-ray (DONE, deployed)
- `orchestrator.py`: optional `on_action(action, state)` async callback, awaited
  after each `apply`.
- `scripts/play_llm.py`: `on_event` param; inner `_on_action` pulls the actor's
  latest `StepRecord.reasoning`, builds the X-ray payload, forwards it.
- `webapp.py`: per-`Job` event buffer; `GET /play/stream/{job_id}` SSE endpoint;
  `_run_game` emits a start event, one event per action, and a final done event
  (winner, cost, replay URL).
- Frontend: `EventSource` renders each turn (public move + private reasoning) as
  it arrives.

### Slice 2 — match the replay look (DONE, pending deploy)
Approach chosen: instead of duplicating the replay's CSS/JS into the index, the
**replay page itself runs in "live mode"** — one renderer, both surfaces.
- `scripts/build_replay.py`: extracted shared `build_steps(state, reasoning)`;
  added `render_live(job_id, seed, mode)`; the template JS now branches on
  `DATA.live` (append streamed steps, auto-follow the newest, hide the scrubber,
  show a ● LIVE pill, link to the full replay on done).
- `scripts/play_llm.py`: `_flush` emits the *exact replay step shape* (roster +
  per-step events) via `build_steps`, plus a trailing flush for the final
  automatic events (last vote outcome, game-over).
- `webapp.py`: new `GET /watch/{job_id}` serves the live page; "Play a game" now
  redirects there; the Slice-1 inline panel was removed.
Verified: replay parity (finished replay byte-identical), mypy clean, dry-run
stream = roster then steps ending in `game_over`, TestClient routes OK.

### Slice 3 — past-transcript polish + spectating
- Richer "Finished games" index (winner / date / cost / mode from `RunSummary`).
- Optional: seed the volume with the local demo games (they are `.dockerignore`'d,
  so the Fly volume starts empty).
- Optional: auto-attach every homepage visitor to the currently-live game.

## Shared-renderer note
Live and replay use the same renderer, so building the incremental view (Slice 2)
also improves the past-transcript experience — one investment, both surfaces.

## Risks / must-handle
- **SSE + Fly**: long-lived connection under `http_service` — send periodic
  heartbeat comments so the proxy does not drop an idle stream.
- **Pacing is LLM-bound**: ~seconds between events. A feature (watchable), not a
  bug.
- **Late join / reconnect**: buffer events on the job and replay from index 0 on
  connect (handled by the event-buffer design above).
