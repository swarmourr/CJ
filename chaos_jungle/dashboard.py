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
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>chaos-jungle</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:        #09090b;
  --surface:   #111113;
  --panel:     #141416;
  --border:    #27272a;
  --border2:   #3f3f46;
  --text:      #fafafa;
  --text2:     #a1a1aa;
  --text3:     #71717a;
  --accent:    #6366f1;
  --accent-dim:rgba(99,102,241,.15);
  --green:     #22c55e;
  --red:       #ef4444;
  --yellow:    #f59e0b;
  --radius:    6px;
  --sidebar-w: 48px;
  --left-w:    240px;
  --detail-w:  380px;
  --font:      'Inter', system-ui, sans-serif;
  --mono:      'JetBrains Mono', monospace;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--font); font-size: 13px; line-height: 1.5; }

/* ── Layout ── */
.app {
  display: grid;
  grid-template-columns: var(--sidebar-w) var(--left-w) 1fr;
  grid-template-rows: 100vh;
  height: 100vh;
  overflow: hidden;
}
.app.dp-open {
  grid-template-columns: var(--sidebar-w) var(--left-w) 1fr var(--detail-w);
}

/* ── Sidebar ── */
.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 10px 0;
  gap: 2px;
  z-index: 10;
}
.sb-logo {
  width: 28px; height: 28px;
  background: var(--accent);
  border-radius: 7px;
  display: flex; align-items: center; justify-content: center;
  margin-bottom: 10px;
  font-size: 11px; font-weight: 700; color: #fff; letter-spacing: -.5px;
  flex-shrink: 0;
}
.sb-btn {
  width: 36px; height: 36px;
  border-radius: var(--radius);
  border: none;
  background: transparent;
  color: var(--text3);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  transition: background .15s, color .15s;
  position: relative;
}
.sb-btn:hover { background: var(--panel); color: var(--text2); }
.sb-btn.active { background: var(--accent-dim); color: var(--accent); }
.sb-btn[title]:hover::after {
  content: attr(title);
  position: absolute; left: calc(100% + 8px); top: 50%; transform: translateY(-50%);
  background: #1e1e20; border: 1px solid var(--border2); border-radius: 4px;
  padding: 3px 8px; font-size: 11px; color: var(--text); white-space: nowrap; z-index: 100;
  pointer-events: none;
}
.sb-spacer { flex: 1; }

/* ── Left panel ── */
.left-panel {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.lp-header {
  padding: 14px 12px 10px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.lp-title {
  font-size: 10px; font-weight: 600; color: var(--text3);
  text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px;
}
.lp-search {
  width: 100%;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 5px 8px;
  color: var(--text); font-family: var(--font); font-size: 12px;
  outline: none;
}
.lp-search:focus { border-color: var(--accent); }
.lp-search::placeholder { color: var(--text3); }
.lp-list { flex: 1; overflow-y: auto; padding: 4px 0; }
.lp-list::-webkit-scrollbar { width: 3px; }
.lp-list::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }

.exp-item {
  padding: 8px 12px;
  cursor: pointer;
  border-left: 2px solid transparent;
  transition: background .1s;
}
.exp-item:hover { background: rgba(255,255,255,.03); }
.exp-item.active { background: var(--accent-dim); border-left-color: var(--accent); }
.exp-name {
  font-size: 12px; font-weight: 500; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px;
}
.exp-meta { display: flex; align-items: center; gap: 5px; }
.exp-count { font-size: 11px; color: var(--text3); }
.exp-rate { font-size: 11px; font-weight: 600; margin-left: 2px; }
.exp-rate.ok   { color: var(--green); }
.exp-rate.warn { color: var(--yellow); }
.exp-rate.fail { color: var(--red); }
.exp-dots { display: flex; gap: 3px; margin-left: auto; }
.exp-dot { width: 6px; height: 6px; border-radius: 50%; }

/* ── Main ── */
.main {
  display: flex; flex-direction: column;
  overflow: hidden; background: var(--bg);
  min-width: 0;
}
.view-panel { display: none; flex-direction: column; flex: 1; overflow: hidden; }
.view-panel.active { display: flex; }

/* Filter bar */
.filter-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0; flex-wrap: wrap;
  min-height: 52px;
}
.view-title { font-size: 14px; font-weight: 600; color: var(--text); margin-right: 4px; }
.filter-input {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 5px 10px;
  color: var(--text); font-family: var(--font); font-size: 12px; outline: none;
  min-width: 180px;
}
.filter-input:focus { border-color: var(--accent); }
.filter-input::placeholder { color: var(--text3); }
.filter-select {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 5px 8px;
  color: var(--text); font-family: var(--font); font-size: 12px;
  outline: none; cursor: pointer;
}
.filter-select:focus { border-color: var(--accent); }
.ml-auto { margin-left: auto; }
.count-badge {
  font-size: 11px; color: var(--text3);
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 2px 8px; white-space: nowrap;
}

/* KPI bar */
.kpi-bar {
  display: flex; gap: 1px; flex-shrink: 0;
  background: var(--border); border-bottom: 1px solid var(--border);
}
.kpi-card { flex: 1; background: var(--bg); padding: 12px 16px; }
.kpi-label { font-size: 10px; color: var(--text3); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 3px; }
.kpi-value { font-size: 20px; font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; }
.kpi-sub   { font-size: 11px; color: var(--text3); margin-top: 1px; }

