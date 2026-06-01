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
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>chaos-jungle</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
/* ── Light theme (default) ── */
:root {
  --bg:       #f8fafc;
  --surface:  #ffffff;
  --card:     #f1f5f9;
  --border:   #e2e8f0;
  --border2:  #cbd5e1;
  --hover-bg: rgba(0,0,0,.03);
  --th-bg:    rgba(0,0,0,.04);
  --green:    #16a34a;
  --green-bg: rgba(22,163,74,.12);
  --red:      #dc2626;
  --red-bg:   rgba(220,38,38,.1);
  --yellow:   #d97706;
  --yellow-bg:rgba(217,119,6,.1);
  --blue:     #2563eb;
  --blue-bg:  rgba(37,99,235,.1);
  --purple:   #7c3aed;
  --cyan:     #0891b2;
  --text:     #0f172a;
  --text2:    #475569;
  --text3:    #94a3b8;
  --radius:   10px;
  --radius-sm:6px;
  --shadow:   0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
}
/* ── Dark theme ── */
[data-theme="dark"] {
  --bg:       #090b10;
  --surface:  #0f1219;
  --card:     #141820;
  --border:   #1e2434;
  --border2:  #252d40;
  --hover-bg: rgba(255,255,255,.025);
  --th-bg:    rgba(0,0,0,.2);
  --green:    #22c55e;
  --green-bg: rgba(34,197,94,.1);
  --red:      #f43f5e;
  --red-bg:   rgba(244,63,94,.1);
  --yellow:   #f59e0b;
  --yellow-bg:rgba(245,158,11,.1);
  --blue:     #3b82f6;
  --blue-bg:  rgba(59,130,246,.1);
  --purple:   #a855f7;
  --cyan:     #06b6d4;
  --text:     #e2e8f0;
  --text2:    #94a3b8;
  --text3:    #4b5870;
  --shadow:   none;
}
* { box-sizing:border-box; margin:0; padding:0 }
body { background:var(--bg); color:var(--text); font-family:'Inter',system-ui,sans-serif;
       font-size:13px; line-height:1.5; min-height:100vh }
a { color:inherit; text-decoration:none }

/* ── Layout ── */
.layout { display:grid; grid-template-rows:56px 1fr; height:100vh; overflow:hidden }

/* ── Header ── */
header {
  background:var(--surface);
  border-bottom:1px solid var(--border);
  display:flex; align-items:center; gap:0;
  padding:0 24px; position:relative; z-index:10;
}
.logo {
  display:flex; align-items:center; gap:10px;
  font-size:15px; font-weight:700; letter-spacing:.3px;
  margin-right:32px; flex-shrink:0;
}
.logo-icon { font-size:20px }
.logo-sub { color:var(--text3); font-size:11px; font-weight:500;
            background:var(--card); border:1px solid var(--border);
            padding:1px 7px; border-radius:4px; margin-left:2px }
nav { display:flex; height:100%; gap:2px }
.nav-btn {
  height:100%; padding:0 16px; font-size:12px; font-weight:500;
  color:var(--text2); cursor:pointer; border:none; background:transparent;
  border-bottom:2px solid transparent; transition:all .15s; white-space:nowrap;
  font-family:'Inter',sans-serif; letter-spacing:.3px;
}
.nav-btn:hover { color:var(--text) }
.nav-btn.active { color:var(--blue); border-bottom-color:var(--blue) }
.header-right { margin-left:auto; display:flex; align-items:center; gap:12px }
.refresh-dot { width:7px; height:7px; border-radius:50%; background:var(--green);
               animation:pulse 2s infinite }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.ts-label { color:var(--text3); font-size:11px; font-family:'JetBrains Mono',monospace }
.theme-btn {
  width:32px; height:32px; border-radius:8px; border:1px solid var(--border);
  background:var(--card); cursor:pointer; display:flex; align-items:center;
  justify-content:center; font-size:15px; transition:all .15s; flex-shrink:0;
}
.theme-btn:hover { border-color:var(--border2); background:var(--surface) }

/* ── Main scroll area ── */
.main { overflow-y:auto; padding:24px }

/* ── Tab panels ── */
.tab-panel { display:none }
.tab-panel.active { display:block }

/* ── KPI Cards ── */
.kpi-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:24px }
.kpi {
  background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
  padding:18px 20px; position:relative; overflow:hidden;
}
.kpi::before {
  content:''; position:absolute; top:0; left:0; right:0; height:2px;
  background:var(--accent,var(--blue));
}
.kpi.green { --accent:var(--green) }
.kpi.red   { --accent:var(--red) }
.kpi.yellow{ --accent:var(--yellow) }
.kpi.purple{ --accent:var(--purple) }
.kpi .val { font-size:32px; font-weight:700; font-family:'JetBrains Mono',monospace;
            color:var(--accent,var(--blue)); line-height:1.2 }
.kpi .lbl { color:var(--text2); font-size:11px; font-weight:500; text-transform:uppercase;
            letter-spacing:.6px; margin-top:6px }
.kpi .sub { color:var(--text3); font-size:11px; margin-top:4px }

/* ── Charts row ── */
.charts-row { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:24px }
@media(max-width:900px){ .charts-row{ grid-template-columns:1fr } }

