"""chaos-jungle experiment tracking dashboard.

A self-contained FastAPI web UI that reads from the local SQLite database
and shows sessions, faults, events, and which system tools are installed.

Launch via:  chaos-jungle dashboard
Or programmatically:  from chaos_jungle.dashboard import run; run()
"""

from __future__ import annotations
import os
import subprocess
from datetime import datetime
from pathlib import Path

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
    <div class="panel-header">
      <span>Tool logs</span>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="log-select" style="background:var(--bg);color:var(--text);border:1px solid var(--border);
          border-radius:4px;padding:3px 8px;font-size:11px;font-family:inherit"></select>
        <span id="log-lines" style="color:var(--muted);font-size:11px"></span>
      </div>
    </div>
    <pre id="log-content" style="padding:14px 16px;font-size:11px;line-height:1.7;
      overflow-x:auto;max-height:320px;overflow-y:auto;color:var(--text);
      white-space:pre-wrap;word-break:break-all">Loading…</pre>
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
  const [d, ana] = await Promise.all([
    fetch(`/api/session/${id}`).then(r=>r.json()),
    fetch(`/api/session/${id}/analysis`).then(r=>r.json()),
  ]);
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

  // ── verification ────────────────────────────────────────────
  html += `<div class="section"><div class="section-title">Verification</div>`;

  // command stats
  const ok  = ana.command_ok    || 0;
  const err = ana.command_error || 0;
  html += `<div style="margin-bottom:10px;font-size:11px">
    Commands executed: <b style="color:var(--green)">${ok} OK</b>
    &nbsp;/&nbsp;<b style="color:var(--red)">${err} ERROR</b>
  </div>`;

  // active tc rules
  if(ana.active_tc_rules && ana.active_tc_rules.length){
    html += `<div style="margin-bottom:8px">
      <div style="color:var(--yellow);font-size:11px;margin-bottom:4px">&#x2713; Active tc qdisc rules (network fault is ON)</div>
      <pre style="font-size:10px;color:var(--muted);background:var(--bg);padding:8px;border-radius:4px;overflow-x:auto">${ana.active_tc_rules.map(esc).join('\\n')}</pre>
    </div>`;
  } else {
    html += `<div style="color:var(--muted);font-size:11px;margin-bottom:8px">No active tc rules (network fault is OFF)</div>`;
  }

  // storage bit-flip records
  if(ana.storage_records && ana.storage_records.length){
    html += `<div style="margin-bottom:8px">
      <div style="color:var(--yellow);font-size:11px;margin-bottom:6px">
        &#x2713; Storage bit-flips recorded in cj.db (${ana.storage_records.length} records)
      </div>
      <table style="width:100%;font-size:10px;border-collapse:collapse">
        <tr style="color:var(--muted)"><th style="text-align:left;padding:3px 6px">File</th>
          <th style="padding:3px 6px">Block</th><th style="padding:3px 6px">Byte</th>
          <th style="padding:3px 6px">Before</th><th style="padding:3px 6px">After</th></tr>
        ${(function(){
          let rows='';
          ana.storage_records.slice(0,20).forEach(function(r){
            const fname = esc((r.filename||'').split('/').pop());
            const ftitle = esc(r.filename||'');
            const orig = (r.origValue||0).toString(16);
            const after = (r.afterValue||0).toString(16);
            rows += '<tr style="border-top:1px solid var(--border)">'
              + '<td style="padding:3px 6px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+ftitle+'">'+fname+'</td>'
              + '<td style="padding:3px 6px;text-align:center;color:var(--muted)">'+(r.targetblock||'')+'</td>'
              + '<td style="padding:3px 6px;text-align:center;color:var(--muted)">'+(r.targetbyte||'')+'</td>'
              + '<td style="padding:3px 6px;text-align:center;color:var(--green)">0x'+orig+'</td>'
              + '<td style="padding:3px 6px;text-align:center;color:var(--red)">0x'+after+'</td>'
              + '</tr>';
          });
          return rows;
        })()}
      </table>
      ${ana.storage_records.length>20 ? '<div style="color:var(--muted);font-size:10px;padding:4px 6px">'+'\u2026 '+(ana.storage_records.length-20)+' more in cj.db</div>' : ''}
    </div>`;
  }
  html += '</div>';

  // ── workflow results ─────────────────────────────────────────
  if(ana.results && ana.results.length){
    html += `<div class="section"><div class="section-title">Workflow results</div>`;
    ana.results.forEach(r=>{
      const m = r.metrics || {};
      let cells = '';
      Object.entries(m).forEach(function(entry){
        const k = entry[0], v = entry[1];
        cells += '<div style="background:var(--bg);border:1px solid var(--border);'
               + 'border-radius:4px;padding:8px;text-align:center">'
               + '<div style="font-size:16px;font-weight:700;color:' + metricColor(k) + '">' + v + '</div>'
               + '<div style="color:var(--muted);font-size:10px;text-transform:uppercase">' + k.replace(/_/g,' ') + '</div>'
               + '</div>';
      });
      html += '<div class="fault-block">'
            + '<div style="color:var(--muted);font-size:10px;margin-bottom:6px">' + (r.recorded_at||'') + '</div>'
            + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">' + cells + '</div>'
            + '</div>';
    });
    html += '</div>';
  }

  document.getElementById('drawer-body').innerHTML = html;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('on');
}