/* Table */
.table-wrap { flex: 1; overflow-y: auto; }
.table-wrap::-webkit-scrollbar { width: 5px; }
.table-wrap::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
thead th {
  position: sticky; top: 0; z-index: 2;
  background: var(--surface); padding: 8px 14px;
  text-align: left; font-size: 10px; font-weight: 600;
  color: var(--text3); text-transform: uppercase; letter-spacing: .06em;
  border-bottom: 1px solid var(--border);
  white-space: nowrap; cursor: pointer; user-select: none;
}
thead th:hover { color: var(--text2); }
thead th.sort-asc::after  { content: ' ↑'; color: var(--accent); }
thead th.sort-desc::after { content: ' ↓'; color: var(--accent); }
tbody tr { border-bottom: 1px solid var(--border); cursor: pointer; transition: background .1s; }
tbody tr:hover    { background: rgba(255,255,255,.03); }
tbody tr.selected { background: var(--accent-dim); }
td { padding: 9px 14px; color: var(--text2); white-space: nowrap; vertical-align: middle; }
td.mono { font-family: var(--mono); font-size: 11px; }
.cell-primary { color: var(--text); font-weight: 500; }

/* Status */
.status-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  margin-right: 6px; vertical-align: middle; flex-shrink: 0;
}
.status-dot.running { background: var(--accent); box-shadow: 0 0 0 2px var(--accent-dim); animation: pulse 2s infinite; }
.status-dot.done    { background: var(--green); }
.status-dot.error   { background: var(--red); }
.status-dot.pending { background: var(--text3); }
@keyframes pulse {
  0%,100% { opacity: 1; }
  50%      { opacity: .5; }
}

/* Chips */
.fault-chips { display: flex; gap: 3px; flex-wrap: wrap; max-width: 260px; }
.chip {
  display: inline-flex; align-items: center;
  padding: 2px 6px; border-radius: 3px;
  font-size: 10px; font-weight: 500; white-space: nowrap;
}

/* Target badge */
.target-badge {
  font-family: var(--mono); font-size: 10px; color: var(--text3);
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 3px; padding: 1px 6px;
}

/* Empty / loading */
.empty-state {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 8px; color: var(--text3);
}
.empty-icon { font-size: 28px; opacity: .35; }
.empty-text { font-size: 12px; }
.loading-state {
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--text3); font-size: 12px; gap: 6px;
}

/* ── Detail panel ── */
.detail-panel {
  background: var(--panel); border-left: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden; min-width: 0;
}
.dp-topbar {
  display: flex; align-items: center;
  padding: 10px 14px; border-bottom: 1px solid var(--border);
  gap: 8px; flex-shrink: 0;
}
.dp-title {
  font-size: 13px; font-weight: 600; color: var(--text);
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.dp-close {
  width: 22px; height: 22px; border: none; background: transparent;
  color: var(--text3); font-size: 18px; cursor: pointer;
  border-radius: 4px; display: flex; align-items: center; justify-content: center;
  line-height: 1;
}
.dp-close:hover { background: var(--border); color: var(--text); }
.dp-tabs {
  display: flex; border-bottom: 1px solid var(--border);
  padding: 0 14px; flex-shrink: 0; overflow-x: auto;
  scrollbar-width: none;
}
.dp-tabs::-webkit-scrollbar { display: none; }
.dp-tab {
  padding: 8px 10px; font-size: 12px; color: var(--text3);
  cursor: pointer; border-bottom: 2px solid transparent;
  white-space: nowrap; margin-bottom: -1px; transition: color .12s;
}
.dp-tab:hover { color: var(--text2); }
.dp-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.dp-body { flex: 1; overflow-y: auto; padding: 14px; }
.dp-body::-webkit-scrollbar { width: 3px; }
.dp-body::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }

/* Detail sections */
.dp-section { margin-bottom: 18px; }
.dp-section-title {
  font-size: 10px; font-weight: 600; color: var(--text3);
  text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px;
}
.dp-kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; }
.dp-kv-k { font-size: 11px; color: var(--text3); padding: 2px 0; white-space: nowrap; }
.dp-kv-v {
  font-size: 11px; color: var(--text); font-family: var(--mono);
  padding: 2px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.dp-kv-v.wrap { font-family: var(--font); white-space: normal; word-break: break-all; }

/* Fault rows */
.fault-row {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 10px 12px; margin-bottom: 8px;
}
.fault-row-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.fault-params { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; }
.fault-pk { font-size: 11px; color: var(--text3); }
.fault-pv { font-size: 11px; color: var(--text2); font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; }

/* Commands */
.cmd-row {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 8px 10px; margin-bottom: 6px;
}
.cmd-label { font-size: 10px; color: var(--text3); margin-bottom: 3px; }
.cmd-text  { font-family: var(--mono); font-size: 11px; color: var(--text2); word-break: break-all; }
.cmd-result{ font-family: var(--mono); font-size: 10px; color: var(--text3); margin-top: 3px; }

/* Events timeline */
.evt-row { display: flex; gap: 8px; padding: 5px 0; border-bottom: 1px solid var(--border); align-items: flex-start; }
.evt-time { font-size: 10px; color: var(--text3); font-family: var(--mono); white-space: nowrap; flex-shrink: 0; padding-top: 1px; }
.evt-level {
  font-size: 9px; font-weight: 600; padding: 1px 5px;
  border-radius: 3px; flex-shrink: 0; text-transform: uppercase; margin-top: 1px;
}
.evt-level.info  { color: var(--accent); background: var(--accent-dim); }
.evt-level.warn  { color: var(--yellow); background: rgba(245,158,11,.12); }
.evt-level.error { color: var(--red);    background: rgba(239,68,68,.12); }
.evt-msg { font-size: 11px; color: var(--text2); word-break: break-word; }

/* Metrics table */
.metric-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.metric-table th { padding: 4px 8px; text-align: left; color: var(--text3); font-weight: 600; border-bottom: 1px solid var(--border); font-size: 10px; }
.metric-table td { padding: 4px 8px; border-bottom: 1px solid var(--border); font-family: var(--mono); }
.metric-table td:first-child { color: var(--text3); font-family: var(--font); font-size: 10px; }
.delta-pos { color: var(--red); }
.delta-neg { color: var(--green); }

/* LLM call cards in detail */
.llm-call-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 10px 12px; margin-bottom: 8px;
}
.llm-call-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.llm-call-model  { font-size: 12px; font-weight: 600; color: var(--text); }
.llm-call-meta   { display: flex; gap: 12px; flex-wrap: wrap; }
.llm-call-meta span { font-size: 11px; color: var(--text3); }
.llm-call-meta .val { color: var(--text2); font-family: var(--mono); }

