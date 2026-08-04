"""PR15 — the X-ray replay: a transcript becomes the thing you actually show.

A finished game is a JSON transcript (PR13/14): a public event history plus, for
every LLM turn, the private deliberation behind it. This script fuses the two into
one self-contained HTML page — the public record playing out beside the private
scheming that produced each line. That juxtaposition is the demo: you watch the
Mafia lie to the table while reading, in the same frame, what they were thinking.

Design notes:

- **Correlation.** Each event an agent produced is matched to the turn behind it
  via a per-`(actor, action_type)` FIFO queue over the reasoning stream — the
  discipline the transcript was shaped for. Automatic events (deaths, vote
  outcomes, the narrator's flavor) have no private side.
- **X-ray, deliberately.** The page shows the omniscient truth — every player's
  real role, the Mafia's night chat, the Seer's result. It is a spectator artifact
  built from a finished game, never something a player sees mid-game, so there is
  no boundary to protect here (that was PR4/PR10's job, upstream).
- **Self-contained + safe.** The whole view model is embedded in one JSON script
  tag with `<`, `>`, `&` escaped, so a chat message can never break out of the
  tag. No external requests: all CSS/JS is inline.
- **Mask moments are marked.** A public speech or vote by a Mafia player that has
  private reasoning is flagged — the exact frames where the mask is on.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from engine.actions import ActionType
from engine.state import EventType, Faction, faction_of
from transcript import NodeOutputs, Transcript, load_transcript

# Which agent action produced each agent-driven event (others are automatic).
_EVENT_TO_ACTION: dict[EventType, ActionType] = {
    EventType.MAFIA_CHAT: ActionType.MAFIA_CHAT,
    EventType.SEER_RESULT: ActionType.INVESTIGATE,
    EventType.NIGHT_KILL: ActionType.NIGHT_KILL,
    EventType.SPEECH: ActionType.SPEECH,
    EventType.VOTE: ActionType.VOTE,
    EventType.NARRATION: ActionType.NARRATE,
}

_PUBLIC_MASKABLE = {EventType.SPEECH, EventType.VOTE}


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #
class _ReasoningQueues:
    """Per-`(actor, action_type)` FIFO cursors over the reasoning stream.

    Events and each actor's reasoning are both chronological, so pulling the next
    reasoning turn of the matching type in order re-pairs them.
    """

    def __init__(self, reasoning: dict[str, list[NodeOutputs]]) -> None:
        self._reasoning = reasoning
        self._cursor: dict[str, int] = {aid: 0 for aid in reasoning}

    def take(self, actor_id: str | None, action_type: ActionType) -> NodeOutputs | None:
        if actor_id is None:
            return None
        steps = self._reasoning.get(actor_id)
        if not steps:
            return None
        j = self._cursor.get(actor_id, 0)
        while j < len(steps):
            if steps[j].action_type is action_type:
                self._cursor[actor_id] = j + 1
                return steps[j]
            j += 1
        return None  # no matching turn left (automatic/narrator/fallback)


# --------------------------------------------------------------------------- #
# View model (only what the page needs; big prompts are dropped)
# --------------------------------------------------------------------------- #
def _reasoning_payload(node: NodeOutputs) -> dict[str, Any]:
    if node.output is None:
        output = None
    elif hasattr(node.output, "message"):
        output = {"kind": "text", "value": node.output.message}
    else:
        output = {"kind": "target", "value": node.output.target_id}
    return {
        "action_type": node.action_type.value if node.action_type else None,
        "text": node.reasoning,
        "output": output,
        "notes": node.belief_after.notes or "",
        "suspicions": node.belief_after.suspicions,
        "cost_usd": node.cost_usd,
    }


def build_view_model(transcript: Transcript) -> dict[str, Any]:
    """Fuse the public history and the private reasoning into one render model."""

    roles = {p.id: p.role for p in transcript.final_state.players}
    players = [
        {
            "id": p.id,
            "seat": p.seat,
            "role": p.role.value,
            "faction": faction_of(p.role).value,
        }
        for p in sorted(transcript.final_state.players, key=lambda p: p.seat)
    ]

    queues = _ReasoningQueues(transcript.reasoning)
    steps: list[dict[str, Any]] = []
    for e in transcript.final_state.history:
        node: NodeOutputs | None = None
        action_type = _EVENT_TO_ACTION.get(e.type)
        if action_type is not None:
            node = queues.take(e.actor_id, action_type)

        actor_role = roles.get(e.actor_id) if e.actor_id else None
        actor_faction = faction_of(actor_role).value if actor_role else None
        is_mask = (
            e.type in _PUBLIC_MASKABLE
            and actor_faction == Faction.MAFIA.value
            and node is not None
        )
        steps.append(
            {
                "event_id": e.event_id,
                "type": e.type.value,
                "phase": e.phase.value,
                "day": e.day_number,
                "is_night": e.phase.value.startswith("night"),
                "actor": e.actor_id,
                "actor_role": actor_role.value if actor_role else None,
                "faction": actor_faction,
                "target": e.target_id,
                "message": e.message,
                "visibility": e.visibility.kind.value,
                "revealed_role": e.revealed_role.value if e.revealed_role else None,
                "seer_result": e.seer_result,
                "winner": e.winner.value if e.winner else None,
                "reasoning": _reasoning_payload(node) if node is not None else None,
                "is_mask": is_mask,
            }
        )

    cfg = transcript.config
    return {
        "meta": {
            "winner": transcript.winner.value if transcript.winner else None,
            "seed": cfg.seed,
            "mode": cfg.voting_mode.value,
            "prompt_version": cfg.prompt_version,
            "actions": len(transcript.actions),
            "fallbacks": transcript.run.fallbacks,
            "total_cost_usd": transcript.run.total_cost_usd,
        },
        "players": players,
        "steps": steps,
    }


# --------------------------------------------------------------------------- #
# Safe embedding
# --------------------------------------------------------------------------- #
def escape_for_script(json_text: str) -> str:
    """Escape a JSON string so it cannot break out of a <script> tag.

    Replacing `<`, `>`, `&` (and the JS line separators) keeps any `</script>` or
    HTML in a chat message inert while remaining valid JSON.
    """

    return (
        json_text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_html(view_model: dict[str, Any]) -> str:
    """Embed the view model into the self-contained page template."""

    data = escape_for_script(json.dumps(view_model, ensure_ascii=False))
    title = f"Mafia X-Ray — seed {view_model['meta']['seed']} ({view_model['meta']['mode']})"
    return _TEMPLATE.replace("__TITLE__", title).replace("__DATA__", data)


# --------------------------------------------------------------------------- #
# Public entry + CLI
# --------------------------------------------------------------------------- #
def build_replay(transcript: Transcript) -> str:
    return render_html(build_view_model(transcript))


def _default_transcript() -> Path:
    games = sorted(Path("games").glob("*.json"))
    if not games:
        raise SystemExit("no transcript found in games/ — run scripts.play_llm first")
    return games[-1]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Build the X-ray replay HTML for a game.")
    p.add_argument("transcript", nargs="?", help="path to a game JSON (default: newest)")
    p.add_argument("--out", help="output HTML path (default: alongside the transcript)")
    args = p.parse_args(argv)

    src = Path(args.transcript) if args.transcript else _default_transcript()
    transcript = load_transcript(src)
    html = build_replay(transcript)
    out = Path(args.out) if args.out else src.with_suffix(".html")
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}  ({len(html):,} bytes)")


# --------------------------------------------------------------------------- #
# The page (all CSS/JS inline; committed dark "stage" theme with night/day ambient)
# --------------------------------------------------------------------------- #
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#0b0d17; --panel:#141828; --panel2:#1b2035; --ink:#e8ebf5; --muted:#8c93ad;
  --line:#272d44; --town:#4aa3ff; --mafia:#ff5d6c; --seer:#c792ea; --accent:#ffce54;
  --mask:#ff7a45;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.stage{transition:background .8s ease;min-height:100%}
.stage.night{background:radial-gradient(1200px 700px at 70% -10%,#1a2140 0%,#0b0d17 60%)}
.stage.day{background:radial-gradient(1200px 700px at 70% -10%,#2a2740 0%,#12111c 55%)}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;padding:18px 24px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0;letter-spacing:.3px}
header .meta{color:var(--muted);font-size:13px}
header .winner{color:var(--accent);font-weight:600}
.wrap{display:grid;grid-template-columns:200px 1fr 380px;gap:18px;padding:18px 24px;max-width:1400px;margin:0 auto}
@media(max-width:1000px){.wrap{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);margin:0;padding:12px 14px;border-bottom:1px solid var(--line)}
/* roster */
.roster ul{list-style:none;margin:0;padding:8px}
.roster li{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:8px;font-size:13px}
.roster li.dead{opacity:.38;text-decoration:line-through}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.town .dot{background:var(--town)} .mafia .dot{background:var(--mafia)} .seer .dot{background:var(--seer)}
.roster .role{margin-left:auto;font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
/* feed */
.feed{padding:12px;height:62vh;overflow:auto;scroll-behavior:smooth}
.ev{margin:0 0 10px;padding:10px 12px;border-radius:10px;background:var(--panel2);border:1px solid transparent;opacity:.5;transition:opacity .2s,border-color .2s}
.ev.on{opacity:1;border-color:#39416a}
.ev .who{font-weight:600;font-size:13px}
.ev .who .tag{font-size:10px;color:var(--muted);font-weight:500;margin-left:6px;text-transform:uppercase;letter-spacing:.5px}
.ev .body{margin-top:3px}
.ev.sys{background:transparent;border-style:dashed;border-color:var(--line);color:var(--muted);font-size:13px;text-align:center}
.ev.narration{background:transparent;border-left:3px solid var(--accent);font-style:italic;color:#e9e2c9;border-radius:0 10px 10px 0}
.ev.mafiaonly{background:linear-gradient(0deg,#241016,#1c0e13);border-color:#5a2230}
.ev.mafiaonly .who{color:var(--mafia)}
.ev.playeronly{background:#1a1330;border-color:#3a2a5c}
.ev.playeronly .who{color:var(--seer)}
.ev.death .body,.ev.vote_outcome .body{color:var(--accent)}
.ev .maskbadge{display:inline-block;margin-left:8px;font-size:9.5px;font-weight:700;letter-spacing:.8px;color:#160b06;background:var(--mask);padding:1px 6px;border-radius:999px;vertical-align:middle}
/* xray */
.xray{position:sticky;top:18px;align-self:start}
.xbody{padding:14px}
.xempty{color:var(--muted);font-size:13px;padding:30px 14px;text-align:center}
.xhead{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.badge{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px}
.badge.mafia{background:rgba(255,93,108,.16);color:var(--mafia)}
.badge.town{background:rgba(74,163,255,.16);color:var(--town)}
.badge.seer{background:rgba(199,146,234,.18);color:var(--seer)}
.xact{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.maskcallout{background:rgba(255,122,69,.12);border:1px solid rgba(255,122,69,.4);color:#ffd9c6;border-radius:8px;padding:8px 10px;font-size:12.5px;margin-bottom:10px}
.reason{white-space:pre-wrap;font-size:13.5px;line-height:1.55;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:11px 12px;max-height:38vh;overflow:auto}
.publine{margin-top:10px;font-size:13px}
.publine .lab{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px;display:block;margin-bottom:2px}
.publine .val{border-left:3px solid var(--town);padding-left:9px}
.mask .publine .val{border-left-color:var(--mask)}
.notes{margin-top:10px;color:var(--muted);font-size:12px}
/* controls */
.controls{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;display:flex;align-items:center;gap:12px;background:rgba(16,19,33,.92);border:1px solid var(--line);border-radius:999px;padding:8px 16px;backdrop-filter:blur(8px);box-shadow:0 8px 30px rgba(0,0,0,.4)}
.controls button{background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:8px;width:34px;height:34px;font-size:15px;cursor:pointer}
.controls button:hover{border-color:#4a557f}
.controls .play{width:40px}
.controls input[type=range]{width:min(46vw,440px)}
.controls .count{color:var(--muted);font-size:12px;min-width:78px;text-align:center;font-variant-numeric:tabular-nums}
.phasepill{font-size:11px;color:var(--muted);letter-spacing:.5px}
</style>
</head>
<body>
<div class="stage night" id="stage">
  <header>
    <h1>🎭 Mafia X-Ray</h1>
    <span class="meta" id="meta"></span>
    <span class="meta phasepill" id="phase"></span>
  </header>
  <div class="wrap">
    <section class="card roster"><h2>Table</h2><ul id="roster"></ul></section>
    <section class="card"><h2 id="feedhead">Game</h2><div class="feed" id="feed"></div></section>
    <aside class="card xray"><h2>X-Ray · private reasoning</h2><div class="xbody" id="xray"></div></aside>
  </div>
</div>
<div class="controls">
  <button id="prev" title="Previous (←)">◀</button>
  <button class="play" id="play" title="Play / pause (space)">▶</button>
  <button id="next" title="Next (→)">▶</button>
  <input type="range" id="scrub" min="0" value="0">
  <span class="count" id="count"></span>
</div>
<script id="game-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('game-data').textContent);
const steps = DATA.steps, players = DATA.players, meta = DATA.meta;
const roleClass = r => r==='mafia'?'mafia':(r==='seer'?'seer':'town');
const nameRole = {}; players.forEach(p=>nameRole[p.id]=p.role);
let i = 0, playing = false, timer = null;

document.getElementById('meta').innerHTML =
  `seed ${meta.seed} · ${meta.mode} · winner <span class="winner">${(meta.winner||'?').toUpperCase()}</span>`
  + ` · ${meta.actions} actions · ${meta.fallbacks} fallbacks · $${meta.total_cost_usd.toFixed(4)}`;
const scrub = document.getElementById('scrub'); scrub.max = steps.length-1;

function deadBefore(idx){
  const dead=new Set();
  for(let k=0;k<=idx;k++){const s=steps[k];
    if(s.type==='death'&&s.target) dead.add(s.target);
    if(s.type==='vote_outcome'&&s.target) dead.add(s.target);
  }
  return dead;
}
function renderRoster(idx){
  const dead=deadBefore(idx), ul=document.getElementById('roster'); ul.innerHTML='';
  players.forEach(p=>{
    const li=document.createElement('li'); li.className=roleClass(p.role)+(dead.has(p.id)?' dead':'');
    li.innerHTML=`<span class="dot"></span>${p.id}<span class="role">${p.role}</span>`;
    ul.appendChild(li);
  });
}
function evLabel(s){
  if(s.type==='game_start') return 'The game begins.';
  if(s.type==='death') return `${s.target} was found dead${s.revealed_role?` — the ${s.revealed_role}`:''}.`;
  if(s.type==='vote_outcome') return s.target?`${s.target} was voted out${s.revealed_role?`, the ${s.revealed_role}`:''}.`:'The vote tied — no one eliminated.';
  if(s.type==='game_over') return `Game over — ${(s.winner||'?').toUpperCase()} wins.`;
  if(s.type==='seer_result') return `Investigation: ${s.target} ${s.seer_result?'IS Mafia':'is NOT Mafia'}.`;
  if(s.type==='night_kill') return `Mafia choose to kill ${s.target}.`;
  if(s.type==='vote') return `votes for ${s.target}.`;
  return s.message||'';
}
function evClass(s){
  if(['game_start','game_over'].includes(s.type)) return 'sys';
  if(s.type==='narration') return 'narration';
  if(s.type==='death'||s.type==='vote_outcome') return s.type;
  if(s.visibility==='mafia_only') return 'mafiaonly';
  if(s.visibility==='player_only') return 'playeronly';
  return '';
}
function whoLine(s){
  if(['game_start','game_over','death','vote_outcome','narration'].includes(s.type)) {
    return s.type==='narration' ? `<span class="who">Narrator</span>` : '';
  }
  const tag = s.visibility==='mafia_only'?'mafia chat':(s.visibility==='player_only'?'seer':nameRole[s.actor]||'');
  const mask = s.is_mask?'<span class="maskbadge">MASK</span>':'';
  return `<span class="who">${s.actor||''}<span class="tag">${tag}</span>${mask}</span>`;
}
function renderFeed(idx){
  const feed=document.getElementById('feed'); feed.innerHTML='';
  for(let k=0;k<=idx;k++){const s=steps[k];
    const d=document.createElement('div'); d.className='ev '+evClass(s)+(k===idx?' on':'');
    const who=whoLine(s); const body=`<div class="body">${evLabel(s)}</div>`;
    d.innerHTML=(who?who:'')+body; feed.appendChild(d);
    if(k===idx) requestAnimationFrame(()=>d.scrollIntoView({block:'nearest'}));
  }
}
function renderXray(idx){
  const s=steps[idx], x=document.getElementById('xray');
  if(!s.reasoning){ x.innerHTML=`<div class="xempty">No private reasoning for this moment.<br>(an automatic event, or the Narrator)</div>`; return; }
  const r=s.reasoning, fac=s.faction, badge=fac==='mafia'?'mafia':(s.actor_role==='seer'?'seer':'town');
  const out = r.output? (r.output.kind==='target'?`→ ${r.output.value}`:`“${r.output.value}”`):'';
  const wrapCls = s.is_mask?'mask':'';
  let html=`<div class="${wrapCls}">`;
  html+=`<div class="xhead"><span class="badge ${badge}">${s.actor} · ${(s.actor_role||'').toUpperCase()}</span>`
      + `<span class="xact">${(r.action_type||'').replace('_',' ')}</span></div>`;
  if(s.is_mask) html+=`<div class="maskcallout">🎭 <b>Mask on.</b> A Mafia player is speaking in public — below is what they were actually thinking.</div>`;
  html+=`<div class="reason">${(r.text||'(no reasoning recorded)')}</div>`;
  if(out) html+=`<div class="publine"><span class="lab">${s.type==='vote'?'Voted':(s.visibility==='public'?'Said in public':'Action')}</span><span class="val">${out}</span></div>`;
  if(r.notes) html+=`<div class="notes"><b>Notes:</b> ${r.notes}</div>`;
  html+=`</div>`;
  x.innerHTML=html;
}
function render(){
  const s=steps[i];
  document.getElementById('stage').className='stage '+(s.is_night?'night':'day');
  document.getElementById('phase').textContent=`Day ${s.day} · ${s.phase.replace(/_/g,' ')}`;
  document.getElementById('count').textContent=`${i+1} / ${steps.length}`;
  scrub.value=i;
  renderRoster(i); renderFeed(i); renderXray(i);
}
function go(n){ i=Math.max(0,Math.min(steps.length-1,n)); render(); if(i===steps.length-1) stop(); }
function stop(){ playing=false; clearInterval(timer); document.getElementById('play').textContent='▶'; }
function play(){
  if(i>=steps.length-1) i=0;
  playing=true; document.getElementById('play').textContent='⏸';
  timer=setInterval(()=>{ if(i>=steps.length-1){stop();return;} go(i+1); }, 1400);
}
document.getElementById('next').onclick=()=>{stop();go(i+1)};
document.getElementById('prev').onclick=()=>{stop();go(i-1)};
document.getElementById('play').onclick=()=>{ playing?stop():play(); };
scrub.oninput=e=>{stop();go(+e.target.value)};
document.addEventListener('keydown',e=>{
  if(e.key==='ArrowRight'){stop();go(i+1)} else if(e.key==='ArrowLeft'){stop();go(i-1)}
  else if(e.key===' '){e.preventDefault();playing?stop():play()}
});
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":  # pragma: no cover
    main()