/* ── Panel ── */
.panel {
  background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
  overflow:hidden; margin-bottom:16px; box-shadow:var(--shadow);
}
.panel-head {
  padding:14px 18px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between; gap:8px;
}
.panel-title { font-size:12px; font-weight:600; text-transform:uppercase;
               letter-spacing:.6px; color:var(--text2) }
.panel-body { padding:16px }
.chart-wrap { padding:16px; height:240px; position:relative }

/* ── Table ── */
.tbl { width:100%; border-collapse:collapse }
.tbl th {
  text-align:left; padding:10px 16px;
  font-size:11px; font-weight:600; text-transform:uppercase;
  letter-spacing:.5px; color:var(--text3);
  border-bottom:1px solid var(--border);
  background:var(--th-bg);
}
.tbl td { padding:11px 16px; border-bottom:1px solid var(--border); vertical-align:middle }
.tbl tr:last-child td { border-bottom:none }
.tbl tr.row { cursor:pointer; transition:background .12s }
.tbl tr.row:hover { background:var(--hover-bg) }

/* ── Badges ── */
.badge {
  display:inline-flex; align-items:center; gap:5px;
  padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600;
}
.badge.running  { background:var(--green-bg); color:var(--green) }
.badge.reverted { background:var(--blue-bg);  color:var(--blue) }
.badge.failed   { background:var(--red-bg);   color:var(--red) }
.badge.ok       { background:var(--green-bg); color:var(--green) }
.badge.missing  { background:var(--yellow-bg);color:var(--yellow) }
.badge.neutral  { background:rgba(148,163,184,.1); color:var(--text2) }
.badge::before { content:''; width:5px; height:5px; border-radius:50%; background:currentColor }

/* ── Fault chip ── */
.chip {
  display:inline-block; padding:1px 7px; border-radius:4px; font-size:10px;
  font-weight:600; background:rgba(168,85,247,.15); color:var(--purple);
  border:1px solid rgba(168,85,247,.2); margin:1px; white-space:nowrap;
}

/* ── Tool grid ── */
.tool-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:8px }
.tool-card {
  display:flex; align-items:center; gap:12px;
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:10px 14px;
}
.tool-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0 }
.tool-dot.ok { background:var(--green) }
.tool-dot.missing { background:var(--red) }
.tool-info { flex:1; min-width:0 }
.tool-name { font-weight:600; font-size:12px }
.tool-path { color:var(--text3); font-size:10px; font-family:'JetBrains Mono',monospace;
             overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.tool-pkg { background:var(--card); border:1px solid var(--border); border-radius:4px;
            padding:1px 7px; font-size:10px; color:var(--text2); flex-shrink:0 }
.tool-role { color:var(--text3); font-size:10px; margin-top:1px }

/* ── Log viewer ── */
.log-pre {
  font-family:'JetBrains Mono',monospace; font-size:11px; line-height:1.8;
  padding:14px 16px; max-height:440px; overflow-y:auto;
  background:var(--surface); white-space:pre-wrap; word-break:break-all;
}

/* ── Search/filter bar ── */
.filter-bar { display:flex; gap:8px; align-items:center; padding:12px 16px;
              border-bottom:1px solid var(--border) }
.filter-bar input, .filter-bar select {
  background:var(--surface); color:var(--text); border:1px solid var(--border);
  border-radius:var(--radius-sm); padding:5px 10px; font-size:12px;
  font-family:'Inter',sans-serif; outline:none;
}
.filter-bar input { flex:1 }
.filter-bar input:focus, .filter-bar select:focus { border-color:var(--blue) }

/* ── Overlay + Drawer ── */
#overlay {
  display:none; position:fixed; inset:0; background:rgba(0,0,0,.6);
  backdrop-filter:blur(2px); z-index:100;
}
#overlay.on { display:block }
#drawer {
  position:fixed; top:0; right:-640px; width:600px; height:100vh;
  background:var(--card); border-left:1px solid var(--border);
  overflow:hidden; transition:right .28s cubic-bezier(.4,0,.2,1); z-index:101;
  display:flex; flex-direction:column;
}
#drawer.open { right:0 }

.drawer-head {
  padding:16px 20px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; gap:12px; flex-shrink:0;
}
.drawer-title { font-size:16px; font-weight:700; flex:1 }
.drawer-close {
  width:28px; height:28px; border-radius:6px; background:var(--surface);
  border:1px solid var(--border); cursor:pointer; display:flex;
  align-items:center; justify-content:center; color:var(--text2); font-size:14px;
}
.drawer-close:hover { color:var(--text); border-color:var(--border2) }

/* Drawer tabs */
.drawer-tabs {
  display:flex; border-bottom:1px solid var(--border); flex-shrink:0;
  background:var(--surface);
}
.dtab {
  padding:10px 16px; font-size:12px; font-weight:500; color:var(--text2);
  cursor:pointer; border-bottom:2px solid transparent; transition:all .12s;
}
.dtab:hover { color:var(--text) }
.dtab.active { color:var(--blue); border-bottom-color:var(--blue) }

.drawer-body { overflow-y:auto; flex:1 }

