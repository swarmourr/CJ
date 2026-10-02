"""Pure helper functions for building fault lifecycle evidence records.

Extracted from llm_proxy.py so they can be unit-tested without starting
the HTTP server or importing the full proxy module.

Public API
----------
canonical_json(payload)          -> str | None
build_lifecycle_chain(...)       -> list[dict]
describe_expected(fault, cfg)    -> str
describe_observed(fault, ev, ok) -> str
"""
from __future__ import annotations

import hashlib
import json

# Map proxy fault name → (CJ class name, layer)
FAULT_META: dict[str, tuple[str, str]] = {
    "latency":               ("LLMLatency",              "llm"),
    "rate_limit":            ("LLMRateLimit",             "llm"),
    "timeout":               ("LLMTimeout",               "llm"),
    "corrupt":               ("LLMResponseCorrupt",       "llm"),
    "unavailable":           ("LLMUnavailable",           "llm"),
    "tool_fault":            ("ToolFault",                "llm"),
    "hallucinate":           ("LLMHallucination",         "llm"),
    "stream_interrupt":      ("LLMStreamInterrupt",       "llm"),
    "token_starve":          ("LLMTokenStarvation",       "llm"),
    "mcp_tool_error":        ("MCPFault",                 "llm"),
    "mcp_unavailable":       ("MCPFault",                 "llm"),
    "mcp_timeout":           ("MCPFault",                 "llm"),
    "semantic_corrupt":      ("SemanticCorrupt",          "llm"),
    "budget_exceeded":       ("LLMBudgetExceeded",        "llm"),
    "skill_misroute":        ("SkillMisroute",            "skill"),
    "skill_conflict":        ("ConflictingSkills",        "skill"),
    "skill_bad_output":      ("SkillBadOutput",           "skill"),
    "skill_instruction_corrupt": ("SkillInstructionCorrupt", "skill"),
    "skill_version_skew":    ("SkillVersionSkew",         "skill"),
    "skill_memory_stale":    ("SkillMemoryStale",         "skill"),
    "passthrough":           ("",                         "llm"),
}

# Faults that manifest by definition when triggered (blocking/timing)
_BLOCKING_FAULTS: frozenset[str] = frozenset({
    "latency", "rate_limit", "timeout", "unavailable",
    "token_starve", "mcp_unavailable", "mcp_timeout",
    "mcp_tool_error", "tool_fault", "stream_interrupt",
    "budget_exceeded",
})


def canonical_json(payload: "bytes | str | dict | None") -> "str | None":
    """Normalize a JSON payload to canonical form.

    Uses sorted keys and compact separators so that two semantically
    identical payloads produce the same string, regardless of whitespace
    or key ordering.  Returns ``None`` if the payload cannot be parsed.
    """
    if payload is None:
        return None
    try:
        if isinstance(payload, (bytes, str)):
            obj = json.loads(payload)
        else:
            obj = payload
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return None


def describe_expected(fault_type: str, cfg: "dict") -> str:
    """Human-readable description of what the fault should produce."""
    if fault_type == "latency":
        return f"added delay >= {cfg.get('delay_s', 0):.1f}s"
    if fault_type == "rate_limit":
        return f"HTTP 429 after {cfg.get('n', cfg.get('max_requests', '?'))} requests"
    if fault_type == "timeout":
        return f"connection held for {cfg.get('timeout_s', '?')}s then 504"
    if fault_type == "corrupt":
        return f"response body {cfg.get('mode', 'truncated')}"
    if fault_type == "unavailable":
        return "HTTP 503 Service Unavailable"
    if fault_type == "hallucinate":
        preview = str(cfg.get("text", cfg.get("inject_text", "")))[:60]
        return f"response replaced with hallucination: {preview!r}..."
    if fault_type == "token_starve":
        return f"max_tokens capped at {cfg.get('max_tokens', '?')}"
    if fault_type == "stream_interrupt":
        return f"stream cut after {cfg.get('interrupt_after', cfg.get('after', '?'))} SSE events"
    if fault_type == "tool_fault":
        return f"tool error injected for {cfg.get('tool_name', '*')!r}"
    if fault_type.startswith("mcp_"):
        return f"MCP fault: {fault_type}"
    if fault_type == "budget_exceeded":
        return "HTTP 402 budget exceeded"
    return f"fault: {fault_type}"


