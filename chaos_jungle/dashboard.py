"""chaos-jungle experiment tracking dashboard.

A self-contained FastAPI web UI that reads from the local SQLite database
and shows all fault injection sessions, LLM call traces, and monitoring charts
across every fault category (network, resource, process, storage, state, LLM,
MCP, skill, semantic, GPU).

Launch via:  chaos-jungle dashboard
Or programmatically:  from chaos_jungle.dashboard import run; run()
"""

from __future__ import annotations
import os
import sqlite3 as _sql
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from chaos_jungle.db.session_db import SessionDB

app = FastAPI(title="chaos-jungle dashboard", docs_url=None, redoc_url=None)

# ── Fault category helpers ──────────────────────────────────────────────────

_FAULT_CATEGORIES = {
    "network":   {"color": "#2563eb", "bg": "rgba(37,99,235,.12)",  "label": "Network"},
    "resource":  {"color": "#ea580c", "bg": "rgba(234,88,12,.12)",  "label": "Resource"},
    "process":   {"color": "#dc2626", "bg": "rgba(220,38,38,.12)",  "label": "Process"},
    "storage":   {"color": "#d97706", "bg": "rgba(217,119,6,.12)",  "label": "Storage"},
    "state":     {"color": "#0891b2", "bg": "rgba(8,145,178,.12)",  "label": "State"},
    "llm":       {"color": "#7c3aed", "bg": "rgba(124,58,237,.12)", "label": "LLM/MCP"},
    "skill":     {"color": "#4f46e5", "bg": "rgba(79,70,229,.12)",  "label": "Skill"},
    "semantic":  {"color": "#db2777", "bg": "rgba(219,39,119,.12)", "label": "Semantic"},
    "gpu":       {"color": "#16a34a", "bg": "rgba(22,163,74,.12)",  "label": "GPU"},
    "other":     {"color": "#64748b", "bg": "rgba(100,116,139,.12)","label": "Other"},
}

_FAULT_KIND_MAP = {
    "NetworkDelay": "network", "NetworkLoss": "network", "NetworkCorrupt": "network",
    "NetworkDuplicate": "network", "SilentNetworkCorrupt": "network",
    "DiskFull": "resource", "CPUStress": "resource", "MemoryStress": "resource",
    "IOStress": "resource",
    "ProcessKill": "process", "ServiceFault": "process", "ContainerKill": "process",
    "StorageCorrupt": "storage",
    "RedisStateCorrupt": "state", "JsonStateCorrupt": "state", "PostgresStateCorrupt": "state",
    "LLMLatency": "llm", "LLMRateLimit": "llm", "LLMTimeout": "llm",
    "LLMResponseCorrupt": "llm", "LLMUnavailable": "llm", "ToolFault": "llm",
    "LLMHallucination": "llm", "LLMStreamInterrupt": "llm", "LLMTokenStarvation": "llm",
    "LLMBudgetExceeded": "llm", "MCPFault": "llm",
    "SkillUnavailable": "skill", "SkillMisroute": "skill", "SkillInstructionCorrupt": "skill",
    "SkillDependencyMissing": "skill", "SkillTimeout": "skill", "SkillBadOutput": "skill",
    "SkillVersionSkew": "skill", "SkillPermissionDenied": "skill", "SkillMemoryStale": "skill",
    "ConflictingSkills": "skill", "LLMSkillFaultGenerator": "skill",
    "SkillFileUnavailable": "skill", "SkillFileInstructionCorrupt": "skill",
    "SkillFileVersionSkew": "skill", "SkillFileBadOutput": "skill",
    "SkillFileMemoryStale": "skill", "SkillFileConflict": "skill",
    "SkillFilePermissionDenied": "skill",
    "SemanticCorrupt": "semantic",
    "GPUThrottle": "gpu", "GPUMemoryPressure": "gpu", "GPUClockLock": "gpu",
}

def _fault_category(kind: str) -> str:
    return _FAULT_KIND_MAP.get(kind, "other")

# ── HTML ────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>chaos-jungle</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:#f8fafc; --surface:#ffffff; --card:#f1f5f9; --border:#e2e8f0; --border2:#cbd5e1;
  --hover-bg:rgba(0,0,0,.03); --th-bg:rgba(0,0,0,.04);
  --green:#16a34a; --green-bg:rgba(22,163,74,.12);
  --red:#dc2626;   --red-bg:rgba(220,38,38,.1);
  --yellow:#d97706; --yellow-bg:rgba(217,119,6,.1);
  --blue:#2563eb;  --blue-bg:rgba(37,99,235,.1);
  --purple:#7c3aed; --cyan:#0891b2; --orange:#ea580c;
  --text:#0f172a; --text2:#475569; --text3:#94a3b8;
  --radius:10px; --radius-sm:6px;
  --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
}
[data-theme="dark"] {
  --bg:#090b10; --surface:#0f1219; --card:#141820; --border:#1e2434; --border2:#252d40;
  --hover-bg:rgba(255,255,255,.025); --th-bg:rgba(0,0,0,.2);
  --green:#22c55e; --green-bg:rgba(34,197,94,.1);
  --red:#f43f5e;   --red-bg:rgba(244,63,94,.1);
  --yellow:#f59e0b; --yellow-bg:rgba(245,158,11,.1);
  --blue:#3b82f6;  --blue-bg:rgba(59,130,246,.1);
  --purple:#a855f7; --cyan:#06b6d4; --orange:#fb923c;
  --text:#e2e8f0; --text2:#94a3b8; --text3:#4b5870;
  --shadow:none;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;font-size:13px;line-height:1.5;min-height:100vh}
a{color:inherit;text-decoration:none}

/* Layout */
.layout{display:grid;grid-template-rows:52px 1fr;height:100vh;overflow:hidden}
header{background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:0;padding:0 20px;z-index:10}
.logo{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700;letter-spacing:.3px;margin-right:24px;flex-shrink:0}
.logo-icon{font-size:18px}
.logo-sub{color:var(--text3);font-size:10px;font-weight:500;background:var(--card);border:1px solid var(--border);padding:1px 6px;border-radius:4px;margin-left:2px}
nav{display:flex;height:100%;gap:1px}
.nav-btn{height:100%;padding:0 14px;font-size:12px;font-weight:500;color:var(--text2);cursor:pointer;border:none;background:transparent;border-bottom:2px solid transparent;transition:all .15s;white-space:nowrap;font-family:'Inter',sans-serif;letter-spacing:.3px}
.nav-btn:hover{color:var(--text)}
.nav-btn.active{color:var(--blue);border-bottom-color:var(--blue)}
.header-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.refresh-dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.ts-label{color:var(--text3);font-size:11px;font-family:'JetBrains Mono',monospace}
.theme-btn{width:30px;height:30px;border-radius:8px;border:1px solid var(--border);background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;transition:all .15s;flex-shrink:0}
.theme-btn:hover{border-color:var(--border2);background:var(--surface)}
.main{overflow-y:auto;padding:20px}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* KPI */
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;position:relative;overflow:hidden}
.kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent,var(--blue))}
.kpi.green{--accent:var(--green)} .kpi.red{--accent:var(--red)} .kpi.yellow{--accent:var(--yellow)}
.kpi.purple{--accent:var(--purple)} .kpi.orange{--accent:var(--orange)} .kpi.cyan{--accent:var(--cyan)}
.kpi .val{font-size:28px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--accent,var(--blue));line-height:1.2}
.kpi .lbl{color:var(--text2);font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:.6px;margin-top:5px}
.kpi .sub{color:var(--text3);font-size:10px;margin-top:3px}

/* KPI section divider */
.kpi-section{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.7px;color:var(--text3);margin-bottom:8px;margin-top:4px}

/* Charts */
.charts-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
.charts-row.three{grid-template-columns:1fr 1fr 1fr}
.charts-row.four{grid-template-columns:1fr 1fr 1fr 1fr}
@media(max-width:960px){.charts-row,.charts-row.three,.charts-row.four{grid-template-columns:1fr}}

/* Panel */
.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:14px;box-shadow:var(--shadow)}
.panel-head{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:8px}
.panel-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--text2)}
.panel-body{padding:14px}
.chart-wrap{padding:14px;height:220px;position:relative}

/* Section header */
.section-head{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--text3);margin:20px 0 10px}

/* Table */
.tbl{width:100%;border-collapse:collapse}
.tbl th{text-align:left;padding:9px 14px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text3);border-bottom:1px solid var(--border);background:var(--th-bg)}
.tbl td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
.tbl tr:last-child td{border-bottom:none}
.tbl tr.row{cursor:pointer;transition:background .12s}
.tbl tr.row:hover{background:var(--hover-bg)}

/* Badges */
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600}
.badge.running{background:var(--green-bg);color:var(--green)}
.badge.reverted{background:var(--blue-bg);color:var(--blue)}
.badge.failed{background:var(--red-bg);color:var(--red)}
.badge::before{content:'';width:5px;height:5px;border-radius:50%;background:currentColor}

/* Fault chip — category-colored via inline style */
.chip{display:inline-block;padding:1px 7px;border-radius:4px;font-size:10px;font-weight:600;margin:1px;white-space:nowrap;border:1px solid transparent}

/* Tag badges for LLM calls */
.tag-blocked{background:var(--red-bg);color:var(--red);padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600}
.tag-modified{background:var(--yellow-bg);color:var(--yellow);padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600}
.tag-clean{background:var(--green-bg);color:var(--green);padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600}
.tag-streaming{background:var(--blue-bg);color:var(--blue);padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600}

/* Filter bar */
.filter-bar{display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap}
.filter-bar input,.filter-bar select{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:var(--radius-sm);padding:5px 9px;font-size:12px;font-family:'Inter',sans-serif;outline:none}
.filter-bar input{flex:1;min-width:180px}
.filter-bar input:focus,.filter-bar select:focus{border-color:var(--blue)}

/* LLM call row */
.call-row td{font-size:11px}
.call-row.expanded{background:var(--hover-bg)}
.call-expand{display:none;background:var(--surface)}
.call-expand.open{display:table-row}
.call-expand td{padding:0;border-bottom:1px solid var(--border)}
.call-expand-inner{padding:14px 16px;display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:800px){.call-expand-inner{grid-template-columns:1fr}}
.call-box{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden}
.call-box-head{padding:7px 12px;border-bottom:1px solid var(--border);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text3);display:flex;justify-content:space-between}
.call-box-body{padding:10px 12px;max-height:200px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text2);white-space:pre-wrap;word-break:break-all;line-height:1.7}
.call-meta-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:6px;padding:10px 12px;border-bottom:1px solid var(--border)}
.call-meta-item{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:7px 10px}
.call-meta-val{font-size:13px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--text)}
.call-meta-key{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}