/* Monitoring */
.charts-grid {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 16px; padding: 16px 20px; overflow-y: auto; flex: 1;
}
.chart-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px;
}
.chart-card-title {
  font-size: 10px; font-weight: 600; color: var(--text3);
  text-transform: uppercase; letter-spacing: .06em; margin-bottom: 12px;
}
.chart-wrap { position: relative; height: 160px; }

/* System */
.tool-ok      { color: var(--green); }
.tool-missing { color: var(--red); }

/* Logs */
.log-files {
  display: flex; gap: 6px; flex-wrap: wrap;
  padding: 10px 20px; flex-shrink: 0; border-bottom: 1px solid var(--border);
}
.log-file-btn {
  padding: 4px 10px; border-radius: var(--radius);
  border: 1px solid var(--border); background: transparent;
  color: var(--text2); font-size: 12px; cursor: pointer; transition: all .12s;
  font-family: var(--font);
}
.log-file-btn:hover  { border-color: var(--accent); color: var(--accent); }
.log-file-btn.active { border-color: var(--accent); background: var(--accent-dim); color: var(--accent); }
.log-content {
  flex: 1; overflow-y: auto; padding: 10px 20px;
  font-family: var(--mono); font-size: 11px; color: var(--text2); line-height: 1.7;
}
.log-line { }
.log-error { color: var(--red); }
.log-warn  { color: var(--yellow); }

