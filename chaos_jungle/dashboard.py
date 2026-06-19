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
:root{
  --bg:#09090b; --surface:#111113; --panel:#141416;
  --border:#27272a; --border2:#3f3f46;
  --text:#fafafa; --text2:#a1a1aa; --text3:#71717a;
  --accent:#6366f1; --accent-dim:rgba(99,102,241,.15);
  --green:#22c55e; --red:#ef4444; --yellow:#f59e0b; --cyan:#06b6d4;
  --radius:6px; --font:'Inter',system-ui,sans-serif; --mono:'JetBrains Mono',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;line-height:1.5}

/* ── grid layout ── */
.app{display:grid;grid-template-columns:48px 220px 1fr;grid-template-rows:100vh;height:100vh;overflow:hidden}
.app.dp-open{grid-template-columns:48px 220px 1fr 380px}

/* ── sidebar ── */
.sidebar{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;align-items:center;padding:10px 0;gap:2px;z-index:10}
.sb-logo{width:28px;height:28px;background:var(--accent);border-radius:7px;display:flex;align-items:center;justify-content:center;margin-bottom:8px;font-size:11px;font-weight:700;color:#fff;letter-spacing:-.5px;flex-shrink:0}
.sb-sep{width:28px;height:1px;background:var(--border);margin:4px 0}
.sb-spacer{flex:1}
.sb-btn{width:36px;height:36px;border-radius:var(--radius);border:none;background:transparent;color:var(--text3);display:flex;align-items:center;justify-content:center;cursor:pointer;position:relative;transition:background .15s,color .15s}
.sb-btn:hover{background:var(--panel);color:var(--text2)}
.sb-btn.active{background:var(--accent-dim);color:var(--accent)}
.sb-btn[data-color].active{color:var(--btn-color);background:var(--btn-bg)}
.sb-btn[title]:hover::after{content:attr(title);position:absolute;left:calc(100% + 8px);top:50%;transform:translateY(-50%);background:#1e1e20;border:1px solid var(--border2);border-radius:4px;padding:3px 8px;font-size:11px;color:var(--text);white-space:nowrap;z-index:100;pointer-events:none}

/* ── left panel ── */
.left-panel{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.lp-header{padding:12px 12px 8px;border-bottom:1px solid var(--border);flex-shrink:0}
.lp-title{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px}
.lp-search{width:100%;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:5px 8px;color:var(--text);font-family:var(--font);font-size:12px;outline:none}
.lp-search:focus{border-color:var(--accent)}
.lp-search::placeholder{color:var(--text3)}
.lp-list{flex:1;overflow-y:auto;padding:4px 0}
.lp-list::-webkit-scrollbar{width:3px}
.lp-list::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
/* tree nodes */
.tree-exp{padding:6px 10px 6px 8px;cursor:pointer;border-left:2px solid transparent;transition:background .1s;display:flex;align-items:center;gap:5px}
.tree-exp:hover{background:rgba(255,255,255,.03)}
.tree-exp.active{background:var(--accent-dim);border-left-color:var(--accent)}
.tree-toggle{font-size:9px;color:var(--text3);width:12px;flex-shrink:0;transition:transform .15s;user-select:none;text-align:center}
.tree-toggle.open{transform:rotate(90deg)}
.tree-exp-name{font-size:12px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.tree-exp-meta{display:flex;align-items:center;gap:4px;flex-shrink:0}
.tree-exp-count{font-size:10px;color:var(--text3)}
.tree-exp-rate{font-size:10px;font-weight:600}
.tree-exp-rate.ok{color:var(--green)} .tree-exp-rate.warn{color:var(--yellow)} .tree-exp-rate.fail{color:var(--red)}
/* tree run leaves */
.tree-run{padding:4px 8px 4px 26px;cursor:pointer;display:flex;align-items:center;gap:5px;border-left:2px solid transparent;transition:background .1s}
.tree-run:hover{background:rgba(255,255,255,.03)}
.tree-run.selected{background:var(--accent-dim);border-left-color:var(--accent)}
.tree-run-id{font-size:10px;color:var(--text3);font-family:var(--mono);flex-shrink:0}
.tree-run-name{font-size:11px;color:var(--text2);flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tree-run-dur{font-size:10px;color:var(--text3);flex-shrink:0;margin-left:auto}

/* ── main ── */
.main{display:flex;flex-direction:column;overflow:hidden;background:var(--bg);min-width:0}
.view-panel{display:none;flex-direction:column;flex:1;overflow:hidden}
.view-panel.active{display:flex}

/* filter bar */
.filter-bar{display:flex;align-items:center;gap:8px;padding:10px 18px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;min-height:50px}
.view-title{font-size:14px;font-weight:600;color:var(--text);margin-right:2px}
.view-sub{font-size:11px;color:var(--text3)}
.filter-input{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:5px 10px;color:var(--text);font-family:var(--font);font-size:12px;outline:none;min-width:160px}
.filter-input:focus{border-color:var(--accent)}
.filter-input::placeholder{color:var(--text3)}
.filter-select{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:5px 8px;color:var(--text);font-family:var(--font);font-size:12px;outline:none;cursor:pointer}
.filter-select:focus{border-color:var(--accent)}
.ml-auto{margin-left:auto}
.count-badge{font-size:11px;color:var(--text3);background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:2px 8px;white-space:nowrap}

/* KPI bar */
.kpi-bar{display:flex;gap:1px;flex-shrink:0;background:var(--border);border-bottom:1px solid var(--border)}
.kpi-card{flex:1;background:var(--bg);padding:10px 14px}
.kpi-label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.kpi-value{font-size:20px;font-weight:600;color:var(--text);font-variant-numeric:tabular-nums;line-height:1.2}
.kpi-sub{font-size:10px;color:var(--text3);margin-top:1px}

/* outcome bar */
.outcome-bar{display:flex;height:3px;flex-shrink:0}
.outcome-pass{background:var(--green)}
.outcome-fail{background:var(--red)}
.outcome-run{background:var(--accent)}

/* table */
.table-wrap{flex:1;overflow-y:auto}
.table-wrap::-webkit-scrollbar{width:5px}
.table-wrap::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
table{width:100%;border-collapse:collapse;font-size:12px}
thead th{position:sticky;top:0;z-index:2;background:var(--surface);padding:7px 14px;text-align:left;font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}
thead th:hover{color:var(--text2)}
thead th.sort-asc::after{content:' ↑';color:var(--accent)}
thead th.sort-desc::after{content:' ↓';color:var(--accent)}
tbody tr{border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
tbody tr:hover{background:rgba(255,255,255,.03)}
tbody tr.selected{background:var(--accent-dim)}
td{padding:8px 14px;color:var(--text2);white-space:nowrap;vertical-align:middle}
td.mono{font-family:var(--mono);font-size:11px}
.cell-primary{color:var(--text);font-weight:500}

/* status */
.sdot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle;flex-shrink:0}
.sdot.running{background:var(--accent);box-shadow:0 0 0 2px var(--accent-dim);animation:pulse 2s infinite}
.sdot.done{background:var(--green)} .sdot.error{background:var(--red)} .sdot.pending{background:var(--text3)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

/* chips */
.chips{display:flex;gap:3px;flex-wrap:wrap;max-width:260px}
.chip{display:inline-flex;align-items:center;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:500;white-space:nowrap}
.target-badge{font-family:var(--mono);font-size:10px;color:var(--text3);background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:1px 6px}

/* param tag */
.param-tag{display:inline-block;font-family:var(--mono);font-size:10px;color:var(--cyan);background:rgba(6,182,212,.1);border-radius:3px;padding:1px 6px;margin-right:3px}

/* metric delta */
.delta-up{color:var(--red)} .delta-dn{color:var(--green)} .delta-ok{color:var(--text3)}

/* empty / loading */
.empty-state{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--text3)}
.empty-icon{font-size:26px;opacity:.35} .empty-text{font-size:12px}
.loading-state{flex:1;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:12px}

/* overview category grid */
.cat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;padding:14px 18px;flex-shrink:0}
.cat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;cursor:pointer;transition:border-color .15s}
.cat-card:hover{border-color:var(--border2)}
.cat-card-name{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
.cat-card-count{font-size:22px;font-weight:600;color:var(--text);font-variant-numeric:tabular-nums;line-height:1}
.cat-card-sub{font-size:10px;color:var(--text3);margin-top:3px}

/* ── detail panel ── */
.detail-panel{background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;min-width:0}
.dp-topbar{display:flex;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);gap:8px;flex-shrink:0}
.dp-title{font-size:13px;font-weight:600;color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dp-close{width:22px;height:22px;border:none;background:transparent;color:var(--text3);font-size:18px;cursor:pointer;border-radius:4px;display:flex;align-items:center;justify-content:center;line-height:1}
.dp-close:hover{background:var(--border);color:var(--text)}
.dp-tabs{display:flex;border-bottom:1px solid var(--border);padding:0 14px;flex-shrink:0;overflow-x:auto;scrollbar-width:none}
.dp-tabs::-webkit-scrollbar{display:none}
.dp-tab{padding:8px 10px;font-size:12px;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;margin-bottom:-1px;transition:color .12s}
.dp-tab:hover{color:var(--text2)} .dp-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.dp-body{flex:1;overflow-y:auto;padding:14px}
.dp-body::-webkit-scrollbar{width:3px}
.dp-body::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}

/* detail content */
.dp-section{margin-bottom:18px}
.dp-section-title{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
.dp-kv{display:grid;grid-template-columns:auto 1fr;gap:3px 10px}
.dp-kv-k{font-size:11px;color:var(--text3);padding:2px 0;white-space:nowrap}
.dp-kv-v{font-size:11px;color:var(--text);font-family:var(--mono);padding:2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dp-kv-v.wrap{font-family:var(--font);white-space:normal;word-break:break-all}
.fault-row{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;margin-bottom:8px}
.fault-row-hdr{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.fault-params{display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
.fault-pk{font-size:11px;color:var(--text3)} .fault-pv{font-size:11px;color:var(--text2);font-family:var(--mono);overflow:hidden;text-overflow:ellipsis}
.cmd-row{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;margin-bottom:6px}
.cmd-label{font-size:10px;color:var(--text3);margin-bottom:3px}
.cmd-text{font-family:var(--mono);font-size:11px;color:var(--text2);word-break:break-all}
.cmd-result{font-family:var(--mono);font-size:10px;color:var(--text3);margin-top:3px}
.evt-row{display:flex;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);align-items:flex-start}
.evt-time{font-size:10px;color:var(--text3);font-family:var(--mono);white-space:nowrap;flex-shrink:0;padding-top:1px}
.evt-lvl{font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;flex-shrink:0;text-transform:uppercase;margin-top:1px}
.evt-lvl.info{color:var(--accent);background:var(--accent-dim)} .evt-lvl.warn{color:var(--yellow);background:rgba(245,158,11,.12)} .evt-lvl.error{color:var(--red);background:rgba(239,68,68,.12)}
.evt-msg{font-size:11px;color:var(--text2);word-break:break-word}
.metric-tbl{width:100%;border-collapse:collapse;font-size:11px}
.metric-tbl th{padding:4px 8px;text-align:left;color:var(--text3);font-weight:600;border-bottom:1px solid var(--border);font-size:10px}
.metric-tbl td{padding:4px 8px;border-bottom:1px solid var(--border);font-family:var(--mono)}
.metric-tbl td:first-child{color:var(--text3);font-family:var(--font);font-size:10px}
.llm-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;margin-bottom:8px}
.llm-card-hdr{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.llm-card-model{font-size:12px;font-weight:600;color:var(--text)}
.llm-meta{display:flex;gap:12px;flex-wrap:wrap}
.llm-meta span{font-size:11px;color:var(--text3)}
.llm-meta .val{color:var(--text2);font-family:var(--mono)}

/* system / logs */
.tool-ok{color:var(--green)} .tool-miss{color:var(--red)}
.log-files{display:flex;gap:6px;flex-wrap:wrap;padding:10px 18px;flex-shrink:0;border-bottom:1px solid var(--border)}
.log-btn{padding:4px 10px;border-radius:var(--radius);border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer;font-family:var(--font);transition:all .12s}
.log-btn:hover{border-color:var(--accent);color:var(--accent)} .log-btn.active{border-color:var(--accent);background:var(--accent-dim);color:var(--accent)}
.log-content{flex:1;overflow-y:auto;padding:10px 18px;font-family:var(--mono);font-size:11px;color:var(--text2);line-height:1.7}
.log-err{color:var(--red)} .log-warn{color:var(--yellow)}

::-webkit-scrollbar{width:5px;height:5px} ::-webkit-scrollbar-track{background:transparent} ::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* ── scenario accordion ── */
.sc-row{display:grid;grid-template-columns:14px 1fr auto auto;align-items:center;gap:14px;padding:11px 18px;cursor:pointer;border-bottom:1px solid var(--border);transition:background .1s;user-select:none}
.sc-row:hover{background:rgba(255,255,255,.03)}
.sc-row.sc-open{background:var(--surface);border-left:3px solid var(--accent);padding-left:15px}
.sc-toggle{font-size:9px;color:var(--text3);line-height:1;text-align:center;transition:transform .15s;flex-shrink:0}
.sc-name{font-size:13px;font-weight:500;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sc-stats{display:flex;align-items:center;gap:6px;flex-shrink:0}
.sc-faults{display:flex;align-items:center;gap:4px;flex-shrink:0}
.sc-badge{font-size:10px;color:var(--text3);background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:2px 8px;white-space:nowrap}
.sc-body{border-bottom:2px solid var(--accent-dim);padding:16px 20px 20px;background:rgba(99,102,241,.025)}
.sc-kpi-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.sc-kpi-card{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:10px 16px;min-width:100px}
.sc-kpi-label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px}
.sc-kpi-value{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.2}
.sc-run-tbl{width:100%;border-collapse:collapse;font-size:12px}
.sc-run-tbl th{padding:6px 10px;text-align:left;font-size:10px;color:var(--text3);font-weight:600;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);white-space:nowrap;background:var(--bg)}
.sc-run-tbl tbody tr{border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
.sc-run-tbl tbody tr:hover{background:rgba(255,255,255,.03)}
.sc-run-tbl tbody tr.selected{background:var(--accent-dim)}
.sc-run-tbl td{padding:8px 10px;color:var(--text2);vertical-align:middle}

/* ── run detail two-row header ── */
.run-hdr{flex-shrink:0;border-bottom:1px solid var(--border)}
.run-hdr-top{display:flex;align-items:center;gap:10px;padding:12px 18px 6px;flex-wrap:wrap}
.run-hdr-sub{display:flex;align-items:center;gap:8px;padding:4px 18px 11px;flex-wrap:wrap}
.run-back{background:transparent;border:1px solid var(--border);border-radius:var(--radius);color:var(--text3);padding:3px 10px;cursor:pointer;font-size:11px;font-family:var(--font);flex-shrink:0;line-height:1.6}
.run-back:hover{border-color:var(--text3);color:var(--text2)}
.run-name{font-size:15px;font-weight:600;color:var(--text);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── charts ── */
.charts-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:14px 18px;flex-shrink:0;border-bottom:1px solid var(--border)}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:13px 15px;position:relative;height:165px}
.chart-title{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
.charts-2col{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
.charts-2col .chart-card{height:240px}
/* token bar */
.tok-bar-wrap{margin:10px 0 6px}
.tok-bar{display:flex;height:7px;border-radius:4px;overflow:hidden;background:var(--border)}
.tok-seg-p{background:#6366f1}
.tok-seg-c{background:#22c55e}
.tok-legend{display:flex;gap:14px;margin-top:5px}
.tok-leg{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--text3)}
.tok-leg-dot{width:8px;height:8px;border-radius:2px;flex-shrink:0}
</style>
</head>
<body>
<div class="app" id="app">

<!-- ── Sidebar ── -->
<nav class="sidebar">
  <div class="sb-logo">CJ</div>
  <button class="sb-btn active" id="sb-overview" onclick="setView('overview')" title="Overview">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
  </button>
  <div class="sb-sep"></div>
  <button class="sb-btn" id="sb-network"  onclick="setView('network')"  title="Network faults"  style="--btn-color:#60a5fa;--btn-bg:rgba(37,99,235,.18)">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M5 12.55a11 11 0 0114.08 0M1.42 9a16 16 0 0121.16 0M8.53 16.11a6 6 0 016.95 0M12 20h.01"/></svg>
  </button>
  <button class="sb-btn" id="sb-resource" onclick="setView('resource')" title="Resource faults" style="--btn-color:#fb923c;--btn-bg:rgba(234,88,12,.18)">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>
  </button>
  <button class="sb-btn" id="sb-process"  onclick="setView('process')"  title="Process faults"  style="--btn-color:#f87171;--btn-bg:rgba(220,38,38,.18)">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 1v3M12 20v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M1 12h3M20 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/></svg>
  </button>
  <button class="sb-btn" id="sb-storage"  onclick="setView('storage')"  title="Storage faults"  style="--btn-color:#fbbf24;--btn-bg:rgba(217,119,6,.18)">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
  </button>
  <button class="sb-btn" id="sb-state"    onclick="setView('state')"    title="State faults"    style="--btn-color:#22d3ee;--btn-bg:rgba(8,145,178,.18)">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><line x1="12" y1="12" x2="12" y2="15"/><circle cx="12" cy="17" r="1"/></svg>
  </button>
  <button class="sb-btn" id="sb-llm"      onclick="setView('llm')"      title="LLM / MCP faults" style="--btn-color:#a78bfa;--btn-bg:rgba(124,58,237,.18)">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/><line x1="9" y1="10" x2="9" y2="10"/><line x1="12" y1="10" x2="12" y2="10"/><line x1="15" y1="10" x2="15" y2="10"/></svg>
  </button>
  <button class="sb-btn" id="sb-skill"    onclick="setView('skill')"    title="Skill faults"    style="--btn-color:#818cf8;--btn-bg:rgba(79,70,229,.18)">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 00-1.4 0l-6.4 6.4a1 1 0 000 1.4l3 3a1 1 0 001.4 0l6.4-6.4a1 1 0 000-1.4z"/><path d="M5 17L3 21l4-2"/><line x1="14" y1="7" x2="17" y2="10"/></svg>
  </button>
  <div class="sb-sep"></div>
  <button class="sb-btn" id="sb-system"   onclick="setView('system')"   title="System tools">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
  </button>
  <button class="sb-btn" id="sb-logs"     onclick="setView('logs')"     title="Logs">
    <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
  </button>
  <div class="sb-spacer"></div>
</nav>

<!-- ── Left panel: experiments ── -->
<aside class="left-panel">
  <div class="lp-header">
    <div class="lp-title">Experiments</div>
    <input class="lp-search" type="text" placeholder="Filter…" id="exp-search" oninput="filterExps()"/>
  </div>
  <div class="lp-list" id="exp-list"><div class="loading-state">Loading…</div></div>
</aside>

<!-- ── Main ── -->
<main class="main">

  <!-- Overview -->
  <div class="view-panel active" id="view-overview">
    <div class="filter-bar">
      <span class="view-title">Overview</span>
      <input class="filter-input" type="text" placeholder="Search runs…" id="ov-search" oninput="filterOv()"/>
      <span class="ml-auto count-badge" id="ov-count">— runs</span>
    </div>
    <div class="kpi-bar" id="ov-kpi"></div>
    <div class="charts-row" id="ov-charts">
      <div class="chart-card"><div class="chart-title">Scenario pass rate</div><canvas id="ch-pass"></canvas></div>
      <div class="chart-card"><div class="chart-title">Quality / duration trend</div><canvas id="ch-quality"></canvas></div>
      <div class="chart-card"><div class="chart-title">Oracle assertions</div><canvas id="ch-oracles"></canvas></div>
    </div>
    <div id="cat-grid" style="display:none"></div>
    <div class="outcome-bar" id="ov-outcome" style="margin:0 18px"></div>
    <div class="table-wrap" id="sc-list">
      <div class="loading-state">Loading…</div>
    </div>
  </div>

  <!-- Per-category views: network, resource, process, storage, state, llm, skill -->
  <div class="view-panel" id="view-network">
    <div class="filter-bar">
      <span class="view-title" style="color:#60a5fa">Network</span>
      <span class="view-sub">fault injection</span>
      <input class="filter-input" type="text" placeholder="Search…" id="net-search" oninput="filterCat('network')"/>
      <span class="ml-auto count-badge" id="net-count">—</span>
    </div>
    <div class="kpi-bar" id="net-kpi"></div>
    <div class="table-wrap"><table><thead><tr>
      <th>#</th><th>Experiment</th><th>Status</th><th>Fault kind</th>
      <th>Key param</th><th>Target</th><th>Duration</th><th>Started</th>
    </tr></thead><tbody id="net-tbody"></tbody></table>
    <div class="empty-state" id="net-empty" style="display:none"><div class="empty-icon">🌐</div><div class="empty-text">No network fault runs</div></div>
    </div>
  </div>

  <div class="view-panel" id="view-resource">
    <div class="filter-bar">
      <span class="view-title" style="color:#fb923c">Resource</span>
      <span class="view-sub">fault injection</span>
      <input class="filter-input" type="text" placeholder="Search…" id="res-search" oninput="filterCat('resource')"/>
      <span class="ml-auto count-badge" id="res-count">—</span>
    </div>
    <div class="kpi-bar" id="res-kpi"></div>
    <div class="table-wrap"><table><thead><tr>
      <th>#</th><th>Experiment</th><th>Status</th><th>Fault kind</th>
      <th>Key param</th><th>Target</th><th>Duration</th><th>Started</th>
    </tr></thead><tbody id="res-tbody"></tbody></table>
    <div class="empty-state" id="res-empty" style="display:none"><div class="empty-icon">💻</div><div class="empty-text">No resource fault runs</div></div>
    </div>
  </div>

  <div class="view-panel" id="view-process">
    <div class="filter-bar">
      <span class="view-title" style="color:#f87171">Process</span>
      <span class="view-sub">fault injection</span>
      <input class="filter-input" type="text" placeholder="Search…" id="proc-search" oninput="filterCat('process')"/>
      <span class="ml-auto count-badge" id="proc-count">—</span>
    </div>
    <div class="kpi-bar" id="proc-kpi"></div>
    <div class="table-wrap"><table><thead><tr>
      <th>#</th><th>Experiment</th><th>Status</th><th>Fault kind</th>
      <th>Key param</th><th>Target</th><th>Duration</th><th>Started</th>
    </tr></thead><tbody id="proc-tbody"></tbody></table>
    <div class="empty-state" id="proc-empty" style="display:none"><div class="empty-icon">⚙️</div><div class="empty-text">No process fault runs</div></div>
    </div>
  </div>

  <div class="view-panel" id="view-storage">
    <div class="filter-bar">
      <span class="view-title" style="color:#fbbf24">Storage</span>
      <span class="view-sub">fault injection</span>
      <input class="filter-input" type="text" placeholder="Search…" id="stor-search" oninput="filterCat('storage')"/>
      <span class="ml-auto count-badge" id="stor-count">—</span>
    </div>
    <div class="kpi-bar" id="stor-kpi"></div>
    <div class="table-wrap"><table><thead><tr>
      <th>#</th><th>Experiment</th><th>Status</th><th>Fault kind</th>
      <th>Key param</th><th>Target</th><th>Duration</th><th>Started</th>
    </tr></thead><tbody id="stor-tbody"></tbody></table>
    <div class="empty-state" id="stor-empty" style="display:none"><div class="empty-icon">💾</div><div class="empty-text">No storage fault runs</div></div>
    </div>
  </div>

  <div class="view-panel" id="view-state">
    <div class="filter-bar">
      <span class="view-title" style="color:#22d3ee">State</span>
      <span class="view-sub">fault injection</span>
      <input class="filter-input" type="text" placeholder="Search…" id="state-search" oninput="filterCat('state')"/>
      <span class="ml-auto count-badge" id="state-count">—</span>
    </div>
    <div class="kpi-bar" id="state-kpi"></div>
    <div class="table-wrap"><table><thead><tr>
      <th>#</th><th>Experiment</th><th>Status</th><th>Fault kind</th>
      <th>Key param</th><th>Target</th><th>Duration</th><th>Started</th>
    </tr></thead><tbody id="state-tbody"></tbody></table>
    <div class="empty-state" id="state-empty" style="display:none"><div class="empty-icon">🗄️</div><div class="empty-text">No state fault runs</div></div>
    </div>
  </div>

  <div class="view-panel" id="view-llm">
    <div class="filter-bar">
      <span class="view-title" style="color:#a78bfa">LLM / MCP</span>
      <span class="view-sub">fault injection</span>
      <input class="filter-input" type="text" placeholder="Search…" id="llm-search" oninput="filterCat('llm')"/>
      <span class="ml-auto count-badge" id="llm-count">—</span>
    </div>
    <div class="kpi-bar" id="llm-kpi"></div>
    <div class="table-wrap"><table><thead><tr>
      <th>#</th><th>Experiment</th><th>Status</th><th>Fault kind</th>
      <th>Key param</th><th>Target</th><th>Duration</th><th>Started</th>
    </tr></thead><tbody id="llm-tbody"></tbody></table>
    <div class="empty-state" id="llm-empty" style="display:none"><div class="empty-icon">🤖</div><div class="empty-text">No LLM fault runs</div></div>
    </div>
  </div>

  <div class="view-panel" id="view-skill">
    <div class="filter-bar">
      <span class="view-title" style="color:#818cf8">Skill</span>
      <span class="view-sub">fault injection</span>
      <input class="filter-input" type="text" placeholder="Search…" id="skill-search" oninput="filterCat('skill')"/>
      <span class="ml-auto count-badge" id="skill-count">—</span>
    </div>
    <div class="kpi-bar" id="skill-kpi"></div>
    <div class="table-wrap"><table><thead><tr>
      <th>#</th><th>Experiment</th><th>Status</th><th>Fault kind</th>
      <th>Key param</th><th>Target</th><th>Duration</th><th>Started</th>
    </tr></thead><tbody id="skill-tbody"></tbody></table>
    <div class="empty-state" id="skill-empty" style="display:none"><div class="empty-icon">🛠️</div><div class="empty-text">No skill fault runs</div></div>
    </div>
  </div>

  <!-- ── Run detail (main screen) ── -->
  <div class="view-panel" id="view-run">
    <div class="run-hdr" id="run-bar">
      <div class="run-hdr-top">
        <button class="run-back" onclick="setView('overview')">&#8592; Back</button>
        <span class="run-name" id="run-title">—</span>
        <span id="run-status-badge"></span>
      </div>
      <div class="run-hdr-sub">
        <span id="run-fault-chips"></span>
        <span id="run-meta" style="font-size:11px;color:var(--text3);margin-left:auto"></span>
      </div>
    </div>
    <div class="dp-tabs" id="run-tabs" style="padding:0 18px;display:flex;overflow-x:auto;border-bottom:1px solid var(--border);flex-shrink:0;scrollbar-width:none">
      <div class="dp-tab active" data-tab="run"      onclick="switchRunTab('run')">Overview</div>
      <div class="dp-tab"        data-tab="faults"   onclick="switchRunTab('faults')">Faults</div>
      <div class="dp-tab"        data-tab="metrics"  onclick="switchRunTab('metrics')">Metrics</div>
      <div class="dp-tab"        data-tab="llm"      onclick="switchRunTab('llm')">LLM Calls</div>
      <div class="dp-tab"        data-tab="tools"    onclick="switchRunTab('tools')">Tool Calls</div>
      <div class="dp-tab"        data-tab="commands" onclick="switchRunTab('commands')">Commands</div>
      <div class="dp-tab"        data-tab="events"   onclick="switchRunTab('events')">Events</div>
    </div>
    <div id="run-body" style="flex:1;overflow-y:auto;padding:20px 24px;width:100%">
      <div class="empty-state"><div class="empty-icon">&#8598;</div><div class="empty-text">Select a run from the left panel</div></div>
    </div>
  </div>

  <!-- System tools -->
  <div class="view-panel" id="view-system">
    <div class="filter-bar"><span class="view-title">System Tools</span></div>
    <div class="table-wrap"><table><thead><tr>
      <th>Binary</th><th>Package</th><th>Role</th><th>Status</th><th>Path</th>
    </tr></thead><tbody id="sys-tbody"></tbody></table></div>
  </div>

  <!-- Logs -->
  <div class="view-panel" id="view-logs">
    <div class="filter-bar"><span class="view-title">Logs</span></div>
    <div class="log-files" id="log-files"></div>
    <div class="log-content" id="log-content">
      <div class="empty-state" style="height:100%"><div class="empty-icon">📄</div><div class="empty-text">Select a log file</div></div>
    </div>
  </div>

</main>

<!-- ── Detail panel ── -->
<aside class="detail-panel" id="detail-panel">
  <div class="dp-topbar">
    <span class="dp-title" id="dp-title">—</span>
    <button class="dp-close" onclick="closeDetail()">&#215;</button>
  </div>
  <div class="dp-tabs">
    <div class="dp-tab active" data-tab="run"      onclick="switchDPTab('run')">Run</div>
    <div class="dp-tab"        data-tab="faults"   onclick="switchDPTab('faults')">Faults</div>
    <div class="dp-tab"        data-tab="metrics"  onclick="switchDPTab('metrics')">Metrics</div>
    <div class="dp-tab"        data-tab="commands" onclick="switchDPTab('commands')">Commands</div>
    <div class="dp-tab"        data-tab="llm"      onclick="switchDPTab('llm')">LLM calls</div>
    <div class="dp-tab"        data-tab="events"   onclick="switchDPTab('events')">Events</div>
  </div>
  <div class="dp-body" id="dp-body">
    <div class="empty-state" style="height:100%"><div class="empty-icon">↖</div><div class="empty-text">Click a run to inspect</div></div>
  </div>
</aside>

</div>
<script>
// ── State ──────────────────────────────────────────────────────────────────
let _runs    = [];
let _exps    = {};
let _expSel  = null;
let _expOpen = new Set();
let _scOpen  = new Set();
let _selId   = null;
let _ovSort  = {col:'id', asc:false};
let _dpTab   = 'run';
let _dpData  = {};
let _rawLLM       = [];
let _rawResults   = [];
let _rawOracles   = [];
let _rawResources = [];
let _rawImpact    = null;
let _rawFaults    = [];

// ── Chart management ────────────────────────────────────────────────────────
const _charts = {};
function _mkChart(id, cfg) {
  if (_charts[id]) { try { _charts[id].destroy(); } catch(e) {} delete _charts[id]; }
  const el = document.getElementById(id);
  if (!el) return;
  _charts[id] = new Chart(el, cfg);
}
// Dark-theme Chart.js defaults
Chart.defaults.color       = '#71717a';
Chart.defaults.borderColor = '#27272a';
Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
Chart.defaults.font.size   = 11;
Chart.defaults.plugins.tooltip.backgroundColor = '#1e1e20';
Chart.defaults.plugins.tooltip.borderColor     = '#27272a';
Chart.defaults.plugins.tooltip.borderWidth     = 1;
Chart.defaults.plugins.tooltip.titleColor      = '#fafafa';
Chart.defaults.plugins.tooltip.bodyColor       = '#a1a1aa';
Chart.defaults.plugins.legend.labels.boxWidth  = 10;
Chart.defaults.plugins.legend.labels.padding   = 12;

// ── Category config ────────────────────────────────────────────────────────
const CATS = {
  network:  {color:'#2563eb', bg:'rgba(37,99,235,.15)',  label:'Network',  id:'net',   search:'net-search',  count:'net-count',  kpi:'net-kpi',  tbody:'net-tbody',  empty:'net-empty'},
  resource: {color:'#ea580c', bg:'rgba(234,88,12,.15)',  label:'Resource', id:'res',   search:'res-search',  count:'res-count',  kpi:'res-kpi',  tbody:'res-tbody',  empty:'res-empty'},
  process:  {color:'#dc2626', bg:'rgba(220,38,38,.15)',  label:'Process',  id:'proc',  search:'proc-search', count:'proc-count', kpi:'proc-kpi', tbody:'proc-tbody', empty:'proc-empty'},
  storage:  {color:'#d97706', bg:'rgba(217,119,6,.15)',  label:'Storage',  id:'stor',  search:'stor-search', count:'stor-count', kpi:'stor-kpi', tbody:'stor-tbody', empty:'stor-empty'},
  state:    {color:'#0891b2', bg:'rgba(8,145,178,.15)',  label:'State',    id:'state', search:'state-search',count:'state-count',kpi:'state-kpi',tbody:'state-tbody',empty:'state-empty'},
  llm:      {color:'#7c3aed', bg:'rgba(124,58,237,.15)', label:'LLM/MCP',  id:'llm',   search:'llm-search',  count:'llm-count',  kpi:'llm-kpi',  tbody:'llm-tbody',  empty:'llm-empty'},
  skill:    {color:'#4f46e5', bg:'rgba(79,70,229,.15)',  label:'Skill',    id:'skill', search:'skill-search',count:'skill-count',kpi:'skill-kpi',tbody:'skill-tbody',empty:'skill-empty'},
};

// Key params to extract per category
const CAT_PARAMS = {
  network:  p => p.delay_ms != null   ? p.delay_ms+'ms delay'
               : p.loss_percent != null ? p.loss_percent+'% loss'
               : p.bandwidth_kbps != null ? p.bandwidth_kbps+' kbps'
               : p.corrupt_percent != null ? p.corrupt_percent+'% corrupt' : null,
  resource: p => p.workers != null ? p.workers+' workers'
               : p.percent != null ? p.percent+'%'
               : p.timeout != null ? p.timeout+'s' : null,
  process:  p => p.target || p.process_name || p.service || null,
  storage:  p => p.target_path || p.path || (p.size_mb != null ? p.size_mb+'MB' : null),
  state:    p => p.target || p.corruption_type || p.key_pattern || null,
  llm:      p => p.latency_ms != null ? p.latency_ms+'ms lat'
               : p.error_rate != null ? p.error_rate+'% err'
               : p.budget_usd != null ? '$'+p.budget_usd : null,
  skill:    p => p.skill_name || p.target || null,
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
function fmtCost(v) { return (!v) ? '—' : v < 0.001 ? '<$0.001' : '$'+v.toFixed(4); }
function fmtMetric(name, value) {
  if (value == null || value === '') return '—';
  const n = Number(value);
  if (isNaN(n)) return String(value);
  const k = String(name);
  if (k.endsWith('_pct')||k.endsWith('_percent')) return n.toFixed(1)+'%';
  if (k.endsWith('_mb')) return n >= 1024 ? (n/1024).toFixed(2)+'GB' : n.toFixed(1)+'MB';
  if (k.endsWith('_bytes')||k === 'bytes') {
    if (n >= 1073741824) return (n/1073741824).toFixed(2)+'GB';
    if (n >= 1048576)    return (n/1048576).toFixed(1)+'MB';
    if (n >= 1024)       return (n/1024).toFixed(1)+'KB';
    return n+'B';
  }
  if (k.endsWith('_s')||k.endsWith('_seconds')) return n >= 1 ? n.toFixed(2)+'s' : (n*1000).toFixed(0)+'ms';
  if (k === 'tokens_per_second') return n.toFixed(1)+' tok/s';
  return n.toLocaleString(undefined,{maximumFractionDigits:4});
}
function flattenObj(obj, prefix, out) {
  out = out||{};
  if (!obj||typeof obj !== 'object') return out;
  for (const k in obj) {
    const v = obj[k], full = prefix ? prefix+'.'+k : k;
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) flattenObj(v, full, out);
    else out[full] = v;
  }
  return out;
}
function sdot(st) {
  const c = st === 'running' ? 'running' : st === 'done' ? 'done' : st === 'error' ? 'error' : 'pending';
  return `<span class="sdot ${c}"></span>`;
}
function chip(cat, lbl) {
  const c = (CATS[cat]||{color:'#64748b'}).color, bg = (CATS[cat]||{bg:'rgba(100,116,139,.15)'}).bg;
  return `<span class="chip" style="color:${c};background:${bg}">${lbl}</span>`;
}
function faultChips(faults) {
  if (!faults||!faults.length) return '<span style="color:var(--text3);font-size:11px">—</span>';
  const seen = new Set();
  let h = '<div class="chips">';
  for (const f of faults) {
    const cat = f.category||'other', key = cat+f.kind;
    if (seen.has(key)) continue; seen.add(key);
    h += chip(cat, f.kind||cat);
  }
  return h+'</div>';
}
function targetBadge(s) {
  const t = s.target_type||'local', a = s.target_addr||'';
  return `<span class="target-badge">${t}${a?':'+a:''}</span>`;
}
function faultKeyParam(faults, cat) {
  const f = faults.find(x => x.category === cat);
  if (!f) return '—';
  const kind = f.kind || cat;
  try {
    const p = typeof f.parameters === 'string' ? JSON.parse(f.parameters||'{}') : (f.parameters||{});
    const fn = CAT_PARAMS[cat];
    const v  = fn ? fn(p) : null;
    if (v) return `<span class="param-tag">${v}</span>`;
    return `<span style="color:var(--text3);font-size:11px">${kind}</span>`;
  } catch(e) {
    return `<span style="color:var(--text3);font-size:11px">${kind}</span>`;
  }
}

// ── Boot ───────────────────────────────────────────────────────────────────
async function boot() {
  await Promise.all([loadRuns(), loadSystem(), loadLogs()]);
}

// ── View switching ─────────────────────────────────────────────────────────
function setView(v) {
  document.querySelectorAll('.sb-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
  const sb = document.getElementById('sb-'+v);
  const vp = document.getElementById('view-'+v);
  if (sb) sb.classList.add('active');
  if (vp) vp.classList.add('active');
  // If switching to a cat view, re-filter
  if (CATS[v]) filterCat(v);
}

// ── Experiments (left panel) ───────────────────────────────────────────────
function buildExps(runs) {
  const map = {};
  for (const r of runs) {
    const n = r.name||'Unnamed';
    if (!map[n]) map[n] = {runs:[], cats:new Set()};
    map[n].runs.push(r);
    for (const f of (r.faults||[])) map[n].cats.add(f.category||'other');
  }
  return map;
}
function renderExps() {
  const el = document.getElementById('exp-list');
  const q  = (document.getElementById('exp-search')?.value||'').toLowerCase();
  const allAct = !_expSel;
  let html = `<div class="tree-exp${allAct?' active':''}" onclick="selectExp(null)">
    <span class="tree-toggle"></span>
    <span class="tree-exp-name">All runs</span>
    <span class="tree-exp-meta"><span class="tree-exp-count">${_runs.length}</span></span>
  </div>`;
  for (const [name, g] of Object.entries(_exps)) {
    if (q && !name.toLowerCase().includes(q)) continue;
    const total = g.runs.length;
    const pass  = g.runs.filter(r => r.status === 'done').length;
    const pct   = total ? Math.round(100*pass/total) : 0;
    const rc    = pct >= 80 ? 'ok' : pct >= 50 ? 'warn' : 'fail';
    const act   = _expSel === name;
    const open  = _expOpen.has(name);
    const ns    = name.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    html += `<div class="tree-exp${act?' active':''}" onclick="selectExp('${ns}')">
      <span class="tree-toggle${open?' open':''}" onclick="event.stopPropagation();toggleExp('${ns}')">&#9654;</span>
      <span class="tree-exp-name" title="${name}">${name}</span>
      <span class="tree-exp-meta">
        <span class="tree-exp-count">${total}</span>
        <span class="tree-exp-rate ${rc}">${pct}%</span>
      </span>
    </div>`;
    if (open) {
      const sorted = [...g.runs].sort((a,b) => (b.started_at||'').localeCompare(a.started_at||''));
      for (const r of sorted) {
        const sel = _selId===r.id ? ' selected' : '';
        html += `<div class="tree-run${sel}" onclick="openDetail(${r.id})">
          <span class="tree-run-id">#${r.id}</span>
          <span class="tree-run-name">${sdot(r.status)}${fmtDate(r.started_at).slice(11)||'—'}</span>
          <span class="tree-run-dur">${fmtDur(r.duration_s)}</span>
        </div>`;
      }
    }
  }
  el.innerHTML = html;
}
function toggleExp(name) {
  if (_expOpen.has(name)) _expOpen.delete(name);
  else _expOpen.add(name);
  renderExps();
}
function filterExps() { renderExps(); }
function selectExp(name) {
  _expSel = name;
  renderExps();
  filterOv();
  for (const cat of Object.keys(CATS)) {
    if (document.getElementById('view-'+cat)?.classList.contains('active')) filterCat(cat);
  }
}

// ── Overview ───────────────────────────────────────────────────────────────
async function loadRuns() {
  try {
    const res = await fetch('/api/sessions');
    _runs = await res.json();
    _exps = buildExps(_runs);
    // Auto-expand all experiments in the left panel so runs are visible
    for (const name of Object.keys(_exps)) _expOpen.add(name);
    renderExps();
    renderOvKPI();
    renderCatGrid();
    filterOv();
  } catch(e) {
    document.getElementById('sc-list').innerHTML =
      `<div style="color:var(--red);padding:14px;font-size:12px">Failed to load: ${e.message}</div>`;
  }
}
function renderOvKPI() {
  const total    = _runs.length;
  const pass     = _runs.filter(r => r.status === 'done').length;
  const running  = _runs.filter(r => r.status === 'running').length;
  const pct      = total ? Math.round(100*pass/total) : 0;
  const totalDur = _runs.reduce((a,r) => a+(r.duration_s||0), 0);
  const totCalls = _runs.reduce((a,r) => a+(r.llm_calls||0), 0);
  const totOrP   = _runs.reduce((a,r) => a+(r.oracle_pass||0), 0);
  const totOrF   = _runs.reduce((a,r) => a+(r.oracle_fail||0), 0);
  const orPct    = (totOrP+totOrF) ? Math.round(100*totOrP/(totOrP+totOrF)) : null;
  const jRuns    = _runs.filter(r => r.judge_faithfulness!=null);
  const avgF     = jRuns.length ? jRuns.reduce((a,r)=>a+(r.judge_faithfulness||0),0)/jRuns.length : null;
  document.getElementById('ov-kpi').innerHTML = `
    <div class="kpi-card"><div class="kpi-label">Total runs</div><div class="kpi-value">${total}</div></div>
    <div class="kpi-card"><div class="kpi-label">Pass rate</div><div class="kpi-value" style="color:${pct>=80?'var(--green)':pct>=50?'var(--yellow)':'var(--red)'}">${pct}%</div><div class="kpi-sub">${pass}/${total} runs</div></div>
    <div class="kpi-card"><div class="kpi-label">Oracle pass</div><div class="kpi-value" style="color:${orPct==null?'var(--text)':orPct>=80?'var(--green)':orPct>=50?'var(--yellow)':'var(--red)'}">${orPct!=null?orPct+'%':'—'}</div><div class="kpi-sub">${totOrP}/${totOrP+totOrF} assertions</div></div>
    <div class="kpi-card"><div class="kpi-label">Avg faithfulness</div><div class="kpi-value" style="color:${avgF==null?'var(--text)':avgF>=.7?'var(--green)':avgF>=.4?'var(--yellow)':'var(--red)'};font-size:${avgF!=null?'20px':'16px'}">${avgF!=null?Math.round(avgF*100)+'%':'—'}</div></div>
    <div class="kpi-card"><div class="kpi-label">Experiments</div><div class="kpi-value">${Object.keys(_exps).length}</div><div class="kpi-sub">${fmtDur(totalDur)} total</div></div>`;
  renderOvCharts();
}
function renderCatGrid() {
  const counts = {};
  for (const r of _runs) for (const f of (r.faults||[])) counts[f.category||'other'] = (counts[f.category||'other']||0)+1;
  let html = '';
  for (const [cat, cfg] of Object.entries(CATS)) {
    const n = counts[cat]||0;
    const runs = _runs.filter(r => (r.faults||[]).some(f => f.category===cat));
    const pass = runs.filter(r => r.status==='done').length;
    const pct  = runs.length ? Math.round(100*pass/runs.length) : 0;
    html += `<div class="cat-card" onclick="setView('${cat}')" style="border-left:3px solid ${cfg.color}">
      <div class="cat-card-name" style="color:${cfg.color}">${cfg.label}</div>
      <div class="cat-card-count">${runs.length}</div>
      <div class="cat-card-sub">runs &middot; ${pct}% pass</div>
    </div>`;
  }
  document.getElementById('cat-grid').innerHTML = html;
}
function renderOvCharts() {
  const _gc = c => c.grid ? {color:'rgba(255,255,255,.05)'} : {display:false};
  const _sc = { responsive:true, maintainAspectRatio:false, animation:{duration:400} };

  // ── Chart 1: per-scenario pass/fail stacked bar ──────────────────────────
  const scMap = {};
  for (const r of _runs) {
    const n = r.name||'Unnamed';
    if (!scMap[n]) scMap[n] = {pass:0,fail:0};
    if (r.status==='done') scMap[n].pass++; else scMap[n].fail++;
  }
  const scNames = Object.keys(scMap).slice(0,10);
  _mkChart('ch-pass', {
    type:'bar',
    data:{
      labels: scNames.map(n => n.length>18 ? n.slice(0,16)+'…' : n),
      datasets:[
        {label:'Pass', data:scNames.map(n=>scMap[n].pass), backgroundColor:'rgba(34,197,94,.75)', borderRadius:3, barPercentage:.65, stack:'s'},
        {label:'Fail', data:scNames.map(n=>scMap[n].fail), backgroundColor:'rgba(239,68,68,.65)',  borderRadius:3, barPercentage:.65, stack:'s'},
      ]
    },
    options:{..._sc, indexAxis:'y', plugins:{legend:{position:'bottom'}},
      scales:{x:{stacked:true,grid:_gc({grid:true}),ticks:{stepSize:1}},
              y:{stacked:true,grid:_gc({}),ticks:{font:{size:10}}}}}
  });

  // ── Chart 2: quality or duration trend (line) ────────────────────────────
  const qRuns = [..._runs].filter(r=>r.judge_faithfulness!=null||r.judge_hallucination!=null)
                          .sort((a,b)=>a.id-b.id).slice(-20);
  if (qRuns.length >= 2) {
    _mkChart('ch-quality', {
      type:'line',
      data:{
        labels: qRuns.map(r=>'#'+r.id),
        datasets:[
          {label:'Faithfulness %', data:qRuns.map(r=>r.judge_faithfulness!=null?+(r.judge_faithfulness*100).toFixed(1):null),
           borderColor:'#6366f1', backgroundColor:'rgba(99,102,241,.12)', tension:.35, fill:true, pointRadius:3, pointHoverRadius:5},
          {label:'Hallucination %', data:qRuns.map(r=>r.judge_hallucination!=null?+(r.judge_hallucination*100).toFixed(1):null),
           borderColor:'#ef4444', backgroundColor:'rgba(239,68,68,.08)', tension:.35, fill:true, pointRadius:3, pointHoverRadius:5},
        ]
      },
      options:{..._sc, plugins:{legend:{position:'bottom'}},
        scales:{x:{grid:_gc({})}, y:{min:0,max:100,grid:_gc({grid:true}),ticks:{callback:v=>v+'%'}}}}
    });
  } else {
    const dRuns = [..._runs].filter(r=>r.duration_s!=null).sort((a,b)=>a.id-b.id).slice(-20);
    _mkChart('ch-quality', {
      type:'line',
      data:{
        labels: dRuns.map(r=>'#'+r.id),
        datasets:[{label:'Duration (s)', data:dRuns.map(r=>+r.duration_s.toFixed(1)),
          borderColor:'#6366f1', backgroundColor:'rgba(99,102,241,.12)', tension:.35, fill:true, pointRadius:3}]
      },
      options:{..._sc, plugins:{legend:{position:'bottom'}},
        scales:{x:{grid:_gc({})}, y:{grid:_gc({grid:true}),ticks:{callback:v=>v+'s'}}}}
    });
  }

  // ── Chart 3: oracle assertions per scenario (or status doughnut) ─────────
  const oMap = {};
  for (const r of _runs) {
    const n = r.name||'Unnamed';
    if (!oMap[n]) oMap[n] = {pass:0,fail:0};
    oMap[n].pass += r.oracle_pass||0;
    oMap[n].fail += r.oracle_fail||0;
  }
  const oNames = Object.keys(oMap).filter(n=>oMap[n].pass+oMap[n].fail>0).slice(0,10);
  if (oNames.length >= 1) {
    _mkChart('ch-oracles', {
      type:'bar',
      data:{
        labels: oNames.map(n=>n.length>18?n.slice(0,16)+'…':n),
        datasets:[
          {label:'Pass', data:oNames.map(n=>oMap[n].pass), backgroundColor:'rgba(34,197,94,.75)', borderRadius:3, barPercentage:.65, stack:'o'},
          {label:'Fail', data:oNames.map(n=>oMap[n].fail), backgroundColor:'rgba(239,68,68,.65)',  borderRadius:3, barPercentage:.65, stack:'o'},
        ]
      },
      options:{..._sc, indexAxis:'y', plugins:{legend:{position:'bottom'}},
        scales:{x:{stacked:true,grid:_gc({grid:true}),ticks:{stepSize:1}},
                y:{stacked:true,grid:_gc({}),ticks:{font:{size:10}}}}}
    });
  } else {
    const tot = _runs.length, p = _runs.filter(r=>r.status==='done').length;
    const run = _runs.filter(r=>r.status==='running').length, er = tot-p-run;
    _mkChart('ch-oracles', {
      type:'doughnut',
      data:{
        labels:['Pass','Running','Error/Other'],
        datasets:[{data:[p,run,Math.max(0,er)], backgroundColor:['rgba(34,197,94,.8)','rgba(99,102,241,.8)','rgba(239,68,68,.7)'], borderWidth:0, hoverOffset:4}]
      },
      options:{..._sc, cutout:'65%', plugins:{legend:{position:'bottom'}}}
    });
  }
}

// ── Scenario accordion ──────────────────────────────────────────────────────
function toggleScenario(name) {
  if (_scOpen.has(name)) _scOpen.delete(name); else _scOpen.add(name);
  filterOv();
}
function buildScenarioBody(gruns) {
  const total  = gruns.length;
  const orPass = gruns.reduce((a,r)=>a+(r.oracle_pass||0),0);
  const orFail = gruns.reduce((a,r)=>a+(r.oracle_fail||0),0);
  const orTotal= orPass+orFail;
  const orPct  = orTotal ? Math.round(100*orPass/orTotal) : null;
  const jRows  = gruns.filter(r=>r.judge_faithfulness!=null);
  const avgF   = jRows.length ? jRows.reduce((a,r)=>a+(r.judge_faithfulness||0),0)/jRows.length : null;
  const avgH   = jRows.length ? jRows.reduce((a,r)=>a+(r.judge_hallucination||0),0)/jRows.length : null;
  const avgDur = gruns.reduce((a,r)=>a+(r.duration_s||0),0)/total;
  const orC  = orPct==null?'var(--text)':orPct>=80?'var(--green)':orPct>=50?'var(--yellow)':'var(--red)';
  const fC   = avgF==null?'var(--text)':avgF>=.7?'var(--green)':avgF>=.4?'var(--yellow)':'var(--red)';
  const hC   = avgH==null?'var(--text)':avgH<=.3?'var(--green)':avgH<=.6?'var(--yellow)':'var(--red)';
  function kpi(label, value, color) {
    return `<div class="sc-kpi-card"><div class="sc-kpi-label">${label}</div><div class="sc-kpi-value" style="color:${color||'var(--text)'}">${value}</div></div>`;
  }
  let html = `<div class="sc-kpi-row">
    ${kpi('Runs', total)}
    ${orPct!=null?kpi('Oracle Pass', orPct+'%', orC):''}
    ${avgF!=null?kpi('Faithfulness', Math.round(avgF*100)+'%', fC):''}
    ${avgH!=null?kpi('Hallucination', Math.round(avgH*100)+'%', hC):''}
    ${kpi('Avg Duration', fmtDur(avgDur), 'var(--text2)')}
  </div>`;
  html += `<table class="sc-run-tbl"><thead><tr>
    <th>Run</th><th>Status</th><th>Faults</th><th>Oracles</th><th>Quality</th><th>Metric &Delta;</th><th>Duration</th><th>Started</th>
  </tr></thead><tbody>`;
  for (const r of [...gruns].sort((a,b)=>b.id-a.id)) {
    const p=r.oracle_pass||0, f=r.oracle_fail||0, t=p+f;
    const oc  = t?(f===0?'var(--green)':p===0?'var(--red)':'var(--yellow)'):'var(--text3)';
    const fcP = r.judge_faithfulness!=null?Math.round(r.judge_faithfulness*100):null;
    const hcP = r.judge_hallucination!=null?Math.round(r.judge_hallucination*100):null;
    const fc2 = fcP!=null?(fcP>=70?'var(--green)':fcP>=40?'var(--yellow)':'var(--red)'):'var(--text3)';
    const hc2 = hcP!=null?(hcP<=30?'var(--green)':hcP<=60?'var(--yellow)':'var(--red)'):'var(--text3)';
    const deltas = r.metric_delta ? Object.entries(r.metric_delta).map(([k,v])=>{
      const dv=+v, s=(dv>0?'+':'')+dv.toFixed(3);
      const c=dv>0?'var(--red)':dv<0?'var(--green)':'var(--text3)';
      return `<span style="color:${c};font-family:var(--mono);font-size:10px;margin-right:6px;white-space:nowrap">${k} ${s}</span>`;
    }).join('') : '<span style="color:var(--text3)">—</span>';
    html += `<tr class="${_selId===r.id?' selected':''}" onclick="openDetail(${r.id})">
      <td><span style="font-family:var(--mono);color:var(--text3)">#${r.id}</span></td>
      <td>${sdot(r.status)}<span>${r.status}</span></td>
      <td>${faultChips(r.faults)}</td>
      <td><span style="font-weight:600;color:${oc}">${t?`${p}/${t}`:'—'}</span></td>
      <td>
        ${fcP!=null?`<span style="color:${fc2};font-weight:600">${fcP}%</span><span style="color:var(--text3);font-size:10px"> faith</span>`:''}
        ${hcP!=null?`<span style="color:${hc2};font-weight:600;margin-left:6px">${hcP}%</span><span style="color:var(--text3);font-size:10px"> hall</span>`:''}
        ${fcP==null&&hcP==null?'<span style="color:var(--text3)">—</span>':''}
      </td>
      <td>${deltas}</td>
      <td>${fmtDur(r.duration_s)}</td>
      <td class="mono" style="color:var(--text3)">${fmtDate(r.started_at).slice(11)||'—'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}
function buildScenarioList(filteredRuns) {
  if (!filteredRuns.length) {
    return '<div class="empty-state"><div class="empty-icon">⚗</div><div class="empty-text">No runs yet</div></div>';
  }
  // Group by scenario name preserving insertion order
  const groups = new Map();
  for (const r of filteredRuns) {
    const n = r.name||'Unnamed';
    if (!groups.has(n)) groups.set(n, []);
    groups.get(n).push(r);
  }
  let html = '';
  for (const [name, gruns] of groups) {
    const open   = _scOpen.has(name);
    const total  = gruns.length;
    const orPass = gruns.reduce((a,r)=>a+(r.oracle_pass||0),0);
    const orFail = gruns.reduce((a,r)=>a+(r.oracle_fail||0),0);
    const orTotal= orPass+orFail;
    const orPct  = orTotal ? Math.round(100*orPass/orTotal) : null;
    const orC    = orPct==null?'var(--text3)':orPct>=80?'var(--green)':orPct>=50?'var(--yellow)':'var(--red)';
    const jRows  = gruns.filter(r=>r.judge_faithfulness!=null);
    const avgF   = jRows.length ? jRows.reduce((a,r)=>a+(r.judge_faithfulness||0),0)/jRows.length : null;
    const fC     = avgF==null?'var(--text3)':avgF>=.7?'var(--green)':avgF>=.4?'var(--yellow)':'var(--red)';
    const ns     = name.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;');
    html += `<div class="sc-row${open?' sc-open':''}" onclick="toggleScenario('${ns}')">
      <span class="sc-toggle">${open?'&#9660;':'&#9654;'}</span>
      <span class="sc-name" title="${name}">${name}</span>
      <span class="sc-stats">
        <span class="sc-badge">${total} run${total!==1?'s':''}</span>
        ${orPct!=null?`<span class="sc-badge" style="color:${orC};border-color:${orC};font-weight:600">${orPct}% pass</span>`:''}
        ${avgF!=null?`<span class="sc-badge" style="color:${fC};border-color:${fC}">${Math.round(avgF*100)}% faith</span>`:''}
      </span>
      <span class="sc-faults">${faultChips((gruns[0]||{}).faults||[])}</span>
    </div>
    ${open?`<div class="sc-body">${buildScenarioBody(gruns)}</div>`:''}`;
  }
  return html;
}
function filterOv() {
  const q = (document.getElementById('ov-search')?.value||'').toLowerCase();
  let rows = _runs.filter(r => {
    if (_expSel && r.name !== _expSel) return false;
    if (q && !r.name.toLowerCase().includes(q) && !String(r.id).includes(q)) return false;
    return true;
  });
  document.getElementById('ov-count').textContent = rows.length+' run'+(rows.length!==1?'s':'');
  document.getElementById('sc-list').innerHTML = buildScenarioList(rows);
}
function sortOv(col) {
  document.querySelectorAll('#view-overview thead th').forEach(th => th.classList.remove('sort-asc','sort-desc'));
  if (_ovSort.col===col) _ovSort.asc=!_ovSort.asc; else { _ovSort.col=col; _ovSort.asc=col!=='id'; }
  const th = document.getElementById('ov-th-'+col);
  if (th) th.classList.add(_ovSort.asc?'sort-asc':'sort-desc');
  filterOv();
}

// ── Per-category views ─────────────────────────────────────────────────────
function filterCat(cat) {
  const cfg = CATS[cat]; if (!cfg) return;
  const q   = (document.getElementById(cfg.search)?.value||'').toLowerCase();
  const runs = _runs.filter(r => {
    if (_expSel && r.name !== _expSel) return false;
    if (!(r.faults||[]).some(f => f.category===cat)) return false;
    if (q && !r.name.toLowerCase().includes(q) && !String(r.id).includes(q)) return false;
    return true;
  });
  // KPI
  const pass = runs.filter(r => r.status==='done').length;
  const pct  = runs.length ? Math.round(100*pass/runs.length) : 0;
  const avgDur = runs.length ? runs.reduce((a,r)=>a+(r.duration_s||0),0)/runs.length : 0;
  const kindCount = {};
  for (const r of runs) for (const f of (r.faults||[])) if (f.category===cat) kindCount[f.kind]=(kindCount[f.kind]||0)+1;
  const topKind = Object.entries(kindCount).sort((a,b)=>b[1]-a[1])[0];
  document.getElementById(cfg.kpi).innerHTML = `
    <div class="kpi-card"><div class="kpi-label">Runs</div><div class="kpi-value">${runs.length}</div></div>
    <div class="kpi-card"><div class="kpi-label">Pass rate</div><div class="kpi-value" style="color:${pct>=80?'var(--green)':pct>=50?'var(--yellow)':'var(--red)'}">${pct}%</div></div>
    <div class="kpi-card"><div class="kpi-label">Avg duration</div><div class="kpi-value" style="font-size:16px">${fmtDur(avgDur)}</div></div>
    <div class="kpi-card"><div class="kpi-label">Top fault kind</div><div class="kpi-value" style="font-size:13px;font-weight:500;color:${cfg.color}">${topKind?topKind[0]:'—'}</div><div class="kpi-sub">${topKind?topKind[1]+' runs':''}</div></div>`;
  // Table
  const tbody = document.getElementById(cfg.tbody);
  const empty = document.getElementById(cfg.empty);
  document.getElementById(cfg.count).textContent = runs.length+' run'+(runs.length!==1?'s':'');
  if (!runs.length) { tbody.innerHTML=''; empty.style.display='flex'; return; }
  empty.style.display='none';
  tbody.innerHTML = runs.map(r => {
    const f = (r.faults||[]).find(x => x.category===cat);
    return `<tr class="${_selId===r.id?' selected':''}" onclick="openDetail(${r.id})">
      <td class="mono">${r.id}</td>
      <td><span class="cell-primary">${r.name||'—'}</span></td>
      <td>${sdot(r.status)}<span>${r.status}</span></td>
      <td>${f ? chip(cat, f.kind||cat) : '—'}</td>
      <td>${faultKeyParam(r.faults, cat)}</td>
      <td>${targetBadge(r)}</td>
      <td>${fmtDur(r.duration_s)}</td>
      <td class="mono" style="color:var(--text3)">${fmtDate(r.started_at)}</td>
    </tr>`;
  }).join('');
}

// ── System ─────────────────────────────────────────────────────────────────
async function loadSystem() {
  try {
    const data = await (await fetch('/api/system')).json();
    document.getElementById('sys-tbody').innerHTML = data.map(t => `<tr>
      <td class="mono cell-primary">${t.binary}</td>
      <td>${t.package}</td>
      <td style="color:var(--text3)">${t.role}</td>
      <td>${t.found?'<span class="tool-ok">&#10003; found</span>':'<span class="tool-miss">&#10007; missing</span>'}</td>
      <td class="mono" style="color:var(--text3)">${t.path||'—'}</td>
    </tr>`).join('');
  } catch(e) {}
}

// ── Logs ───────────────────────────────────────────────────────────────────
async function loadLogs() {
  try {
    const data = await (await fetch('/api/logs')).json();
    const el   = document.getElementById('log-files');
    if (!data.length) { el.innerHTML='<span style="color:var(--text3);font-size:12px">No log files found</span>'; return; }
    el.innerHTML = data.map(f =>
      `<button class="log-btn" onclick="loadLogContent('${f.name}',this)">${f.name} <span style="color:var(--text3);font-size:10px">${f.size_kb}KB</span></button>`
    ).join('');
  } catch(e) {}
}
async function loadLogContent(name, btn) {
  document.querySelectorAll('.log-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const el = document.getElementById('log-content');
  el.innerHTML = '<div style="color:var(--text3);font-size:12px">Loading…</div>';
  try {
    const data = await (await fetch(`/api/logs/${name}`)).json();
    if (data.error) { el.innerHTML=`<div style="color:var(--red)">${data.error}</div>`; return; }
    el.innerHTML = data.lines.map(l => {
      const cls = /error|Error|ERROR|FAIL/.test(l)?'log-err':/warn|WARN/.test(l)?'log-warn':'';
      return `<div class="log-line ${cls}">${l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
    }).join('');
    el.scrollTop = el.scrollHeight;
  } catch(e) { el.innerHTML='<div style="color:var(--red)">Failed to load</div>'; }
}

// ── Run detail in main screen ──────────────────────────────────────────────
let _runTab = 'run';
function switchRunTab(tab) {
  _runTab = tab; _dpTab = tab;
  document.querySelectorAll('#run-tabs .dp-tab').forEach(t => t.classList.toggle('active', t.dataset.tab===tab));
  document.getElementById('run-body').innerHTML = _dpData[tab] || '<div style="color:var(--text3);font-size:12px">No data</div>';
  _postRender(tab);
}
function _postRender(tab) {
  if (tab === 'run')     _initImpactCharts(_rawImpact, _rawResources);
  if (tab === 'llm')     _initLLMCharts(_rawLLM);
  if (tab === 'metrics') _initMetricsChart(_rawResults, _rawOracles);
  if (tab === 'faults')  _initResourceChart(_rawResources);
}
function _initLLMCharts(calls) {
  if (!calls || !calls.length) return;
  const labels = calls.map((_,i) => `#${i+1}`);
  const _gc = {color:'rgba(255,255,255,.05)'};
  const _so = {responsive:true, maintainAspectRatio:false, animation:{duration:200}};
  const _xt = {font:{size:9}, maxRotation:0, autoSkip:true, maxTicksLimit:24};

  _mkChart('llm-ch-tokens', {
    type:'bar',
    data:{
      labels,
      datasets:[
        {label:'Prompt',     data:calls.map(c=>c.prompt_tokens||0),     backgroundColor:'rgba(99,102,241,.75)', borderRadius:2, stack:'t'},
        {label:'Completion', data:calls.map(c=>c.completion_tokens||0), backgroundColor:'rgba(34,197,94,.75)',  borderRadius:2, stack:'t'},
      ]
    },
    options:{..._so, plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10,padding:10}}},
      scales:{x:{stacked:true,grid:{display:false},ticks:_xt},
              y:{stacked:true,grid:_gc,ticks:{callback:v=>v>=1000?Math.round(v/100)/10+'k':v,font:{size:9}}}}}
  });

  _mkChart('llm-ch-lat', {
    type:'bar',
    data:{
      labels,
      datasets:[{
        label:'Latency (s)',
        data: calls.map(c=>+(c.latency_s||0).toFixed(3)),
        backgroundColor: calls.map(c => c.was_blocked?'rgba(239,68,68,.75)':c.was_modified?'rgba(245,158,11,.75)':'rgba(6,182,212,.65)'),
        borderRadius:2, barPercentage:.75
      }]
    },
    options:{..._so, plugins:{legend:{display:false},
      tooltip:{callbacks:{label:ctx=>{
        const c=calls[ctx.dataIndex];
        const parts=[ctx.parsed.y.toFixed(2)+'s'];
        if(c.ttft_s>0) parts.push('TTFT '+c.ttft_s.toFixed(3)+'s');
        if(c.was_blocked) parts.push('BLOCKED');
        if(c.was_modified) parts.push('MODIFIED');
        if(c.error_type&&c.error_type!=='none') parts.push(c.error_type);
        return parts;
      }}}},
      scales:{x:{grid:{display:false},ticks:_xt},y:{grid:_gc,ticks:{callback:v=>v+'s',font:{size:9}}}}}
  });
}
function _initImpactCharts(impact, resources) {
  const _gc = {color:'rgba(255,255,255,.05)'};
  const _so = {responsive:true, maintainAspectRatio:false, animation:{duration:200}};

  // ── Workload metrics chart ───────────────────────────────────────────────────
  if (document.getElementById('wl-ch-compare')) {
    let metricRows = [];
    for (const r of (_rawResults||[])) {
      const m = typeof r.metrics==='string'?JSON.parse(r.metrics||'{}'):(r.metrics||{});
      if (m.baseline && typeof m.baseline==='object') {
        const b=flattenObj(m.baseline,'',{}), f=flattenObj(m.fault||m.chaos||{},'',{});
        const keys=[...new Set([...Object.keys(b),...Object.keys(f)])];
        metricRows=keys.filter(k=>!isNaN(+b[k])&&b[k]!=='').map(k=>({key:k,b:+b[k]||0,f:+f[k]||0})).slice(0,8);
        break;
      }
    }
    if (metricRows.length) {
      _mkChart('wl-ch-compare',{
        type:'bar',
        data:{
          labels:metricRows.map(r=>r.key.replace(/_/g,' ')),
          datasets:[
            {label:'Baseline',data:metricRows.map(r=>r.b),backgroundColor:'rgba(99,102,241,.7)',borderRadius:3,barPercentage:.72},
            {label:'Fault',   data:metricRows.map(r=>r.f),backgroundColor:'rgba(239,68,68,.65)', borderRadius:3,barPercentage:.72},
          ]
        },
        options:{..._so,indexAxis:'y',
          plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10,padding:10}}},
          scales:{x:{grid:_gc,ticks:{font:{size:9}}},y:{grid:{display:false},ticks:{font:{size:10}}}}
        }
      });
    }
  }

  // ── System resources — 3 split charts ───────────────────────────────────────
  const _snapChartFault = (_rawFaults||[]).find(f => {
    try {
      const sb = typeof f.snapshot_before==='string'?JSON.parse(f.snapshot_before||'null'):f.snapshot_before;
      return sb && Object.keys(sb).length > 0;
    } catch(e) { return false; }
  });
  if (_snapChartFault) {
    const _sb = typeof _snapChartFault.snapshot_before==='string'?JSON.parse(_snapChartFault.snapshot_before||'{}'):(_snapChartFault.snapshot_before||{});
    const _sa = typeof _snapChartFault.snapshot_after==='string'?JSON.parse(_snapChartFault.snapshot_after||'{}'):(_snapChartFault.snapshot_after||{});
    const _baChart = (id, pairs) => {
      // pairs: [{k, label}] — before vs after grouped bar
      const rows = pairs.filter(p => _sb[p.k] != null || _sa[p.k] != null);
      if (!rows.length || !document.getElementById(id)) return;
      _mkChart(id, {
        type:'bar',
        data:{
          labels: rows.map(p => p.label),
          datasets:[
            {label:'Before', data:rows.map(p=>+(+(_sb[p.k]||0)).toFixed(2)), backgroundColor:'rgba(99,102,241,.7)', borderRadius:3, barPercentage:.72},
            {label:'After',  data:rows.map(p=>+(+(_sa[p.k]||0)).toFixed(2)), backgroundColor:'rgba(239,68,68,.65)',  borderRadius:3, barPercentage:.72},
          ]
        },
        options:{..._so, indexAxis:'y',
          plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10,padding:10}}},
          scales:{x:{grid:_gc,ticks:{font:{size:9}}},y:{grid:{display:false},ticks:{font:{size:10}}}}
        }
      });
    };
    // CPU chart: cpu_pct, load_1, load_5
    _baChart('res-ch-cpu', [
      {k:'cpu_pct', label:'CPU %'},
      {k:'load_1',  label:'Load 1m'},
      {k:'load_5',  label:'Load 5m'},
    ]);
    // Memory chart: mem_pct, mem_used_mb
    _baChart('res-ch-mem', [
      {k:'mem_pct',     label:'Mem %'},
      {k:'mem_used_mb', label:'Mem used (MB)'},
    ]);
    // I/O chart: delta (after − before) — cumulative counters, delta = activity during fault
    if (document.getElementById('res-ch-io')) {
      const ioPairs = [
        {k:'disk_read_mb',  label:'Disk read'},
        {k:'disk_write_mb', label:'Disk write'},
        {k:'net_rx_mb',     label:'Net RX'},
        {k:'net_tx_mb',     label:'Net TX'},
      ].filter(p => _sb[p.k] != null && _sa[p.k] != null);
      if (ioPairs.length) {
        _mkChart('res-ch-io', {
          type:'bar',
          data:{
            labels: ioPairs.map(p => p.label),
            datasets:[{
              label:'MB during fault',
              data: ioPairs.map(p => +Math.max(0, +(_sa[p.k]||0) - +(_sb[p.k]||0)).toFixed(2)),
              backgroundColor: ioPairs.map(p => p.k.startsWith('disk')?'rgba(249,115,22,.75)':'rgba(34,197,94,.75)'),
              borderRadius:3, barPercentage:.65,
            }]
          },
          options:{..._so, indexAxis:'y',
            plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10,padding:10}}},
            scales:{x:{grid:_gc,ticks:{font:{size:9},callback:v=>v+' MB'}},y:{grid:{display:false},ticks:{font:{size:10}}}}
          }
        });
      }
    }
  }

  if (impact && document.getElementById('imp-ch-compare')) {
    const rows = [
      {label:'Avg Latency (s)',  b: impact.avg_latency_baseline||0, f: impact.avg_latency_fault||0},
      {label:'p99 Latency (s)',  b: impact.p99_latency_baseline||0, f: impact.p99_latency_fault||0},
      {label:'Error Rate (%)',   b: (impact.error_rate_baseline||0)*100, f: (impact.error_rate_fault||0)*100},
      {label:'Cost (USD)',       b: impact.cost_baseline||0, f: impact.cost_fault||0},
    ].filter(r => r.b > 0 || r.f > 0);
    if (rows.length) {
      _mkChart('imp-ch-compare', {
        type:'bar',
        data:{
          labels: rows.map(r=>r.label),
          datasets:[
            {label:'Baseline', data:rows.map(r=>+r.b.toFixed(5)), backgroundColor:'rgba(99,102,241,.7)', borderRadius:3, barPercentage:.72},
            {label:'Fault',    data:rows.map(r=>+r.f.toFixed(5)), backgroundColor:'rgba(239,68,68,.65)',  borderRadius:3, barPercentage:.72},
          ]
        },
        options:{..._so, indexAxis:'y',
          plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10,padding:10}}},
          scales:{x:{grid:_gc,ticks:{font:{size:9}}},y:{grid:{display:false},ticks:{font:{size:10}}}}
        }
      });
    }
  }

  if (resources && resources.length > 1 && document.getElementById('imp-ch-res')) {
    const labels = resources.map(s => s.elapsed_s.toFixed(1)+'s');
    _mkChart('imp-ch-res', {
      type:'line',
      data:{
        labels,
        datasets:[
          {label:'CPU %',    data:resources.map(s=>s.cpu_pct),  borderColor:'#f97316', backgroundColor:'rgba(249,115,22,.1)', tension:.35, pointRadius:0, fill:true},
          {label:'Memory %', data:resources.map(s=>s.mem_pct),  borderColor:'#6366f1', backgroundColor:'rgba(99,102,241,.1)', tension:.35, pointRadius:0, fill:true},
        ]
      },
      options:{..._so,
        plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:10,padding:10}}},
        scales:{x:{grid:{display:false},ticks:{font:{size:9},maxRotation:0,autoSkip:true,maxTicksLimit:10}},
                y:{grid:_gc,min:0,max:100,ticks:{callback:v=>v+'%',font:{size:9}}}}
      }
    });
  }
}
function _initMetricsChart(results, oracles) {
  let metricRows = [];
  for (const r of (results||[])) {
    const m = typeof r.metrics==='string' ? JSON.parse(r.metrics||'{}') : (r.metrics||{});
    if (!metricRows.length && m.baseline && typeof m.baseline==='object') {
      const b = flattenObj(m.baseline,'',{}), f = flattenObj(m.fault||m.chaos||{},'',{});
      const keys = [...new Set([...Object.keys(b),...Object.keys(f)])];
      metricRows = keys
        .filter(k => typeof b[k]==='number' || (!isNaN(+b[k]) && b[k]!==''))
        .map(k => ({key:k, b:+b[k]||0, f:+f[k]||0}))
        .slice(0,12);
    }
  }
  if (!metricRows.length) return;
  _mkChart('met-ch-compare', {
    type:'bar',
    data:{
      labels: metricRows.map(r=>r.key),
      datasets:[
        {label:'Baseline', data:metricRows.map(r=>r.b), backgroundColor:'rgba(99,102,241,.75)', borderRadius:3, barPercentage:.65},
        {label:'Fault',    data:metricRows.map(r=>r.f), backgroundColor:'rgba(239,68,68,.70)',  borderRadius:3, barPercentage:.65},
      ]
    },
    options:{
      responsive:true, maintainAspectRatio:false, animation:{duration:300},
      indexAxis:'y',
      plugins:{legend:{position:'bottom'}},
      scales:{x:{grid:{color:'rgba(255,255,255,.05)'}},y:{grid:{display:false},ticks:{font:{size:10}}}}
    }
  });
}
function _initResourceChart(samples) {
  if (!samples || samples.length < 2) return;
  const labels = samples.map(s => s.elapsed_s.toFixed(1)+'s');
  _mkChart('res-ch-cpu', {
    type:'line',
    data:{
      labels,
      datasets:[
        {label:'CPU %',    data:samples.map(s=>s.cpu_pct),  borderColor:'#f97316', backgroundColor:'rgba(249,115,22,.10)', tension:.3, pointRadius:1, fill:true},
        {label:'Memory %', data:samples.map(s=>s.mem_pct),  borderColor:'#6366f1', backgroundColor:'rgba(99,102,241,.10)', tension:.3, pointRadius:1, fill:true},
      ]
    },
    options:{
      responsive:true, maintainAspectRatio:false, animation:{duration:200},
      plugins:{legend:{position:'bottom'}},
      scales:{
        x:{ticks:{font:{size:9},maxTicksLimit:12},grid:{color:'rgba(255,255,255,.05)'}},
        y:{min:0,max:100,grid:{color:'rgba(255,255,255,.05)'},ticks:{callback:v=>v+'%',font:{size:9}}}
      }
    }
  });
}
async function openDetail(id) {
  _selId = id;
  renderExps();
  // Switch main view to run detail
  setView('run');
  // Ensure right panel is closed
  document.getElementById('app').classList.remove('dp-open');

  document.getElementById('run-title').textContent = `#${id}`;
  document.getElementById('run-status-badge').innerHTML = '';
  document.getElementById('run-fault-chips').innerHTML = '';
  document.getElementById('run-meta').textContent = '';
  document.getElementById('run-body').innerHTML = '<div class="loading-state">Loading…</div>';

  try {
    const [sRes, lRes, tcRes, impRes, rsRes] = await Promise.all([
      fetch(`/api/session/${id}`),
      fetch(`/api/session/${id}/llm_calls`),
      fetch(`/api/session/${id}/tool_calls`),
      fetch(`/api/session/${id}/impact`),
      fetch(`/api/session/${id}/resources`),
    ]);
    const sd        = await sRes.json();
    const llm       = await lRes.json();
    const toolCalls = await tcRes.json();
    const impact    = impRes.ok ? await impRes.json() : null;
    const resources = rsRes.ok ? await rsRes.json() : [];
    const s   = sd.session||{};
    _rawLLM       = llm;
    _rawResults   = sd.results||[];
    _rawOracles   = sd.oracles||[];
    _rawResources = resources;
    _rawImpact    = impact;
    _rawFaults    = sd.faults||[];

    // Populate header
    document.getElementById('run-title').textContent = s.name || `Session #${id}`;
    document.getElementById('run-status-badge').innerHTML =
      `<span style="font-family:var(--mono);font-size:11px;color:var(--text3);margin-right:6px">#${id}</span>${sdot(s.status)}<span style="font-size:11px;color:var(--text3)">${s.status||''}</span>`;
    const fc = (sd.faults||[]);
    if (fc.length) document.getElementById('run-fault-chips').innerHTML = faultChips(fc);
    const dur = s.duration_s!=null ? fmtDur(s.duration_s) : '';
    const started = s.started_at ? fmtDate(s.started_at) : '';
    document.getElementById('run-meta').textContent = [dur, started].filter(Boolean).join('  ·  ');

    _dpData = {
      run:      buildDPRun(s, sd.faults||[], impact, resources, sd.results||[]),
      faults:   buildDPFaults(sd.faults||[], resources),
      metrics:  buildDPMetrics(sd.results||[], sd.oracles||[]),
      commands: buildDPCommands(sd.commands||[]),
      llm:      buildDPLLM(llm),
      tools:    buildDPToolCalls(toolCalls, s),
      events:   buildDPEvents(sd.events||[]),
    };
    switchRunTab(_runTab);
  } catch(e) {
    document.getElementById('run-body').innerHTML = `<div style="color:var(--red);padding:8px">Failed: ${e.message}</div>`;
  }
}
// Keep legacy detail panel functions (not actively used but avoid JS errors)
function closeDetail() { document.getElementById('app').classList.remove('dp-open'); }
function switchDPTab(tab) { _dpTab = tab; }
function showDPTab(tab) {}

