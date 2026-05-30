"""chaos-jungle experiment tracking dashboard.

A self-contained FastAPI web UI that reads from the local SQLite database
and shows sessions, faults, events, and which system tools are installed.

Launch via:  chaos-jungle dashboard
Or programmatically:  from chaos_jungle.dashboard import run; run()
"""

from __future__ import annotations
import subprocess
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from chaos_jungle.db.session_db import SessionDB

app = FastAPI(title="chaos-jungle dashboard", docs_url=None, redoc_url=None)

# ── HTML shell ─────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>chaos-jungle dashboard</title>
<style>
  :root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;--green:#22c55e;
        --red:#ef4444;--yellow:#f59e0b;--blue:#3b82f6;--text:#e2e8f0;
        --muted:#64748b}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px}
  header{background:var(--card);border-bottom:1px solid var(--border);
         padding:14px 24px;display:flex;align-items:center;gap:12px}
  header h1{font-size:18px;letter-spacing:1px}
  .tag{background:#1e3a5f;color:var(--blue);padding:2px 8px;border-radius:4px;font-size:11px}
  .refresh{margin-left:auto;color:var(--muted);font-size:11px}
  main{padding:20px 24px;display:grid;gap:20px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:8px;
        padding:16px;text-align:center}
  .card .val{font-size:28px;font-weight:700;margin-bottom:4px}
  .card .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  .card.green .val{color:var(--green)}
  .card.red   .val{color:var(--red)}
  .card.blue  .val{color:var(--blue)}
  .card.yellow .val{color:var(--yellow)}
  .panel{background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden}
  .panel-header{padding:12px 16px;border-bottom:1px solid var(--border);
                font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);
                display:flex;align-items:center;justify-content:space-between}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;padding:10px 16px;border-bottom:1px solid var(--border);
     font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
  td{padding:10px 16px;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr.clickable{cursor:pointer;transition:background .15s}
  tr.clickable:hover{background:rgba(255,255,255,.03)}
  .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
  .badge.running {background:#1c3a1c;color:var(--green)}
  .badge.reverted{background:#1e2a3a;color:var(--blue)}
  .badge.failed  {background:#3a1c1c;color:var(--red)}
  .badge.ok      {background:#1c3a1c;color:var(--green)}
  .badge.missing {background:#3a2e1c;color:var(--yellow)}
  .tool-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;padding:16px}
  .tool{display:flex;align-items:center;gap:10px;padding:10px 12px;
        background:var(--bg);border:1px solid var(--border);border-radius:6px}
  .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .dot.ok     {background:var(--green)}
  .dot.missing{background:var(--red)}
  .tool .name{font-weight:600;flex:1}
  .tool .path{color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;
              white-space:nowrap;max-width:130px}
  /* drawer */
  #drawer{position:fixed;top:0;right:-500px;width:480px;height:100vh;
          background:var(--card);border-left:1px solid var(--border);
          overflow-y:auto;transition:right .25s;z-index:100}
  #drawer.open{right:0}
  #drawer-close{position:sticky;top:0;background:var(--card);padding:12px 16px;
                border-bottom:1px solid var(--border);cursor:pointer;
                text-align:right;color:var(--muted);font-size:11px}
  #drawer-close:hover{color:var(--text)}
  #drawer-body{padding:16px}
  .fault-block{background:var(--bg);border:1px solid var(--border);border-radius:6px;
               margin-bottom:12px;padding:12px}
  .fault-block .fk{color:var(--yellow);font-weight:700;margin-bottom:6px}
  .fault-block pre{color:var(--muted);font-size:11px;line-height:1.7;white-space:pre-wrap}
  .event{display:flex;gap:10px;margin-bottom:8px;font-size:11px}
  .event .ts{color:var(--muted);flex-shrink:0;width:80px}
  .event .msg{color:var(--text);line-height:1.5}
  .event .msg.err{color:var(--red)}
  .section{margin-bottom:20px}
  .section-title{font-size:11px;text-transform:uppercase;letter-spacing:.5px;
                 color:var(--muted);margin-bottom:10px;padding-bottom:6px;
                 border-bottom:1px solid var(--border)}
  #overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99}
  #overlay.on{display:block}
  .empty{padding:24px;text-align:center;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>&#x1F332; chaos-jungle</h1>
  <span class="tag">dashboard</span>
  <span class="refresh" id="ts">loading…</span>
</header>
<main>
  <div class="cards" id="cards"></div>

  <div class="panel">
    <div class="panel-header">
      <span>System tools</span>
      <span id="tool-summary" style="font-size:11px"></span>
    </div>
    <div class="tool-grid" id="tools"></div>
  </div>

  <div class="panel">
    <div class="panel-header"><span>Experiment sessions</span></div>
    <table>
      <thead><tr>
        <th>ID</th><th>Scenario</th><th>Status</th>
        <th>Faults</th><th>Started</th><th>Duration</th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
</main>

<div id="overlay" onclick="closeDrawer()"></div>
<div id="drawer">
  <div id="drawer-close" onclick="closeDrawer()">&#x2715; close</div>
  <div id="drawer-body"></div>
</div>

<script>
async function load(){
  const [sessions, tools] = await Promise.all([
    fetch('/api/sessions').then(r=>r.json()),
    fetch('/api/system').then(r=>r.json()),
  ]);

  // ── summary cards ──────────────────────────────────────────────
  const total   = sessions.length;
  const running = sessions.filter(s=>s.status==='running').length;
  const reverted = sessions.filter(s=>s.status==='reverted').length;
  const failed   = sessions.filter(s=>s.status==='failed').length;
  const last     = sessions[0];

  document.getElementById('cards').innerHTML = `
    <div class="card blue">
      <div class="val">${total}</div>
      <div class="lbl">Total sessions</div>
    </div>
    <div class="card ${running?'green':''}">
      <div class="val">${running}</div>
      <div class="lbl">Running now</div>
    </div>
    <div class="card green">
      <div class="val">${reverted}</div>
      <div class="lbl">Reverted (clean)</div>
    </div>
    <div class="card ${failed?'red':''}">
      <div class="val">${failed}</div>
      <div class="lbl">Failed</div>
    </div>
  `;

  // ── tools grid ─────────────────────────────────────────────────
  const found = tools.filter(t=>t.found).length;
  document.getElementById('tool-summary').textContent =
    found + '/' + tools.length + ' installed';

  document.getElementById('tools').innerHTML = tools.map(t=>`
    <div class="tool">
      <div class="dot ${t.found?'ok':'missing'}"></div>
      <div style="flex:1;min-width:0">
        <div class="name">${t.binary}</div>
        <div class="path">${t.found ? t.path : 'not installed'}</div>
      </div>
      <div style="text-align:right">
        <span class="badge ${t.found?'ok':'missing'}">${t.package}</span>
        <div style="color:var(--muted);font-size:10px;margin-top:3px">${t.role}</div>
      </div>
    </div>
  `).join('');

  // ── sessions table ─────────────────────────────────────────────
  if(!sessions.length){
    document.getElementById('sessions-body').innerHTML =
      '<tr><td colspan="6" class="empty">No sessions yet. Run your first experiment!</td></tr>';
  } else {
    document.getElementById('sessions-body').innerHTML = sessions.map(s=>{
      const dur = s.duration_s != null ? s.duration_s + 's' : (s.status==='running'?'&#x23F1; running':'—');
      const started = (s.started_at||'').replace('T',' ').slice(0,19) || '—';
      const flist   = (s.faults||[]).map(f=>f.kind).join(', ') || '—';
      return `<tr class="clickable" onclick="openSession(${s.id})">
        <td style="color:var(--muted)">#${s.id}</td>
        <td><b>${s.name}</b></td>
        <td><span class="badge ${s.status}">${s.status}</span></td>
        <td style="color:var(--yellow)">${flist}</td>
        <td style="color:var(--muted)">${started}</td>
        <td>${dur}</td>
      </tr>`;
    }).join('');
  }

  document.getElementById('ts').textContent =
    'Refreshed ' + new Date().toLocaleTimeString();
}

async function openSession(id){
  const d = await fetch(`/api/session/${id}`).then(r=>r.json());
  const s = d.session;
  const dur = s.duration_s != null ? s.duration_s + 's' : (s.status==='running' ? 'still running' : '—');

  let html = `
  <div class="section">
    <div class="section-title">Session #${s.id}</div>
    <div style="margin-bottom:8px">
      <span style="font-size:15px;font-weight:700">${s.name}</span>
      <span class="badge ${s.status}" style="margin-left:10px">${s.status}</span>
    </div>
    <div style="color:var(--muted);font-size:11px;line-height:2">
      Started:  ${s.started_at||'—'}<br>
      Stopped:  ${s.stopped_at||'—'}<br>
      Duration: ${dur}
    </div>
  </div>`;

  // faults
  html += `<div class="section"><div class="section-title">Faults injected (${d.faults.length})</div>`;
  if(!d.faults.length){
    html += '<div style="color:var(--muted);font-size:11px">No faults recorded.</div>';
  } else {
    d.faults.forEach(f=>{
      let params;
      try {
        const p = typeof f.parameters === 'string' ? JSON.parse(f.parameters) : f.parameters;
        params = JSON.stringify(p, null, 2);
      } catch(e){ params = String(f.parameters||'{}'); }
      html += `<div class="fault-block">
        <div class="fk">${f.kind}</div>
        <pre>${params}</pre>
      </div>`;
    });
  }
  html += '</div>';

  // events
  html += `<div class="section"><div class="section-title">Event log (${d.events.length})</div>`;
  if(!d.events.length){
    html += '<div style="color:var(--muted);font-size:11px">No events recorded.</div>';
  } else {
    d.events.forEach(e=>{
      const ts  = (e.timestamp||'').replace('T',' ').slice(11,19);
      const err = e.message.startsWith('ERROR');
      html += `<div class="event">
        <span class="ts">${ts}</span>
        <span class="msg ${err?'err':''}">${e.message}</span>
      </div>`;
    });
  }
  html += '</div>';

  document.getElementById('drawer-body').innerHTML = html;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('on');
}

function closeDrawer(){
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('on');
}

load();
setInterval(load, 5000);
</script>
</body>
</html>"""


# ── API ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/api/sessions")
async def api_sessions():
    db = SessionDB()
    sessions = db.list_sessions()
    result = []
    for row in sessions:
        s = dict(row)
        data = db.export_session(s["id"])
        duration_s = _calc_duration(s.get("started_at"), s.get("stopped_at"))
        result.append({
            "id":         s["id"],
            "name":       s["name"],
            "status":     s["status"],
            "started_at": s.get("started_at"),
            "stopped_at": s.get("stopped_at"),
            "duration_s": duration_s,
            "faults":     data["faults"],
        })
    return JSONResponse(result)


@app.get("/api/session/{session_id}")
async def api_session(session_id: int):
    db = SessionDB()
    data = db.export_session(session_id)
    s = data["session"]
    duration_s = _calc_duration(s.get("started_at"), s.get("stopped_at"))
    return JSONResponse({
        "session": {**s, "duration_s": duration_s},
        "faults":  data["faults"],
        "events":  data["events"],
    })


@app.get("/api/system")
async def api_system():
    """Which system tools are installed on this machine."""
    TOOLS = [
        ("tc",          "iproute2",      "network faults (netem)"),
        ("ip",          "iproute2",      "interface detection"),
        ("filefrag",    "e2fsprogs",     "storage — extent info"),
        ("dd",          "coreutils",     "storage — bit-flip"),
        ("inotifywait", "inotify-tools", "storage — file watch"),
        ("python3",     "python3",       "storage / BPF scripts"),
        ("pip3",        "python3-pip",   "Python package install"),
        ("ssh",         "openssh-client","SSH target support"),
    ]
    result = []
    for binary, package, role in TOOLS:
        try:
            path = subprocess.check_output(
                ["which", binary], stderr=subprocess.DEVNULL, text=True
            ).strip()
            found = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            path, found = "", False
        result.append({"binary": binary, "package": package,
                        "role": role, "found": found, "path": path})
    return JSONResponse(result)


# ── helpers ────────────────────────────────────────────────────────────────

def _calc_duration(started_at, stopped_at):
    if not started_at or not stopped_at:
        return None
    try:
        t0 = datetime.fromisoformat(started_at)
        t1 = datetime.fromisoformat(stopped_at)
        return round((t1 - t0).total_seconds(), 1)
    except Exception:
        return None


# ── entry point ────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = 8050) -> None:
    """Start the dashboard server (blocking)."""
    uvicorn.run(app, host=host, port=port, log_level="warning")