/* LLM summary bar */
.llm-summary{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px;margin-bottom:14px}
.llm-stat{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px;text-align:center}
.llm-stat-val{font-size:18px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--blue)}
.llm-stat-key{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}

/* Tool grid */
.tool-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
.tool-card{display:flex;align-items:center;gap:12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 14px}
.tool-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.tool-dot.ok{background:var(--green)} .tool-dot.missing{background:var(--red)}
.tool-info{flex:1;min-width:0}
.tool-name{font-weight:600;font-size:12px}
.tool-path{color:var(--text3);font-size:10px;font-family:'JetBrains Mono',monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool-pkg{background:var(--card);border:1px solid var(--border);border-radius:4px;padding:1px 7px;font-size:10px;color:var(--text2);flex-shrink:0}

/* Small action button */
.btn-sm{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--blue);font-size:11px;font-weight:500;padding:3px 9px;cursor:pointer;font-family:'Inter',sans-serif;transition:all .12s;white-space:nowrap}
.btn-sm:hover{background:var(--blue-bg);border-color:var(--blue)}

/* Log viewer */
.log-pre{font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.8;padding:12px 14px;max-height:420px;overflow-y:auto;background:var(--surface);white-space:pre-wrap;word-break:break-all}

/* Overlay + Drawer */
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(2px);z-index:100}
#overlay.on{display:block}
#drawer{position:fixed;top:0;right:-680px;width:640px;height:100vh;background:var(--card);border-left:1px solid var(--border);overflow:hidden;transition:right .28s cubic-bezier(.4,0,.2,1);z-index:101;display:flex;flex-direction:column}
#drawer.open{right:0}
.drawer-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}
.drawer-title{font-size:15px;font-weight:700;flex:1}
.drawer-close{width:26px;height:26px;border-radius:6px;background:var(--surface);border:1px solid var(--border);cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--text2);font-size:13px}
.drawer-close:hover{color:var(--text);border-color:var(--border2)}
.drawer-tabs{display:flex;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--surface);overflow-x:auto}
.dtab{padding:9px 14px;font-size:11px;font-weight:500;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;transition:all .12s;white-space:nowrap}
.dtab:hover{color:var(--text)}
.dtab.active{color:var(--blue);border-bottom-color:var(--blue)}
.drawer-body{overflow-y:auto;flex:1}
.dsec{padding:14px 18px;border-bottom:1px solid var(--border)}
.dsec:last-child{border-bottom:none}
.dsec-title{font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.7px;color:var(--text3);margin-bottom:10px}

/* Meta grid in drawer */
.meta-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.meta-item{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:9px 11px}
.meta-val{font-size:13px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--text)}
.meta-key{font-size:9px;color:var(--text3);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}

/* Metric compare table */
.metric-tbl{width:100%;border-collapse:collapse}
.metric-tbl th{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;padding:5px 7px;border-bottom:1px solid var(--border);text-align:left}
.metric-tbl td{padding:6px 7px;border-bottom:1px solid var(--border);vertical-align:middle}
.metric-tbl tr:last-child td{border-bottom:none}
.metric-name{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text2)}
.metric-val{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;text-align:right}
.delta-bar-wrap{position:relative;height:4px;background:var(--border);border-radius:2px;width:70px;overflow:hidden}
.delta-bar{position:absolute;top:0;left:0;height:100%;border-radius:2px;min-width:2px}
.delta-val{font-size:10px;font-weight:600;font-family:'JetBrains Mono',monospace;white-space:nowrap}
.delta-up{color:var(--red)} .delta-down{color:var(--green)} .delta-neu{color:var(--text3)}

/* Timeline */
.timeline{padding:2px 0}
.tl-item{display:flex;gap:10px;padding:6px 0;position:relative}
.tl-item::before{content:'';position:absolute;left:11px;top:26px;bottom:-6px;width:1px;background:var(--border)}
.tl-item:last-child::before{display:none}
.tl-dot{width:22px;height:22px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:9px;margin-top:1px}
.tl-dot.info{background:var(--blue-bg);color:var(--blue)} .tl-dot.ok{background:var(--green-bg);color:var(--green)}
.tl-dot.err{background:var(--red-bg);color:var(--red)} .tl-dot.warn{background:var(--yellow-bg);color:var(--yellow)}
.tl-content{flex:1;min-width:0}
.tl-ts{font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace}
.tl-msg{font-size:11px;color:var(--text2);line-height:1.5;word-break:break-word}
.tl-msg.err{color:var(--red)}