/* Drawer sections */
.dsec { padding:16px 20px; border-bottom:1px solid var(--border) }
.dsec:last-child { border-bottom:none }
.dsec-title { font-size:10px; font-weight:600; text-transform:uppercase;
              letter-spacing:.7px; color:var(--text3); margin-bottom:12px }

/* Meta row */
.meta-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px }
.meta-item { background:var(--surface); border:1px solid var(--border);
             border-radius:var(--radius-sm); padding:10px 12px }
.meta-val { font-size:14px; font-weight:700; font-family:'JetBrains Mono',monospace;
            color:var(--text) }
.meta-key { font-size:10px; color:var(--text3); margin-top:3px; text-transform:uppercase; letter-spacing:.5px }

/* Metric compare table */
.metric-tbl { width:100%; border-collapse:collapse }
.metric-tbl th {
  font-size:10px; color:var(--text3); text-transform:uppercase; letter-spacing:.5px;
  padding:6px 8px; border-bottom:1px solid var(--border); text-align:left;
}
.metric-tbl td { padding:7px 8px; border-bottom:1px solid var(--border); vertical-align:middle }
.metric-tbl tr:last-child td { border-bottom:none }
.metric-name { font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--text2) }
.metric-val  { font-family:'JetBrains Mono',monospace; font-size:12px; font-weight:600; text-align:right }
.delta-bar-wrap { position:relative; height:4px; background:var(--border); border-radius:2px;
                  width:80px; overflow:hidden }
.delta-bar { position:absolute; top:0; left:0; height:100%; border-radius:2px; min-width:2px }
.delta-val { font-size:11px; font-weight:600; font-family:'JetBrains Mono',monospace;
             white-space:nowrap }
.delta-up   { color:var(--red) }
.delta-down { color:var(--green) }
.delta-neu  { color:var(--text3) }

/* ── Event timeline ── */
.timeline { padding:4px 0 }
.tl-item { display:flex; gap:12px; padding:8px 0; position:relative }
.tl-item::before {
  content:''; position:absolute; left:12px; top:28px; bottom:-8px;
  width:1px; background:var(--border);
}
.tl-item:last-child::before { display:none }
.tl-dot {
  width:24px; height:24px; border-radius:50%; flex-shrink:0; display:flex;
  align-items:center; justify-content:center; font-size:10px; margin-top:1px;
}
.tl-dot.info  { background:var(--blue-bg);  color:var(--blue)   }
.tl-dot.ok    { background:var(--green-bg); color:var(--green)  }
.tl-dot.err   { background:var(--red-bg);   color:var(--red)    }
.tl-dot.warn  { background:var(--yellow-bg);color:var(--yellow) }
.tl-content { flex:1; min-width:0 }
.tl-ts  { font-size:10px; color:var(--text3); font-family:'JetBrains Mono',monospace }
.tl-msg { font-size:11px; color:var(--text2); line-height:1.5; word-break:break-word }
.tl-msg.err { color:var(--red) }

/* ── Fault block ── */
.fault-block {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  margin-bottom:10px; overflow:hidden;
}
.fault-head { padding:10px 14px; border-bottom:1px solid var(--border);
              display:flex; align-items:center; gap:8px }
.fault-kind { font-size:12px; font-weight:700; color:var(--purple) }
.fault-params { padding:10px 14px }
.fault-params pre { font-size:11px; color:var(--text2); line-height:1.7;
                    white-space:pre-wrap; font-family:'JetBrains Mono',monospace }

/* ── Empty state ── */
.empty-state { padding:48px; text-align:center; color:var(--text3) }
.empty-icon { font-size:36px; margin-bottom:12px }
.empty-msg { font-size:13px }

/* ── Misc ── */
.divider { height:1px; background:var(--border); margin:16px 0 }
.mono { font-family:'JetBrains Mono',monospace }
.muted { color:var(--text2) }
.small { font-size:11px }
</style>
</head>
<body>
<div class="layout">

<!-- ── Header ── -->
<header>
  <div class="logo">
    <span class="logo-icon">🌿</span>
    chaos-jungle
    <span class="logo-sub">v0.1</span>
  </div>
  <nav>
    <button class="nav-btn active" onclick="switchTab('overview',this)">Overview</button>
    <button class="nav-btn" onclick="switchTab('sessions',this)">Sessions</button>
    <button class="nav-btn" onclick="switchTab('tools',this)">System tools</button>
    <button class="nav-btn" onclick="switchTab('logs',this)">Logs</button>
  </nav>
  <div class="header-right">
    <div class="refresh-dot"></div>
    <span class="ts-label" id="ts">loading…</span>
    <button class="theme-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle light / dark">🌙</button>
  </div>
</header>