/* Global scrollbar */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>
<div class="app" id="app">

  <!-- Sidebar -->
  <nav class="sidebar">
    <div class="sb-logo">CJ</div>
    <button class="sb-btn active" id="sb-runs"       onclick="setView('runs')"       title="Runs">
      <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
    </button>
    <button class="sb-btn" id="sb-llm"        onclick="setView('llm')"        title="LLM Calls">
      <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
    </button>
    <button class="sb-btn" id="sb-monitoring" onclick="setView('monitoring')" title="Monitoring">
      <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
    </button>
    <button class="sb-btn" id="sb-system"     onclick="setView('system')"     title="System">
      <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 1v3M12 20v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M1 12h3M20 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/></svg>
    </button>
    <button class="sb-btn" id="sb-logs"       onclick="setView('logs')"       title="Logs">
      <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
    </button>
    <div class="sb-spacer"></div>
  </nav>

  <!-- Left panel -->
  <aside class="left-panel">
    <div class="lp-header">
      <div class="lp-title">Experiments</div>
      <input class="lp-search" type="text" placeholder="Filter…" id="exp-search" oninput="filterExps()"/>
    </div>
    <div class="lp-list" id="exp-list">
      <div class="loading-state">Loading…</div>
    </div>
  </aside>

  <!-- Main -->
  <main class="main">

    <!-- Runs -->
    <div class="view-panel active" id="view-runs">
      <div class="filter-bar">
        <span class="view-title">Runs</span>
        <input class="filter-input" type="text" placeholder="Search runs…" id="runs-search" oninput="filterRuns()"/>
        <select class="filter-select" id="runs-status" onchange="filterRuns()">
          <option value="">All status</option>
          <option value="done">Done</option>
          <option value="running">Running</option>
          <option value="error">Error</option>
        </select>
        <select class="filter-select" id="runs-category" onchange="filterRuns()">
          <option value="">All categories</option>
          <option value="network">Network</option>
          <option value="resource">Resource</option>
          <option value="process">Process</option>
          <option value="storage">Storage</option>
          <option value="state">State</option>
          <option value="llm">LLM/MCP</option>
          <option value="skill">Skill</option>
          <option value="gpu">GPU</option>
        </select>
        <span class="ml-auto count-badge" id="runs-count">— runs</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th id="th-id"         onclick="sortRuns('id')">#</th>
              <th id="th-name"       onclick="sortRuns('name')">Name</th>
              <th id="th-status"     onclick="sortRuns('status')">Status</th>
              <th>Target</th>
              <th>Faults</th>
              <th id="th-duration_s" onclick="sortRuns('duration_s')">Duration</th>
              <th id="th-started_at" onclick="sortRuns('started_at')">Started</th>
            </tr>
          </thead>
          <tbody id="runs-tbody"></tbody>
        </table>
        <div class="empty-state" id="runs-empty" style="display:none">
          <div class="empty-icon">⚗</div>
          <div class="empty-text">No runs found</div>
        </div>
      </div>
    </div>

    <!-- LLM Calls -->
    <div class="view-panel" id="view-llm">
      <div class="filter-bar">
        <span class="view-title">LLM Calls</span>
        <input class="filter-input" type="text" placeholder="Search model, session…" id="llm-search" oninput="filterLLM()"/>
        <select class="filter-select" id="llm-ff" onchange="filterLLM()">
          <option value="">All calls</option>
          <option value="blocked">Blocked only</option>
          <option value="modified">Modified only</option>
        </select>
        <span class="ml-auto count-badge" id="llm-count">— calls</span>
      </div>
      <div class="kpi-bar" id="llm-kpi" style="display:none"></div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Session</th>
              <th>Model</th>
              <th>Tokens in</th>
              <th>Tokens out</th>
              <th>Cost</th>
              <th>Latency</th>
              <th>TTFT</th>
              <th>Status</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody id="llm-tbody"></tbody>
        </table>
        <div class="empty-state" id="llm-empty" style="display:none">
          <div class="empty-icon">💬</div>
          <div class="empty-text">No LLM calls recorded</div>
        </div>
      </div>
    </div>

    <!-- Monitoring -->
    <div class="view-panel" id="view-monitoring">
      <div class="filter-bar">
        <span class="view-title">Monitoring</span>
      </div>
      <div class="charts-grid">
        <div class="chart-card"><div class="chart-card-title">Calls per session</div><div class="chart-wrap"><canvas id="chart-calls"></canvas></div></div>
        <div class="chart-card"><div class="chart-card-title">Cost per session (USD)</div><div class="chart-wrap"><canvas id="chart-cost"></canvas></div></div>
        <div class="chart-card"><div class="chart-card-title">Avg latency per session (s)</div><div class="chart-wrap"><canvas id="chart-latency"></canvas></div></div>
        <div class="chart-card"><div class="chart-card-title">Blocked &amp; modified calls</div><div class="chart-wrap"><canvas id="chart-blocked"></canvas></div></div>
      </div>
    </div>

    <!-- System -->
    <div class="view-panel" id="view-system">
      <div class="filter-bar">
        <span class="view-title">System Tools</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Binary</th><th>Package</th><th>Role</th><th>Status</th><th>Path</th>
            </tr>
          </thead>
          <tbody id="system-tbody"></tbody>
        </table>
      </div>
    </div>

    <!-- Logs -->
    <div class="view-panel" id="view-logs">
      <div class="filter-bar">
        <span class="view-title">Logs</span>
      </div>
      <div class="log-files" id="log-files"></div>
      <div class="log-content" id="log-content">
        <div class="empty-state" style="height:100%">
          <div class="empty-icon">📄</div>
          <div class="empty-text">Select a log file above</div>
        </div>
      </div>
    </div>

  </main>

  <!-- Detail panel -->
  <aside class="detail-panel" id="detail-panel">
    <div class="dp-topbar">
      <span class="dp-title" id="dp-title">—</span>
      <button class="dp-close" onclick="closeDetail()">&#215;</button>
    </div>
    <div class="dp-tabs" id="dp-tabs">
      <div class="dp-tab active" data-tab="run"      onclick="switchDPTab('run')">Run</div>
      <div class="dp-tab"        data-tab="faults"   onclick="switchDPTab('faults')">Faults</div>
      <div class="dp-tab"        data-tab="commands" onclick="switchDPTab('commands')">Commands</div>
      <div class="dp-tab"        data-tab="llm"      onclick="switchDPTab('llm')">LLM</div>
      <div class="dp-tab"        data-tab="events"   onclick="switchDPTab('events')">Events</div>
    </div>
    <div class="dp-body" id="dp-body">
      <div class="empty-state" style="height:100%">
        <div class="empty-icon">↖</div>
        <div class="empty-text">Click a run to inspect</div>
      </div>
    </div>
  </aside>

</div>
<script>
// ── State ──────────────────────────────────────────────────────────────────
let _runs    = [];
let _llm     = [];
let _exps    = {};
let _expSel  = null;   // selected experiment filter (null = all)
let _selId   = null;   // open detail session id
let _sort    = {col:'id', asc:false};
let _charts  = {};
let _dpData  = {};     // tab → html string
let _dpTab   = 'run';

// ── Category colours ───────────────────────────────────────────────────────
const CC = {
  network:'#2563eb', resource:'#ea580c', process:'#dc2626',
  storage:'#d97706', state:'#0891b2',   llm:'#7c3aed',
  skill:'#4f46e5',   semantic:'#db2777', gpu:'#16a34a', other:'#64748b',
};
const CB = {
  network:'rgba(37,99,235,.15)',  resource:'rgba(234,88,12,.15)',
  process:'rgba(220,38,38,.15)',  storage:'rgba(217,119,6,.15)',
  state:'rgba(8,145,178,.15)',    llm:'rgba(124,58,237,.15)',
  skill:'rgba(79,70,229,.15)',    semantic:'rgba(219,39,119,.15)',
  gpu:'rgba(22,163,74,.15)',      other:'rgba(100,116,139,.15)',
};