def describe_observed(fault_type: str, ev: "dict", triggered: bool) -> str:
    """Human-readable description of what actually happened."""
    if not triggered:
        return "not triggered — request did not match fault condition"
    if fault_type == "latency":
        observed = ev.get("observed_injected_delay_s")
        configured = ev.get("configured_delay_s", ev.get("delay_s", "?"))
        if observed is not None:
            return f"delayed {observed:.4f}s (configured {configured}s)"
        return "delayed"
    if fault_type in {"rate_limit", "unavailable", "budget_exceeded"}:
        status = ev.get("http_status", "?")
        return f"HTTP {status} returned"
    if fault_type == "timeout":
        return "connection held then 504"
    if fault_type == "corrupt":
        mode = ev.get("mode", "?")
        bh = ev.get("before_hash", "")[:8]
        ah = ev.get("after_hash", "")[:8]
        return f"response {mode}d (before={bh} after={ah})"
    if fault_type == "hallucinate":
        preview = str(ev.get("injected_text_preview", ""))[:80]
        return f"response replaced: {preview!r}"
    if fault_type == "token_starve":
        return "max_tokens rewritten in request"
    if fault_type == "stream_interrupt":
        n = ev.get("events_forwarded", "?")
        return f"stream cut after {n} events"
    if fault_type == "tool_fault":
        return "tool error injected"
    if fault_type.startswith("mcp_"):
        return "MCP fault applied"
    return "triggered"


def build_lifecycle_chain(
    chain: "list[dict]",
    triggered_faults: "list[str]",
    fault_evidence: "dict",
    call_index: int,
    req_body: "dict | None" = None,
) -> "list[dict]":
    """Build full causal-chain lifecycle records for one LLM call.

    One record per configured fault covering the full lifecycle:
      ``configured → activated → target_matched → triggered →
        applied → manifested``

    Primary experiment results must filter on ``manifested=True``.

    Parameters
    ----------
    chain :
        List of fault config dicts from the proxy FAULT_CHAIN.
    triggered_faults :
        Fault names that fired on this request.
    fault_evidence :
        Per-fault evidence dict produced by the mutation functions.
    call_index :
        The CJ trace / call index for this request.
    req_body :
        Parsed request body for target description.
    """
    records = []
    for cfg in chain:
        fault_type = cfg.get("fault", "")
        ev = fault_evidence.get(fault_type, {})
        triggered = fault_type in triggered_faults

        # Manifestation: use canonical hash comparison for content-mutation faults;
        # blocking/timing faults manifest by definition when triggered.
        manifested = False
        if triggered:
            b_hash = ev.get("before_hash", "")
            a_hash = ev.get("after_hash", "")
            if b_hash and a_hash:
                manifested = (b_hash != a_hash)
            elif fault_type in _BLOCKING_FAULTS:
                manifested = True
            elif ev:
                manifested = True

        # Target description
        model = (req_body or {}).get("model", "")
        operation = "mcp.call" if fault_type.startswith("mcp_") else "chat.completions"
        target_info: dict = {"operation": operation, "call_index": call_index}
        if model:
            target_info["model"] = model
        if fault_type == "tool_fault":
            target_info["tool_name"] = cfg.get("tool_name", "*")

        fault_class, layer = FAULT_META.get(fault_type, ("", "llm"))
        orig = ev.get("original_canonical")
        mutated = ev.get("mutated_canonical")

        rec = {
            "fault_id": f"{fault_type}_call{call_index}",
            "fault_type": fault_type,
            "fault_class": fault_class,
            "layer": layer,
            "target": target_info,
            "configured": True,
            "activated": True,
            "target_matched": _fault_target_reached(fault_type, cfg, req_body),
            "triggered": triggered,
            "applied": triggered,
            "manifested": manifested,
            "recovered": None,
            "evidence": {
                "method": "proxy_interception",
                "expected": describe_expected(fault_type, cfg),
                "observed": describe_observed(fault_type, ev, triggered),
                **{k: v for k, v in ev.items()
                   if k not in {"original_canonical", "mutated_canonical"}},
            },
            "original_value": orig,
            "mutated_value": mutated,
            "delivered_value": mutated if manifested else orig,
        }
        records.append(rec)
    return records


def _is_tool_request(body: "dict | None") -> bool:
    if not body:
        return False
    return any(m.get("role") == "tool" for m in body.get("messages", []))


def _is_mcp_request(body: "dict | None") -> bool:
    if not body:
        return False
    return "jsonrpc" in body or "method" in body


def _fault_target_reached(fault_type: str, cfg: "dict", body: "dict | None") -> bool:
    if fault_type == "tool_fault":
        if not _is_tool_request(body):
            return False
        tool_name = cfg.get("tool_name", "")
        if not tool_name:
            return True
        return any(
            m.get("name") == tool_name
            for m in (body.get("messages", []) if body else [])
            if m.get("role") == "tool"
        )
    if fault_type.startswith("mcp_"):
        return _is_mcp_request(body)
    if fault_type.startswith("skill_"):
        return _is_tool_request(body)
    return True