<!-- ── Main ── -->
<div class="main">

  <!-- ═══ OVERVIEW TAB ═══ -->
  <div class="tab-panel active" id="tab-overview">
    <div class="kpi-row" id="kpi-row"></div>
    <div class="charts-row">
      <div class="panel">
        <div class="panel-head"><span class="panel-title">Fault distribution</span></div>
        <div class="chart-wrap"><canvas id="chart-faults"></canvas></div>
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">Session status</span></div>
        <div class="chart-wrap"><canvas id="chart-status"></canvas></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <span class="panel-title">Recent sessions</span>
        <span class="small muted" id="overview-count"></span>
      </div>
      <table class="tbl" id="overview-sessions"></table>
    </div>
  </div>

  <!-- ═══ SESSIONS TAB ═══ -->
  <div class="tab-panel" id="tab-sessions">
    <div class="panel">
      <div class="filter-bar">
        <input type="search" id="session-search" placeholder="Filter by name, fault, status…" oninput="filterSessions()"/>
        <select id="session-status-filter" onchange="filterSessions()">
          <option value="">All statuses</option>
          <option value="running">Running</option>
          <option value="reverted">Reverted</option>
          <option value="failed">Failed</option>
        </select>
      </div>
      <table class="tbl">
        <thead><tr>
          <th style="width:52px">ID</th>
          <th>Scenario</th>
          <th>Status</th>
          <th>Faults</th>
          <th>Started</th>
          <th style="width:80px">Duration</th>
        </tr></thead>
        <tbody id="sessions-body"></tbody>
      </table>
    </div>
  </div>

  <!-- ═══ TOOLS TAB ═══ -->
  <div class="tab-panel" id="tab-tools">
    <div class="panel">
      <div class="panel-head">
        <span class="panel-title">System dependencies</span>
        <span class="small muted" id="tool-summary"></span>
      </div>
      <div class="panel-body">
        <div class="tool-grid" id="tools"></div>
      </div>
    </div>
  </div>

  <!-- ═══ LOGS TAB ═══ -->
  <div class="tab-panel" id="tab-logs">
    <div class="panel">
      <div class="panel-head">
        <span class="panel-title">Log viewer</span>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="log-select" style="background:var(--surface);color:var(--text);
            border:1px solid var(--border);border-radius:var(--radius-sm);
            padding:4px 10px;font-size:12px;font-family:'Inter',sans-serif"></select>
          <span class="small muted" id="log-lines"></span>
        </div>
      </div>
      <pre class="log-pre" id="log-content">Select a log file above.</pre>
    </div>
  </div>

</div><!-- .main -->
</div><!-- .layout -->

<!-- ── Overlay + Drawer ── -->
<div id="overlay" onclick="closeDrawer()"></div>
<div id="drawer">
  <div class="drawer-head">
    <div class="drawer-title" id="drawer-title">Session</div>
    <span class="badge" id="drawer-badge"></span>
    <div class="drawer-close" onclick="closeDrawer()">✕</div>
  </div>
  <div class="drawer-tabs" id="drawer-tabs"></div>
  <div class="drawer-body" id="drawer-body"></div>
</div>

<script>
// ─────────────────────────────────────────────────────────────
// Theme
// ─────────────────────────────────────────────────────────────
function isDark(){ return document.documentElement.getAttribute('data-theme')==='dark' }

function setTheme(t, save=true){
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('theme-btn').textContent = t==='dark' ? '☀️' : '🌙';
  if(save) localStorage.setItem('cj-theme', t);
  if(_sessions.length) renderCharts(_sessions);
}
function toggleTheme(){ setTheme(isDark() ? 'light' : 'dark') }
function initTheme(){ setTheme(localStorage.getItem('cj-theme') || 'light', false) }

function chartColors(){
  return isDark()
    ? { grid:'#1e2434', tick:'#4b5870', legend:'#94a3b8' }
    : { grid:'#e2e8f0', tick:'#94a3b8', legend:'#475569' };
}

// ─────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────
let _sessions = [];
let _tools    = [];
let _chartFaults = null;
let _chartStatus = null;
let _drawerTabs  = {};
let _activeDTab  = 'summary';

// ─────────────────────────────────────────────────────────────
// Tab navigation
// ─────────────────────────────────────────────────────────────
function switchTab(id, btn){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  btn.classList.add('active');
  if(id==='logs') loadLogs();
}

// ─────────────────────────────────────────────────────────────
// Main data load
// ─────────────────────────────────────────────────────────────
async function load(){
  try {
    const [sessions, tools] = await Promise.all([
      fetch('/api/sessions').then(r=>r.json()),
      fetch('/api/system').then(r=>r.json()),
    ]);
    _sessions = sessions;
    _tools    = tools;
    renderKPI(sessions);
    renderCharts(sessions);
    renderOverviewTable(sessions);
    renderSessionsTable(sessions);
    renderTools(tools);
    document.getElementById('ts').textContent =
      'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('ts').textContent = 'Error loading data';
  }
}

// ─────────────────────────────────────────────────────────────
// KPI cards
// ─────────────────────────────────────────────────────────────
function renderKPI(sessions){
  const total    = sessions.length;
  const running  = sessions.filter(s=>s.status==='running').length;
  const reverted = sessions.filter(s=>s.status==='reverted').length;
  const failed   = sessions.filter(s=>s.status==='failed').length;
  const avgDur   = sessions.filter(s=>s.duration_s).length
    ? (sessions.filter(s=>s.duration_s).reduce((a,s)=>a+s.duration_s,0)
       / sessions.filter(s=>s.duration_s).length).toFixed(1)
    : '—';

  document.getElementById('kpi-row').innerHTML = `
    <div class="kpi blue">
      <div class="val">${total}</div>
      <div class="lbl">Total sessions</div>
      <div class="sub">all time</div>
    </div>
    <div class="kpi ${running?'green':''}">
      <div class="val">${running}</div>
      <div class="lbl">Running now</div>
      <div class="sub">${running?'faults active':'idle'}</div>
    </div>
    <div class="kpi green">
      <div class="val">${reverted}</div>
      <div class="lbl">Clean reverts</div>
      <div class="sub">faults removed</div>
    </div>
    <div class="kpi ${failed?'red':''}">
      <div class="val">${failed}</div>
      <div class="lbl">Failed</div>
      <div class="sub">did not revert</div>
    </div>
    <div class="kpi yellow">
      <div class="val">${avgDur}${avgDur!=='—'?'s':''}</div>
      <div class="lbl">Avg duration</div>
      <div class="sub">per session</div>
    </div>
  `;
}