/* Fault block in drawer */
.fault-block{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:8px;overflow:hidden}
.fault-head{padding:9px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
.fault-kind{font-size:12px;font-weight:700;color:var(--purple)}
.fault-params{padding:9px 12px}
.fault-params pre{font-size:11px;color:var(--text2);line-height:1.7;white-space:pre-wrap;font-family:'JetBrains Mono',monospace}

/* Empty state */
.empty-state{padding:40px;text-align:center;color:var(--text3)}
.empty-icon{font-size:32px;margin-bottom:10px}
.empty-msg{font-size:12px}

/* Misc */
.divider{height:1px;background:var(--border);margin:14px 0}
.mono{font-family:'JetBrains Mono',monospace}
.muted{color:var(--text2)}
.small{font-size:11px}
.expand-btn{cursor:pointer;color:var(--text3);transition:color .12s;user-select:none;font-size:12px;width:20px;display:inline-block;text-align:center}
.expand-btn:hover{color:var(--text)}
</style>
</head>
<body>
<div class="layout">

<!-- Header -->
<header>
  <div class="logo">
    <span class="logo-icon">🌿</span>
    chaos-jungle
    <span class="logo-sub">v1.0</span>
  </div>
  <nav>
    <button class="nav-btn active" onclick="switchTab('overview',this)">Overview</button>
    <button class="nav-btn" onclick="switchTab('experiments',this)">Experiments</button>
    <button class="nav-btn" onclick="switchTab('runs',this)">Runs</button>
    <button class="nav-btn" onclick="switchTab('llm',this)">LLM Calls</button>
    <button class="nav-btn" onclick="switchTab('monitoring',this)">Monitoring</button>
    <button class="nav-btn" onclick="switchTab('tools',this)">System</button>
    <button class="nav-btn" onclick="switchTab('logs',this)">Logs</button>
  </nav>
  <div class="header-right">
    <div class="refresh-dot"></div>
    <span class="ts-label" id="ts">loading…</span>
    <button class="theme-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle theme">🌙</button>
  </div>
</header>

<div class="main">

<!-- ═══ OVERVIEW ═══ -->
<div class="tab-panel active" id="tab-overview">
  <div class="kpi-section">Fault injection</div>
  <div class="kpi-row" id="kpi-faults"></div>
  <div class="kpi-section" id="kpi-llm-label" style="display:none">LLM telemetry</div>
  <div class="kpi-row" id="kpi-llm" style="display:none"></div>
  <div class="charts-row">
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Fault type distribution</span></div>
      <div class="chart-wrap"><canvas id="chart-faults"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Session status</span></div>
      <div class="chart-wrap"><canvas id="chart-status"></canvas></div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-head">
      <span class="panel-title">Recent experiments</span>
      <span class="small muted" id="overview-count"></span>
    </div>
    <table class="tbl" id="overview-sessions"></table>
  </div>
</div>

<!-- ═══ EXPERIMENTS (grouped) ═══ -->
<div class="tab-panel" id="tab-experiments">
  <div class="panel">
    <div class="filter-bar">
      <input type="search" id="expg-search" placeholder="Filter experiments…" oninput="filterExperimentsGrouped()"/>
    </div>
    <table class="tbl">
      <thead><tr>
        <th>Experiment</th>
        <th style="width:60px;text-align:center">Runs</th>
        <th style="width:90px;text-align:center">Pass rate</th>
        <th style="width:90px;text-align:right">Avg duration</th>
        <th>Fault types</th>
        <th style="width:140px">Last run</th>
        <th style="width:80px"></th>
      </tr></thead>
      <tbody id="expg-body"></tbody>
    </table>
  </div>
</div>

<!-- ═══ RUNS ═══ -->
<div class="tab-panel" id="tab-runs">
  <div class="panel">
    <div class="filter-bar">
      <input type="search" id="exp-search" placeholder="Filter by name, fault, target…" oninput="filterRuns()"/>
      <select id="exp-experiment" onchange="filterRuns()">
        <option value="">All experiments</option>
      </select>
      <select id="exp-status" onchange="filterRuns()">
        <option value="">All statuses</option>
        <option value="running">Running</option>
        <option value="reverted">Reverted</option>
        <option value="failed">Failed</option>
      </select>
      <select id="exp-category" onchange="filterRuns()">
        <option value="">All fault types</option>
        <option value="network">Network</option>
        <option value="resource">Resource</option>
        <option value="process">Process</option>
        <option value="storage">Storage</option>
        <option value="state">State</option>
        <option value="llm">LLM / MCP</option>
        <option value="skill">Skill</option>
        <option value="semantic">Semantic</option>
        <option value="gpu">GPU</option>
      </select>
    </div>
    <table class="tbl">
      <thead><tr>
        <th style="width:48px">ID</th>
        <th>Run / Scenario</th>
        <th>Status</th>
        <th>Faults</th>
        <th>Target</th>
        <th>Started</th>
        <th style="width:80px">Duration</th>
      </tr></thead>
      <tbody id="exp-body"></tbody>
    </table>
  </div>
</div>

<!-- ═══ LLM CALLS ═══ -->
<div class="tab-panel" id="tab-llm">
  <div id="llm-summary" class="llm-summary"></div>
  <div class="panel">
    <div class="filter-bar">
      <input type="search" id="llm-search" placeholder="Filter by session, model, fault…" oninput="filterLLM()"/>
      <select id="llm-model-filter" onchange="filterLLM()">
        <option value="">All models</option>
      </select>
      <select id="llm-fault-filter" onchange="filterLLM()">
        <option value="">All faults</option>
      </select>
      <select id="llm-phase-filter" onchange="filterLLM()">
        <option value="">All phases</option>
        <option value="fault">Fault</option>
        <option value="baseline">Baseline</option>
      </select>
      <select id="llm-status-filter" onchange="filterLLM()">
        <option value="">All calls</option>
        <option value="blocked">Blocked only</option>
        <option value="modified">Modified only</option>
        <option value="streaming">Streaming only</option>
      </select>
    </div>
    <table class="tbl" id="llm-table">
      <thead><tr>
        <th style="width:28px"></th>
        <th style="width:32px">#</th>
        <th>Session</th>
        <th>Model</th>
        <th style="text-align:right">In</th>
        <th style="text-align:right">Out</th>
        <th style="text-align:right">Tok/s</th>
        <th style="text-align:right">Cost</th>
        <th style="text-align:right">Latency</th>
        <th style="text-align:right">TTFT</th>
        <th>Fault</th>
        <th>Status</th>
      </tr></thead>
      <tbody id="llm-body"></tbody>
    </table>
  </div>
</div>

<!-- ═══ MONITORING ═══ -->
<div class="tab-panel" id="tab-monitoring">
  <div class="section-head">Fault Injection Overview</div>
  <div class="charts-row">
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Experiments over time</span></div>
      <div class="chart-wrap"><canvas id="chart-exp-timeline"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Fault category breakdown</span></div>
      <div class="chart-wrap"><canvas id="chart-cat-donut"></canvas></div>
    </div>
  </div>
  <div class="section-head" id="mon-llm-section" style="display:none">LLM Telemetry</div>
  <div class="charts-row" id="mon-llm-charts" style="display:none">
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Latency per call</span></div>
      <div class="chart-wrap"><canvas id="chart-latency"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Token usage per call</span></div>
      <div class="chart-wrap"><canvas id="chart-tokens"></canvas></div>
    </div>
  </div>
  <div class="charts-row" id="mon-llm-charts2" style="display:none">
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Cumulative cost ($)</span></div>
      <div class="chart-wrap"><canvas id="chart-cost"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-head"><span class="panel-title">Call outcome breakdown</span></div>
      <div class="chart-wrap"><canvas id="chart-outcome"></canvas></div>
    </div>
  </div>
</div>

<!-- ═══ SYSTEM ═══ -->
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

<!-- ═══ LOGS ═══ -->
<div class="tab-panel" id="tab-logs">
  <div class="panel">
    <div class="panel-head">
      <span class="panel-title">Log viewer</span>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="log-select" style="background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:var(--radius-sm);padding:4px 9px;font-size:12px;font-family:'Inter',sans-serif"></select>
        <span class="small muted" id="log-lines"></span>
      </div>
    </div>
    <pre class="log-pre" id="log-content">Select a log file above.</pre>
  </div>
</div>

</div><!-- .main -->
</div><!-- .layout -->

<!-- Overlay + Drawer -->
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
// ─── Theme ──────────────────────────────────────────────────
function isDark(){ return document.documentElement.getAttribute('data-theme')==='dark' }
function setTheme(t,save=true){
  document.documentElement.setAttribute('data-theme',t);
  document.getElementById('theme-btn').textContent = t==='dark' ? '☀️' : '🌙';
  if(save) localStorage.setItem('cj-theme',t);
}
function toggleTheme(){ setTheme(isDark()?'light':'dark') }
function initTheme(){ setTheme(localStorage.getItem('cj-theme')||'light',false) }

function chartColors(){
  return isDark()
    ? {grid:'#1e2434',tick:'#4b5870',legend:'#94a3b8'}
    : {grid:'#e2e8f0',tick:'#94a3b8',legend:'#475569'};
}

// ─── State ──────────────────────────────────────────────────
let _sessions=[], _llmCalls=[], _tools=[];
let _chartFaults=null, _chartStatus=null;
let _chartExpTimeline=null, _chartCatDonut=null;
let _chartLatency=null, _chartTokens=null, _chartCost=null, _chartOutcome=null;
let _drawerTabs={}, _activeDTab='summary';

// ─── Fault category color map (mirrors server) ───────────────
const CAT = {
  network: {color:'#2563eb',bg:'rgba(37,99,235,.15)'},
  resource:{color:'#ea580c',bg:'rgba(234,88,12,.15)'},
  process: {color:'#dc2626',bg:'rgba(220,38,38,.15)'},
  storage: {color:'#d97706',bg:'rgba(217,119,6,.15)'},
  state:   {color:'#0891b2',bg:'rgba(8,145,178,.15)'},
  llm:     {color:'#7c3aed',bg:'rgba(124,58,237,.15)'},
  skill:   {color:'#4f46e5',bg:'rgba(79,70,229,.15)'},
  semantic:{color:'#db2777',bg:'rgba(219,39,119,.15)'},
  gpu:     {color:'#16a34a',bg:'rgba(22,163,74,.15)'},
  other:   {color:'#64748b',bg:'rgba(100,116,139,.15)'},
};

// ─── Tab navigation ─────────────────────────────────────────
function switchTab(id,btn){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  btn.classList.add('active');
  if(id==='logs') loadLogs();
  if(id==='monitoring') renderMonitoringCharts();
}

// ─── Main load ──────────────────────────────────────────────
async function load(){
  try {
    const [sessions, tools, llmCalls] = await Promise.all([
      fetch('/api/sessions').then(r=>r.json()),
      fetch('/api/system').then(r=>r.json()),
      fetch('/api/llm_calls').then(r=>r.json()),
    ]);
    _sessions  = sessions;
    _tools     = tools;
    _llmCalls  = llmCalls;
    renderKPI(sessions, llmCalls);
    renderOverviewCharts(sessions);
    renderOverviewTable(sessions);
    renderExperimentsGrouped(sessions);
    renderRunsTable(sessions);
    populateRunsFilters(sessions);
    renderTools(tools);
    renderLLMSummary(llmCalls);
    renderLLMTable(llmCalls);
    populateLLMFilters(llmCalls);
    document.getElementById('ts').textContent = 'Updated '+new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('ts').textContent = 'Error loading data';
    console.error(e);
  }
}

// ─── KPI ────────────────────────────────────────────────────
function renderKPI(sessions, llmCalls){
  const total   = sessions.length;
  const running = sessions.filter(s=>s.status==='running').length;
  const reverted= sessions.filter(s=>s.status==='reverted').length;
  const failed  = sessions.filter(s=>s.status==='failed').length;
  const durs    = sessions.filter(s=>s.duration_s);
  const avgDur  = durs.length ? (durs.reduce((a,s)=>a+s.duration_s,0)/durs.length).toFixed(1) : '—';

  document.getElementById('kpi-faults').innerHTML = `
    <div class="kpi blue"><div class="val">${total}</div><div class="lbl">Experiments</div><div class="sub">all time</div></div>
    <div class="kpi ${running?'green':''}"><div class="val">${running}</div><div class="lbl">Running</div><div class="sub">${running?'faults active':'idle'}</div></div>
    <div class="kpi green"><div class="val">${reverted}</div><div class="lbl">Reverted</div><div class="sub">clean teardown</div></div>
    <div class="kpi ${failed?'red':''}"><div class="val">${failed}</div><div class="lbl">Failed</div><div class="sub">no revert</div></div>
    <div class="kpi yellow"><div class="val">${avgDur}${avgDur!=='—'?'s':''}</div><div class="lbl">Avg duration</div><div class="sub">per session</div></div>
  `;

  if(!llmCalls.length) return;
  document.getElementById('kpi-llm-label').style.display='block';
  document.getElementById('kpi-llm').style.display='grid';

  const totalCalls  = llmCalls.length;
  const blockedN    = llmCalls.filter(c=>c.was_blocked).length;
  const blockedPct  = totalCalls ? Math.round(blockedN/totalCalls*100) : 0;
  const totalCost   = llmCalls.reduce((a,c)=>a+(c.cost_usd||0),0);
  const lats        = llmCalls.map(c=>c.latency_s).filter(Boolean);
  const avgLat      = lats.length ? (lats.reduce((a,v)=>a+v,0)/lats.length).toFixed(2) : '—';
  const totalTok    = llmCalls.reduce((a,c)=>a+(c.prompt_tokens||0)+(c.completion_tokens||0),0);
  const ttfts       = llmCalls.map(c=>c.ttft_s).filter(v=>v!=null&&v>0);
  const avgTTFT     = ttfts.length ? (ttfts.reduce((a,v)=>a+v,0)/ttfts.length).toFixed(3) : '—';

  document.getElementById('kpi-llm').innerHTML = `
    <div class="kpi purple"><div class="val">${totalCalls}</div><div class="lbl">LLM calls</div><div class="sub">captured by proxy</div></div>
    <div class="kpi ${blockedPct>20?'red':'green'}"><div class="val">${blockedPct}%</div><div class="lbl">Blocked rate</div><div class="sub">${blockedN} of ${totalCalls}</div></div>
    <div class="kpi cyan"><div class="val">${avgLat}${avgLat!=='—'?'s':''}</div><div class="lbl">Avg latency</div><div class="sub">per call</div></div>
    <div class="kpi orange"><div class="val">${avgTTFT}${avgTTFT!=='—'?'s':''}</div><div class="lbl">Avg TTFT</div><div class="sub">streaming first token</div></div>
    <div class="kpi yellow"><div class="val">$${totalCost.toFixed(4)}</div><div class="lbl">Total cost</div><div class="sub">auto-priced</div></div>
    <div class="kpi blue"><div class="val">${fmtNum(totalTok)}</div><div class="lbl">Total tokens</div><div class="sub">in + out</div></div>
  `;
}

// ─── Overview charts ────────────────────────────────────────
function renderOverviewCharts(sessions){
  const c = chartColors();
  const ao = { ticks:{color:c.tick,font:{size:10,family:'Inter'}}, grid:{color:c.grid} };

  // Fault distribution (by category)
  const catMap={};
  sessions.forEach(s=>(s.faults||[]).forEach(f=>{
    const cat=f.category||'other';
    catMap[cat]=(catMap[cat]||0)+1;
  }));
  const catLabels=Object.keys(catMap);
  const catColors=catLabels.map(k=>(CAT[k]||CAT.other).color);

  if(_chartFaults) _chartFaults.destroy();
  _chartFaults=new Chart(document.getElementById('chart-faults'),{
    type:'bar',
    data:{
      labels: catLabels.length ? catLabels : ['No data'],
      datasets:[{label:'Sessions',data:catLabels.length?Object.values(catMap):[0],
        backgroundColor:catColors,borderRadius:4,borderSkipped:false}]
    },
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:ao,y:{...ao,ticks:{...ao.ticks,stepSize:1}}}}
  });

  // Session status donut
  const st={running:0,reverted:0,failed:0};
  sessions.forEach(s=>{if(st[s.status]!==undefined)st[s.status]++});
  if(_chartStatus) _chartStatus.destroy();
  _chartStatus=new Chart(document.getElementById('chart-status'),{
    type:'doughnut',
    data:{
      labels:['Reverted','Running','Failed'],
      datasets:[{data:[st.reverted,st.running,st.failed],
        backgroundColor:['#16a34a','#2563eb','#dc2626'],borderWidth:0,hoverOffset:4}]
    },
    options:{responsive:true,maintainAspectRatio:false,cutout:'72%',
      plugins:{legend:{position:'right',labels:{color:c.legend,font:{family:'Inter',size:11},boxWidth:10,padding:12}}}}
  });
}