// ── Formatters ─────────────────────────────────────────────────────────────
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtDur(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(1)+'s';
  return Math.floor(s/60)+'m '+(s%60|0)+'s';
}
function fmtCost(v) {
  if (!v) return '—';
  return v < 0.001 ? '<$0.001' : '$'+v.toFixed(4);
}
function fmtMetric(name, value) {
  if (value == null || value === '') return '—';
  const n = Number(value);
  if (isNaN(n)) return String(value);
  const k = String(name);
  if (k.endsWith('_pct') || k.endsWith('_percent'))       return n.toFixed(1)+'%';
  if (k.endsWith('_mb'))   return n >= 1024 ? (n/1024).toFixed(2)+'GB' : n.toFixed(1)+'MB';
  if (k.endsWith('_bytes') || k === 'bytes') {
    if (n >= 1073741824) return (n/1073741824).toFixed(2)+'GB';
    if (n >= 1048576)    return (n/1048576).toFixed(1)+'MB';
    if (n >= 1024)       return (n/1024).toFixed(1)+'KB';
    return n+'B';
  }
  if (k.endsWith('_s') || k.endsWith('_seconds'))
    return n >= 1 ? n.toFixed(2)+'s' : (n*1000).toFixed(0)+'ms';
  if (k === 'tokens_per_second') return n.toFixed(1)+' tok/s';
  return n.toLocaleString(undefined, {maximumFractionDigits:4});
}
function flattenObj(obj, prefix, out) {
  out = out || {};
  if (!obj || typeof obj !== 'object') return out;
  for (const k in obj) {
    const v = obj[k], full = prefix ? prefix+'.'+k : k;
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) flattenObj(v, full, out);
    else out[full] = v;
  }
  return out;
}
function chip(cat, label) {
  const c = CC[cat]||CC.other, bg = CB[cat]||CB.other;
  return `<span class="chip" style="color:${c};background:${bg}">${label}</span>`;
}
function statusDot(st) {
  const cls = st === 'running' ? 'running' : st === 'done' ? 'done' : st === 'error' ? 'error' : 'pending';
  return `<span class="status-dot ${cls}"></span>`;
}
function faultChips(faults) {
  if (!faults || !faults.length) return '<span style="color:var(--text3);font-size:11px">—</span>';
  const seen = new Set();
  let h = '<div class="fault-chips">';
  for (const f of faults) {
    const cat = f.category||'other', key = cat+f.kind;
    if (seen.has(key)) continue; seen.add(key);
    h += chip(cat, f.kind||cat);
  }
  return h + '</div>';
}
function targetBadge(s) {
  const t = s.target_type||'local', a = s.target_addr||'';
  return `<span class="target-badge">${t}${a?':'+a:''}</span>`;
}

// ── Boot ───────────────────────────────────────────────────────────────────
async function boot() {
  await Promise.all([loadRuns(), loadLLM(), loadSystem(), loadLogs()]);
}

// ── View switching ─────────────────────────────────────────────────────────
function setView(v) {
  document.querySelectorAll('.sb-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
  const sb = document.getElementById('sb-'+v);
  const vp = document.getElementById('view-'+v);
  if (sb) sb.classList.add('active');
  if (vp) vp.classList.add('active');
  if (v === 'monitoring') renderMonitoring();
}

// ── Experiments (left panel) ───────────────────────────────────────────────
function buildExps(runs) {
  const map = {};
  for (const r of runs) {
    const n = r.name || 'Unnamed';
    if (!map[n]) map[n] = {runs:[], cats:new Set()};
    map[n].runs.push(r);
    for (const f of (r.faults||[])) map[n].cats.add(f.category||'other');
  }
  return map;
}
function renderExps() {
  const el = document.getElementById('exp-list');
  const q  = (document.getElementById('exp-search')?.value||'').toLowerCase();
  let html = '';
  const allAct = !_expSel;
  html += `<div class="exp-item${allAct?' active':''}" onclick="selectExp(null)">
    <div class="exp-name">All runs</div>
    <div class="exp-meta"><span class="exp-count">${_runs.length} runs</span></div>
  </div>`;
  for (const [name, g] of Object.entries(_exps)) {
    if (q && !name.toLowerCase().includes(q)) continue;
    const total = g.runs.length;
    const pass  = g.runs.filter(r => r.status === 'done').length;
    const pct   = total ? Math.round(100*pass/total) : 0;
    const rc    = pct >= 80 ? 'ok' : pct >= 50 ? 'warn' : 'fail';
    const act   = _expSel === name;
    let dots = '';
    for (const cat of g.cats) dots += `<span class="exp-dot" style="background:${CC[cat]||CC.other}" title="${cat}"></span>`;
    html += `<div class="exp-item${act?' active':''}" onclick="selectExp(${JSON.stringify(name)})">
      <div class="exp-name">${name}</div>
      <div class="exp-meta">
        <span class="exp-count">${total} run${total!==1?'s':''}</span>
        <span class="exp-rate ${rc}">${pct}%</span>
        <div class="exp-dots">${dots}</div>
      </div>
    </div>`;
  }
  el.innerHTML = html;
}
function filterExps() { renderExps(); }
function selectExp(name) { _expSel = name; renderExps(); filterRuns(); }

// ── Runs ───────────────────────────────────────────────────────────────────
async function loadRuns() {
  try {
    const res  = await fetch('/api/sessions');
    _runs = await res.json();
    _exps = buildExps(_runs);
    renderExps();
    filterRuns();
  } catch(e) {
    document.getElementById('runs-tbody').innerHTML =
      `<tr><td colspan="7" style="color:var(--red);padding:16px 14px">Failed to load: ${e.message}</td></tr>`;
  }
}
function sortRuns(col) {
  // Update sort header classes
  document.querySelectorAll('thead th').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
  });
  if (_sort.col === col) _sort.asc = !_sort.asc;
  else { _sort.col = col; _sort.asc = col !== 'id'; }
  const th = document.getElementById('th-'+col);
  if (th) th.classList.add(_sort.asc ? 'sort-asc' : 'sort-desc');
  filterRuns();
}
function filterRuns() {
  const q   = (document.getElementById('runs-search')?.value||'').toLowerCase();
  const st  = document.getElementById('runs-status')?.value||'';
  const cat = document.getElementById('runs-category')?.value||'';
  let rows = _runs.filter(r => {
    if (_expSel && r.name !== _expSel) return false;
    if (st  && r.status !== st)  return false;
    if (cat && !(r.faults||[]).some(f => f.category === cat)) return false;
    if (q   && !r.name.toLowerCase().includes(q) && !String(r.id).includes(q)) return false;
    return true;
  });
  const {col, asc} = _sort;
  rows.sort((a,b) => {
    let va = a[col] ?? '', vb = b[col] ?? '';
    if (typeof va === 'number') return asc ? va-vb : vb-va;
    return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
  });
  const tbody = document.getElementById('runs-tbody');
  const empty = document.getElementById('runs-empty');
  document.getElementById('runs-count').textContent = rows.length+' run'+(rows.length!==1?'s':'');
  if (!rows.length) { tbody.innerHTML=''; empty.style.display='flex'; return; }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map(r => {
    const sel = _selId===r.id ? ' selected':'';
    return `<tr class="${sel}" onclick="openDetail(${r.id})">
      <td class="mono">${r.id}</td>
      <td><span class="cell-primary">${r.name||'—'}</span></td>
      <td>${statusDot(r.status)}<span>${r.status}</span></td>
      <td>${targetBadge(r)}</td>
      <td>${faultChips(r.faults)}</td>
      <td>${fmtDur(r.duration_s)}</td>
      <td class="mono" style="color:var(--text3)">${fmtDate(r.started_at)}</td>
    </tr>`;
  }).join('');
}