// ─────────────────────────────────────────────────────────────
// Charts
// ─────────────────────────────────────────────────────────────
function renderCharts(sessions){
  const c = chartColors();
  const axisOpts = {
    ticks:{ color:c.tick, font:{size:10,family:'Inter'} },
    grid:{ color:c.grid },
  };
  const faultMap = {};
  sessions.forEach(s=>(s.faults||[]).forEach(f=>{
    faultMap[f.kind] = (faultMap[f.kind]||0) + 1;
  }));
  const faultLabels = Object.keys(faultMap);
  const faultData   = Object.values(faultMap);
  const palette = ['#7c3aed','#2563eb','#16a34a','#d97706','#dc2626',
                   '#0891b2','#db2777','#65a30d','#ea580c','#0d9488'];

  if(_chartFaults) _chartFaults.destroy();
  _chartFaults = new Chart(document.getElementById('chart-faults'), {
    type: 'bar',
    data:{
      labels: faultLabels.length ? faultLabels.map(l=>l.replace('LLM','').replace('Network','Net')) : ['No data'],
      datasets:[{ label:'Sessions', data: faultData.length ? faultData : [0],
        backgroundColor: palette, borderRadius:4, borderSkipped:false }]
    },
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false} },
      scales:{ x:axisOpts, y:{...axisOpts, ticks:{...axisOpts.ticks, stepSize:1}} } }
  });

  const st = { running:0, reverted:0, failed:0 };
  sessions.forEach(s=>{ if(st[s.status]!==undefined) st[s.status]++ });

  if(_chartStatus) _chartStatus.destroy();
  _chartStatus = new Chart(document.getElementById('chart-status'), {
    type: 'doughnut',
    data:{
      labels:['Reverted','Running','Failed'],
      datasets:[{ data:[st.reverted, st.running, st.failed],
        backgroundColor:['#16a34a','#2563eb','#dc2626'],
        borderWidth:0, hoverOffset:4 }]
    },
    options:{ responsive:true, maintainAspectRatio:false, cutout:'72%',
      plugins:{ legend:{ position:'right',
        labels:{ color:c.legend, font:{family:'Inter',size:11}, boxWidth:10, padding:14 }}}}
  });
}

// ─────────────────────────────────────────────────────────────
// Tables
// ─────────────────────────────────────────────────────────────
function sessionRow(s, slim){
  const dur     = s.duration_s != null ? s.duration_s+'s'
                : (s.status==='running' ? '⏱ running' : '—');
  const started = (s.started_at||'').replace('T',' ').slice(0,19) || '—';
  const faults  = (s.faults||[]).map(f=>`<span class="chip">${f.kind}</span>`).join('') || '<span class="muted small">—</span>';
  return `<tr class="row" onclick="openSession(${s.id})">
    <td class="mono small muted">#${s.id}</td>
    <td style="font-weight:600">${esc(s.name)}</td>
    <td><span class="badge ${s.status}">${s.status}</span></td>
    <td>${faults}</td>
    <td class="small muted mono">${started}</td>
    <td class="small mono">${dur}</td>
  </tr>`;
}

function renderOverviewTable(sessions){
  const recent = sessions.slice(0,8);
  document.getElementById('overview-count').textContent = `${sessions.length} total`;
  const tbody = recent.length
    ? `<thead><tr><th style="width:52px">ID</th><th>Scenario</th><th>Status</th><th>Faults</th><th>Started</th><th>Duration</th></tr></thead><tbody>`
      + recent.map(s=>sessionRow(s)).join('') + '</tbody>'
    : `<tbody><tr><td colspan="6"><div class="empty-state"><div class="empty-icon">🌿</div><div class="empty-msg">No sessions yet. Run your first experiment!</div></div></td></tr></tbody>`;
  document.getElementById('overview-sessions').innerHTML = tbody;
}

function renderSessionsTable(sessions){
  document.getElementById('sessions-body').innerHTML = sessions.length
    ? sessions.map(s=>sessionRow(s)).join('')
    : `<tr><td colspan="6"><div class="empty-state"><div class="empty-icon">🌿</div><div class="empty-msg">No sessions yet.</div></div></td></tr>`;
}

function filterSessions(){
  const q   = document.getElementById('session-search').value.toLowerCase();
  const st  = document.getElementById('session-status-filter').value;
  const filtered = _sessions.filter(s=>{
    const matchQ  = !q || s.name.toLowerCase().includes(q)
      || (s.faults||[]).some(f=>f.kind.toLowerCase().includes(q))
      || String(s.id).includes(q);
    const matchSt = !st || s.status===st;
    return matchQ && matchSt;
  });
  document.getElementById('sessions-body').innerHTML = filtered.length
    ? filtered.map(s=>sessionRow(s)).join('')
    : `<tr><td colspan="6"><div class="empty-state"><div class="empty-icon">🔍</div><div class="empty-msg">No sessions match your filter.</div></div></td></tr>`;
}