// ─── Monitoring charts ───────────────────────────────────────
function renderMonitoringCharts(){
  const c = chartColors();
  const ao = { ticks:{color:c.tick,font:{size:10,family:'Inter'}}, grid:{color:c.grid} };

  // Experiments over time (session count by day)
  const dayMap={};
  _sessions.forEach(s=>{
    const d=(s.started_at||'').slice(0,10);
    if(d) dayMap[d]=(dayMap[d]||0)+1;
  });
  const days=Object.keys(dayMap).sort();
  if(_chartExpTimeline) _chartExpTimeline.destroy();
  _chartExpTimeline=new Chart(document.getElementById('chart-exp-timeline'),{
    type:'bar',
    data:{labels:days.length?days:['No data'],
      datasets:[{label:'Experiments',data:days.length?days.map(d=>dayMap[d]):[0],
        backgroundColor:'rgba(37,99,235,.6)',borderRadius:3}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},scales:{x:ao,y:{...ao,ticks:{...ao.ticks,stepSize:1}}}}
  });

  // Fault category donut
  const catMap={};
  _sessions.forEach(s=>(s.faults||[]).forEach(f=>{
    const cat=f.category||'other';
    catMap[cat]=(catMap[cat]||0)+1;
  }));
  const catL=Object.keys(catMap);
  const catC=catL.map(k=>(CAT[k]||CAT.other).color);
  if(_chartCatDonut) _chartCatDonut.destroy();
  _chartCatDonut=new Chart(document.getElementById('chart-cat-donut'),{
    type:'doughnut',
    data:{labels:catL.length?catL:['No data'],
      datasets:[{data:catL.length?catL.map(k=>catMap[k]):[1],
        backgroundColor:catL.length?catC:['#64748b'],borderWidth:0,hoverOffset:4}]},
    options:{responsive:true,maintainAspectRatio:false,cutout:'68%',
      plugins:{legend:{position:'right',labels:{color:c.legend,font:{family:'Inter',size:11},boxWidth:10,padding:12}}}}
  });

  if(!_llmCalls.length) return;
  document.getElementById('mon-llm-section').style.display='block';
  document.getElementById('mon-llm-charts').style.display='grid';
  document.getElementById('mon-llm-charts2').style.display='grid';

  const calls=_llmCalls.slice().sort((a,b)=>a.id-b.id);
  const labels=calls.map((_,i)=>i+1);

  // Latency per call (colored by blocked/modified/clean)
  const latColors=calls.map(c=>c.was_blocked?'#f43f5e':c.was_modified?'#f59e0b':'#22c55e');
  if(_chartLatency) _chartLatency.destroy();
  _chartLatency=new Chart(document.getElementById('chart-latency'),{
    type:'bar',
    data:{labels,datasets:[{label:'Latency (s)',data:calls.map(c=>c.latency_s||0),
      backgroundColor:latColors,borderRadius:2}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{...ao,ticks:{...ao.ticks,maxTicksLimit:10}},y:{...ao,title:{display:true,text:'seconds',color:c.tick,font:{size:10}}}}}
  });

  // Token usage per call (stacked prompt + completion)
  if(_chartTokens) _chartTokens.destroy();
  _chartTokens=new Chart(document.getElementById('chart-tokens'),{
    type:'bar',
    data:{labels,datasets:[
      {label:'Prompt',data:calls.map(c=>c.prompt_tokens||0),backgroundColor:'rgba(37,99,235,.6)',stack:'t'},
      {label:'Completion',data:calls.map(c=>c.completion_tokens||0),backgroundColor:'rgba(124,58,237,.6)',stack:'t'},
    ]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:'top',labels:{color:c.legend,font:{family:'Inter',size:10},boxWidth:10,padding:10}}},
      scales:{x:{...ao,ticks:{...ao.ticks,maxTicksLimit:10}},y:ao}}
  });

  // Cumulative cost (line)
  let cumCost=0;
  const costData=calls.map(c=>{cumCost+=c.cost_usd||0;return +cumCost.toFixed(6);});
  if(_chartCost) _chartCost.destroy();
  _chartCost=new Chart(document.getElementById('chart-cost'),{
    type:'line',
    data:{labels,datasets:[{label:'Cumulative cost',data:costData,
      borderColor:'#f59e0b',backgroundColor:'rgba(245,158,11,.1)',fill:true,
      borderWidth:2,pointRadius:0,tension:.3}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{...ao,ticks:{...ao.ticks,maxTicksLimit:10}},y:{...ao,title:{display:true,text:'USD',color:c.tick,font:{size:10}}}}}
  });

  // Outcome donut (blocked / modified / clean)
  const blocked =calls.filter(c=>c.was_blocked).length;
  const modified=calls.filter(c=>!c.was_blocked&&c.was_modified).length;
  const clean   =calls.filter(c=>!c.was_blocked&&!c.was_modified).length;
  if(_chartOutcome) _chartOutcome.destroy();
  _chartOutcome=new Chart(document.getElementById('chart-outcome'),{
    type:'doughnut',
    data:{labels:['Clean','Modified','Blocked'],
      datasets:[{data:[clean,modified,blocked],
        backgroundColor:['#22c55e','#f59e0b','#f43f5e'],borderWidth:0,hoverOffset:4}]},
    options:{responsive:true,maintainAspectRatio:false,cutout:'68%',
      plugins:{legend:{position:'right',labels:{color:c.legend,font:{family:'Inter',size:11},boxWidth:10,padding:12}}}}
  });
}

// ─── Experiment table ────────────────────────────────────────
function chipFor(f){
  const cat=f.category||'other';
  const st=CAT[cat]||CAT.other;
  return `<span class="chip" style="color:${st.color};background:${st.bg};border-color:${st.color}33">${esc(f.kind)}</span>`;
}

function targetBadge(s){
  const t=s.target_type||'';
  const a=s.target_addr||'';
  if(!t) return '';
  const colors={local:'var(--text3)',http:'var(--blue)',ssh:'var(--cyan)'};
  const col=colors[t]||'var(--text3)';
  const label=a?`${t}:${a}`:t;
  return `<span style="font-size:9px;font-family:'JetBrains Mono',monospace;color:${col};background:var(--card);border:1px solid var(--border);border-radius:3px;padding:1px 5px;margin-left:4px;vertical-align:middle" title="${esc(a)}">${esc(label.length>28?label.slice(0,26)+'…':label)}</span>`;
}

function sessionRow(s){
  const dur    = s.duration_s!=null ? s.duration_s+'s' : (s.status==='running'?'⏱ live':'—');
  const started= (s.started_at||'').replace('T',' ').slice(0,19)||'—';
  const faults = (s.faults||[]).map(chipFor).join('')||'<span class="muted small">—</span>';
  const ttype  = s.target_type||'';
  const taddr  = s.target_addr||'';
  const targetCell = ttype
    ? `<span style="font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--text2)">${esc(ttype)}</span>
       <span style="font-size:10px;color:var(--text3);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px" title="${esc(taddr)}">${esc(taddr)}</span>`
    : '<span class="muted small">local</span>';
  return `<tr class="row" onclick="openSession(${s.id})">
    <td class="mono small muted">#${s.id}</td>
    <td style="font-weight:600;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(s.name)}">${esc(s.name)}</td>
    <td><span class="badge ${s.status}">${s.status}</span></td>
    <td>${faults}</td>
    <td style="max-width:180px">${targetCell}</td>
    <td class="small muted mono">${started}</td>
    <td class="small mono">${dur}</td>
  </tr>`;
}

function renderOverviewTable(sessions){
  const recent=sessions.slice(0,8);
  document.getElementById('overview-count').textContent=sessions.length+' total';
  document.getElementById('overview-sessions').innerHTML = recent.length
    ? `<thead><tr><th style="width:48px">ID</th><th>Scenario</th><th>Status</th><th>Faults</th><th>Started</th><th>Duration</th></tr></thead><tbody>${recent.map(overviewRow).join('')}</tbody>`
    : `<tbody><tr><td colspan="6"><div class="empty-state"><div class="empty-icon">🌿</div><div class="empty-msg">No experiments yet.</div></div></td></tr></tbody>`;
}

function overviewRow(s){
  const dur    = s.duration_s!=null ? s.duration_s+'s' : (s.status==='running'?'⏱ live':'—');
  const started= (s.started_at||'').replace('T',' ').slice(0,19)||'—';
  const faults = (s.faults||[]).map(chipFor).join('')||'<span class="muted small">—</span>';
  return `<tr class="row" onclick="openSession(${s.id})">
    <td class="mono small muted">#${s.id}</td>
    <td style="font-weight:600">${esc(s.name)}</td>
    <td><span class="badge ${s.status}">${s.status}</span></td>
    <td>${faults}</td>
    <td class="small muted mono">${started}</td>
    <td class="small mono">${dur}</td>
  </tr>`;
}

// ─── Experiments (grouped by scenario name) ─────────────────
function renderExperimentsGrouped(sessions){
  const groups={};
  sessions.forEach(s=>{
    if(!groups[s.name]) groups[s.name]={runs:[],name:s.name};
    groups[s.name].runs.push(s);
  });
  const rows=Object.values(groups).sort((a,b)=>{
    const la=(a.runs[0]?.started_at||'');
    const lb=(b.runs[0]?.started_at||'');
    return lb.localeCompare(la);
  });
  document.getElementById('expg-body').innerHTML = rows.length
    ? rows.map(expGroupRow).join('')
    : `<tr><td colspan="7"><div class="empty-state"><div class="empty-icon">🌿</div><div class="empty-msg">No experiments yet.</div></div></td></tr>`;
}

function expGroupRow(g){
  const runs  =g.runs;
  const n     =runs.length;
  const passed=runs.filter(s=>s.status==='reverted').length;
  const failed=runs.filter(s=>s.status==='failed').length;
  const running=runs.filter(s=>s.status==='running').length;
  const passRate=n>0?Math.round(passed/n*100):0;
  const durs  =runs.filter(s=>s.duration_s!=null).map(s=>s.duration_s);
  const avgDur=durs.length?(durs.reduce((a,v)=>a+v,0)/durs.length).toFixed(1)+'s':'—';
  // Collect unique fault chips
  const seen=new Set();
  const chips=[];
  runs.forEach(s=>(s.faults||[]).forEach(f=>{
    const key=f.category+'|'+f.kind;
    if(!seen.has(key)){ seen.add(key); chips.push(chipFor(f)); }
  }));
  const last=(runs[0]?.started_at||'').replace('T',' ').slice(0,16)||'—';
  const passColor=passRate===100?'var(--green)':passRate===0&&n>0?'var(--red)':'var(--yellow)';
  const statusIcons=`<span style="color:var(--green);font-size:11px">${passed}✓</span>`+
    (failed?` <span style="color:var(--red);font-size:11px">${failed}✕</span>`:'')+
    (running?` <span style="color:var(--cyan);font-size:11px">${running}↻</span>`:'');
  return `<tr class="row" onclick="drillExperiment(${JSON.stringify(esc(g.name))})">
    <td style="font-weight:600;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(g.name)}">${esc(g.name)}</td>
    <td style="text-align:center;font-weight:600;font-family:'JetBrains Mono',monospace">${n}</td>
    <td style="text-align:center">
      <span style="font-weight:700;color:${passColor};font-family:'JetBrains Mono',monospace">${passRate}%</span>
      <div style="font-size:9px;color:var(--text3);margin-top:2px">${statusIcons}</div>
    </td>
    <td style="text-align:right;font-family:'JetBrains Mono',monospace;font-size:11px">${avgDur}</td>
    <td style="max-width:200px;overflow:hidden">${chips.slice(0,5).join(' ')}${chips.length>5?`<span class="muted small"> +${chips.length-5}</span>`:''}</td>
    <td class="small muted mono">${last}</td>
    <td><button class="btn-sm" onclick="event.stopPropagation();drillExperiment(${JSON.stringify(esc(g.name))})">Runs →</button></td>
  </tr>`;
}