// ── LLM Calls ─────────────────────────────────────────────────────────────
async function loadLLM() {
  try {
    const res = await fetch('/api/llm_calls');
    _llm = await res.json();
    buildLLMKPI(_llm);
    filterLLM();
  } catch(e) { /* ignore */ }
}
function buildLLMKPI(calls) {
  const bar = document.getElementById('llm-kpi');
  if (!calls.length) { bar.style.display='none'; return; }
  const cost   = calls.reduce((a,c) => a+(c.cost_usd||0), 0);
  const tokens = calls.reduce((a,c) => a+(c.prompt_tokens||0)+(c.completion_tokens||0), 0);
  const lat    = calls.reduce((a,c) => a+(c.latency_s||0), 0) / calls.length;
  const blk    = calls.filter(c => c.was_blocked).length;
  bar.style.display = 'flex';
  bar.innerHTML = `
    <div class="kpi-card"><div class="kpi-label">Total calls</div><div class="kpi-value">${calls.length.toLocaleString()}</div></div>
    <div class="kpi-card"><div class="kpi-label">Total cost</div><div class="kpi-value">${cost < 0.001 ? '< $0.001' : '$'+cost.toFixed(3)}</div></div>
    <div class="kpi-card"><div class="kpi-label">Total tokens</div><div class="kpi-value">${(tokens/1000).toFixed(1)}K</div></div>
    <div class="kpi-card"><div class="kpi-label">Avg latency</div><div class="kpi-value">${lat.toFixed(2)}s</div></div>
    <div class="kpi-card"><div class="kpi-label">Blocked</div><div class="kpi-value" style="color:var(--red)">${blk}</div></div>`;
}
function filterLLM() {
  const q  = (document.getElementById('llm-search')?.value||'').toLowerCase();
  const ff = document.getElementById('llm-ff')?.value||'';
  let rows = _llm.filter(c => {
    if (ff === 'blocked'  && !c.was_blocked)  return false;
    if (ff === 'modified' && !c.was_modified) return false;
    if (q && !(c.model||'').toLowerCase().includes(q) && !(c.session_name||'').toLowerCase().includes(q)) return false;
    return true;
  });
  const tbody = document.getElementById('llm-tbody');
  const empty = document.getElementById('llm-empty');
  document.getElementById('llm-count').textContent = rows.length+' call'+(rows.length!==1?'s':'');
  if (!rows.length) { tbody.innerHTML=''; empty.style.display='flex'; return; }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map(c => {
    const blk = c.was_blocked  ? chip('process','blocked')  : '';
    const mod = c.was_modified ? chip('storage','modified') : '';
    const st  = blk || mod || '<span style="color:var(--green);font-size:11px">ok</span>';
    return `<tr>
      <td><span class="cell-primary">${c.session_name||c.session_id||'—'}</span></td>
      <td class="mono">${c.model||'—'}</td>
      <td class="mono">${(c.prompt_tokens||0).toLocaleString()}</td>
      <td class="mono">${(c.completion_tokens||0).toLocaleString()}</td>
      <td class="mono">${fmtCost(c.cost_usd)}</td>
      <td class="mono">${c.latency_s != null ? c.latency_s.toFixed(2)+'s':'—'}</td>
      <td class="mono">${c.ttft_s && c.ttft_s > 0 ? c.ttft_s.toFixed(3)+'s':'—'}</td>
      <td>${st}</td>
      <td class="mono" style="color:var(--text3)">${fmtDate(c.created_at)}</td>
    </tr>`;
  }).join('');
}

// ── Monitoring ─────────────────────────────────────────────────────────────
async function renderMonitoring() {
  try {
    const res  = await fetch('/api/monitoring');
    const data = await res.json();
    const agg  = data.session_aggregates || [];
    if (!agg.length) return;
    const nm = {};
    for (const r of _runs) nm[r.id] = r.name || String(r.id);
    const labels = agg.map(r => nm[r.session_id] || String(r.session_id));
    const defOpts = () => ({
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false} },
      scales:{
        x:{ ticks:{color:'#71717a',font:{size:9}}, grid:{color:'#27272a'} },
        y:{ ticks:{color:'#71717a',font:{size:9}}, grid:{color:'#27272a'} }
      }
    });
    const mk = (id, type, label, data, color) => {
      if (_charts[id]) { _charts[id].destroy(); delete _charts[id]; }
      const ctx = document.getElementById(id)?.getContext('2d');
      if (!ctx) return;
      _charts[id] = new Chart(ctx, {
        type, data:{ labels, datasets:[{
          label, data, fill: type==='line',
          backgroundColor: color+'33', borderColor: color, borderWidth:1.5, tension:.3
        }]},
        options: defOpts()
      });
    };
    mk('chart-calls',   'bar',  'Calls',           agg.map(r=>r.total_calls||0),   '#6366f1');
    mk('chart-cost',    'bar',  'Cost (USD)',       agg.map(r=>r.total_cost||0),    '#22c55e');
    mk('chart-latency', 'line', 'Avg latency (s)',  agg.map(r=>r.avg_latency||0),   '#f59e0b');
    mk('chart-blocked', 'bar',  'Blocked',          agg.map(r=>r.blocked_calls||0), '#ef4444');
  } catch(e) { /* ignore */ }
}