// ─────────────────────────────────────────────────────────────
// Tools
// ─────────────────────────────────────────────────────────────
function renderTools(tools){
  const found = tools.filter(t=>t.found).length;
  document.getElementById('tool-summary').textContent = found+'/'+tools.length+' installed';
  document.getElementById('tools').innerHTML = tools.map(t=>`
    <div class="tool-card">
      <div class="tool-dot ${t.found?'ok':'missing'}"></div>
      <div class="tool-info">
        <div class="tool-name">${t.binary}</div>
        <div class="tool-path">${t.found ? t.path : 'not installed'}</div>
        <div class="tool-role muted" style="font-size:10px">${t.role}</div>
      </div>
      <div class="tool-pkg">${t.package}</div>
    </div>
  `).join('');
}

// ─────────────────────────────────────────────────────────────
// Session drawer
// ─────────────────────────────────────────────────────────────
async function openSession(id){
  // Reset drawer
  document.getElementById('drawer-title').textContent = 'Session #' + id;
  document.getElementById('drawer-body').innerHTML =
    '<div class="empty-state"><div class="empty-icon">⏳</div><div class="empty-msg">Loading…</div></div>';
  document.getElementById('drawer-tabs').innerHTML = '';
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('on');

  const [d, ana] = await Promise.all([
    fetch(`/api/session/${id}`).then(r=>r.json()),
    fetch(`/api/session/${id}/analysis`).then(r=>r.json()),
  ]);

  const s   = d.session;
  const dur = s.duration_s != null ? s.duration_s+'s'
            : (s.status==='running' ? 'still running' : '—');

  // Badge
  const badgeEl = document.getElementById('drawer-badge');
  badgeEl.className = 'badge '+s.status;
  badgeEl.innerHTML = '<span style="width:5px;height:5px;border-radius:50%;background:currentColor;display:inline-block;margin-right:5px"></span>'+s.status;

  // Build drawer tabs
  const tabs = [
    { id:'summary', label:'Summary' },
    { id:'metrics', label:'Metrics' + (ana.results&&ana.results.length ? ` (${ana.results.length})` : '') },
    { id:'events',  label:'Events'  + (d.events.length ? ` (${d.events.length})` : '') },
    { id:'faults',  label:'Faults'  + (d.faults.length ? ` (${d.faults.length})` : '') },
  ];

  document.getElementById('drawer-tabs').innerHTML = tabs.map(t=>
    `<div class="dtab${t.id==='summary'?' active':''}" onclick="switchDTab('${t.id}',this)">${t.label}</div>`
  ).join('');

  // Build all tab contents
  _drawerTabs = {
    summary: buildSummaryTab(s, dur, ana),
    metrics: buildMetricsTab(ana),
    events:  buildEventsTab(d.events),
    faults:  buildFaultsTab(d.faults),
  };
  _activeDTab = 'summary';
  document.getElementById('drawer-body').innerHTML = _drawerTabs['summary'];
}