function closeDrawer(){
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('on');
}

function metricColor(k){
  if(k.includes('fail')||k.includes('corrupt')||k.includes('miss')) return 'var(--red)';
  if(k.includes('retry')||k.includes('warn')) return 'var(--yellow)';
  return 'var(--blue)';
}

// ── logs ──────────────────────────────────────────────────────────
let _logFiles = [];

async function loadLogList(){
  const files = await fetch('/api/logs').then(r=>r.json());
  _logFiles = files;
  const sel = document.getElementById('log-select');
  const prev = sel.value;
  sel.innerHTML = files.length
    ? files.map(f=>`<option value="${f.name}">${f.name} (${f.size_kb}kb)</option>`).join('')
    : '<option value="">no log files yet</option>';
  if(prev && files.find(f=>f.name===prev)) sel.value = prev;
}

async function loadLogContent(){
  const sel = document.getElementById('log-select');
  if(!sel.value) return;
  const data = await fetch(`/api/logs/${encodeURIComponent(sel.value)}?lines=120`).then(r=>r.json());
  const pre = document.getElementById('log-content');
  if(data.error){
    pre.textContent = data.error;
    return;
  }
  // colour-code lines
  pre.innerHTML = data.lines.map(l=>{
    if(/ERROR|FAIL|critical/i.test(l))  return `<span style="color:var(--red)">${esc(l)}</span>`;
    if(/WARN/i.test(l))                 return `<span style="color:var(--yellow)">${esc(l)}</span>`;
    if(/CORRUPT|inversion/i.test(l))    return `<span style="color:var(--yellow)">${esc(l)}</span>`;
    if(/REVERT|stop|OK/i.test(l))       return `<span style="color:var(--green)">${esc(l)}</span>`;
    return `<span style="color:var(--muted)">${esc(l)}</span>`;
  }).join('\\n');
  // auto-scroll to bottom
  pre.scrollTop = pre.scrollHeight;
  document.getElementById('log-lines').textContent = data.lines.length + ' lines';
}

function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

document.getElementById('log-select').addEventListener('change', loadLogContent);

async function loadLogs(){
  await loadLogList();
  await loadLogContent();
}

// ── boot ──────────────────────────────────────────────────────────
load();
loadLogs();
setInterval(()=>{ load(); loadLogContent(); }, 5000);
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


_CJ_HOME = Path(os.path.expanduser("~/.chaos-jungle"))