function filterExperimentsGrouped(){
  const q=document.getElementById('expg-search').value.toLowerCase();
  const groups={};
  _sessions.forEach(s=>{
    if(!q||s.name.toLowerCase().includes(q)){
      if(!groups[s.name]) groups[s.name]={runs:[],name:s.name};
      groups[s.name].runs.push(s);
    }
  });
  const rows=Object.values(groups).sort((a,b)=>(b.runs[0]?.started_at||'').localeCompare(a.runs[0]?.started_at||''));
  document.getElementById('expg-body').innerHTML = rows.length
    ? rows.map(expGroupRow).join('')
    : `<tr><td colspan="7"><div class="empty-state"><div class="empty-icon">🔍</div><div class="empty-msg">No experiments match.</div></div></td></tr>`;
}

function drillExperiment(name){
  // Switch to Runs tab, pre-filter by this experiment
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-runs').classList.add('active');
  document.querySelectorAll('.nav-btn').forEach(b=>{ if(b.textContent==='Runs') b.classList.add('active'); });
  const sel=document.getElementById('exp-experiment');
  // find the matching option or add it
  let found=false;
  for(const opt of sel.options){ if(opt.value===name){sel.value=name;found=true;break;} }
  if(!found){ const o=document.createElement('option');o.value=name;o.textContent=name;sel.appendChild(o);sel.value=name; }
  filterRuns();
}

// ─── Runs table ──────────────────────────────────────────────
function populateRunsFilters(sessions){
  const expSel=document.getElementById('exp-experiment');
  const names=[...new Set(sessions.map(s=>s.name))].sort();
  // keep first option, add the rest
  while(expSel.options.length>1) expSel.remove(1);
  names.forEach(n=>{ const o=document.createElement('option');o.value=n;o.textContent=n;expSel.appendChild(o); });
}

function renderRunsTable(sessions){
  document.getElementById('exp-body').innerHTML = sessions.length
    ? sessions.map(sessionRow).join('')
    : `<tr><td colspan="7"><div class="empty-state"><div class="empty-icon">🌿</div><div class="empty-msg">No runs yet.</div></div></td></tr>`;
}

function filterRuns(){
  const q   =document.getElementById('exp-search').value.toLowerCase();
  const exp =document.getElementById('exp-experiment').value;
  const st  =document.getElementById('exp-status').value;
  const cat =document.getElementById('exp-category').value;
  const filtered=_sessions.filter(s=>{
    const matchQ  =!q||s.name.toLowerCase().includes(q)||(s.faults||[]).some(f=>f.kind.toLowerCase().includes(q))||String(s.id).includes(q)||(s.target_addr||'').toLowerCase().includes(q);
    const matchExp=!exp||s.name===exp;
    const matchSt =!st||s.status===st;
    const matchCat=!cat||(s.faults||[]).some(f=>(f.category||'other')===cat);
    return matchQ&&matchExp&&matchSt&&matchCat;
  });
  document.getElementById('exp-body').innerHTML = filtered.length
    ? filtered.map(sessionRow).join('')
    : `<tr><td colspan="7"><div class="empty-state"><div class="empty-icon">🔍</div><div class="empty-msg">No runs match.</div></div></td></tr>`;
}

// ─── LLM Calls tab ──────────────────────────────────────────
function populateLLMFilters(calls){
  const models=[ ...new Set(calls.map(c=>c.model).filter(Boolean))].sort();
  const faults=[ ...new Set(calls.map(c=>c.fault_name).filter(Boolean))].sort();
  const mSel=document.getElementById('llm-model-filter');
  const fSel=document.getElementById('llm-fault-filter');
  models.forEach(m=>{ const o=document.createElement('option');o.value=m;o.textContent=m;mSel.appendChild(o); });
  faults.forEach(f=>{ const o=document.createElement('option');o.value=f;o.textContent=f;fSel.appendChild(o); });
}

function renderLLMSummary(calls){
  if(!calls.length){
    document.getElementById('llm-summary').innerHTML='';
    return;
  }
  const n         =calls.length;
  const blocked   =calls.filter(c=>c.was_blocked).length;
  const modified  =calls.filter(c=>c.was_modified).length;
  const totalIn   =calls.reduce((a,c)=>a+(c.prompt_tokens||0),0);
  const totalOut  =calls.reduce((a,c)=>a+(c.completion_tokens||0),0);
  const totalCost =calls.reduce((a,c)=>a+(c.cost_usd||0),0);
  const lats      =calls.map(c=>c.latency_s).filter(Boolean);
  const avgLat    =lats.length?(lats.reduce((a,v)=>a+v,0)/lats.length).toFixed(2):'—';
  const sortedLat =[...lats].sort((a,b)=>a-b);
  const p99       =sortedLat.length?sortedLat[Math.min(Math.floor(sortedLat.length*.99),sortedLat.length-1)].toFixed(2):'—';
  const ttfts     =calls.map(c=>c.ttft_s).filter(v=>v!=null&&v>0);
  const avgTTFT   =ttfts.length?(ttfts.reduce((a,v)=>a+v,0)/ttfts.length).toFixed(3):'—';

  document.getElementById('llm-summary').innerHTML=`
    <div class="llm-stat"><div class="llm-stat-val">${n}</div><div class="llm-stat-key">Total calls</div></div>
    <div class="llm-stat"><div class="llm-stat-val" style="color:var(--red)">${blocked}</div><div class="llm-stat-key">Blocked</div></div>
    <div class="llm-stat"><div class="llm-stat-val" style="color:var(--yellow)">${modified}</div><div class="llm-stat-key">Modified</div></div>
    <div class="llm-stat"><div class="llm-stat-val">${fmtNum(totalIn)}</div><div class="llm-stat-key">Prompt tokens</div></div>
    <div class="llm-stat"><div class="llm-stat-val">${fmtNum(totalOut)}</div><div class="llm-stat-key">Completion tokens</div></div>
    <div class="llm-stat"><div class="llm-stat-val" style="color:var(--yellow)">$${totalCost.toFixed(4)}</div><div class="llm-stat-key">Total cost</div></div>
    <div class="llm-stat"><div class="llm-stat-val">${avgLat}${avgLat!=='—'?'s':''}</div><div class="llm-stat-key">Avg latency</div></div>
    <div class="llm-stat"><div class="llm-stat-val">${p99}${p99!=='—'?'s':''}</div><div class="llm-stat-key">p99 latency</div></div>
    <div class="llm-stat"><div class="llm-stat-val">${avgTTFT}${avgTTFT!=='—'?'s':''}</div><div class="llm-stat-key">Avg TTFT</div></div>
  `;
}

function renderLLMTable(calls){
  if(!calls.length){
    document.getElementById('llm-body').innerHTML=
      `<tr><td colspan="12"><div class="empty-state"><div class="empty-icon">📡</div><div class="empty-msg">No LLM calls captured yet.<br>Use an LLM proxy fault with ChaosRunner to start capturing.</div></div></td></tr>`;
    return;
  }
  document.getElementById('llm-body').innerHTML=calls.map(llmCallRow).join('');
}

function llmCallRow(c){
  const tps    =c.tokens_per_second>0 ? c.tokens_per_second.toFixed(1) : '—';
  const cost   =c.cost_usd>0 ? '$'+c.cost_usd.toFixed(6) : '$0';
  const lat    =c.latency_s!=null ? c.latency_s.toFixed(2)+'s' : '—';
  const ttft   =c.ttft_s!=null ? c.ttft_s.toFixed(3)+'s' : '—';
  const sesName=esc(c.session_name||('#'+c.session_id));
  const model  =esc(c.model||'—');
  let statusHtml='';
  if(c.was_blocked)  statusHtml='<span class="tag-blocked">blocked</span>';
  else if(c.was_modified) statusHtml='<span class="tag-modified">modified</span>';
  else statusHtml='<span class="tag-clean">clean</span>';
  if(c.is_streaming) statusHtml+=' <span class="tag-streaming">stream</span>';
  const finish =esc(c.finish_reason||String(c.http_status));
  const faultName=esc(c.fault_name||'—');
  return `
    <tr class="call-row row" id="cr-${c.id}" onclick="toggleCallRow(${c.id})">
      <td><span class="expand-btn" id="ex-${c.id}">▶</span></td>
      <td class="mono small muted">${c.call_index}</td>
      <td class="small">${sesName}</td>
      <td class="mono small">${model}</td>
      <td class="mono small muted" style="text-align:right">${c.prompt_tokens||0}</td>
      <td class="mono small muted" style="text-align:right">${c.completion_tokens||0}</td>
      <td class="mono small muted" style="text-align:right">${tps}</td>
      <td class="mono small" style="text-align:right;color:var(--yellow)">${cost}</td>
      <td class="mono small" style="text-align:right">${lat}</td>
      <td class="mono small muted" style="text-align:right">${ttft}</td>
      <td class="small mono muted">${faultName}</td>
      <td>${statusHtml} <span class="muted small">${finish}</span></td>
    </tr>
    <tr class="call-expand" id="ce-${c.id}">
      <td colspan="12">
        <div class="call-meta-grid">
          <div class="call-meta-item"><div class="call-meta-val">${c.message_count||0}</div><div class="call-meta-key">Messages</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.tool_count||0}</div><div class="call-meta-key">Tools</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.response_tool_calls||0}</div><div class="call-meta-key">Tool calls</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.temperature!=null?c.temperature:'—'}</div><div class="call-meta-key">Temperature</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.max_tokens_requested!=null?c.max_tokens_requested:'—'}</div><div class="call-meta-key">Max tokens</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.system_fingerprint||'—'}</div><div class="call-meta-key">Fingerprint</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.request_size_bytes||0}</div><div class="call-meta-key">Req bytes</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.response_size_bytes||0}</div><div class="call-meta-key">Resp bytes</div></div>
          <div class="call-meta-item"><div class="call-meta-val">${c.rate_limit_remaining_requests!=null?c.rate_limit_remaining_requests:'—'}</div><div class="call-meta-key">RL remaining req</div></div>
        </div>
        <div class="call-expand-inner">
          <div class="call-box">
            <div class="call-box-head"><span>Prompt (last user message)</span><span class="muted">${(c.prompt_text||'').length} chars</span></div>
            <div class="call-box-body">${esc(c.prompt_text||'(empty)')}</div>
          </div>
          <div class="call-box">
            <div class="call-box-head"><span>Response</span><span class="muted">${c.response_length_chars||0} chars</span></div>
            <div class="call-box-body">${esc(c.response_text||'(empty / blocked)')}</div>
          </div>
        </div>
      </td>
    </tr>`;
}