function switchDTab(id, el){
  document.querySelectorAll('.dtab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  _activeDTab = id;
  document.getElementById('drawer-body').innerHTML = _drawerTabs[id];
}

// ── Summary tab ──────────────────────────────────────────────
function buildSummaryTab(s, dur, ana){
  const started = (s.started_at||'—').replace('T',' ').slice(0,19);
  const stopped = (s.stopped_at||'—').replace('T',' ').slice(0,19);
  const ok  = ana.command_ok    || 0;
  const err = ana.command_error || 0;

  return `
  <div class="dsec">
    <div class="dsec-title">Session info</div>
    <div class="meta-grid">
      <div class="meta-item">
        <div class="meta-val mono">${started}</div>
        <div class="meta-key">Started</div>
      </div>
      <div class="meta-item">
        <div class="meta-val mono">${stopped}</div>
        <div class="meta-key">Stopped</div>
      </div>
      <div class="meta-item">
        <div class="meta-val mono">${dur}</div>
        <div class="meta-key">Duration</div>
      </div>
      <div class="meta-item">
        <div class="meta-val">
          <span style="color:var(--green)">${ok}</span>
          <span style="color:var(--text3);font-size:12px"> / </span>
          <span style="color:var(--red)">${err}</span>
        </div>
        <div class="meta-key">Commands OK / Error</div>
      </div>
    </div>
  </div>
  ${ana.active_tc_rules && ana.active_tc_rules.length ? `
  <div class="dsec">
    <div class="dsec-title">Active network rules</div>
    <pre style="font-size:10px;color:var(--text2);background:var(--surface);padding:10px;
      border-radius:var(--radius-sm);overflow-x:auto;line-height:1.7">${ana.active_tc_rules.map(esc).join('\\n')}</pre>
  </div>` : ''}
  ${ana.storage_records && ana.storage_records.length ? `
  <div class="dsec">
    <div class="dsec-title">Storage bit-flips (${ana.storage_records.length})</div>
    <table class="metric-tbl">
      <tr><th>File</th><th>Block</th><th>Before</th><th>After</th></tr>
      ${ana.storage_records.slice(0,15).map(r=>`<tr>
        <td class="metric-name" style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${esc(r.filename||'')}">${esc((r.filename||'').split('/').pop())}</td>
        <td class="metric-val muted">${r.targetblock||''}</td>
        <td class="metric-val" style="color:var(--green)">0x${(r.origValue||0).toString(16)}</td>
        <td class="metric-val" style="color:var(--red)">0x${(r.afterValue||0).toString(16)}</td>
      </tr>`).join('')}
    </table>
    ${ana.storage_records.length>15?`<div class="small muted" style="padding:6px 8px">… ${ana.storage_records.length-15} more</div>`:''}
  </div>` : ''}
  `;
}

// ── Metrics tab ──────────────────────────────────────────────
function buildMetricsTab(ana){
  if(!ana.results || !ana.results.length){
    return '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-msg">No metric results recorded for this session.</div></div>';
  }

  let html = '';
  ana.results.forEach((r, idx)=>{
    const m = r.metrics || {};
    const keys = Object.keys(m);

    // Group into baseline/chaos/delta groups and plain keys
    const groups = {};
    keys.forEach(k=>{
      const m3 = k.match(/^(baseline|chaos|delta)_(.+)$/);
      if(m3){
        const grp = m3[2];
        if(!groups[grp]) groups[grp] = {};
        groups[grp][m3[1]] = m[k];
      }
    });
    const plainKeys = keys.filter(k=>!k.match(/^(baseline|chaos|delta)_/));

    html += `<div class="dsec"><div class="dsec-title">Result #${idx+1}`;
    if(r.recorded_at) html += ` <span class="muted" style="font-size:10px;font-weight:400">${r.recorded_at.slice(0,19).replace('T',' ')}</span>`;
    html += '</div>';

    // Metric comparison table
    const grpKeys = Object.keys(groups);
    if(grpKeys.length){
      html += `<table class="metric-tbl">
        <tr><th>Metric</th><th style="text-align:right">Baseline</th><th style="text-align:right">Chaos</th><th>Delta</th></tr>`;
      grpKeys.forEach(g=>{
        const gd = groups[g];
        const b  = gd.baseline;
        const c  = gd.chaos;
        const d  = gd.delta;
        const bFmt = fmt(b);
        const cFmt = fmt(c);
        let deltaHtml = '';
        if(typeof d === 'number'){
          // Determine if higher is better for this metric
          const higherBetter = /token|speed|throughput|rate|mbps|count/.test(g);
          const worse = higherBetter ? d < 0 : d > 0;
          const cls   = Math.abs(d) < 0.001 ? 'neu' : (worse ? 'up' : 'down');
          const sign  = d > 0 ? '+' : '';
          const pct   = b && b !== 0 ? ` (${sign}${((d/Math.abs(b))*100).toFixed(0)}%)` : '';
          const barW  = Math.min(100, Math.abs(b && b!==0 ? (d/Math.abs(b)*100) : 0));
          deltaHtml = `<div style="display:flex;align-items:center;gap:8px">
            <div class="delta-bar-wrap">
              <div class="delta-bar" style="width:${barW}%;background:${cls==='up'?'var(--red)':cls==='down'?'var(--green)':'var(--text3)'}"></div>
            </div>
            <span class="delta-val delta-${cls}">${sign}${d.toFixed(3)}${pct}</span>
          </div>`;
        }
        html += `<tr>
          <td class="metric-name">${g.replace(/_/g,' ')}</td>
          <td class="metric-val muted">${bFmt}</td>
          <td class="metric-val">${cFmt}</td>
          <td>${deltaHtml}</td>
        </tr>`;
      });
      html += '</table>';
    }

    // Plain key-value results
    if(plainKeys.length){
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:6px;margin-top:10px">';
      plainKeys.forEach(k=>{
        const v = m[k];
        const color = k.includes('fail')||k.includes('error') ? 'var(--red)'
                    : k.includes('ok')||k.includes('success') ? 'var(--green)'
                    : 'var(--blue)';
        html += `<div style="background:var(--surface);border:1px solid var(--border);
          border-radius:var(--radius-sm);padding:8px 10px;text-align:center">
          <div style="font-size:16px;font-weight:700;font-family:'JetBrains Mono',monospace;color:${color}">${fmt(v)}</div>
          <div style="color:var(--text3);font-size:10px;text-transform:uppercase;letter-spacing:.4px;margin-top:3px">${k.replace(/_/g,' ')}</div>
        </div>`;
      });
      html += '</div>';
    }

    html += '</div>';
  });
  return html;
}

// ── Events tab ───────────────────────────────────────────────
function buildEventsTab(events){
  if(!events.length) return '<div class="empty-state"><div class="empty-icon">📋</div><div class="empty-msg">No events recorded.</div></div>';
  const items = events.map(e=>{
    const ts  = (e.timestamp||'').replace('T',' ').slice(11,19);
    const isErr  = /ERROR|error/i.test(e.message);
    const isOk   = /OK|start|stop|revert/i.test(e.message);
    const isWarn = /WARN|warn/i.test(e.message);
    const cls    = isErr ? 'err' : isOk ? 'ok' : isWarn ? 'warn' : 'info';
    const icon   = isErr ? '✕' : isOk ? '✓' : isWarn ? '!' : '·';
    return `<div class="tl-item">
      <div class="tl-dot ${cls}">${icon}</div>
      <div class="tl-content">
        <div class="tl-ts">${ts}</div>
        <div class="tl-msg ${isErr?'err':''}">${esc(e.message)}</div>
      </div>
    </div>`;
  });
  return `<div class="dsec"><div class="dsec-title">Event log</div><div class="timeline">${items.join('')}</div></div>`;
}

// ── Faults tab ───────────────────────────────────────────────
function buildFaultsTab(faults){
  if(!faults.length) return '<div class="empty-state"><div class="empty-icon">⚡</div><div class="empty-msg">No faults recorded.</div></div>';
  const blocks = faults.map(f=>{
    let params;
    try {
      const p = typeof f.parameters==='string' ? JSON.parse(f.parameters) : f.parameters;
      params = JSON.stringify(p, null, 2);
    } catch(e){ params = String(f.parameters||'{}'); }
    return `<div class="fault-block">
      <div class="fault-head">
        <span class="fault-kind">${esc(f.kind)}</span>
      </div>
      <div class="fault-params"><pre>${esc(params)}</pre></div>
    </div>`;
  });
  return `<div class="dsec"><div class="dsec-title">Injected faults</div>${blocks.join('')}</div>`;
}

function closeDrawer(){
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('on');
}

// ─────────────────────────────────────────────────────────────
// Logs
// ─────────────────────────────────────────────────────────────
let _logFiles = [];

async function loadLogList(){
  const files = await fetch('/api/logs').then(r=>r.json());
  _logFiles = files;
  const sel = document.getElementById('log-select');
  const prev = sel.value;
  sel.innerHTML = files.length
    ? files.map(f=>`<option value="${f.name}">${f.name} (${f.size_kb}kb)</option>`).join('')
    : '<option value="">no log files found</option>';
  if(prev && files.find(f=>f.name===prev)) sel.value = prev;
}

async function loadLogContent(){
  const sel = document.getElementById('log-select');
  if(!sel.value) return;
  const data = await fetch(`/api/logs/${encodeURIComponent(sel.value)}?lines=150`).then(r=>r.json());
  const pre  = document.getElementById('log-content');
  if(data.error){ pre.textContent = data.error; return; }
  pre.innerHTML = data.lines.map(l=>{
    if(/ERROR|FAIL|critical/i.test(l)) return `<span style="color:var(--red)">${esc(l)}</span>`;
    if(/WARN/i.test(l))                return `<span style="color:var(--yellow)">${esc(l)}</span>`;
    if(/CORRUPT|inversion/i.test(l))   return `<span style="color:var(--yellow)">${esc(l)}</span>`;
    if(/REVERT|stop|OK/i.test(l))      return `<span style="color:var(--green)">${esc(l)}</span>`;
    return `<span style="color:var(--text2)">${esc(l)}</span>`;
  }).join('\\n');
  pre.scrollTop = pre.scrollHeight;
  document.getElementById('log-lines').textContent = data.lines.length + ' lines';
}

async function loadLogs(){
  await loadLogList();
  await loadLogContent();
}

document.getElementById('log-select').addEventListener('change', loadLogContent);

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────
function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function fmt(v){
  if(v === null || v === undefined) return '—';
  if(typeof v === 'number'){
    if(Math.abs(v) >= 1000) return v.toLocaleString(undefined,{maximumFractionDigits:1});
    if(Number.isInteger(v)) return String(v);
    return v.toFixed(3).replace(/\\.?0+$/,'');
  }
  return esc(String(v));
}

// ─────────────────────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────────────────────
initTheme();
load();
setInterval(()=>{ load(); if(document.getElementById('tab-logs').classList.contains('active')) loadLogContent(); }, 6000);
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
        ("tc",          "iproute2",       "network faults (netem)"),
        ("ip",          "iproute2",       "interface detection"),
        ("filefrag",    "e2fsprogs",      "storage — extent info"),
        ("dd",          "coreutils",      "storage — bit-flip"),
        ("inotifywait", "inotify-tools",  "storage — file watch"),
        ("python3",     "python3",        "storage / BPF scripts"),
        ("pip3",        "python3-pip",    "Python package install"),
        ("ssh",         "openssh-client", "SSH target support"),
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
async def api_log_content(filename: str, lines: int = 150):
    """Return the last N lines of a log file under ~/.chaos-jungle/."""
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

    # ── command summary ─────────────────────────────────────────────
    events = data["events"]
    cmd_ok    = sum(1 for e in events if "[cmd:OK]"    in e.get("message", ""))
    cmd_error = sum(1 for e in events if "[cmd:ERROR]" in e.get("message", ""))

    return JSONResponse({
        "session_id":      session_id,
        "command_ok":      cmd_ok,
        "command_error":   cmd_error,
        "storage_records": storage_records,
        "active_tc_rules": tc_rules,
        "results":         results,
    })


@app.get("/api/cj_records")
async def api_cj_records():
    """Return all bit-flip records from cj.db."""
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
