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

### Slice 1 — live pipe, full X-ray (IN PROGRESS)
- `orchestrator.py`: optional `on_action(action, state)` async callback, awaited
  after each `apply`.
- `scripts/play_llm.py`: `on_event` param; inner `_on_action` pulls the actor's
  latest `StepRecord.reasoning`, builds the X-ray payload, forwards it.
- `webapp.py`: per-`Job` event buffer; `GET /play/stream/{job_id}` SSE endpoint;
  `_run_game` emits a start event, one event per action, and a final done event
  (winner, cost, replay URL).
- Frontend: `EventSource` renders each turn (public move + private reasoning) as
  it arrives.

### Slice 2 — match the replay look
Reuse `build_replay`'s components so the live board and the saved replay look
identical. At game end, swap in the existing "Open full replay →" link.

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