_LOG_FILES = [
    ("cj.log",       "storage bit-flip user log"),
    ("cj_debug.log", "storage debug log"),
    ("chaos.log",    "chaos-jungle runner log"),
]


@app.get("/api/logs")
async def api_logs():
    """List available log files under ~/.chaos-jungle/."""
    result = []
    # scan known log files + any *.log in cj_home
    seen = set()
    candidates = list(_LOG_FILES)
    for p in _CJ_HOME.glob("**/*.log"):
        name = p.name
        if name not in seen:
            candidates.append((name, str(p.relative_to(_CJ_HOME))))
    for name, desc in candidates:
        path = _CJ_HOME / name
        if path.exists():
            size_kb = round(path.stat().st_size / 1024, 1)
            result.append({"name": name, "desc": desc, "size_kb": size_kb})
            seen.add(name)
    return JSONResponse(result)


@app.get("/api/logs/{filename}")
async def api_log_content(filename: str, lines: int = 120):
    """Return the last N lines of a log file under ~/.chaos-jungle/."""
    # safety: no path traversal
    safe = Path(filename).name
    path = _CJ_HOME / safe
    if not path.exists():
        return JSONResponse({"error": f"Log file not found: {safe}", "lines": []})
    try:
        text = path.read_text(errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return JSONResponse({"name": safe, "total": len(all_lines), "lines": tail})
    except Exception as exc:
        return JSONResponse({"error": str(exc), "lines": []})


@app.get("/api/session/{session_id}/analysis")
async def api_analysis(session_id: int):
    """Cross-reference chaos session with cj.db records and live tc rules."""
    db = SessionDB()
    data = db.export_session(session_id)
    results = db.get_results(session_id)

    # ── storage: read bit-flip records from cj.db ──────────────────
    cj_db_path = _CJ_HOME / "cj.db"
    storage_records = []
    if cj_db_path.exists():
        import sqlite3 as _sql
        try:
            conn = _sql.connect(str(cj_db_path))
            conn.row_factory = _sql.Row
            rows = conn.execute(
                "SELECT * FROM records ORDER BY id DESC LIMIT 200"
            ).fetchall()
            storage_records = [dict(r) for r in rows]
            conn.close()
        except Exception as e:
            storage_records = [{"error": str(e)}]

    # ── network: read active tc qdisc rules ────────────────────────
    tc_rules = []
    try:
        out = subprocess.check_output(
            ["tc", "qdisc", "show"], stderr=subprocess.DEVNULL, text=True
        )
        tc_rules = [l.strip() for l in out.splitlines() if l.strip()
                    and "noqueue" not in l and "noop" not in l]
    except (subprocess.CalledProcessError, FileNotFoundError):
        tc_rules = []

    # ── command summary: count OK vs ERROR ─────────────────────────
    events = data["events"]
    cmd_ok    = sum(1 for e in events if "[cmd:OK]"    in e.get("message",""))
    cmd_error = sum(1 for e in events if "[cmd:ERROR]" in e.get("message",""))

    return JSONResponse({
        "session_id":       session_id,
        "command_ok":       cmd_ok,
        "command_error":    cmd_error,
        "storage_records":  storage_records,
        "active_tc_rules":  tc_rules,
        "results":          results,
    })


@app.get("/api/cj_records")
async def api_cj_records():
    """Return all bit-flip records from cj.db (storage corruption evidence)."""
    cj_db_path = _CJ_HOME / "cj.db"
    if not cj_db_path.exists():
        return JSONResponse({"records": [], "note": "cj.db not found — no storage faults run yet"})
    import sqlite3 as _sql
    try:
        conn = _sql.connect(str(cj_db_path))
        conn.row_factory = _sql.Row
        rows = conn.execute("SELECT * FROM records ORDER BY id DESC").fetchall()
        conn.close()
        return JSONResponse({"records": [dict(r) for r in rows]})
    except Exception as e:
        return JSONResponse({"records": [], "error": str(e)})


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
