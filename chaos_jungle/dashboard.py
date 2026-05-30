"""Web dashboard for tracking chaos-jungle experiments.

Serves a self-contained single-page UI that reads directly from the
local SQLite session database.

Launch via CLI::

    chaos-jungle dashboard

Or programmatically::

    from chaos_jungle.dashboard import run
    run(port=8050)
"""

from __future__ import annotations
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

from chaos_jungle.db.session_db import SessionDB


app = FastAPI(title="chaos-jungle dashboard", docs_url=None, redoc_url=None)

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>chaos-jungle dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e0e0e0; }

  header {
    background: #1a1d27; border-bottom: 1px solid #2e3347;
    padding: 16px 32px; display: flex; align-items: center; gap: 12px;
  }
  header h1 { font-size: 1.2rem; font-weight: 600; color: #fff; }
  header span { font-size: 0.78rem; color: #666; margin-left: auto; }

  .cards {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 16px; padding: 24px 32px 0;
  }
  .card {
    background: #1a1d27; border: 1px solid #2e3347; border-radius: 8px;
    padding: 20px; text-align: center;
  }
  .card .val { font-size: 2rem; font-weight: 700; color: #7c9ef8; }
  .card .lbl { font-size: 0.78rem; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: .05em; }

  .section { padding: 24px 32px; }
  .section h2 { font-size: 0.9rem; font-weight: 600; color: #aaa; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 12px; }

  table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
  th { background: #1a1d27; color: #888; font-weight: 500; padding: 10px 14px; text-align: left; border-bottom: 1px solid #2e3347; }
  tr.row { cursor: pointer; transition: background .15s; }
  tr.row:hover { background: #1f2235; }
  td { padding: 10px 14px; border-bottom: 1px solid #1e2130; vertical-align: top; }

  .badge {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 0.75rem; font-weight: 500;
  }
  .badge.running  { background: #1a3a2a; color: #4ade80; }
  .badge.reverted { background: #1a2640; color: #60a5fa; }
  .badge.stopped  { background: #2e2a1a; color: #fbbf24; }
  .badge.error    { background: #3a1a1a; color: #f87171; }

  .detail-panel {
    background: #1a1d27; border: 1px solid #2e3347; border-radius: 8px;
    margin: 0 32px 24px; padding: 24px; display: none;
  }
  .detail-panel h3 { font-size: 1rem; font-weight: 600; margin-bottom: 16px; color: #fff; }
  .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  .detail-label { font-size: 0.75rem; color: #666; text-transform: uppercase; margin-bottom: 4px; }
  .detail-value { font-size: 0.875rem; color: #ccc; }

  .fault-chip {
    display: inline-block; background: #1e2a40; border: 1px solid #2e4060;
    border-radius: 6px; padding: 6px 12px; margin: 4px 4px 0 0;
    font-size: 0.8rem; color: #90b4f8;
  }
  .fault-chip .params { font-size: 0.72rem; color: #666; margin-top: 2px; }

  .events { margin-top: 16px; max-height: 280px; overflow-y: auto; }
  .event { display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid #1e2130; font-size: 0.8rem; }
  .event .ts { color: #555; white-space: nowrap; min-width: 200px; }
  .event .msg { color: #bbb; }
  .event .msg.err { color: #f87171; }

  .refresh-bar {
    text-align: right; padding: 8px 32px; font-size: 0.75rem; color: #444;
  }
  #last-refresh { color: #555; }
</style>
</head>
<body>

<header>
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#7c9ef8" stroke-width="2">
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
  </svg>
  <h1>chaos-jungle</h1>
  <span id="last-refresh"></span>
</header>

<div class="cards" id="cards">
  <div class="card"><div class="val" id="c-total">—</div><div class="lbl">Total Sessions</div></div>
  <div class="card"><div class="val" id="c-running" style="color:#4ade80">—</div><div class="lbl">Running</div></div>
  <div class="card"><div class="val" id="c-reverted" style="color:#60a5fa">—</div><div class="lbl">Reverted</div></div>
  <div class="card"><div class="val" id="c-last">—</div><div class="lbl">Last Experiment</div></div>
</div>

<div class="section">
  <h2>Sessions</h2>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Name</th><th>Status</th>
        <th>Started</th><th>Duration</th><th>Faults</th>
      </tr>
    </thead>
    <tbody id="sessions-body"></tbody>
  </table>
</div>

<div class="detail-panel" id="detail-panel">
  <h3 id="detail-title"></h3>
  <div class="detail-grid">
    <div>
      <div class="detail-label">Faults injected</div>
      <div id="detail-faults"></div>
    </div>
    <div>
      <div class="detail-label">Timing</div>
      <div class="detail-value" id="detail-timing"></div>
    </div>
  </div>
  <div class="detail-label" style="margin-top:16px">Event log</div>
  <div class="events" id="detail-events"></div>
</div>

<div class="refresh-bar">Auto-refresh every 5s &nbsp;|&nbsp; <span id="last-refresh2"></span></div>

<script>
let activeSession = null;

function badge(status) {
  return `<span class="badge ${status}">${status}</span>`;
}

function duration(start, stop) {
  if (!stop) return '<span style="color:#4ade80">active</span>';
  const s = Math.round((new Date(stop) - new Date(start)) / 1000);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}

function shortTs(ts) {
  if (!ts) return '—';
  return ts.replace('T', ' ').replace(/\\.\\d+/, '').replace('+00:00','');
}

async function loadSessions() {
  const res = await fetch('/api/sessions');
  const data = await res.json();

  document.getElementById('c-total').textContent    = data.total;
  document.getElementById('c-running').textContent  = data.running;
  document.getElementById('c-reverted').textContent = data.reverted;
  document.getElementById('c-last').textContent     = data.last_name || '—';

  const tbody = document.getElementById('sessions-body');
  tbody.innerHTML = '';
  for (const s of data.sessions) {
    const tr = document.createElement('tr');
    tr.className = 'row';
    tr.innerHTML = `
      <td>${s.id}</td>
      <td><strong>${s.name}</strong></td>
      <td>${badge(s.status)}</td>
      <td>${shortTs(s.started_at)}</td>
      <td>${duration(s.started_at, s.stopped_at)}</td>
      <td>${s.fault_kinds || '—'}</td>
    `;
    tr.onclick = () => loadDetail(s.id);
    tbody.appendChild(tr);
  }

  const now = new Date().toLocaleTimeString();
  document.getElementById('last-refresh').textContent  = 'Last refresh: ' + now;
  document.getElementById('last-refresh2').textContent = 'Last refresh: ' + now;

  if (activeSession) loadDetail(activeSession);
}

async function loadDetail(sessionId) {
  activeSession = sessionId;
  const res  = await fetch('/api/session/' + sessionId);
  const data = await res.json();
  const sess = data.session;

  const panel = document.getElementById('detail-panel');
  panel.style.display = 'block';
  document.getElementById('detail-title').textContent =
    `Session ${sess.id} — ${sess.name}`;

  // faults
  const faultsEl = document.getElementById('detail-faults');
  if (data.faults.length === 0) {
    faultsEl.innerHTML = '<span style="color:#555">none</span>';
  } else {
    faultsEl.innerHTML = data.faults.map(f => {
      const p = typeof f.parameters === 'object'
        ? Object.entries(f.parameters)
            .filter(([,v]) => v !== null)
            .map(([k,v]) => `${k}=${v}`).join(', ')
        : f.parameters;
      return `<div class="fault-chip">${f.kind}<div class="params">${p}</div></div>`;
    }).join('');
  }

  // timing
  document.getElementById('detail-timing').innerHTML =
    `<div>Start: ${shortTs(sess.started_at)}</div>` +
    `<div>Stop: ${shortTs(sess.stopped_at) || 'still running'}</div>` +
    `<div>Duration: ${duration(sess.started_at, sess.stopped_at)}</div>`;

  // events
  const eventsEl = document.getElementById('detail-events');
  eventsEl.innerHTML = data.events.map(e => {
    const isErr = e.message.startsWith('ERROR');
    return `<div class="event">
      <span class="ts">${shortTs(e.timestamp)}</span>
      <span class="msg ${isErr ? 'err' : ''}">${e.message}</span>
    </div>`;
  }).join('');
  eventsEl.scrollTop = eventsEl.scrollHeight;
}

loadSessions();
setInterval(loadSessions, 5000);
</script>
</body>
</html>
"""


# ── API ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/api/sessions")
async def api_sessions():
    db = SessionDB()
    sessions = db.list_sessions()
    rows = []
    for s in sessions:
        faults = db._conn.execute(
            "SELECT kind FROM faults WHERE session_id = ?", (s["id"],)
        ).fetchall()
        kinds = ", ".join(f["kind"] for f in faults) if faults else ""
        row = dict(s)
        row["fault_kinds"] = kinds
        rows.append(row)

    total    = len(rows)
    running  = sum(1 for r in rows if r["status"] == "running")
    reverted = sum(1 for r in rows if r["status"] == "reverted")
    last_name = rows[0]["name"] if rows else None

    return {
        "total": total,
        "running": running,
        "reverted": reverted,
        "last_name": last_name,
        "sessions": rows,
    }


@app.get("/api/session/{session_id}")
async def api_session(session_id: int):
    db = SessionDB()
    try:
        return db.export_session(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Session not found")


# ── Entry point ───────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = 8050) -> None:
    """Start the dashboard web server.

    Parameters
    ----------
    host : str
        Bind address. Default ``127.0.0.1`` (local only).
    port : int
        Port. Default ``8050``.
    """
    print(f"[chaos-jungle] Dashboard running at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