function toggleCallRow(id){
  const expRow=document.getElementById('ce-'+id);
  const btn   =document.getElementById('ex-'+id);
  const row   =document.getElementById('cr-'+id);
  const isOpen=expRow.classList.contains('open');
  expRow.classList.toggle('open',!isOpen);
  row.classList.toggle('expanded',!isOpen);
  btn.textContent=isOpen?'▶':'▼';
}

function filterLLM(){
  const q   =document.getElementById('llm-search').value.toLowerCase();
  const mdl =document.getElementById('llm-model-filter').value;
  const flt =document.getElementById('llm-fault-filter').value;
  const phs =document.getElementById('llm-phase-filter').value;
  const sts =document.getElementById('llm-status-filter').value;
  const filtered=_llmCalls.filter(c=>{
    if(q&&!( (c.session_name||'').toLowerCase().includes(q)||(c.model||'').toLowerCase().includes(q)||(c.fault_name||'').toLowerCase().includes(q) )) return false;
    if(mdl&&c.model!==mdl) return false;
    if(flt&&c.fault_name!==flt) return false;
    if(phs&&c.phase!==phs) return false;
    if(sts==='blocked'&&!c.was_blocked) return false;
    if(sts==='modified'&&!c.was_modified) return false;
    if(sts==='streaming'&&!c.is_streaming) return false;
    return true;
  });
  document.getElementById('llm-body').innerHTML=filtered.length
    ? filtered.map(llmCallRow).join('')
    : `<tr><td colspan="12"><div class="empty-state"><div class="empty-icon">🔍</div><div class="empty-msg">No calls match your filter.</div></div></td></tr>`;
  renderLLMSummary(filtered);
}

// ─── Tools ──────────────────────────────────────────────────
function renderTools(tools){
  const found=tools.filter(t=>t.found).length;
  document.getElementById('tool-summary').textContent=found+'/'+tools.length+' installed';
  document.getElementById('tools').innerHTML=tools.map(t=>`
    <div class="tool-card">
      <div class="tool-dot ${t.found?'ok':'missing'}"></div>
      <div class="tool-info">
        <div class="tool-name">${t.binary}</div>
        <div class="tool-path">${t.found?t.path:'not installed'}</div>
        <div class="tool-role muted" style="font-size:10px">${t.role}</div>
      </div>
      <div class="tool-pkg">${t.package}</div>
    </div>`).join('');
}

// ─── Session drawer ─────────────────────────────────────────
async function openSession(id){
  document.getElementById('drawer-title').textContent='Session #'+id;
  document.getElementById('drawer-body').innerHTML='<div class="empty-state"><div class="empty-icon">⏳</div><div class="empty-msg">Loading…</div></div>';
  document.getElementById('drawer-tabs').innerHTML='';
  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('on');

  const [d, ana, llmCalls] = await Promise.all([
    fetch(`/api/session/${id}`).then(r=>r.json()),
    fetch(`/api/session/${id}/analysis`).then(r=>r.json()),
    fetch(`/api/session/${id}/llm_calls`).then(r=>r.json()),
  ]);
  const commands = d.commands||[];

  const s  =d.session;
  const dur=s.duration_s!=null?s.duration_s+'s':(s.status==='running'?'still running':'—');

  document.getElementById('drawer-badge').className='badge '+s.status;
  document.getElementById('drawer-badge').innerHTML=`<span style="width:5px;height:5px;border-radius:50%;background:currentColor;display:inline-block;margin-right:5px"></span>${s.status}`;

  const tabs=[
    {id:'summary',  label:'Summary'},
    {id:'metrics',  label:'Metrics'+(ana.results&&ana.results.length?` (${ana.results.length})`:'')},
    ...(llmCalls.length?[{id:'llmcalls',label:`LLM Calls (${llmCalls.length})`}]:[]),
    {id:'commands', label:'Commands'+(commands.length?` (${commands.length})`:'')},
    {id:'events',   label:'Events'+(d.events.length?` (${d.events.length})`:'')} ,
    {id:'faults',   label:'Faults'+(d.faults.length?` (${d.faults.length})`:'')},
  ];

  document.getElementById('drawer-tabs').innerHTML=tabs.map(t=>
    `<div class="dtab${t.id==='summary'?' active':''}" onclick="switchDTab('${t.id}',this)">${t.label}</div>`
  ).join('');

  _drawerTabs={
    summary:  buildSummaryTab(s,dur,ana),
    metrics:  buildMetricsTab(ana),
    llmcalls: buildDrawerLLMTab(llmCalls),
    commands: buildCommandsTab(commands),
    events:   buildEventsTab(d.events),
    faults:   buildFaultsTab(d.faults),
  };
  _activeDTab='summary';
  document.getElementById('drawer-body').innerHTML=_drawerTabs['summary'];
}

