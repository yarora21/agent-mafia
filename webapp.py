"""A deployable web service that plays live Mafia games on demand.

This is the network front door to the existing pipeline. It reuses the clean
in-process functions unchanged:

    play_llm(config, factory=anthropic_factory(key), write=False) -> (transcript, run)
    build_replay(transcript) -> self-contained HTML

Because the service is PUBLIC and every game spends real Anthropic money, the
guards — not the wiring — are the point. Three hard caps sit in front of every
``POST /play``:

- **Per-game ceiling** (``GAME_COST_CEILING_USD``): the orchestrator aborts a
  single game if its ledger passes this. Bounds the blast radius of one game.
- **Global daily cap** (``DAILY_COST_CAP_USD``): total spend across all games in
  the current UTC day. A new game is refused unless
  ``spent_today + per_game_ceiling <= daily_cap`` — conservative, so we never
  start a game we could not afford in the worst case.
- **Concurrency + per-IP rate** (``MAX_CONCURRENT_GAMES``, ``GAMES_PER_IP_PER_HOUR``):
  cap simultaneous games and how fast one client can start them.

Job state lives in memory (lost on restart); transcripts and replays persist to
``games/`` (durable, and mounted as a Fly volume in the deploy).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import GameConfig, api_key_from_env
from engine.state import VotingMode
from scripts.build_replay import build_replay
from scripts.play_llm import anthropic_factory, play_llm
from transcript import load_transcript, write_transcript

# --------------------------------------------------------------------------- #
# Tunable guards (all overridable by environment)
# --------------------------------------------------------------------------- #
GAMES_DIR = Path(os.environ.get("GAMES_DIR", "games"))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


GAME_COST_CEILING_USD = _env_float("GAME_COST_CEILING_USD", 0.50)
DAILY_COST_CAP_USD = _env_float("DAILY_COST_CAP_USD", 3.00)
MAX_CONCURRENT_GAMES = _env_int("MAX_CONCURRENT_GAMES", 1)
GAMES_PER_IP_PER_HOUR = _env_int("GAMES_PER_IP_PER_HOUR", 5)

JobStatus = Literal["running", "done", "failed"]


# --------------------------------------------------------------------------- #
# Job + spend bookkeeping (in-memory)
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    seed: int
    mode: str
    status: JobStatus = "running"
    winner: str | None = None
    cost_usd: float = 0.0
    replay_file: str | None = None  # basename under games/, served statically
    error: str | None = None
    started_at: float = field(default_factory=time.time)


@dataclass
class SpendLedger:
    """Total spend across all games, reset at each UTC day boundary."""

    day: str = ""
    spent: float = 0.0

    def _roll(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.spent = 0.0

    def spent_today(self) -> float:
        self._roll()
        return self.spent

    def add(self, usd: float) -> None:
        self._roll()
        self.spent += usd


jobs: dict[str, Job] = {}
spend = SpendLedger()
_ip_starts: dict[str, deque[float]] = defaultdict(deque)


def _running_count() -> int:
    return sum(1 for j in jobs.values() if j.status == "running")


def _ip_rate_ok(ip: str) -> bool:
    """Sliding one-hour window of game starts per client IP."""

    now = time.time()
    window = _ip_starts[ip]
    while window and now - window[0] > 3600:
        window.popleft()
    return len(window) < GAMES_PER_IP_PER_HOUR


def _admissible() -> str | None:
    """Return a rejection reason, or ``None`` if a new game may start now.

    The daily check is worst-case: it reserves the full per-game ceiling so we
    never begin a game whose worst outcome would breach the daily cap.
    """

    if _running_count() >= MAX_CONCURRENT_GAMES:
        return "a game is already running; only one runs at a time — try again shortly"
    if spend.spent_today() + GAME_COST_CEILING_USD > DAILY_COST_CAP_USD:
        return "daily budget reached; new games are paused until tomorrow (UTC)"
    return None


# --------------------------------------------------------------------------- #
# The game runner (background task)
# --------------------------------------------------------------------------- #
async def _run_game(job: Job, seed: int, mode: VotingMode) -> None:
    """Play one game, persist the transcript + replay, and update the job.

    Any cost actually spent is added to the daily ledger even on failure, so an
    aborted or crashed game still counts against the cap it consumed.
    """

    api_key = api_key_from_env()
    config = GameConfig(
        seed=seed,
        voting_mode=mode,
        cost_ceiling_usd=GAME_COST_CEILING_USD,
    )
    try:
        transcript, _run = await play_llm(
            config, factory=anthropic_factory(api_key), write=False
        )
        job.cost_usd = transcript.run.total_cost_usd
        job.winner = transcript.winner.value if transcript.winner else None

        json_path = write_transcript(transcript, GAMES_DIR)
        html_path = json_path.with_suffix(".html")
        html_path.write_text(build_replay(transcript), encoding="utf-8")

        job.replay_file = html_path.name
        job.status = "done"
    except Exception as exc:  # a crashed game must still free the slot + count spend
        job.error = f"{type(exc).__name__}: {exc}"
        job.status = "failed"
    finally:
        spend.add(job.cost_usd)


# --------------------------------------------------------------------------- #
# App + routes
# --------------------------------------------------------------------------- #
app = FastAPI(title="Mafia X-Ray")

GAMES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/games", StaticFiles(directory=str(GAMES_DIR)), name="games")


@app.on_event("startup")
async def _recover_spend() -> None:
    """Rebuild today's spend total from persisted transcripts.

    Job state is in memory and a restart forgets it — but the daily cap must not
    reset with it, or a crash-loop would let spend run past the budget. Transcript
    filenames carry a UTC ``YYYYMMDDT...Z`` stamp, so summing today's games back
    into the ledger makes the cap survive restarts (as long as ``games/`` is on a
    persistent volume).
    """

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    total = 0.0
    for p in GAMES_DIR.glob(f"{today}T*.json"):
        try:
            total += load_transcript(p).run.total_cost_usd
        except Exception:
            continue  # a malformed/partial file must not block startup
    if total:
        spend.add(total)


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honoring a single proxy hop (Fly sets this)."""

    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/play")