// ── Agent name helper ────────────────────────────────────────────────────────
function agentName(targetType, targetAddr) {
  if (!targetAddr) return targetType||'local';
  // Map well-known cj-daemon ports to agent names
  const PORT_MAP = {'7781':'flight-agent','7782':'hotel-agent','7783':'activity-agent',
                    '7784':'budget-agent','7785':'orchestrator'};
  const m = targetAddr.match(/:(\d+)$/);
  if (m && PORT_MAP[m[1]]) return PORT_MAP[m[1]];
  return targetAddr.replace(/^https?:\/\//,'');
}

// ── Fault description map ─────────────────────────────────────────────────────
const _FAULT_DESC = {
  NetworkDelay:           p=>`Inject ${p.delay_ms??p.delay_s*1000??'?'}ms delay${p.jitter_ms?` ±${p.jitter_ms}ms jitter`:''}${p.interface?` on ${p.interface}`:''}`,
  NetworkLoss:            p=>`Drop ${p.loss_pct??'?'}% of packets${p.interface?` on ${p.interface}`:''}`,
  NetworkCorrupt:         p=>`Corrupt ${p.corrupt_pct??'?'}% of packets${p.interface?` on ${p.interface}`:''}`,
  NetworkDuplicate:       p=>`Duplicate ${p.dup_pct??'?'}% of packets${p.interface?` on ${p.interface}`:''}`,
  SilentNetworkCorrupt:   p=>`Silently corrupt ${p.corrupt_pct??'?'}% of packets${p.interface?` on ${p.interface}`:''}`,
  CPUStress:              p=>`Saturate CPU to ${p.cpu_pct??'?'}%${p.cores?` across ${p.cores} cores`:''}`,
  MemoryStress:           p=>`Consume ${p.mem_mb?p.mem_mb+'MB':p.mem_pct?p.mem_pct+'%':'?'} of memory`,
  DiskFull:               p=>`Fill disk to ${p.fill_pct??'?'}%${p.mount_point?` on ${p.mount_point}`:''}`,
  IOStress:               p=>`Saturate disk I/O to ${p.io_pct??'?'}%`,
  ProcessKill:            p=>`Send ${p.signal||'SIGKILL'} to process ${p.process_name||'?'}`,
  ServiceFault:           p=>`${p.action||'Stop'} service "${p.service_name||'?'}"`,
  ContainerKill:          p=>`Kill container "${p.container_name||'?'}"`,
  StorageCorrupt:         p=>`Corrupt ${p.corrupt_pct??'?'}% of bytes in ${p.path||'target file'}`,
  RedisStateCorrupt:      p=>`Corrupt Redis key "${p.key||'?'}" → ${JSON.stringify(p.value??'corrupted')}`,
  JsonStateCorrupt:       p=>`Corrupt JSON state at path "${p.path||'?'}"`,
  PostgresStateCorrupt:   p=>`Corrupt Postgres table "${p.table||'?'}"`,
  LLMLatency:             p=>`Add ${p.delay_s??'?'}s latency to every LLM call`,
  LLMRateLimit:           p=>`Rate-limit after ${p.n??'?'} requests (HTTP 429)`,
  LLMTimeout:             p=>`Return gateway timeout after ${p.timeout_s??'?'}s`,
  LLMResponseCorrupt:     p=>`Corrupt LLM response (mode: ${p.mode||'truncate'})`,
  LLMUnavailable:         p=>`Return HTTP 503 for all LLM requests`,
  LLMHallucination:       p=>`Inject hallucination${p.text?`: "${String(p.text).slice(0,50)}"…`:''}`,
  LLMStreamInterrupt:     p=>`Interrupt SSE stream after ${p.interrupt_after??'?'} chunks`,
  LLMTokenStarvation:     p=>`Cap response to ${p.max_tokens??'?'} tokens`,
  LLMBudgetExceeded:      p=>`Block calls when total cost exceeds $${p.budget_max_cost_usd??'?'}`,
  ToolFault:              p=>`Return error for tool${p.tool_name?' "'+p.tool_name+'"':' calls'}`,
  MCPFault:               p=>`MCP ${p.fault||'error'} fault${p.tool_name?' on "'+p.tool_name+'"':''}`,
  SemanticCorrupt:        p=>`Semantic corruption (mode: ${p.semantic_mode||p.mode||'entity_swap'})`,
  SkillUnavailable:       p=>`Make skill "${p.skill_name||'?'}" unavailable`,
  SkillMisroute:          p=>`Misroute skill calls to "${p.wrong_skill||'?'}"`,
  SkillInstructionCorrupt:p=>`Corrupt skill instructions${p.corrupt_instruction?': "'+String(p.corrupt_instruction).slice(0,40)+'"':''}`,
  SkillDependencyMissing: p=>`Report missing dependency for skill "${p.skill_name||'?'}"`,
  SkillTimeout:           p=>`Timeout skill "${p.skill_name||'?'}" after ${p.skill_timeout_s??'?'}s`,
  SkillBadOutput:         p=>`Inject bad output from skill "${p.skill_name||'?'}" (${p.bad_output_mode||'invalid_json'})`,
  SkillVersionSkew:       p=>`Inject old version "${p.old_version||'?'}" for skill "${p.skill_name||'?'}"`,
  SkillPermissionDenied:  p=>`Deny permission for skill "${p.skill_name||'?'}"`,
  SkillMemoryStale:       p=>`Inject stale memory for skill "${p.skill_name||'?'}"`,
  GPUThrottle:            p=>`Throttle GPU to ${p.target_pct??'?'}% utilisation`,
  GPUMemoryPressure:      p=>`Consume ${p.mem_mb??'?'}MB of GPU memory`,
  GPUClockLock:           p=>`Lock GPU clocks to ${p.core_mhz??'?'}MHz core / ${p.mem_mhz??'?'}MHz mem`,
};

function faultDesc(kind, params) {
  const fn = _FAULT_DESC[kind];
  try { return fn ? fn(params||{}) : null; } catch(e) { return null; }
}

// ── Category color helper ─────────────────────────────────────────────────────
const _CAT_COLOR = {
  network:'#2563eb', resource:'#ea580c', process:'#dc2626', storage:'#d97706',
  state:'#0891b2', llm:'#7c3aed', skill:'#4f46e5', semantic:'#db2777',
  gpu:'#16a34a', other:'#64748b',
};

// Detail tab builders
function buildDPRun(s, faults, impact, resources, results) {
  const agent = agentName(s.target_type, s.target_addr);
  faults = faults||[];
  results = results||[];
  const domCat = (faults[0]||{}).category||'other';
  function row(label, value, mono) {
    return `<div style="display:flex;gap:0;padding:7px 0;border-bottom:1px solid var(--border)">
      <span style="width:120px;flex-shrink:0;font-size:11px;color:var(--text3)">${label}</span>
      <span style="font-size:12px;color:var(--text);${mono?'font-family:var(--mono)':''}">${value}</span>
    </div>`;
  }
  const statusColor = s.status==='reverted'||s.status==='done'?'var(--green)':s.status==='running'?'var(--accent)':'var(--red)';

  // ── Section 1: Workload metrics (primary — always shown when data exists) ────
  let workloadHtml = '';
  {
    let bMetrics={}, fMetrics={}, dMetrics={};
    for (const r of results) {
      const m = typeof r.metrics==='string'?JSON.parse(r.metrics||'{}'):(r.metrics||{});
      if (m.baseline && typeof m.baseline==='object') {
        bMetrics=flattenObj(m.baseline,'',{});
        fMetrics=flattenObj(m.fault||m.chaos||{},'',{});
        dMetrics=flattenObj(m.delta||{},'',{});
        break;
      }
    }
    const wlKeys = Object.keys(bMetrics).filter(k => !isNaN(+bMetrics[k]) && bMetrics[k]!=='');
    if (wlKeys.length) {
      const kpiCards = wlKeys.slice(0,6).map(k => {
        const bv=+bMetrics[k]||0, fv=+fMetrics[k]||0, dv=+dMetrics[k]||0;
        const changed = Math.abs(dv) > 1e-9;
        const higherWorse = /error|latency|duration|time|fail/.test(k);
        const worse = changed && (higherWorse ? fv > bv : fv < bv);
        const color = !changed?'var(--text3)':worse?'var(--red)':'var(--green)';
        const arrow = !changed?'':worse?' ▲':' ▼';
        const bg = !changed?'':worse?'border-top:2px solid var(--red)':'border-top:2px solid var(--green)';
        return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;${bg}">
          <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${k}">${k.replace(/_/g,' ')}</div>
          <div style="display:flex;flex-direction:column;gap:2px">
            <div style="display:flex;justify-content:space-between"><span style="font-size:10px;color:var(--text3)">Baseline</span><span style="font-size:12px;font-weight:600;font-family:var(--mono)">${fmtMetric(k,bv)}</span></div>
            <div style="display:flex;justify-content:space-between"><span style="font-size:10px;color:var(--text3)">Fault</span><span style="font-size:12px;font-weight:700;font-family:var(--mono);color:${color}">${fmtMetric(k,fv)}${arrow}</span></div>
          </div>
        </div>`;
      }).join('');
      workloadHtml = `<div style="margin-bottom:24px">
        <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Workload Metrics — Baseline vs Fault</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;margin-bottom:12px">${kpiCards}</div>
        <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;height:180px">
          <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Metric comparison</div>
          <canvas id="wl-ch-compare"></canvas>
        </div>
      </div>`;
    }
  }

  // ── Section 2: System resources — KPI cards + chart (always when data exists) ─
  let resourcesHtml = '';
  {
    const hasMonitoring = resources && resources.length > 1;
    const snapFault = faults.find(f => {
      try {
        const sb = typeof f.snapshot_before==='string'?JSON.parse(f.snapshot_before||'null'):f.snapshot_before;
        return sb && Object.keys(sb).length > 0;
      } catch(e) { return false; }
    });

    if (hasMonitoring || snapFault) {
      // Metric definitions per fault category — controls which KPI cards appear
      const _SNAP_META = {
        resource: [{k:'cpu_pct',l:'CPU %',u:'%'},{k:'mem_pct',l:'Mem %',u:'%'},{k:'load_1',l:'Load 1m',u:''},{k:'mem_used_mb',l:'Mem used',u:' MB'}],
        network:  [{k:'cpu_pct',l:'CPU %',u:'%'},{k:'net_rx_mb',l:'Net RX',u:' MB'},{k:'net_tx_mb',l:'Net TX',u:' MB'},{k:'mem_pct',l:'Mem %',u:'%'}],
        process:  [{k:'cpu_pct',l:'CPU %',u:'%'},{k:'mem_pct',l:'Mem %',u:'%'},{k:'load_1',l:'Load 1m',u:''},{k:'mem_used_mb',l:'Mem used',u:' MB'}],
        storage:  [{k:'disk_read_mb',l:'Disk Read',u:' MB'},{k:'disk_write_mb',l:'Disk Write',u:' MB'},{k:'cpu_pct',l:'CPU %',u:'%'},{k:'mem_pct',l:'Mem %',u:'%'}],
      };
      const metaDefs = _SNAP_META[domCat] || [{k:'cpu_pct',l:'CPU %',u:'%'},{k:'mem_pct',l:'Mem %',u:'%'},{k:'load_1',l:'Load 1m',u:''},{k:'mem_used_mb',l:'Mem used',u:' MB'}];

      let sb = {}, sa = {};
      if (snapFault) {
        try {
          sb = typeof snapFault.snapshot_before==='string'?JSON.parse(snapFault.snapshot_before||'{}'):(snapFault.snapshot_before||{});
          sa = typeof snapFault.snapshot_after==='string'?JSON.parse(snapFault.snapshot_after||'{}'):(snapFault.snapshot_after||{});
        } catch(e){}
      }

      const kpiCards = metaDefs.filter(m => sb[m.k] != null || sa[m.k] != null).map(m => {
        const bv = sb[m.k], fv = sa[m.k];
        const fmtV = v => v != null ? v.toFixed(m.k==='load_1'?2:m.k.endsWith('_mb')?0:1)+m.u : '—';
        const changed = bv != null && fv != null && Math.abs(fv - bv) > 0.1;
        const worse = changed && fv > bv;
        const color = !changed?'var(--text3)':worse?'var(--red)':'var(--green)';
        const arrow = !changed?'':worse?' ▲':' ▼';
        const bg = !changed?'':worse?'border-top:2px solid var(--red)':'border-top:2px solid var(--green)';
        return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 13px;${bg}">
          <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">${m.l}</div>
          <div style="display:flex;flex-direction:column;gap:3px">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-size:10px;color:var(--text3)">Before</span>
              <span style="font-size:13px;font-weight:600;color:var(--text);font-family:var(--mono)">${fmtV(bv)}</span>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-size:10px;color:var(--text3)">After</span>
              <span style="font-size:13px;font-weight:700;color:${color};font-family:var(--mono)">${fmtV(fv)}${arrow}</span>
            </div>
          </div>
        </div>`;
      }).join('');

      const hasKpi = kpiCards.length > 0;
      // Three split canvases: CPU · Memory · I/O (each only rendered when data exists)
      const _mkSnapDiv = (id, label) =>
        `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;height:180px">
          <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">${label}</div>
          <canvas id="${id}"></canvas>
        </div>`;
      const hasCpu = sb.cpu_pct != null || sa.cpu_pct != null;
      const hasMem = sb.mem_pct != null || sa.mem_pct != null;
      const hasIO  = sb.disk_read_mb != null || sa.disk_read_mb != null || sb.net_rx_mb != null || sa.net_rx_mb != null;
      const cpuChartHtml = (hasKpi && hasCpu) ? _mkSnapDiv('res-ch-cpu','CPU · Load — before vs after') : '';
      const memChartHtml = (hasKpi && hasMem) ? _mkSnapDiv('res-ch-mem','Memory — before vs after') : '';
      const ioChartHtml  = (hasKpi && hasIO)  ? _mkSnapDiv('res-ch-io', 'I/O activity during fault') : '';
      const monitorHtml = hasMonitoring ? `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;height:180px">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Continuous monitoring — <span style="color:#f97316">CPU %</span> · <span style="color:#6366f1">Mem %</span></div>
        <canvas id="imp-ch-res"></canvas>
      </div>` : '';

      if (hasKpi || hasMonitoring) {
        const snapDivs = [cpuChartHtml, memChartHtml, ioChartHtml, monitorHtml].filter(Boolean);
        const snapCols = Math.min(snapDivs.length, 4);
        resourcesHtml = `<div style="margin-bottom:24px">
          <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">System Resources</div>
          ${hasKpi?`<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;margin-bottom:12px">${kpiCards}</div>`:''}
          ${snapDivs.length?`<div style="display:grid;grid-template-columns:repeat(${snapCols},1fr);gap:12px">${snapDivs.join('')}</div>`:''}
        </div>`;
      }
    }
  }

  // ── Section 3: LLM call impact (shown whenever LLM calls are present) ────────
  let llmImpactHtml = '';
  const _hasImpact = impact && (impact.calls_baseline > 0 || impact.calls_fault > 0);
  const _hasRawLLM = _rawLLM && _rawLLM.length > 0;
  if (_hasImpact || _hasRawLLM) {
    // Quick stats from raw calls when impact summary is unavailable
    let summaryBadges = '';
    if (_hasImpact) {
      summaryBadges = `
        <span style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:6px 12px;font-size:11px;color:var(--text3)">
          Calls&nbsp; <b style="color:var(--text)">${impact.calls_baseline}</b> baseline → <b style="color:var(--text)">${impact.calls_fault}</b> fault
        </span>
        <span style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:6px 12px;font-size:11px;color:var(--text3)">
          Blocked <b style="color:${impact.blocked_count>0?'var(--red)':'var(--text)'}">${impact.blocked_count}</b>
          &nbsp;·&nbsp; Modified <b style="color:${impact.modified_count>0?'var(--yellow)':'var(--text)'}">${impact.modified_count}</b>
          &nbsp;·&nbsp; Retries <b style="color:${impact.retry_count>0?'var(--accent)':'var(--text)'}">${impact.retry_count}</b>
        </span>`;
    } else if (_hasRawLLM) {
      const totalCalls = _rawLLM.length;
      const totalCost  = _rawLLM.reduce((a,c)=>a+(c.cost_usd||0),0);
      const avgLat     = _rawLLM.reduce((a,c)=>a+(c.latency_s||0),0)/totalCalls;
      const blocked    = _rawLLM.filter(c=>c.was_blocked).length;
      summaryBadges = `
        <span style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:6px 12px;font-size:11px;color:var(--text3)">
          <b style="color:var(--text)">${totalCalls}</b> LLM calls &nbsp;·&nbsp; avg <b style="color:var(--text)">${avgLat.toFixed(2)}s</b> &nbsp;·&nbsp; ${fmtCost(totalCost)}
        </span>
        ${blocked>0?`<span style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:6px 12px;font-size:11px;color:var(--text3)">Blocked <b style="color:var(--red)">${blocked}</b></span>`:''}
        <span style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:6px 12px;font-size:10px;color:var(--text3)">
          Baseline/fault split unavailable — see LLM Calls tab
        </span>`;
    }

    // KPI comparison grid (only when impact data exists)
    let kpiGrid = '';
    if (_hasImpact) {
      function impKpi(label, bv, fv, fmt, higherIsBetter) {
        const bvF=fmt(bv), fvF=fmt(fv);
        const changed=bv!==fv;
        const worse=higherIsBetter?fv<bv:fv>bv;
        const color=!changed?'var(--text3)':worse?'var(--red)':'var(--green)';
        const arrow=!changed?'':worse?' ▲':' ▼';
        const bg=!changed?'':worse?'border-top:2px solid var(--red)':'border-top:2px solid var(--green)';
        return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 13px;${bg}">
          <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">${label}</div>
          <div style="display:flex;flex-direction:column;gap:3px">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-size:10px;color:var(--text3)">Baseline</span>
              <span style="font-size:13px;font-weight:600;color:var(--text);font-family:var(--mono)">${bvF}</span>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-size:10px;color:var(--text3)">Fault</span>
              <span style="font-size:13px;font-weight:700;color:${color};font-family:var(--mono)">${fvF}${arrow}</span>
            </div>
          </div>
        </div>`;
      }
      const errPct=v=>(v*100).toFixed(1)+'%', latFmt=v=>v.toFixed(2)+'s', costFmt=v=>fmtCost(v);
      const tokFmt=v=>v>0?v.toLocaleString():'-', tcFmt=v=>v>0?v.toString():'-';
      kpiGrid = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:14px">
        ${impKpi('Error rate',  impact.error_rate_baseline,  impact.error_rate_fault,  errPct,  false)}
        ${impKpi('Avg latency', impact.avg_latency_baseline, impact.avg_latency_fault,  latFmt,  false)}
        ${impKpi('p99 latency', impact.p99_latency_baseline, impact.p99_latency_fault,  latFmt,  false)}
        ${impKpi('Total cost',  impact.cost_baseline,        impact.cost_fault,         costFmt, false)}
        ${impKpi('Tokens',      impact.tokens_baseline||0,   impact.tokens_fault||0,    tokFmt,  false)}
        ${(impact.tool_calls_baseline||0)+(impact.tool_calls_fault||0)>0 ? impKpi('Tool calls', impact.tool_calls_baseline||0, impact.tool_calls_fault||0, tcFmt, false) : ''}
      </div>
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;height:200px">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">LLM metrics comparison</div>
        <canvas id="imp-ch-compare"></canvas>
      </div>`;
    }

    llmImpactHtml = `<div style="margin-bottom:24px">
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">LLM Calls</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">${summaryBadges}</div>
      ${kpiGrid}
    </div>`;
  }

  // ── Section 4: Response Quality (faithfulness, hallucination, coherence) ─────
  let qualityHtml = '';
  {
    let jb = null, jf = null, jd = null;
    for (const r of (results||[])) {
      const m = typeof r.metrics==='string'?JSON.parse(r.metrics||'{}'):(r.metrics||{});
      if (m.judge_fault || m.judge_baseline) {
        jf = m.judge_fault  || {};
        jb = m.judge_baseline || {};
        jd = m.judge_delta   || {};
        break;
      }
    }
    if (jf || jb) {
      const _qDefs = [
        {k:'faithfulness',  l:'Faithfulness',  lowBad:true},
        {k:'hallucination', l:'Hallucination',  lowBad:false},
        {k:'coherence',     l:'Coherence',      lowBad:true},
      ];
      const qCards = _qDefs.map(({k, l, lowBad}) => {
        const bv = jb&&jb[k]!=null ? +jb[k] : null;
        const fv = jf&&jf[k]!=null ? +jf[k] : null;
        const dv = jd&&jd[k]!=null ? +jd[k] : (fv!=null&&bv!=null ? fv-bv : null);
        const changed = dv!=null && Math.abs(dv) > 0.02;
        const worse = changed && (lowBad ? dv < 0 : dv > 0);
        const border = !changed ? '' : worse ? 'border-top:2px solid var(--red)' : 'border-top:2px solid var(--green)';
        const fvColor = !changed ? 'var(--text)' : worse ? 'var(--red)' : 'var(--green)';
        const arrow = !changed ? '' : worse ? ' ▼' : ' ▲';
        const fmt = v => v!=null ? Math.round(v*100)+'%' : '—';
        const dstr = dv!=null ? ` <span style="font-size:10px;color:${worse?'var(--red)':'var(--green)'};">(${dv>0?'+':''}${(dv*100).toFixed(1)}pp)</span>` : '';
        return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 13px;${border}">
          <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">${l}</div>
          <div style="display:flex;flex-direction:column;gap:3px">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-size:10px;color:var(--text3)">Baseline</span>
              <span style="font-size:13px;font-weight:600;color:var(--text);font-family:var(--mono)">${fmt(bv)}</span>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-size:10px;color:var(--text3)">Under fault</span>
              <span style="font-size:13px;font-weight:700;color:${fvColor};font-family:var(--mono)">${fmt(fv)}${arrow}${dstr}</span>
            </div>
          </div>
        </div>`;
      }).join('');
      const gv = jf&&jf.guardrail_violation;
      const guardrailBadge = gv ? `<div style="margin-top:10px;padding:8px 14px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.4);border-radius:var(--radius);font-size:12px;color:var(--red);font-weight:500">⚠ Guardrail violation detected in fault phase</div>` : '';
      const _esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
      const _expl = (jf&&jf.explanations&&Object.keys(jf.explanations).length)
        ? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
            <div style="font-size:9px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Per-dimension analysis</div>
            ${Object.entries(jf.explanations).map(([k,v])=>`<div style="margin-bottom:4px"><span style="font-size:10px;font-weight:600;color:var(--text2);text-transform:capitalize">${_esc(k)}: </span><span style="font-size:11px;color:var(--text2)">${_esc(v)}</span></div>`).join('')}
           </div>` : '';
      const verdictCard = (jb&&jb.reasoning)||(jf&&jf.reasoning) ? `<div style="margin-top:10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Judge Verdict</div>
        <div style="display:grid;grid-template-columns:${(jb&&jb.reasoning)&&(jf&&jf.reasoning)?'1fr 1fr':'1fr'};gap:10px">
          ${jb&&jb.reasoning?`<div><div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Baseline</div><div style="font-size:11px;color:var(--text2);line-height:1.65">${_esc(jb.reasoning).slice(0,300)}</div></div>`:''}
          ${jf&&jf.reasoning?`<div><div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Under fault</div><div style="font-size:11px;color:var(--text2);line-height:1.65">${_esc(jf.reasoning).slice(0,300)}</div></div>`:''}
        </div>
        ${_expl}
      </div>` : '';
      qualityHtml = `<div style="margin-bottom:24px">
        <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Response Quality</div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">${qCards}</div>
        ${guardrailBadge}${verdictCard}
      </div>`;
    }
  }

  // ── Section 5: Injected faults ───────────────────────────────────────────────
  let faultSummary = '';
  if (faults.length) {
    faultSummary = `<div style="margin-bottom:24px">
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Injected faults</div>
      ${faults.map(f => {
        const p = typeof f.parameters==='string'?JSON.parse(f.parameters||'{}'):(f.parameters||{});
        const cat=f.category||'other', cc=_CAT_COLOR[cat]||'#64748b';
        const desc=faultDesc(f.kind,p);
        const dur=(f.started_at&&f.stopped_at)?fmtDur((new Date(f.stopped_at)-new Date(f.started_at))/1000):null;
        return `<div style="border:1px solid var(--border);border-left:3px solid ${cc};border-radius:var(--radius);background:var(--surface);padding:9px 14px;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="font-size:13px;font-weight:700;color:${cc}">${f.kind||'?'}</span>
            ${chip(cat,cat)}
            ${dur?`<span style="font-size:10px;color:var(--text3);margin-left:auto">⏱ ${dur}</span>`:''}
          </div>
          ${desc?`<div style="font-size:12px;color:var(--text2);margin-top:5px">${desc}</div>`:''}
        </div>`;
      }).join('')}
    </div>`;
  }

  return `
    <div style="margin-bottom:24px">
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">Session</div>
      ${row('Run ID', `#${s.id||'—'}`, true)}
      ${row('Scenario', s.name||'—', false)}
      ${row('Status', `<span style="color:${statusColor};font-weight:600">${sdot(s.status)}${s.status||'—'}</span>`, false)}
      ${row('Agent / Target', `<span style="font-family:var(--mono);background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:1px 7px;font-size:11px">${agent}</span>`, false)}
      ${row('Target address', s.target_addr||'local', true)}
      ${row('Duration', `<span style="font-weight:600">${fmtDur(s.duration_s)}</span>`, false)}
      ${row('Started', fmtDate(s.started_at), true)}
      ${row('Stopped', fmtDate(s.stopped_at), true)}
    </div>
    ${workloadHtml}
    ${resourcesHtml}
    ${llmImpactHtml}
    ${qualityHtml}
    ${faultSummary}`;
}

// ── Param label friendly names ────────────────────────────────────────────────
const _PARAM_LABELS = {
  delay_ms:'Delay', delay_s:'Delay', jitter_ms:'Jitter', loss_pct:'Loss %',
  corrupt_pct:'Corrupt %', dup_pct:'Duplicate %', interface:'Interface',
  cpu_pct:'CPU target', cores:'Cores', mem_mb:'Memory', mem_pct:'Memory %',
  mount_point:'Mount point', fill_pct:'Fill %', io_pct:'I/O target',
  process_name:'Process', signal:'Signal', service_name:'Service', action:'Action',
  container_name:'Container', path:'Path', key:'Key', value:'Value',
  table:'Table', n:'Request limit', timeout_s:'Timeout', mode:'Mode',
  text:'Injected text', interrupt_after:'Interrupt after', max_tokens:'Max tokens',
  budget_max_cost_usd:'Budget ($)', budget_input_price:'Input price', budget_output_price:'Output price',
  tool_name:'Tool name', fault:'Fault type', semantic_mode:'Mode',
  skill_name:'Skill', wrong_skill:'Misrouted to', corrupt_instruction:'Corrupted instruction',
  skill_timeout_s:'Timeout', bad_output_mode:'Output mode', old_version:'Old version',
  stale_data:'Stale data', target_pct:'Target %', mem_mhz:'Mem clock', core_mhz:'Core clock',
  generator_url:'Generator URL', generator_model:'Generator model',
};

function buildDPFaults(faults, resources) {
  if (!faults.length) return '<div style="color:var(--text3);font-size:12px;padding:4px">No faults recorded</div>';
  resources = resources||[];

  // Resource chart (if samples exist)
  let resourceChart = '';
  if (resources.length > 1) {
    resourceChart = `<div style="margin-bottom:20px">
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">Resource monitoring during fault</div>
      <div class="chart-card" style="height:200px"><div class="chart-title">CPU % · Memory %</div><canvas id="res-ch-cpu"></canvas></div>
    </div>`;
  }

  function snapRow(label, before, after) {
    if (before == null && after == null) return '';
    const fmt = v => v != null ? (typeof v === 'number' ? v.toFixed(1) : v) : '—';
    const changed = before != null && after != null && Math.abs(after - before) > 0.5;
    const color = changed ? (after > before ? 'var(--red)' : 'var(--green)') : 'var(--text2)';
    return `<div style="display:flex;gap:8px;padding:3px 0;font-size:11px">
      <span style="width:90px;color:var(--text3);flex-shrink:0">${label}</span>
      <span style="font-family:var(--mono);color:var(--text3)">${fmt(before)}</span>
      <span style="color:var(--text3)">→</span>
      <span style="font-family:var(--mono);color:${color};font-weight:${changed?'600':'400'}">${fmt(after)}</span>
    </div>`;
  }

  return resourceChart + faults.map((f, i) => {
    const p = typeof f.parameters==='string' ? JSON.parse(f.parameters||'{}') : (f.parameters||{});
    const cat = f.category||'other';
    const cc  = _CAT_COLOR[cat]||'#64748b';
    const desc = faultDesc(f.kind, p);
    const dur = (f.started_at && f.stopped_at)
      ? fmtDur((new Date(f.stopped_at)-new Date(f.started_at))/1000)
      : null;

    // Parameter rows — use friendly labels, filter internal/empty values
    const SKIP = new Set(['session_id','id']);
    const flat = flattenObj(p,'',{});
    const paramRows = Object.entries(flat)
      .filter(([k,v]) => !SKIP.has(k) && v !== null && v !== undefined && v !== '')
      .map(([k,v]) => {
        const label = _PARAM_LABELS[k] || k.replace(/_/g,' ');
        const valStr = typeof v==='string' && v.length>80
          ? `<details style="display:inline"><summary style="cursor:pointer;color:var(--accent);font-size:10px">${v.slice(0,60)}… ▶</summary><span style="font-family:var(--mono);font-size:10px;word-break:break-all">${v.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</span></details>`
          : `<span style="font-family:var(--mono);font-size:11px;color:var(--text)">${String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;')}</span>`;
        return `<div style="display:contents">
          <span style="font-size:11px;color:var(--text3);padding:3px 0">${label}</span>
          <div style="padding:3px 0">${valStr}</div>
        </div>`;
      }).join('');

    // Resource snapshots before/after
    let snapHtml = '';
    try {
      const sb = typeof f.snapshot_before === 'string' ? JSON.parse(f.snapshot_before||'{}') : (f.snapshot_before||{});
      const sa = typeof f.snapshot_after  === 'string' ? JSON.parse(f.snapshot_after||'{}')  : (f.snapshot_after||{});
      const hasSnap = Object.keys(sb).length > 0 || Object.keys(sa).length > 0;
      if (hasSnap) {
        snapHtml = `<div style="margin-bottom:10px;padding:8px 10px;background:rgba(255,255,255,.02);border-radius:4px;border:1px solid var(--border)">
          <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">System state (before → after injection)</div>
          ${snapRow('CPU %',      sb.cpu_pct,      sa.cpu_pct)}
          ${snapRow('Memory %',   sb.mem_pct,      sa.mem_pct)}
          ${snapRow('Mem used',   sb.mem_used_mb!=null?sb.mem_used_mb+'MB':null, sa.mem_used_mb!=null?sa.mem_used_mb+'MB':null)}
          ${snapRow('Load 1m',    sb.load_1,       sa.load_1)}
          ${snapRow('Net RX',     sb.net_rx_mb!=null?sb.net_rx_mb+'MB':null,  sa.net_rx_mb!=null?sa.net_rx_mb+'MB':null)}
          ${snapRow('Net TX',     sb.net_tx_mb!=null?sb.net_tx_mb+'MB':null,  sa.net_tx_mb!=null?sa.net_tx_mb+'MB':null)}
          ${snapRow('Disk read',  sb.disk_read_mb!=null?sb.disk_read_mb+'MB':null,  sa.disk_read_mb!=null?sa.disk_read_mb+'MB':null)}
          ${snapRow('Disk write', sb.disk_write_mb!=null?sb.disk_write_mb+'MB':null, sa.disk_write_mb!=null?sa.disk_write_mb+'MB':null)}
        </div>`;
      }
    } catch(e) {}

    return `<div style="border:1px solid var(--border);border-left:3px solid ${cc};border-radius:var(--radius);background:var(--surface);padding:14px 16px;margin-bottom:10px">
      <!-- Header -->
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">
        <span style="font-size:14px;font-weight:700;color:${cc}">${f.kind||'?'}</span>
        ${chip(cat, cat)}
        ${dur?`<span style="font-size:10px;color:var(--text3);background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:1px 7px">⏱ ${dur}</span>`:''}
        <span style="margin-left:auto;font-size:10px;color:var(--text3)">fault #${f.id||i+1}</span>
      </div>
      <!-- Description -->
      ${desc?`<div style="font-size:12px;font-weight:500;color:var(--text2);margin-bottom:10px;padding:6px 10px;background:rgba(255,255,255,.03);border-radius:4px;border-left:2px solid ${cc}88">${desc}</div>`:''}
      <!-- Parameters grid -->
      ${paramRows?`<div style="display:grid;grid-template-columns:140px 1fr;gap:1px 12px;margin-bottom:10px">${paramRows}</div>`:'<div style="font-size:11px;color:var(--text3);margin-bottom:8px">No parameters</div>'}
      <!-- Resource snapshots -->
      ${snapHtml}
      <!-- Timing -->
      <div style="display:flex;gap:16px;padding-top:8px;border-top:1px solid var(--border)">
        <div><span style="font-size:10px;color:var(--text3)">Started </span><span style="font-family:var(--mono);font-size:10px;color:var(--text2)">${fmtDate(f.started_at)||'—'}</span></div>
        <div><span style="font-size:10px;color:var(--text3)">Stopped </span><span style="font-family:var(--mono);font-size:10px;color:var(--text2)">${fmtDate(f.stopped_at)||'still active'}</span></div>
      </div>
    </div>`;
  }).join('');
}
function buildDPMetrics(results, oracles) {
  let html = '';

  // ── Parse first result record ──────────────────────────────────────────────
  let judgeM = null, metricRows = [];
  for (const r of (results||[])) {
    const m = typeof r.metrics==='string' ? JSON.parse(r.metrics) : (r.metrics||{});
    if (!judgeM && (m.judge_fault || m.judge_delta)) judgeM = m;
    if (!metricRows.length && m.baseline && typeof m.baseline==='object') {
      const b = flattenObj(m.baseline,'',{}), f = flattenObj(m.fault||m.chaos||{},'',{});
      const d = flattenObj(m.delta||{},'',{});
      const keys = [...new Set([...Object.keys(b),...Object.keys(f)])];
      metricRows = keys
        .filter(k => typeof b[k]==='number' || (typeof b[k]==='string' && b[k]!=='' && !isNaN(+b[k])))
        .map(k => ({key:k, b:b[k], f:f[k], d:d[k]}));
    }
  }

  // ── Section helper ─────────────────────────────────────────────────────────
  function section(title, badge, content) {
    return `<div style="margin-bottom:28px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;padding-bottom:7px;border-bottom:2px solid var(--border)">
        <span style="font-size:12px;font-weight:700;color:var(--text);letter-spacing:.02em">${title}</span>
        ${badge?`<span style="font-size:10px;color:var(--text3);background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1px 8px">${badge}</span>`:''}
      </div>
      ${content}
    </div>`;
  }

  // ── 1. Quality Scores ──────────────────────────────────────────────────────
  if (judgeM) {
    const jf = judgeM.judge_fault     || {};
    const jb = judgeM.judge_baseline  || {};
    const jd = judgeM.judge_delta     || {};
    function gauge(label, bval, fval, low) {
      const bp = bval!=null ? Math.round(+bval*100) : null;
      const fp = fval!=null ? Math.round(+fval*100) : null;
      const pct = fp!=null ? fp : bp;
      const c = pct==null ? 'var(--text3)' : low
        ? (pct<=30?'var(--green)':pct<=60?'var(--yellow)':'var(--red)')
        : (pct>=70?'var(--green)':pct>=40?'var(--yellow)':'var(--red)');
      const dv = jd[label.toLowerCase()];
      const dstr = dv!=null ? `<span style="font-size:11px;color:${+dv>0.02?'var(--red)':+dv<-0.02?'var(--green)':'var(--text3)'};font-family:var(--mono);margin-left:4px">${(+dv>0?'+':'')+dv.toFixed(3)}</span>` : '';
      const baselineRow = bp!=null ? `<div style="font-size:11px;color:var(--text3);margin-top:4px">Baseline: <b style="color:var(--text2)">${bp}%</b></div>` : '';
      return `<div style="background:var(--surface);border:1px solid var(--border);border-left:3px solid ${c};border-radius:var(--radius);padding:14px 16px">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">${label}</div>
        <div style="font-size:28px;font-weight:700;color:${c};font-variant-numeric:tabular-nums;line-height:1">${pct!=null?pct+'%':'—'}${dstr}</div>
        ${baselineRow}
        <div style="margin-top:8px;height:4px;background:var(--border);border-radius:2px;overflow:hidden">
          <div style="height:100%;width:${pct||0}%;background:${c};border-radius:2px"></div>
        </div>
        <div style="font-size:10px;color:var(--text3);margin-top:5px">${low?'Lower is better':'Higher is better'}</div>
      </div>`;
    }
    let qContent = `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
      ${gauge('Faithfulness', jb.faithfulness, jf.faithfulness, false)}
      ${gauge('Hallucination', jb.hallucination, jf.hallucination, true)}
      ${gauge('Coherence', jb.coherence, jf.coherence, false)}
    </div>`;
    if (jb.reasoning || jf.reasoning) {
      const _e = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
      const _expl2 = (jf.explanations&&Object.keys(jf.explanations).length)
        ? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
            <div style="font-size:9px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Per-dimension analysis</div>
            ${Object.entries(jf.explanations).map(([k,v])=>`<div style="margin-bottom:4px"><span style="font-size:10px;font-weight:600;color:var(--text2);text-transform:capitalize">${_e(k)}: </span><span style="font-size:11px;color:var(--text2)">${_e(v)}</span></div>`).join('')}
           </div>` : '';
      qContent += `<div style="margin-top:12px;padding:12px 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius)">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Judge Verdict</div>
        <div style="display:grid;grid-template-columns:${jb.reasoning&&jf.reasoning?'1fr 1fr':'1fr'};gap:10px">
          ${jb.reasoning?`<div><div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Baseline</div><div style="font-size:12px;color:var(--text2);line-height:1.7">${_e(jb.reasoning)}</div></div>`:''}
          ${jf.reasoning?`<div><div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Under fault</div><div style="font-size:12px;color:var(--text2);line-height:1.7">${_e(jf.reasoning)}</div></div>`:''}
        </div>
        ${_expl2}
      </div>`;
    }
    if (jf.guardrail_violation) {
      qContent += `<div style="margin-top:8px;padding:10px 14px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.4);border-radius:var(--radius);font-size:12px;color:var(--red);font-weight:500">
        &#9888;&nbsp; Guardrail violation detected in fault phase
      </div>`;
    }
    html += section('Quality Scores', 'LLM Judge', qContent);
  }

  // ── 2. Oracle Assertions ───────────────────────────────────────────────────
  const oa = (oracles||[]).filter(t => t.kind==='oracle_result');
  if (oa.length) {
    const pass = oa.filter(t=>(t.data||{}).passed).length;
    const fail = oa.length - pass;
    const passRatio = Math.round(100*pass/oa.length);
    const ratioC = passRatio>=80?'var(--green)':passRatio>=50?'var(--yellow)':'var(--red)';

    // ── Summary pill strip (MLflow-style "Detailed assessments") ──
    let pillStrip = `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:16px">
      <span style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-right:4px">Assessments</span>
      <span style="font-size:16px;font-weight:700;color:${ratioC};font-variant-numeric:tabular-nums;margin-right:8px">${passRatio}%</span>`;
    for (const t of oa) {
      const d = t.data||{};
      const ok = d.passed;
      const pc = ok?'rgba(34,197,94,.15)':'rgba(239,68,68,.15)';
      const bc = ok?'rgba(34,197,94,.4)':'rgba(239,68,68,.4)';
      const tc = ok?'var(--green)':'var(--red)';
      const score = d.score!=null?` · ${(+d.score).toFixed(2)}`:'';
      pillStrip += `<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 9px;background:${pc};border:1px solid ${bc};border-radius:20px;font-size:11px;white-space:nowrap" title="${(d.reason||'').replace(/"/g,'&quot;')}">
        <span style="color:${tc};font-weight:700;font-size:13px;line-height:1">${ok?'✓':'✗'}</span>
        <span style="color:var(--text);font-weight:500">${d.oracle||'oracle'}${score}</span>
      </span>`;
    }
    pillStrip += '</div>';

    // ── Per-phase 2-column grid ──
    let oContent = pillStrip;
    const phases = [...new Set(oa.map(t=>(t.data||{}).phase||''))];
    for (const phase of phases) {
      const phaseItems = oa.filter(t=>(t.data||{}).phase===phase);
      const phPass = phaseItems.filter(t=>(t.data||{}).passed).length;
      if (phases.length > 1) {
        const phC = phPass===phaseItems.length?'var(--green)':phPass===0?'var(--red)':'var(--yellow)';
        oContent += `<div style="display:flex;align-items:center;gap:8px;margin:14px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:11px;font-weight:700;color:var(--text);text-transform:uppercase;letter-spacing:.06em">${phase||'unknown'} phase</span>
          <span style="font-size:12px;font-weight:700;color:${phC};margin-left:auto">${phPass}/${phaseItems.length} pass</span>
        </div>`;
      }
      oContent += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">`;
      for (const t of phaseItems) {
        const d = t.data||{};
        const ok = d.passed;
        const lc = ok?'#22c55e':'#ef4444';
        const bg = ok?'rgba(34,197,94,.05)':'rgba(239,68,68,.05)';
        const sc = d.score!=null?`<span style="font-size:15px;font-weight:700;color:${lc};font-family:var(--mono);font-variant-numeric:tabular-nums">${(+d.score).toFixed(2)}</span>`:'';
        oContent += `<div style="background:${bg};border-left:3px solid ${lc};border-radius:0 var(--radius) var(--radius) 0;padding:8px 12px;display:flex;gap:8px;align-items:flex-start">
          <span style="font-size:16px;color:${lc};font-weight:700;line-height:1.3;flex-shrink:0">${ok?'✓':'✗'}</span>
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
              <span style="font-size:12px;font-weight:600;color:var(--text)">${d.oracle||'oracle'}</span>
              ${sc}
            </div>
            ${d.reason?`<div style="font-size:11px;color:var(--text3);margin-top:2px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">${String(d.reason).replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>`:''}
          </div>
        </div>`;
      }
      oContent += '</div>';
    }
    html += section('Oracle Assertions', `${pass} / ${oa.length} pass`, oContent);
  }

  // ── 3. Baseline vs Fault ───────────────────────────────────────────────────
  if (metricRows.length) {
    // Chart: baseline vs fault comparison
    const chartHtml = `<div class="chart-card" style="margin-bottom:20px;height:${Math.min(Math.max(160, metricRows.length*28+50), 400)}px">
      <div class="chart-title">Baseline vs fault — metric comparison</div>
      <canvas id="met-ch-compare"></canvas>
    </div>`;

    let tbl = chartHtml + `<table style="width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:2px solid var(--border)">
        <th style="padding:7px 12px;text-align:left;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em">Metric</th>
        <th style="padding:7px 12px;text-align:right;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em">Baseline</th>
        <th style="padding:7px 12px;text-align:right;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em">Fault</th>
        <th style="padding:7px 12px;text-align:right;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em">Delta</th>
      </tr></thead><tbody>`;
    for (const r of metricRows) {
      const dv = r.d!=null ? +r.d : null;
      const dc = dv!=null ? (Math.abs(dv)<0.0001?'var(--text3)':dv>0?'var(--red)':'var(--green)') : 'var(--text3)';
      const darrow = dv!=null && Math.abs(dv)>0.0001 ? (dv>0?' ↑':' ↓') : '';
      const ds = dv!=null ? `<span style="color:${dc};font-family:var(--mono)">${(dv>0?'+':'')+fmtMetric(r.key,dv)}${darrow}</span>` : '<span style="color:var(--text3)">—</span>';
      tbl += `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:8px 12px;font-size:12px;color:var(--text);font-weight:500">${r.key}</td>
        <td style="padding:8px 12px;font-size:12px;font-family:var(--mono);color:var(--text2);text-align:right">${fmtMetric(r.key,r.b)}</td>
        <td style="padding:8px 12px;font-size:12px;font-family:var(--mono);color:var(--text2);text-align:right">${fmtMetric(r.key,r.f)}</td>
        <td style="padding:8px 12px;text-align:right">${ds}</td>
      </tr>`;
    }
    tbl += '</tbody></table>';
    html += section('Baseline vs Fault', `${metricRows.length} metrics`, tbl);
  }

  if (!html) return `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px;color:var(--text3);gap:8px">
    <div style="font-size:28px;opacity:.3">📊</div>
    <div style="font-size:13px">No metrics collected for this run</div>
    <div style="font-size:11px;opacity:.7">Use <code>strategy=CollectStrategy.SNAPSHOT</code> and <code>oracles=[...]</code> in runner.measure()</div>
  </div>`;
  return html;
}
function buildDPCommands(commands) {
  if (!commands.length) return '<div style="color:var(--text3);font-size:12px;padding:4px">No commands recorded</div>';
  return commands.map((c,i) => {
    const raw = typeof c==='string' ? c : (c.command||c.cmd||JSON.stringify(c));
    const res = typeof c==='object' ? (c.result||c.output||(c.returncode!=null?'exit '+c.returncode:'')) : '';
    const safe = raw.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const LIMIT = 320;
    const cmdHtml = safe.length > LIMIT
      ? `<details><summary style="cursor:pointer;list-style:none;font-family:var(--mono);font-size:11px;color:var(--text2);word-break:break-all">${safe.slice(0,LIMIT)}<span style="color:var(--text3)">… (${raw.length.toLocaleString()} chars) ▶</span></summary><div style="margin-top:4px;font-family:var(--mono);font-size:11px;color:var(--text2);white-space:pre-wrap;word-break:break-all;max-height:220px;overflow-y:auto">${safe}</div></details>`
      : `<div class="cmd-text">${safe}</div>`;
    return `<div class="cmd-row">
      <div class="cmd-label">Command ${i+1}</div>
      ${cmdHtml}
      ${res?`<div class="cmd-result">&#8594; ${String(res).replace(/&/g,'&amp;').replace(/</g,'&lt;').slice(0,200)}</div>`:''}
    </div>`;
  }).join('');
}
function buildDPLLM(calls) {
  if (!calls.length) return `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px;color:var(--text3);gap:8px">
    <div style="font-size:28px;opacity:.3">🤖</div>
    <div style="font-size:13px">No LLM calls recorded for this session</div>
  </div>`;

  const total  = calls.length;
  const cost   = calls.reduce((a,c)=>a+(c.cost_usd||0),0);
  const blkCnt = calls.filter(c=>c.was_blocked).length;
  const modCnt = calls.filter(c=>c.was_modified).length;
  const lats   = calls.map(c=>c.latency_s||0);
  const avgLat = lats.reduce((a,v)=>a+v,0)/lats.length;
  const p99Lat = [...lats].sort((a,b)=>a-b)[Math.min(Math.floor(lats.length*.99),lats.length-1)];
  const totIn  = calls.reduce((a,c)=>a+(c.prompt_tokens||0),0);
  const totOut = calls.reduce((a,c)=>a+(c.completion_tokens||0),0);
  const models = [...new Set(calls.map(c=>c.model||'unknown'))];

  function kpi(label, val, color, sub) {
    return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px">
      <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">${label}</div>
      <div style="font-size:20px;font-weight:700;color:${color||'var(--text)'};font-variant-numeric:tabular-nums;line-height:1.1">${val}</div>
      ${sub?`<div style="font-size:10px;color:var(--text3);margin-top:3px">${sub}</div>`:''}
    </div>`;
  }

  const retryCnt  = calls.filter(c=>c.is_retry).length;
  const totTools  = calls.reduce((a,c)=>a+(c.response_tool_calls||0),0);
  const tpsList   = calls.map(c=>c.tokens_per_second||0).filter(v=>v>0);
  const avgTps    = tpsList.length ? tpsList.reduce((a,v)=>a+v,0)/tpsList.length : 0;
  let html = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:6px;margin-bottom:12px">
    ${kpi('Calls', total, 'var(--text)', models.length===1?models[0].split('/').pop().slice(0,18):models.length+' models')}
    ${kpi('Total cost', fmtCost(cost), 'var(--cyan)', totIn.toLocaleString()+' in / '+totOut.toLocaleString()+' out tok')}
    ${kpi('Avg latency', avgLat.toFixed(2)+'s', 'var(--text)', 'p99 '+p99Lat.toFixed(2)+'s')}
    ${avgTps>0?kpi('Avg tok/s', avgTps.toFixed(1), 'var(--text2)', tpsList.length+' measured'):''}
    ${kpi('Blocked', blkCnt, blkCnt>0?'var(--red)':'var(--green)', blkCnt>0?'fault intercepted':'all through')}
    ${kpi('Modified', modCnt, modCnt>0?'var(--yellow)':'var(--text3)', modCnt>0?'response altered':'')}
    ${kpi('Retries', retryCnt, retryCnt>0?'var(--yellow)':'var(--text3)', retryCnt>0?retryCnt+' retry calls':'')}
    ${totTools>0?kpi('Tool calls', totTools, '#a78bfa', 'from responses'):''}
  </div>`;

  // Charts: tokens stacked + latency side by side
  html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;height:180px">
      <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">Token usage per call</div>
      <canvas id="llm-ch-tokens" style="height:140px!important"></canvas>
    </div>
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;height:180px">
      <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">Latency per call <span style="font-weight:400;text-transform:none;letter-spacing:0">— <span style="color:var(--cyan)">■</span> normal <span style="color:var(--red)">■</span> blocked <span style="color:var(--yellow)">■</span> modified</span></div>
      <canvas id="llm-ch-lat" style="height:140px!important"></canvas>
    </div>
  </div>`;

  // ── Group calls by agent addr ────────────────────────────────────────────
  // Assign friendly names to agent IPs
  const _agentColors = ['#6366f1','#06b6d4','#f59e0b','#22c55e','#ec4899','#8b5cf6','#f97316'];
  const _agentMap = {};   // ip -> {name, color, idx}
  let _agentIdx = 0;
  function _resolveAgent(addr) {
    if (!addr) addr = 'unknown';
    if (!_agentMap[addr]) {
      // Try to derive a friendly name from the IP (Docker assigns sequential IPs)
      const name = addr === '127.0.0.1' || addr === '::1' ? 'localhost'
                 : addr.match(/\.(\d+)$/) ? `agent-${addr.match(/\.(\d+)$/)[1]}` : addr;
      _agentMap[addr] = {name, color: _agentColors[_agentIdx % _agentColors.length], idx: _agentIdx++};
    }
    return _agentMap[addr];
  }

  // Pre-resolve all agents so the map is built in encounter order
  calls.forEach(c => _resolveAgent(c.agent_addr||''));
  const agents = [...new Set(calls.map(c => c.agent_addr||''))];
  const multiAgent = agents.length > 1;

  // Add agent legend if multiple agents detected
  if (multiAgent) {
    html += `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px">
      <span style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em">Agents</span>
      ${agents.map(a => {
        const ag = _resolveAgent(a);
        const cnt = calls.filter(c=>(c.agent_addr||'')==a).length;
        return `<span style="display:inline-flex;align-items:center;gap:5px;padding:3px 9px;background:${ag.color}1a;border:1px solid ${ag.color}55;border-radius:20px;font-size:11px">
          <span style="width:7px;height:7px;border-radius:50%;background:${ag.color};flex-shrink:0"></span>
          <span style="color:var(--text);font-weight:500">${ag.name}</span>
          <span style="color:var(--text3)">${a}</span>
          <span style="color:var(--text3)">· ${cnt} calls</span>
        </span>`;
      }).join('')}
    </div>`;
  }

  // Render calls — grouped by agent when multi-agent, flat otherwise
  const renderCall = (c, i, globalIdx) => {
    const isBlocked  = !!c.was_blocked;
    const isModified = !!c.was_modified;
    const bdrCol = isBlocked?'rgba(239,68,68,.5)':isModified?'rgba(245,158,11,.5)':'var(--border)';
    const bgCol  = isBlocked?'rgba(239,68,68,.04)':isModified?'rgba(245,158,11,.03)':'var(--surface)';
    const ag = multiAgent ? _resolveAgent(c.agent_addr||'') : null;

    const phaseBadge = c.phase
      ? `<span style="font-size:10px;color:var(--text3);background:var(--panel);border:1px solid var(--border);border-radius:3px;padding:2px 7px">${c.phase}</span>` : '';
    const errType = c.error_type && c.error_type !== 'none' ? c.error_type : null;
    const errBadge = errType
      ? `<span style="background:rgba(239,68,68,.12);color:var(--red);font-size:10px;font-weight:600;padding:2px 8px;border-radius:3px;border:1px solid rgba(239,68,68,.3)">${errType.replace(/_/g,' ')}</span>` : '';
    const retryBadge = c.is_retry
      ? `<span style="background:rgba(99,102,241,.12);color:#a5b4fc;font-size:10px;padding:2px 7px;border-radius:3px;border:1px solid rgba(99,102,241,.3)">retry</span>` : '';
    const finalBadge = c.is_final_response
      ? `<span style="background:rgba(34,197,94,.12);color:#86efac;font-size:10px;padding:2px 7px;border-radius:3px;border:1px solid rgba(34,197,94,.3)">final</span>` : '';
    const offsetBadge = c.fault_offset_s != null
      ? `<span style="font-size:10px;color:var(--text3);margin-left:auto">⚡+${c.fault_offset_s.toFixed(1)}s</span>` : '';

    const pTok = c.prompt_tokens||0, cTok = c.completion_tokens||0, totTok = pTok+cTok||1;
    const pPct = Math.round(100*pTok/totTok), cPct = 100-pPct;

    function textBlock(label, text, tokens) {
      if (!text) return '';
      const safe = text.slice(0,4000).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const trunc = text.length>4000?`<div style="font-size:10px;color:var(--text3);margin-top:4px;font-style:italic">… truncated — ${text.length.toLocaleString()} chars total</div>`:'';
      const tokStr = tokens>0 ? `<span style="font-size:10px;color:var(--text3);margin-left:auto">${tokens.toLocaleString()} tokens</span>` : '';
      return `<details style="margin-top:5px">
        <summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;padding:5px 9px;background:var(--panel);border:1px solid var(--border);border-radius:4px;user-select:none">
          <span style="font-size:10px;color:var(--text3)">▶</span>
          <span style="font-size:11px;font-weight:500;color:var(--text2)">${label}</span>
          ${tokStr}
        </summary>
        <div style="margin-top:1px;padding:10px 12px;background:var(--bg);border:1px solid var(--border);border-top:none;border-radius:0 0 4px 4px;font-family:var(--mono);font-size:11px;color:var(--text2);white-space:pre-wrap;word-break:break-word;max-height:280px;overflow-y:auto;line-height:1.65">${safe}</div>
        ${trunc}
      </details>`;
    }

    const prompt   = (c.prompt_text||'').trim();
    const resp     = (c.response_text||'').trim();
    const sysPrmpt = (c.system_prompt||'').trim();

    const leftAccent = isBlocked?'var(--red)':isModified?'var(--yellow)':c.is_retry?'#818cf8':'var(--border)';
    const alertBar = isBlocked
      ? `<div style="background:rgba(239,68,68,.12);padding:4px 14px;font-size:10px;font-weight:700;color:var(--red);letter-spacing:.05em">⊘ BLOCKED</div>`
      : isModified
      ? `<div style="background:rgba(245,158,11,.1);padding:4px 14px;font-size:10px;font-weight:700;color:var(--yellow);letter-spacing:.05em">⚠ MODIFIED</div>`
      : '';

    const agentDot = ag ? `<span style="width:7px;height:7px;border-radius:50%;background:${ag.color};flex-shrink:0;display:inline-block"></span>` : '';
    return `<div style="background:${bgCol};border:1px solid ${bdrCol};border-left:3px solid ${ag?ag.color:leftAccent};border-radius:var(--radius);overflow:hidden;margin-bottom:7px">
      ${alertBar}
      <div style="padding:9px 14px">
        <!-- header row -->
        <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:6px">
          <span style="font-size:10px;font-weight:600;color:var(--text3);font-family:var(--mono);background:var(--panel);border:1px solid var(--border);border-radius:3px;padding:1px 5px">#${globalIdx+1}</span>
          ${agentDot}
          <span style="font-size:13px;font-weight:600;color:var(--text)">${c.model||'unknown'}</span>
          ${phaseBadge}${errBadge}${retryBadge}${finalBadge}
          ${c.finish_reason?`<span style="font-size:10px;color:var(--text3);font-family:var(--mono)">${c.finish_reason}</span>`:''}
          ${c.fault_offset_s!=null?`<span style="font-size:10px;color:var(--text3);margin-left:auto">⚡+${c.fault_offset_s.toFixed(1)}s</span>`:''}
        </div>
        <!-- stats inline row -->
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:5px;flex-wrap:wrap">
          <span style="font-size:13px;font-weight:600;color:var(--text);font-family:var(--mono)">${c.latency_s!=null?c.latency_s.toFixed(2)+'s':'—'}</span>
          <span style="font-size:11px;color:var(--text3)">|</span>
          <span style="font-size:11px;color:var(--text2);font-family:var(--mono)">${(pTok+cTok).toLocaleString()} tok</span>
          ${c.tokens_per_second>0?`<span style="font-size:10px;color:var(--text3)">@ <b style="color:var(--text2)">${c.tokens_per_second.toFixed(1)}</b> tok/s</span>`:''}
          <span style="font-size:11px;color:var(--text3)">|</span>
          <span style="font-size:11px;color:var(--cyan);font-family:var(--mono)">${fmtCost(c.cost_usd)}</span>
          ${c.ttft_s&&c.ttft_s>0?`<span style="font-size:10px;color:var(--text3)">TTFT <b style="color:var(--text2)">${c.ttft_s.toFixed(3)}s</b></span>`:''}
          ${c.message_count>0?`<span style="font-size:10px;color:var(--text3)">${c.message_count} msg${c.message_count!==1?'s':''}</span>`:''}
          ${c.response_tool_calls>0?`<span style="font-size:10px;color:#a78bfa;font-weight:600">${c.response_tool_calls} tool call${c.response_tool_calls!==1?'s':''}</span>`:''}
        </div>
        <!-- secondary details chips -->
        <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:6px">
          ${c.is_streaming?`<span style="font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid rgba(99,102,241,.4);color:#a5b4fc;background:rgba(99,102,241,.1)">streaming</span>`:''}
          ${c.temperature!=null?`<span style="font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid var(--border);color:var(--text3);background:var(--panel)">temp ${c.temperature}</span>`:''}
          ${c.max_tokens_requested>0?`<span style="font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid var(--border);color:var(--text3);background:var(--panel)">max ${c.max_tokens_requested.toLocaleString()} tok</span>`:''}
          ${c.request_size_bytes>0?`<span style="font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid var(--border);color:var(--text3);background:var(--panel)">req ${(c.request_size_bytes/1024).toFixed(1)} KB</span>`:''}
          ${c.response_size_bytes>0?`<span style="font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid var(--border);color:var(--text3);background:var(--panel)">res ${(c.response_size_bytes/1024).toFixed(1)} KB</span>`:''}
          ${c.rate_limit_remaining_requests!=null?`<span style="font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid var(--border);color:var(--text3);background:var(--panel)">rl-req ${c.rate_limit_remaining_requests.toLocaleString()}</span>`:''}
          ${c.rate_limit_remaining_tokens!=null?`<span style="font-size:9px;padding:1px 6px;border-radius:3px;border:1px solid var(--border);color:var(--text3);background:var(--panel)">rl-tok ${c.rate_limit_remaining_tokens.toLocaleString()}</span>`:''}
        </div>
        <!-- compact token bar -->
        <div style="margin-bottom:7px">
          <div style="display:flex;height:4px;border-radius:3px;overflow:hidden;background:var(--border)">
            <div style="width:${pPct}%;background:#6366f1"></div><div style="width:${cPct}%;background:#22c55e"></div>
          </div>
          <div style="display:flex;justify-content:space-between;margin-top:2px">
            <span style="font-size:9px;color:var(--text3)">Prompt ${pTok.toLocaleString()} (${pPct}%)</span>
            <span style="font-size:9px;color:var(--text3)">Completion ${cTok.toLocaleString()} (${cPct}%)</span>
          </div>
        </div>
        <!-- collapsible text blocks -->
        ${sysPrmpt ? textBlock('System Prompt', sysPrmpt, 0) : ''}
        ${textBlock('Prompt / Input', prompt, pTok)}
        ${textBlock('Response / Output', resp, cTok)}
      </div>
    </div>`;
  };  // end renderCall

  html += '<div style="margin-top:4px">';
  if (multiAgent) {
    // Group by agent, show each agent's calls in a collapsible section
    for (const addr of agents) {
      const ag = _resolveAgent(addr);
      const agCalls = calls.filter(c => (c.agent_addr||'') === addr);
      const agLat = agCalls.reduce((a,c)=>a+(c.latency_s||0),0)/agCalls.length;
      const agTok = agCalls.reduce((a,c)=>a+(c.prompt_tokens||0)+(c.completion_tokens||0),0);
      const agBlk = agCalls.filter(c=>c.was_blocked).length;
      html += `<details open style="margin-bottom:12px">
        <summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;padding:8px 12px;background:${ag.color}12;border:1px solid ${ag.color}44;border-radius:var(--radius);user-select:none;margin-bottom:8px">
          <span style="width:9px;height:9px;border-radius:50%;background:${ag.color};flex-shrink:0"></span>
          <span style="font-size:12px;font-weight:600;color:var(--text)">${ag.name}</span>
          <span style="font-size:11px;color:var(--text3);font-family:var(--mono)">${addr}</span>
          <span style="margin-left:auto;display:flex;gap:10px;align-items:center">
            <span style="font-size:10px;color:var(--text3)">${agCalls.length} calls</span>
            <span style="font-size:10px;color:var(--text3)">avg ${agLat.toFixed(2)}s</span>
            <span style="font-size:10px;color:var(--text3)">${agTok.toLocaleString()} tok</span>
            ${agBlk?`<span style="font-size:10px;color:var(--red);font-weight:600">${agBlk} blocked</span>`:''}
          </span>
        </summary>
        ${agCalls.map((c,i) => renderCall(c, i, calls.indexOf(c))).join('')}
      </details>`;
    }
  } else {
    html += calls.map((c,i) => renderCall(c, i, i)).join('');
  }
  html += '</div>';

  return html;
}
function buildDPEvents(events) {
  if (!events.length) return '<div style="color:var(--text3);font-size:12px;padding:4px">No events recorded</div>';
  return events.map(e => {
    const lvl = (e.level||'info').toLowerCase();
    return `<div class="evt-row">
      <span class="evt-time">${fmtDate(e.timestamp||e.created_at)}</span>
      <span class="evt-lvl ${lvl}">${lvl}</span>
      <span class="evt-msg">${(e.message||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')}</span>
    </div>`;
  }).join('');
}

function buildDPToolCalls(calls, session) {
  if (!Array.isArray(calls) || !calls.length) return `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px;color:var(--text3);gap:8px">
    <div style="font-size:28px;opacity:.3">🔧</div>
    <div style="font-size:13px">No tool calls recorded for this session</div>
    <div style="font-size:11px;opacity:.6">Tool calls are captured automatically when the proxy intercepts LLM traffic</div>
  </div>`;

  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  // KPI bar
  const tools   = [...new Set(calls.map(c=>c.tool_name).filter(Boolean))];
  const agents  = [...new Set(calls.map(c=>c.agent_addr).filter(Boolean))];
  const phases  = [...new Set(calls.map(c=>c.phase).filter(Boolean))];
  const errors  = calls.filter(c=>c.was_error).length;

  function kpi(label, val, color, sub) {
    return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px">
      <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">${label}</div>
      <div style="font-size:20px;font-weight:700;color:${color||'var(--text)'};font-variant-numeric:tabular-nums;line-height:1.1">${val}</div>
      ${sub?`<div style="font-size:10px;color:var(--text3);margin-top:3px">${sub}</div>`:''}
    </div>`;
  }

  let html = `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:20px">
    ${kpi('Total calls', calls.length, 'var(--text)', tools.length+' unique tools')}
    ${kpi('Agents', agents.length||1, 'var(--cyan)', agents.length ? agents.map(a=>a.split(':')[0]).join(', ').slice(0,28) : (session&&session.target_addr)||'local')}
    ${kpi('Phases', phases.length, 'var(--text3)', phases.join(', ').slice(0,28)||'—')}
    ${kpi('Errors', errors, errors>0?'var(--red)':'var(--green)', errors>0?'tool returned error':'all successful')}
  </div>`;

  // Table
  html += `<div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead>
      <tr style="border-bottom:1px solid var(--border)">
        <th style="text-align:left;padding:7px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:600;white-space:nowrap">#</th>
        <th style="text-align:left;padding:7px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:600;white-space:nowrap">Tool</th>
        <th style="text-align:left;padding:7px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:600;white-space:nowrap">Agent</th>
        <th style="text-align:left;padding:7px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:600;white-space:nowrap">Phase</th>
        <th style="text-align:left;padding:7px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:600;white-space:nowrap">Arguments</th>
        <th style="text-align:left;padding:7px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:600;white-space:nowrap">Result</th>
        <th style="text-align:left;padding:7px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:600;white-space:nowrap">Time</th>
      </tr>
    </thead>
    <tbody>`;

  calls.forEach((c, i) => {
    const isErr = c.was_error;
    const args  = typeof c.arguments === 'object' ? JSON.stringify(c.arguments, null, 2) : (c.arguments||'{}');
    const res   = c.result||'';
    const LIMIT = 120;

    function collapseCell(text) {
      const safe = esc(text);
      if (text.length <= LIMIT) return `<span style="font-family:var(--mono);font-size:10px;color:var(--text2)">${safe}</span>`;
      return `<details><summary style="font-family:var(--mono);font-size:10px;color:var(--text2);cursor:pointer;list-style:none">${esc(text.slice(0,LIMIT))}… <span style="color:var(--accent)">▶</span></summary><pre style="font-family:var(--mono);font-size:10px;white-space:pre-wrap;word-break:break-all;margin:4px 0 0;color:var(--text2)">${safe}</pre></details>`;
    }

    const agentDisplay = c.agent_addr ? c.agent_addr.split(':')[0] : (session&&session.target_addr||'local').split(':')[0];
    const rowBg = isErr ? 'rgba(239,68,68,.06)' : (i%2===0 ? 'transparent' : 'rgba(255,255,255,.02)');

    html += `<tr style="border-bottom:1px solid var(--border);background:${rowBg};vertical-align:top">
      <td style="padding:7px 10px;color:var(--text3);font-family:var(--mono);font-size:10px">${i+1}</td>
      <td style="padding:7px 10px;white-space:nowrap">
        <span style="font-family:var(--mono);font-size:11px;font-weight:600;color:${isErr?'var(--red)':'var(--cyan)'}">${esc(c.tool_name||'—')}</span>
        ${isErr?'<span style="font-size:9px;background:rgba(239,68,68,.15);color:var(--red);border-radius:4px;padding:1px 5px;margin-left:5px">ERR</span>':''}
      </td>
      <td style="padding:7px 10px;white-space:nowrap"><span style="font-family:var(--mono);font-size:10px;color:var(--text2)">${esc(agentDisplay)}</span></td>
      <td style="padding:7px 10px;white-space:nowrap"><span style="font-size:10px;color:var(--text3)">${esc(c.phase||'—')}</span></td>
      <td style="padding:7px 10px;max-width:240px">${collapseCell(args)}</td>
      <td style="padding:7px 10px;max-width:240px">${collapseCell(res||'—')}</td>
      <td style="padding:7px 10px;white-space:nowrap"><span style="font-size:10px;color:var(--text3)">${fmtDate(c.timestamp)}</span></td>
    </tr>`;
  });

  html += `</tbody></table></div>`;
  return html;
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
    # Bulk-fetch oracle counts + judge scores in two SQL queries
    conn = _sql.connect(db.path, timeout=5)
    extras = _bulk_session_extras(conn)
    conn.close()
    result = []
    for row in sessions:
        s = dict(row)
        data = db.export_session(s["id"])
        duration_s = _calc_duration(s.get("started_at"), s.get("stopped_at"))
        faults_with_cat = [
            {**f, "category": _fault_category(f.get("kind", ""))}
            for f in data["faults"]
        ]
        ex = extras.get(s["id"], {})
        result.append({
            "id":                s["id"],
            "name":              s["name"],
            "status":            s["status"],
            "started_at":        s.get("started_at"),
            "stopped_at":        s.get("stopped_at"),
            "duration_s":        duration_s,
            "faults":            faults_with_cat,
            "target_type":       s.get("target_type", ""),
            "target_addr":       s.get("target_addr", ""),
            "oracle_pass":       ex.get("oracle_pass", 0),
            "oracle_fail":       ex.get("oracle_fail", 0),
            "judge_faithfulness": ex.get("judge_faithfulness"),
            "judge_hallucination": ex.get("judge_hallucination"),
            "metric_delta":      ex.get("metric_delta"),
        })
    return JSONResponse(result)


@app.get("/api/session/{session_id}")
async def api_session(session_id: int):
    db = SessionDB()
    data = db.export_session(session_id)
    results = db.get_results(session_id)
    traces  = db.get_trace(session_id)
    s = data["session"]
    duration_s = _calc_duration(s.get("started_at"), s.get("stopped_at"))
    faults_with_cat = [
        {**f, "category": _fault_category(f.get("kind", ""))}
        for f in data["faults"]
    ]
    oracle_traces = [
        t for t in traces if t.get("kind") == "oracle_result"
    ]
    return JSONResponse({
        "session":  {**s, "duration_s": duration_s},
        "faults":   faults_with_cat,
        "events":   data["events"],
        "commands": data.get("commands", []),
        "results":  results,
        "oracles":  oracle_traces,
    })


@app.get("/api/session/{session_id}/llm_calls")
async def api_session_llm_calls(session_id: int):
    """LLM calls captured for a single session."""
    db = SessionDB()
    calls = db.get_llm_calls(session_id)
    return JSONResponse(calls)


@app.get("/api/session/{session_id}/tool_calls")
async def api_session_tool_calls(session_id: int):
    """Individual tool calls captured for a single session."""
    db = SessionDB()
    try:
        calls = db.get_tool_calls(session_id)
        return JSONResponse(calls)
    except Exception:
        return JSONResponse([])


@app.get("/api/session/{session_id}/impact")
async def api_session_impact(session_id: int):
    """Fault impact summary for a session (auto-computed at session close)."""
    db = SessionDB()
    try:
        impact = db.get_fault_impact(session_id)
        if impact is None:
            return JSONResponse(None)
        return JSONResponse(impact)
    except Exception:
        return JSONResponse(None)


@app.get("/api/session/{session_id}/resources")
async def api_session_resources(session_id: int):
    """Resource monitoring samples for a session."""
    db = SessionDB()
    try:
        samples = db.get_resource_samples(session_id)
        return JSONResponse(samples)
    except Exception:
        return JSONResponse([])


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

def _bulk_session_extras(conn) -> dict:
    """One-pass bulk fetch of oracle counts + judge scores for all sessions."""
    import json as _json2
    oracle: dict = {}
    try:
        for r in conn.execute(
            "SELECT session_id, data FROM traces WHERE kind='oracle_result'"
        ).fetchall():
            sid = r[0]
            try:
                d = _json2.loads(r[1]) if isinstance(r[1], str) else r[1]
                passed = bool(d.get("passed"))
            except Exception:
                passed = False
            c = oracle.setdefault(sid, {"oracle_pass": 0, "oracle_fail": 0})
            if passed:
                c["oracle_pass"] += 1
            else:
                c["oracle_fail"] += 1
    except Exception:
        pass

    judge: dict = {}
    try:
        for r in conn.execute(
            "SELECT session_id, metrics FROM results ORDER BY id"
        ).fetchall():
            sid = r[0]
            if sid in judge:
                continue
            try:
                m = _json2.loads(r[1]) if isinstance(r[1], str) else (r[1] or {})
                info: dict = {}
                jf = m.get("judge_fault") or {}
                if jf.get("faithfulness") is not None:
                    info["judge_faithfulness"] = jf["faithfulness"]
                if jf.get("hallucination") is not None:
                    info["judge_hallucination"] = jf["hallucination"]
                delta = m.get("delta") or {}
                num_delta = {k: v for k, v in list(delta.items())[:4]
                             if isinstance(v, (int, float))}
                if num_delta:
                    info["metric_delta"] = num_delta
                if info:
                    judge[sid] = info
            except Exception:
                pass
    except Exception:
        pass

    result: dict = {}
    for sid in set(oracle) | set(judge):
        result[sid] = {
            **oracle.get(sid, {"oracle_pass": 0, "oracle_fail": 0}),
            **judge.get(sid, {}),
        }
    return result


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