function switchDTab(id,el){
  document.querySelectorAll('.dtab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  _activeDTab=id;
  document.getElementById('drawer-body').innerHTML=_drawerTabs[id]||'';
}

function buildSummaryTab(s,dur,ana){
  const started=(s.started_at||'—').replace('T',' ').slice(0,19);
  const stopped=(s.stopped_at||'—').replace('T',' ').slice(0,19);
  const ok =ana.command_ok||0;
  const err=ana.command_error||0;
  const ttype=s.target_type||'';
  const taddr=s.target_addr||'';
  const targetRow=ttype?`
      <div class="meta-item" style="grid-column:span 2">
        <div class="meta-val mono" style="font-size:11px;word-break:break-all">
          <span style="color:var(--blue)">${esc(ttype)}</span>
          ${taddr?`<span style="color:var(--text3)"> → </span><span style="color:var(--text)">${esc(taddr)}</span>`:''}
        </div>
        <div class="meta-key">Injection target</div>
      </div>`:'';
  return `
  <div class="dsec">
    <div class="dsec-title">Session info</div>
    <div class="meta-grid">
      <div class="meta-item"><div class="meta-val mono">${started}</div><div class="meta-key">Started</div></div>
      <div class="meta-item"><div class="meta-val mono">${stopped}</div><div class="meta-key">Stopped</div></div>
      <div class="meta-item"><div class="meta-val mono">${dur}</div><div class="meta-key">Duration</div></div>
      <div class="meta-item"><div class="meta-val"><span style="color:var(--green)">${ok}</span><span style="color:var(--text3);font-size:11px"> / </span><span style="color:var(--red)">${err}</span></div><div class="meta-key">Commands OK / Error</div></div>
      ${targetRow}
    </div>
  </div>
  ${ana.active_tc_rules&&ana.active_tc_rules.length?`
  <div class="dsec">
    <div class="dsec-title">Active network rules</div>
    <pre style="font-size:10px;color:var(--text2);background:var(--surface);padding:9px;border-radius:var(--radius-sm);overflow-x:auto;line-height:1.7">${ana.active_tc_rules.map(esc).join('\n')}</pre>
  </div>`:''}
  ${ana.storage_records&&ana.storage_records.length?`
  <div class="dsec">
    <div class="dsec-title">Storage bit-flips (${ana.storage_records.length})</div>
    <table class="metric-tbl">
      <tr><th>File</th><th>Block</th><th>Before</th><th>After</th></tr>
      ${ana.storage_records.slice(0,15).map(r=>`<tr>
        <td class="metric-name" style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.filename||'')}">${esc((r.filename||'').split('/').pop())}</td>
        <td class="metric-val muted">${r.targetblock||''}</td>
        <td class="metric-val" style="color:var(--green)">0x${(r.origValue||0).toString(16)}</td>
        <td class="metric-val" style="color:var(--red)">0x${(r.afterValue||0).toString(16)}</td>
      </tr>`).join('')}
    </table>
    ${ana.storage_records.length>15?`<div class="small muted" style="padding:5px 8px">… ${ana.storage_records.length-15} more</div>`:''}
  </div>`:''}`;
}

// Recursively flatten nested object to dot-notation leaf paths
function flattenObj(obj,prefix,out){
  if(!obj||typeof obj!=='object') return;
  for(const [k,v] of Object.entries(obj)){
    const key=prefix?prefix+'.'+k:k;
    if(v!==null&&typeof v==='object'&&!Array.isArray(v)) flattenObj(v,key,out);
    else out[key]=v;
  }
}

function _deltaBar(b,d,metricKey){
  if(typeof d!=='number') return '—';
  const higherBetter=/token|speed|throughput|rate|mbps|count|remaining/.test(metricKey);
  const worse=higherBetter?d<0:d>0;
  const cls=Math.abs(d)<0.001?'neu':(worse?'up':'down');
  const sign=d>0?'+':'';
  const pct=b&&b!==0?` (${sign}${((d/Math.abs(b))*100).toFixed(0)}%)`:'';
  const barW=Math.min(100,Math.abs(b&&b!==0?d/Math.abs(b)*100:0));
  return `<div style="display:flex;align-items:center;gap:6px">
    <div class="delta-bar-wrap"><div class="delta-bar" style="width:${barW}%;background:${cls==='up'?'var(--red)':cls==='down'?'var(--green)':'var(--text3)'}"></div></div>
    <span class="delta-val delta-${cls}">${sign}${fmtMetric(metricKey,d)}${pct}</span></div>`;
}

function buildMetricsTab(ana){
  if(!ana.results||!ana.results.length)
    return '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-msg">No metric results recorded for this session.</div></div>';
  let html='';
  ana.results.forEach((r,idx)=>{
    const m=r.metrics||{};
    html+=`<div class="dsec"><div class="dsec-title">Result #${idx+1}`;
    if(r.recorded_at) html+=` <span class="muted" style="font-size:9px;font-weight:400">${r.recorded_at.slice(0,19).replace('T',' ')}</span>`;
    html+='</div>';

    // ── Case A: nested {baseline, fault, delta} structure ──────────
    if(m.baseline&&typeof m.baseline==='object'&&!Array.isArray(m.baseline)){
      const fb={},ff={};
      flattenObj(m.baseline,'',fb);
      flattenObj(m.fault||{},'',ff);
      const precompDelta=m.delta||{};
      const allKeys=[...new Set([...Object.keys(fb),...Object.keys(ff)])];
      const numKeys=allKeys.filter(k=>typeof fb[k]==='number'||typeof ff[k]==='number');
      if(numKeys.length){
        html+=`<table class="metric-tbl"><tr><th>Metric</th><th style="text-align:right">Baseline</th><th style="text-align:right">Fault</th><th>Delta</th></tr>`;
        numKeys.forEach(k=>{
          const b=typeof fb[k]==='number'?fb[k]:null;
          const f=typeof ff[k]==='number'?ff[k]:null;
          const shortKey=k.split('.').pop()||k;
          const d=precompDelta[shortKey]!=null?precompDelta[shortKey]:(b!=null&&f!=null?f-b:null);
          html+=`<tr>
            <td class="metric-name" title="${esc(k)}">${esc(k.replace(/_/g,' '))}</td>
            <td class="metric-val muted" style="text-align:right">${b!=null?fmtMetric(k,b):'—'}</td>
            <td class="metric-val" style="text-align:right">${f!=null?fmtMetric(k,f):'—'}</td>
            <td>${_deltaBar(b,d,k)}</td>
          </tr>`;
        });
        html+='</table>';
      }
      // Changed string values
      const strKeys=allKeys.filter(k=>(typeof fb[k]==='string'||typeof ff[k]==='string')&&fb[k]!==ff[k]);
      if(strKeys.length){
        html+=`<div style="margin-top:10px"><div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text3);margin-bottom:6px">Changed values</div>`;
        html+=`<table class="metric-tbl"><tr><th>Field</th><th>Baseline</th><th>Fault</th></tr>`;
        strKeys.forEach(k=>{
          html+=`<tr><td class="metric-name">${esc(k)}</td><td class="metric-val muted">${esc(String(fb[k]??'—'))}</td><td class="metric-val">${esc(String(ff[k]??'—'))}</td></tr>`;
        });
        html+='</table></div>';
      }
      if(!numKeys.length&&!strKeys.length)
        html+='<div class="muted small" style="padding:8px 0">No comparable metrics found.</div>';
    }
    // ── Case B: flat baseline_X / chaos_X / delta_X keys ──────────
    else {
      const keys=Object.keys(m);
      const groups={};
      keys.forEach(k=>{
        const m3=k.match(/^(baseline|chaos|delta)_(.+)$/);
        if(m3){ const g=m3[2]; if(!groups[g])groups[g]={}; groups[g][m3[1]]=m[k]; }
      });
      const plainKeys=keys.filter(k=>!k.match(/^(baseline|chaos|delta)_/));
      const grpKeys=Object.keys(groups);
      if(grpKeys.length){
        html+=`<table class="metric-tbl"><tr><th>Metric</th><th style="text-align:right">Baseline</th><th style="text-align:right">Fault</th><th>Delta</th></tr>`;
        grpKeys.forEach(g=>{
          const gd=groups[g];
          const b=gd.baseline,c=gd.chaos,d=gd.delta;
          html+=`<tr>
            <td class="metric-name">${g.replace(/_/g,' ')}</td>
            <td class="metric-val muted" style="text-align:right">${fmtMetric(g,b)}</td>
            <td class="metric-val" style="text-align:right">${fmtMetric(g,c)}</td>
            <td>${_deltaBar(b,d,g)}</td>
          </tr>`;
        });
        html+='</table>';
      }
      if(plainKeys.length){
        html+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px;margin-top:10px">';
        plainKeys.forEach(k=>{
          const v=m[k];
          const color=k.includes('fail')||k.includes('error')?'var(--red)':k.includes('ok')||k.includes('success')?'var(--green)':'var(--blue)';
          html+=`<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:7px 9px;text-align:center">
            <div style="font-size:15px;font-weight:700;font-family:'JetBrains Mono',monospace;color:${color}">${fmtMetric(k,v)}</div>
            <div style="color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:.4px;margin-top:2px">${k.replace(/_/g,' ')}</div></div>`;
        });
        html+='</div>';
      }
    }
    html+='</div>';
  });
  return html;
}

function buildDrawerLLMTab(calls){
  if(!calls.length)
    return '<div class="empty-state"><div class="empty-icon">📡</div><div class="empty-msg">No LLM calls captured for this session.</div></div>';
  const n        =calls.length;
  const blocked  =calls.filter(c=>c.was_blocked).length;
  const totalIn  =calls.reduce((a,c)=>a+(c.prompt_tokens||0),0);
  const totalOut =calls.reduce((a,c)=>a+(c.completion_tokens||0),0);
  const totalCost=calls.reduce((a,c)=>a+(c.cost_usd||0),0);
  const lats     =calls.map(c=>c.latency_s).filter(Boolean);
  const avgLat   =lats.length?(lats.reduce((a,v)=>a+v,0)/lats.length).toFixed(2):'—';
  return `
  <div class="dsec">
    <div class="dsec-title">Summary</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:7px">
      <div class="meta-item"><div class="meta-val">${n}</div><div class="meta-key">Total calls</div></div>
      <div class="meta-item"><div class="meta-val" style="color:var(--red)">${blocked}</div><div class="meta-key">Blocked</div></div>
      <div class="meta-item"><div class="meta-val" style="color:var(--yellow)">$${totalCost.toFixed(4)}</div><div class="meta-key">Cost</div></div>
      <div class="meta-item"><div class="meta-val">${fmtNum(totalIn)}</div><div class="meta-key">Prompt tokens</div></div>
      <div class="meta-item"><div class="meta-val">${fmtNum(totalOut)}</div><div class="meta-key">Completion tokens</div></div>
      <div class="meta-item"><div class="meta-val">${avgLat}${avgLat!=='—'?'s':''}</div><div class="meta-key">Avg latency</div></div>
    </div>
  </div>
  <div class="dsec">
    <div class="dsec-title">Calls</div>
    <table class="metric-tbl">
      <tr><th>#</th><th>Model</th><th>In</th><th>Out</th><th>Cost</th><th>Lat</th><th>Status</th></tr>
      ${calls.map(c=>{
        const tag=c.was_blocked?'<span class="tag-blocked">blocked</span>':c.was_modified?'<span class="tag-modified">modified</span>':'<span class="tag-clean">clean</span>';
        return `<tr>
          <td class="metric-name">${c.call_index}</td>
          <td class="metric-name">${esc(c.model||'—')}</td>
          <td class="metric-val muted">${c.prompt_tokens||0}</td>
          <td class="metric-val muted">${c.completion_tokens||0}</td>
          <td class="metric-val" style="color:var(--yellow)">$${(c.cost_usd||0).toFixed(5)}</td>
          <td class="metric-val">${c.latency_s!=null?c.latency_s.toFixed(2)+'s':'—'}</td>
          <td>${tag}</td>
        </tr>
        ${(c.prompt_text||c.response_text)?`<tr><td colspan="7">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:6px 0">
            <div class="call-box"><div class="call-box-head">Prompt</div><div class="call-box-body" style="max-height:100px">${esc(c.prompt_text||'')}</div></div>
            <div class="call-box"><div class="call-box-head">Response</div><div class="call-box-body" style="max-height:100px">${esc(c.response_text||'(blocked)')}</div></div>
          </div></td></tr>`:''}`;
      }).join('')}
    </table>
  </div>`;
}

function buildEventsTab(events){
  if(!events.length) return '<div class="empty-state"><div class="empty-icon">📋</div><div class="empty-msg">No events recorded.</div></div>';
  const items=events.map(e=>{
    const ts=(e.timestamp||'').replace('T',' ').slice(11,19);
    const isErr=/ERROR|error/i.test(e.message);
    const isOk =/OK|start|stop|revert/i.test(e.message);
    const isWarn=/WARN|warn/i.test(e.message);
    const cls=isErr?'err':isOk?'ok':isWarn?'warn':'info';
    const icon=isErr?'✕':isOk?'✓':isWarn?'!':'·';
    return `<div class="tl-item"><div class="tl-dot ${cls}">${icon}</div><div class="tl-content"><div class="tl-ts">${ts}</div><div class="tl-msg ${isErr?'err':''}">${esc(e.message)}</div></div></div>`;
  });
  return `<div class="dsec"><div class="dsec-title">Event log</div><div class="timeline">${items.join('')}</div></div>`;
}

function buildFaultsTab(faults){
  if(!faults.length) return '<div class="empty-state"><div class="empty-icon">⚡</div><div class="empty-msg">No faults recorded.</div></div>';
  const blocks=faults.map(f=>{
    let params;
    try{ const p=typeof f.parameters==='string'?JSON.parse(f.parameters):f.parameters; params=JSON.stringify(p,null,2); }
    catch(e){ params=String(f.parameters||'{}'); }
    const cat=f.category||'other';
    const st=CAT[cat]||CAT.other;
    return `<div class="fault-block">
      <div class="fault-head"><span class="fault-kind" style="color:${st.color}">${esc(f.kind)}</span>
        <span class="chip" style="color:${st.color};background:${st.bg};border-color:${st.color}33;font-size:9px">${cat}</span>
      </div>
      <div class="fault-params"><pre>${esc(params)}</pre></div>
    </div>`;
  });
  return `<div class="dsec"><div class="dsec-title">Injected faults</div>${blocks.join('')}</div>`;
}

function buildCommandsTab(commands){
  if(!commands.length)
    return '<div class="empty-state"><div class="empty-icon">💻</div><div class="empty-msg">No commands recorded for this session.</div></div>';
  const failed=commands.filter(c=>c.exit_code!==0).length;
  const rows=commands.map(c=>{
    const ts=(c.timestamp||'').slice(11,19);
    const ok=c.exit_code===0;
    const priv=c.privileged?'<span style="font-size:9px;color:var(--yellow);margin-left:4px">sudo</span>':'';
    const stdoutHtml=c.stdout&&c.stdout.trim()?
      `<div style="margin-top:4px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text2);background:var(--card);border-radius:3px;padding:4px 7px;max-height:80px;overflow-y:auto;white-space:pre-wrap;word-break:break-all">${esc(c.stdout.trim().slice(0,500))}${c.stdout.length>500?'\n…':''}</div>`:'';
    const stderrHtml=c.stderr&&c.stderr.trim()?
      `<div style="margin-top:3px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--red);background:var(--red-bg);border-radius:3px;padding:4px 7px;max-height:60px;overflow-y:auto;white-space:pre-wrap;word-break:break-all">${esc(c.stderr.trim().slice(0,300))}</div>`:'';
    return `<div style="border-bottom:1px solid var(--border);padding:8px 0">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace;flex-shrink:0">${ts}</span>
        <span style="width:6px;height:6px;border-radius:50%;background:${ok?'var(--green)':'var(--red)'};flex-shrink:0" title="exit ${c.exit_code}"></span>
        <code style="font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--text);word-break:break-all;flex:1">${esc(c.cmd)}</code>
        ${priv}
        <span style="font-size:10px;font-family:'JetBrains Mono',monospace;color:${ok?'var(--green)':'var(--red)'}">exit ${c.exit_code}</span>
      </div>
      ${stdoutHtml}${stderrHtml}
    </div>`;
  });
  return `<div class="dsec">
    <div class="dsec-title">Injected commands <span class="muted" style="font-size:10px;font-weight:400">${commands.length} total${failed?' · '+failed+' failed':''}</span></div>
    <div style="font-size:11px">${rows.join('')}</div>
  </div>`;
}

function closeDrawer(){
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('on');
}

// ─── Logs ────────────────────────────────────────────────────
let _logFiles=[];
async function loadLogList(){
  const files=await fetch('/api/logs').then(r=>r.json());
  _logFiles=files;
  const sel=document.getElementById('log-select');
  const prev=sel.value;
  sel.innerHTML=files.length
    ? files.map(f=>`<option value="${f.name}">${f.name} (${f.size_kb}kb)</option>`).join('')
    : '<option value="">no log files found</option>';
  if(prev&&files.find(f=>f.name===prev)) sel.value=prev;
}
async function loadLogContent(){
  const sel=document.getElementById('log-select');
  if(!sel.value) return;
  const data=await fetch(`/api/logs/${encodeURIComponent(sel.value)}?lines=150`).then(r=>r.json());
  const pre=document.getElementById('log-content');
  if(data.error){ pre.textContent=data.error; return; }
  pre.innerHTML=data.lines.map(l=>{
    if(/ERROR|FAIL|critical/i.test(l)) return `<span style="color:var(--red)">${esc(l)}</span>`;
    if(/WARN/i.test(l))                return `<span style="color:var(--yellow)">${esc(l)}</span>`;
    if(/CORRUPT|inversion/i.test(l))   return `<span style="color:var(--yellow)">${esc(l)}</span>`;
    if(/REVERT|stop|OK/i.test(l))      return `<span style="color:var(--green)">${esc(l)}</span>`;
    return `<span style="color:var(--text2)">${esc(l)}</span>`;
  }).join('\n');
  pre.scrollTop=pre.scrollHeight;
  document.getElementById('log-lines').textContent=data.lines.length+' lines';
}
async function loadLogs(){ await loadLogList(); await loadLogContent(); }
document.getElementById('log-select').addEventListener('change',loadLogContent);

// ─── Helpers ─────────────────────────────────────────────────
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }
function fmt(v){
  if(v===null||v===undefined) return '—';
  if(typeof v==='number'){
    if(Math.abs(v)>=1000) return v.toLocaleString(undefined,{maximumFractionDigits:1});
    if(Number.isInteger(v)) return String(v);
    return v.toFixed(3).replace(/\.?0+$/,'');
  }
  return esc(String(v));
}
function fmtNum(n){ return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n) }
function fmtMetric(name, value){
  if(value===null||value===undefined) return '—';
  const n=typeof value==='number'?value:parseFloat(value);
  if(isNaN(n)) return esc(String(value));
  // percentage
  if(/_pct$|_percent$/.test(name)) return n.toFixed(1)+'%';
  // memory / disk in MB
  if(/_mb$|_mb_/.test(name)){
    if(n>=1024) return (n/1024).toFixed(1)+' GB';
    return n.toFixed(1)+' MB';
  }
  // raw bytes
  if(/_bytes$/.test(name)){
    if(n>=1073741824) return (n/1073741824).toFixed(1)+' GB';
    if(n>=1048576) return (n/1048576).toFixed(1)+' MB';
    if(n>=1024) return (n/1024).toFixed(1)+' KB';
    return n.toFixed(0)+' B';
  }
  // time
  if(/_s$/.test(name)||/latency|ttft|duration/.test(name)){
    if(n<0.001) return (n*1000).toFixed(2)+' ms';
    if(n<1) return (n*1000).toFixed(0)+' ms';
    return n.toFixed(3)+' s';
  }
  // throughput
  if(/tokens_per_second|tok_per_s/.test(name)) return n.toFixed(1)+' tok/s';
  if(/net_.*_mb/.test(name)){
    if(n>=1024) return (n/1024).toFixed(2)+' GB/s';
    return n.toFixed(2)+' MB/s';
  }
  if(/iops/.test(name)) return n.toFixed(0)+' IOPS';
  if(/load_/.test(name)) return n.toFixed(2);
  return fmt(n);
}