async def play(request: Request) -> JSONResponse:
    if api_key_from_env() is None:
        return JSONResponse(
            {"error": "server misconfigured: ANTHROPIC_API_KEY is not set"},
            status_code=503,
        )

    ip = _client_ip(request)
    if not _ip_rate_ok(ip):
        return JSONResponse(
            {"error": f"rate limit: max {GAMES_PER_IP_PER_HOUR} games per hour"},
            status_code=429,
        )
    reason = _admissible()
    if reason is not None:
        return JSONResponse({"error": reason}, status_code=429)

    params = dict(request.query_params)
    try:
        seed = int(params.get("seed", "0"))
    except ValueError:
        return JSONResponse({"error": "seed must be an integer"}, status_code=400)
    mode_raw = params.get("mode", VotingMode.SIMULTANEOUS.value)
    try:
        mode = VotingMode(mode_raw)
    except ValueError:
        return JSONResponse(
            {"error": f"mode must be one of {[m.value for m in VotingMode]}"},
            status_code=400,
        )

    job = Job(id=uuid.uuid4().hex[:12], seed=seed, mode=mode.value)
    jobs[job.id] = job
    _ip_starts[ip].append(time.time())
    asyncio.create_task(_run_game(job, seed, mode))
    return JSONResponse({"job_id": job.id})


@app.get("/jobs/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return JSONResponse(
        {
            "id": job.id,
            "status": job.status,
            "seed": job.seed,
            "mode": job.mode,
            "winner": job.winner,
            "cost_usd": round(job.cost_usd, 4),
            "error": job.error,
            "replay_url": f"/games/{job.replay_file}" if job.replay_file else None,
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "running": _running_count(),
            "spent_today_usd": round(spend.spent_today(), 4),
            "daily_cap_usd": DAILY_COST_CAP_USD,
            "api_key_set": api_key_from_env() is not None,
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    replays = sorted(GAMES_DIR.glob("*.html"), reverse=True)
    cards = "\n".join(
        f'<li><a href="/games/{p.name}">{p.stem}</a></li>' for p in replays
    ) or "<li class='empty'>No games yet — start one above.</li>"
    modes = "".join(
        f'<option value="{m.value}">{m.value}</option>' for m in VotingMode
    )
    return HTMLResponse(_INDEX.replace("__CARDS__", cards).replace("__MODES__", modes))


# --------------------------------------------------------------------------- #
# Landing page (self-contained; polls the job it starts)
# --------------------------------------------------------------------------- #
_INDEX = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mafia X-Ray</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0b0d17;color:#e8ebf5;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:32px 24px}
h1{font-size:22px;letter-spacing:.3px}
.muted{color:#8c93ad;font-size:13px}
.panel{background:#141828;border:1px solid #272d44;border-radius:12px;padding:18px;margin:18px 0}
label{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:#8c93ad;margin-right:6px}
select,input{background:#1b2035;color:#e8ebf5;border:1px solid #272d44;border-radius:8px;padding:7px 9px;font:inherit}
button{background:#4aa3ff;color:#04121f;border:0;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
#status{margin-top:12px;font-size:14px;min-height:20px}
ul{list-style:none;padding:0;margin:0}
li{padding:8px 0;border-bottom:1px solid #1b2035}
li a{color:#4aa3ff;text-decoration:none}
li.empty{color:#8c93ad}
</style></head><body><div class="wrap">
<h1>🎭 Mafia X-Ray</h1>
<p class="muted">LLM agents play Mafia. Start a live game, then scrub the replay:
the public record beside every agent's private reasoning. Games take a few
minutes and cost a few cents; a shared daily budget limits total spend.</p>
<div class="panel">
  <label for="seed">Seed</label><input id="seed" type="number" value="0" style="width:80px">
  <label for="mode" style="margin-left:12px">Mode</label><select id="mode">__MODES__</select>
  <button id="go" style="margin-left:12px">Play a game</button>
  <div id="status"></div>
</div>
<div class="panel"><h2 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#8c93ad;margin-top:0">Finished games</h2>
<ul>__CARDS__</ul></div>
</div><script>
const go=document.getElementById('go'), st=document.getElementById('status');
go.onclick=async()=>{
  go.disabled=true; st.textContent='Starting…';
  const seed=document.getElementById('seed').value, mode=document.getElementById('mode').value;
  let r=await fetch(`/play?seed=${encodeURIComponent(seed)}&mode=${encodeURIComponent(mode)}`,{method:'POST'});
  let j=await r.json();
  if(!r.ok){ st.textContent='⛔ '+(j.error||'could not start'); go.disabled=false; return; }
  const id=j.job_id;
  st.textContent='Playing… this takes a few minutes.';
  const poll=setInterval(async()=>{
    const s=await(await fetch(`/jobs/${id}`)).json();
    if(s.status==='running'){ st.textContent=`Playing… $${(s.cost_usd||0).toFixed(3)} so far.`; return; }
    clearInterval(poll); go.disabled=false;
    if(s.status==='done'){ st.innerHTML=`✅ ${(s.winner||'?').toUpperCase()} won ($${s.cost_usd.toFixed(3)}). `+
      `<a href="${s.replay_url}">Open the replay →</a>`; }
    else { st.textContent='⛔ '+(s.error||'game failed'); }
  }, 3000);
};
</script></body></html>"""