// ── System ─────────────────────────────────────────────────────────────────
async function loadSystem() {
  try {
    const res  = await fetch('/api/system');
    const data = await res.json();
    document.getElementById('system-tbody').innerHTML = data.map(t => `<tr>
      <td class="mono cell-primary">${t.binary}</td>
      <td>${t.package}</td>
      <td style="color:var(--text3)">${t.role}</td>
      <td>${t.found
        ? '<span class="tool-ok">&#10003; found</span>'
        : '<span class="tool-missing">&#10007; missing</span>'}</td>
      <td class="mono" style="color:var(--text3)">${t.path||'—'}</td>
    </tr>`).join('');
  } catch(e) { /* ignore */ }
}

// ── Logs ───────────────────────────────────────────────────────────────────
async function loadLogs() {
  try {
    const res  = await fetch('/api/logs');
    const data = await res.json();
    const el   = document.getElementById('log-files');
    if (!data.length) { el.innerHTML='<span style="color:var(--text3);font-size:12px">No log files found</span>'; return; }
    el.innerHTML = data.map(f =>
      `<button class="log-file-btn" onclick="loadLogContent('${f.name}',this)">${f.name} <span style="color:var(--text3);font-size:10px">${f.size_kb}KB</span></button>`
    ).join('');
  } catch(e) { /* ignore */ }
}
async function loadLogContent(name, btn) {
  document.querySelectorAll('.log-file-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const el = document.getElementById('log-content');
  el.innerHTML = '<div style="color:var(--text3);font-size:12px">Loading…</div>';
  try {
    const data = await (await fetch(`/api/logs/${name}`)).json();
    if (data.error) { el.innerHTML=`<div style="color:var(--red)">${data.error}</div>`; return; }
    el.innerHTML = data.lines.map(l => {
      const cls = /error|Error|ERROR|FAIL/.test(l) ? 'log-error' : /warn|WARN|Warning/.test(l) ? 'log-warn' : '';
      return `<div class="log-line ${cls}">${l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
    }).join('');
    el.scrollTop = el.scrollHeight;
  } catch(e) { el.innerHTML='<div style="color:var(--red)">Failed to load log</div>'; }
}

// ── Detail panel ───────────────────────────────────────────────────────────
async function openDetail(id) {
  _selId = id;
  filterRuns();  // re-render with selection highlight

  document.getElementById('app').classList.add('dp-open');
  document.getElementById('dp-title').textContent = 'Loading…';
  document.getElementById('dp-body').innerHTML = '<div class="loading-state">Loading…</div>';

  try {
    const [sRes, lRes] = await Promise.all([
      fetch(`/api/session/${id}`),
      fetch(`/api/session/${id}/llm_calls`),
    ]);
    const sd  = await sRes.json();
    const llm = await lRes.json();
    const s   = sd.session || {};
    document.getElementById('dp-title').textContent = s.name || `Session #${id}`;
    _dpData = {
      run:      buildDPRun(s),
      faults:   buildDPFaults(sd.faults||[]),
      commands: buildDPCommands(sd.commands||[]),
      llm:      buildDPLLM(llm),
      events:   buildDPEvents(sd.events||[]),
    };
    showDPTab(_dpTab);
  } catch(e) {
    document.getElementById('dp-body').innerHTML = `<div style="color:var(--red);padding:8px">Failed: ${e.message}</div>`;
  }
}
function closeDetail() {
  _selId = null;
  document.getElementById('app').classList.remove('dp-open');
  filterRuns();
}
function switchDPTab(tab) {
  _dpTab = tab;
  document.querySelectorAll('.dp-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  showDPTab(tab);
}
function showDPTab(tab) {
  if (_dpData[tab] !== undefined) document.getElementById('dp-body').innerHTML = _dpData[tab];
}

// Detail builders
function buildDPRun(s) {
  let html = `<div class="dp-section">
    <div class="dp-section-title">Run info</div>
    <div class="dp-kv">
      <span class="dp-kv-k">ID</span>       <span class="dp-kv-v">${s.id||'—'}</span>
      <span class="dp-kv-k">Name</span>     <span class="dp-kv-v wrap">${s.name||'—'}</span>
      <span class="dp-kv-k">Status</span>   <span class="dp-kv-v">${statusDot(s.status)}${s.status||'—'}</span>
      <span class="dp-kv-k">Duration</span> <span class="dp-kv-v">${fmtDur(s.duration_s)}</span>
      <span class="dp-kv-k">Started</span>  <span class="dp-kv-v">${fmtDate(s.started_at)}</span>
      <span class="dp-kv-k">Stopped</span>  <span class="dp-kv-v">${fmtDate(s.stopped_at)}</span>
      <span class="dp-kv-k">Target</span>   <span class="dp-kv-v">${s.target_type||'local'}</span>
      <span class="dp-kv-k">Address</span>  <span class="dp-kv-v wrap">${s.target_addr||'—'}</span>
    </div>
  </div>`;
  if (s.results) {
    try {
      const r = typeof s.results === 'string' ? JSON.parse(s.results) : s.results;
      if (r && typeof r === 'object') html += buildMetrics(r);
    } catch(e) { /* skip */ }
  }
  return html;
}
function buildMetrics(m) {
  let rows = [];
  if (m.baseline && typeof m.baseline === 'object') {
    const b = flattenObj(m.baseline,'',{}), f = flattenObj(m.fault||m.chaos||{},'',{});
    const d = flattenObj(m.delta||{},'',{});
    const keys = [...new Set([...Object.keys(b), ...Object.keys(f)])];
    rows = keys
      .filter(k => { const v=b[k]; return typeof v==='number' || (typeof v==='string' && v!=='' && !isNaN(+v)); })
      .map(k => ({key:k, b:b[k], f:f[k], d:d[k]}));
  } else {
    rows = Object.keys(m).filter(k => k.startsWith('baseline_')).map(k => {
      const s = k.slice(9);
      return {key:s, b:m[k], f:m['chaos_'+s]||m['fault_'+s], d:m['delta_'+s]};
    });
  }
  if (!rows.length) return '';
  let html = `<div class="dp-section"><div class="dp-section-title">Metrics</div>
    <table class="metric-table">
      <thead><tr><th>Metric</th><th>Baseline</th><th>Fault</th><th>Delta</th></tr></thead><tbody>`;
  for (const r of rows) {
    const dv = r.d != null ? +r.d : null;
    const dc = dv != null ? (dv > 0 ? 'delta-pos' : dv < 0 ? 'delta-neg' : '') : '';
    const ds = dv != null ? (dv > 0 ? '+' : '') + fmtMetric(r.key, dv) : '—';
    html += `<tr><td>${r.key}</td><td>${fmtMetric(r.key,r.b)}</td><td>${fmtMetric(r.key,r.f)}</td><td class="${dc}">${ds}</td></tr>`;
  }
  return html + '</tbody></table></div>';
}
function buildDPFaults(faults) {
  if (!faults.length) return '<div style="color:var(--text3);font-size:12px;padding:4px">No faults recorded</div>';
  return faults.map(f => {
    let params = '';
    try {
      const p = typeof f.params==='string' ? JSON.parse(f.params) : (f.params||{});
      const flat = flattenObj(p,'',{});
      params = Object.entries(flat).map(([k,v]) =>
        `<span class="fault-pk">${k}</span><span class="fault-pv">${v}</span>`
      ).join('');
    } catch(e) {
      params = `<span class="fault-pk">raw</span><span class="fault-pv">${f.params||'—'}</span>`;
    }
    const cat = f.category||'other';
    return `<div class="fault-row">
      <div class="fault-row-header">
        ${chip(cat, f.kind||cat)}
        ${f.duration_s != null ? `<span style="font-size:11px;color:var(--text3)">${fmtDur(f.duration_s)}</span>` : ''}
      </div>
      <div class="fault-params">${params||'<span style="color:var(--text3);font-size:11px">no params</span>'}</div>
    </div>`;
  }).join('');
}
function buildDPCommands(commands) {
  if (!commands.length) return '<div style="color:var(--text3);font-size:12px;padding:4px">No commands recorded</div>';
  return commands.map((c,i) => {
    const cmd = typeof c==='string' ? c : (c.command||c.cmd||JSON.stringify(c));
    const res = typeof c==='object' ? (c.result||c.output||c.returncode!=null?c.returncode:'') : '';
    return `<div class="cmd-row">
      <div class="cmd-label">Command ${i+1}</div>
      <div class="cmd-text">${cmd}</div>
      ${res!=='' && res!=null ? `<div class="cmd-result">&#8594; ${res}</div>` : ''}
    </div>`;
  }).join('');
}
function buildDPLLM(calls) {
  if (!calls.length) return '<div style="color:var(--text3);font-size:12px;padding:4px">No LLM calls in this session</div>';
  return calls.map(c => {
    const blk = c.was_blocked  ? chip('process','blocked')  : '';
    const mod = c.was_modified ? chip('storage','modified') : '';
    return `<div class="llm-call-card">
      <div class="llm-call-header"><span class="llm-call-model">${c.model||'unknown'}</span>${blk}${mod}</div>
      <div class="llm-call-meta">
        <span>in <span class="val">${(c.prompt_tokens||0).toLocaleString()}</span></span>
        <span>out <span class="val">${(c.completion_tokens||0).toLocaleString()}</span></span>
        <span>cost <span class="val">${fmtCost(c.cost_usd)}</span></span>
        <span>lat <span class="val">${c.latency_s!=null?c.latency_s.toFixed(2)+'s':'—'}</span></span>
        ${c.ttft_s&&c.ttft_s>0?`<span>ttft <span class="val">${c.ttft_s.toFixed(3)}s</span></span>`:''}
      </div>
    </div>`;
  }).join('');
}
function buildDPEvents(events) {
  if (!events.length) return '<div style="color:var(--text3);font-size:12px;padding:4px">No events recorded</div>';
  return events.map(e => {
    const lvl = (e.level||'info').toLowerCase();
    return `<div class="evt-row">
      <span class="evt-time">${fmtDate(e.timestamp||e.created_at)}</span>
      <span class="evt-level ${lvl}">${lvl}</span>
      <span class="evt-msg">${(e.message||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')}</span>
    </div>`;
  }).join('');
}

boot();
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