// ─── Boot ────────────────────────────────────────────────────
initTheme();
load();
setInterval(()=>{
  load();
  if(document.getElementById('tab-logs').classList.contains('active')) loadLogContent();
}, 8000);
</script>
</body>
</html>"""


# ── API ─────────────────────────────────────────────────────────────────────

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
        faults_with_cat = [
            {**f, "category": _fault_category(f.get("kind", ""))}
            for f in data["faults"]
        ]
        result.append({
            "id":          s["id"],
            "name":        s["name"],
            "status":      s["status"],
            "started_at":  s.get("started_at"),
            "stopped_at":  s.get("stopped_at"),
            "duration_s":  duration_s,
            "faults":      faults_with_cat,
            "target_type": s.get("target_type", ""),
            "target_addr": s.get("target_addr", ""),
        })
    return JSONResponse(result)


@app.get("/api/session/{session_id}")
async def api_session(session_id: int):
    db = SessionDB()
    data = db.export_session(session_id)
    s = data["session"]
    duration_s = _calc_duration(s.get("started_at"), s.get("stopped_at"))
    faults_with_cat = [
        {**f, "category": _fault_category(f.get("kind", ""))}
        for f in data["faults"]
    ]
    return JSONResponse({
        "session":  {**s, "duration_s": duration_s},
        "faults":   faults_with_cat,
        "events":   data["events"],
        "commands": data.get("commands", []),
    })


@app.get("/api/session/{session_id}/llm_calls")
async def api_session_llm_calls(session_id: int):
    """LLM calls captured for a single session."""
    db = SessionDB()
    calls = db.get_llm_calls(session_id)
    return JSONResponse(calls)


@app.get("/api/llm_calls")
async def api_llm_calls(limit: int = 1000):
    """All LLM calls across all sessions, joined with session name."""
    db = SessionDB()
    try:
        conn = _sql.connect(db.path, timeout=5)
        conn.row_factory = _sql.Row
        rows = conn.execute(
            "SELECT c.*, s.name AS session_name "
            "FROM llm_calls c "
            "JOIN sessions s ON c.session_id = s.id "
            "ORDER BY c.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/monitoring")
async def api_monitoring():
    """Aggregated data for the Monitoring tab charts."""
    db = SessionDB()
    try:
        conn = _sql.connect(db.path, timeout=5)
        conn.row_factory = _sql.Row

        # Per-session aggregates
        session_agg = conn.execute(
            "SELECT session_id, "
            "  COUNT(*) AS total_calls, "
            "  SUM(was_blocked) AS blocked_calls, "
            "  SUM(was_modified) AS modified_calls, "
            "  SUM(prompt_tokens) AS prompt_tokens, "
            "  SUM(completion_tokens) AS completion_tokens, "
            "  SUM(cost_usd) AS total_cost, "
            "  AVG(latency_s) AS avg_latency, "
            "  AVG(CASE WHEN ttft_s > 0 THEN ttft_s END) AS avg_ttft "
            "FROM llm_calls GROUP BY session_id"
        ).fetchall()

        conn.close()
        return JSONResponse({
            "session_aggregates": [dict(r) for r in session_agg],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/system")
async def api_system():
    TOOLS = [
        ("tc",          "iproute2",       "network faults (netem)"),
        ("ip",          "iproute2",       "interface detection"),
        ("filefrag",    "e2fsprogs",      "storage — extent info"),
        ("dd",          "coreutils",      "storage — bit-flip"),
        ("inotifywait", "inotify-tools",  "storage — file watch"),
        ("python3",     "python3",        "storage / BPF scripts"),
        ("pip3",        "python3-pip",    "Python package install"),
        ("ssh",         "openssh-client", "SSH target support"),
        ("stress-ng",   "stress-ng",      "CPU / memory / I/O stress"),
        ("redis-cli",   "redis-tools",    "Redis state faults"),
        ("psql",        "postgresql-client","Postgres state faults"),
        ("docker",      "docker-cli",     "container faults"),
        ("nvidia-smi",  "nvidia-utils",   "GPU faults"),
        ("iostat",      "sysstat",        "I/O metric collection"),
        ("ping",        "iputils",        "RTT metric collection"),
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


@app.get("/api/logs")
async def api_logs():
    result, seen = [], set()
    candidates = [
        ("cj.log", "storage bit-flip user log"),
        ("cj_debug.log", "storage debug log"),
        ("chaos.log", "chaos-jungle runner log"),
    ]
    for p in _CJ_HOME.glob("**/*.log"):
        if p.name not in seen:
            candidates.append((p.name, str(p.relative_to(_CJ_HOME))))
    for name, desc in candidates:
        path = _CJ_HOME / name
        if path.exists():
            size_kb = round(path.stat().st_size / 1024, 1)
            result.append({"name": name, "desc": desc, "size_kb": size_kb})
            seen.add(name)
    return JSONResponse(result)


@app.get("/api/logs/{filename}")
async def api_log_content(filename: str, lines: int = 150):
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
    db = SessionDB()
    data = db.export_session(session_id)
    results = db.get_results(session_id)

    cj_db_path = _CJ_HOME / "cj.db"
    storage_records = []
    if cj_db_path.exists():
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

    tc_rules = []
    try:
        out = subprocess.check_output(
            ["tc", "qdisc", "show"], stderr=subprocess.DEVNULL, text=True
        )
        tc_rules = [l.strip() for l in out.splitlines()
                    if l.strip() and "noqueue" not in l and "noop" not in l]
    except (subprocess.CalledProcessError, FileNotFoundError):
        tc_rules = []

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
    cj_db_path = _CJ_HOME / "cj.db"
    if not cj_db_path.exists():
        return JSONResponse({"records": [], "note": "cj.db not found"})
    try:
        conn = _sql.connect(str(cj_db_path))
        conn.row_factory = _sql.Row
        rows = conn.execute("SELECT * FROM records ORDER BY id DESC").fetchall()
        conn.close()
        return JSONResponse({"records": [dict(r) for r in rows]})
    except Exception as e:
        return JSONResponse({"records": [], "error": str(e)})


# ── helpers ─────────────────────────────────────────────────────────────────

def _calc_duration(started_at, stopped_at):
    if not started_at or not stopped_at:
        return None
    try:
        t0 = datetime.fromisoformat(started_at)
        t1 = datetime.fromisoformat(stopped_at)
        return round((t1 - t0).total_seconds(), 1)
    except Exception:
        return None


# ── entry point ──────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = 8050) -> None:
    """Start the dashboard server (blocking)."""
    uvicorn.run(app, host=host, port=port, log_level="warning")
