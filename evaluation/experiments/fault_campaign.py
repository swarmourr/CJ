"""CJ fault campaign configuration.

Defines the fault catalog used in the evaluation.  Every fault entry is a
dict that can be passed to ``build_cj_fault()`` to obtain a ready-to-use
CJ Scenario.

Fault selection criteria
------------------------
Selected faults have:
  1. Clear activation evidence (verify_active returns a real check).
  2. Deterministic expected behaviour in smoke tests.
  3. LocalTarget compatibility (no SSH or GPU required for CI).

Common state-of-the-art subset (matches AgentChaos faults):
  - llm_timeout        → LLMTimeout
  - llm_rate_limit     → LLMRateLimit
  - llm_unavailable    → LLMUnavailable
  - response_truncation → LLMResponseCorrupt(mode="truncate")
  - malformed_response → LLMResponseCorrupt(mode="invalid_json")

CJ-specific extensions:
  - tool_failure       → ToolFault
  - token_starvation   → LLMTokenStarvation
  - llm_latency        → LLMLatency
"""

from __future__ import annotations

from typing import Any

# ── Fault catalog ──────────────────────────────────────────────────────────────
# Each entry: name, cj_class, parameters, layer, description, soa_comparable

FAULT_CATALOG: list[dict[str, Any]] = [
    # ── Common SoA subset ──────────────────────────────────────────────────────
    {
        "name":           "llm_timeout",
        "cj_class":       "LLMTimeout",
        "parameters":     {"timeout_s": 5.0},
        "layer":          "llm",
        "description":    "LLM API hangs for 5 s then returns 504",
        "soa_comparable": True,
    },
    {
        "name":           "llm_rate_limit",
        "cj_class":       "LLMRateLimit",
        "parameters":     {"n": 0},
        "layer":          "llm",
        "description":    "Every LLM request is rate-limited (HTTP 429)",
        "soa_comparable": True,
    },
    {
        "name":           "llm_unavailable",
        "cj_class":       "LLMUnavailable",
        "parameters":     {},
        "layer":          "llm",
        "description":    "LLM API completely unavailable (HTTP 503)",
        "soa_comparable": True,
    },
    {
        "name":           "response_truncation",
        "cj_class":       "LLMResponseCorrupt",
        "parameters":     {"mode": "truncate"},
        "layer":          "llm",
        "description":    "LLM response truncated to half length (partial JSON)",
        "soa_comparable": True,
    },
    {
        "name":           "malformed_response",
        "cj_class":       "LLMResponseCorrupt",
        "parameters":     {"mode": "invalid_json"},
        "layer":          "llm",
        "description":    "LLM response replaced with non-JSON string",
        "soa_comparable": True,
    },
    # ── CJ-specific extensions ─────────────────────────────────────────────────
    {
        "name":           "tool_failure",
        "cj_class":       "ToolFault",
        "parameters":     {},
        "layer":          "llm",
        "description":    "All tool-call messages intercepted and failed",
        "soa_comparable": False,
    },
    {
        "name":           "token_starvation",
        "cj_class":       "LLMTokenStarvation",
        "parameters":     {"max_tokens": 10},
        "layer":          "llm",
        "description":    "LLM max_tokens capped to 10 — forces truncated responses",
        "soa_comparable": False,
    },
    {
        "name":           "llm_latency",
        "cj_class":       "LLMLatency",
        "parameters":     {"delay_s": 3.0},
        "layer":          "llm",
        "description":    "3 s artificial delay on every LLM API call",
        "soa_comparable": False,
    },
    # ── Role-scoped multi-agent proxy faults ─────────────────────────────────────
    {
        "name":            "planner_llm_unavailable",
        "cj_class":        "LLMUnavailable",
        "parameters":      {"selector": {"agent_role": "planner"}},
        "layer":           "llm",
        "description":     "Planner-scoped LLM API unavailable fault",
        "soa_comparable":  False,
        "publication_all": False,
    },
    {
        "name":            "reviewer_response_corrupt",
        "cj_class":        "LLMResponseCorrupt",
        "parameters":      {"mode": "invalid_json", "selector": {"agent_role": "reviewer"}},
        "layer":           "llm",
        "description":     "Reviewer-scoped malformed response fault",
        "soa_comparable":  False,
        "publication_all": False,
    },
    {
        "name":            "coder_tool_fault",
        "cj_class":        "ToolFault",
        "parameters":      {"selector": {"agent_role": "coder"}},
        "layer":           "tool",
        "description":     "Coder-scoped tool-call failure where tool-role traffic exists",
        "soa_comparable":  False,
        "publication_all": False,
    },
]

# Lookup by name
_CATALOG_BY_NAME: dict[str, dict] = {f["name"]: f for f in FAULT_CATALOG}


def get_fault_spec(name: str) -> dict:
    """Return the fault spec dict by name or raise KeyError."""
    if name not in _CATALOG_BY_NAME:
        available = sorted(_CATALOG_BY_NAME)
        raise KeyError(f"Unknown fault {name!r}. Available: {available}")
    return _CATALOG_BY_NAME[name]


def build_cj_fault(name: str):
    """Construct and return a CJ Fault object for the named fault.

    Parameters
    ----------
    name : str
        One of the keys in FAULT_CATALOG (e.g. ``"llm_timeout"``).

    Returns
    -------
    chaos_jungle.faults.base.Fault

    Proxy routing note
    ------------------
    LLM proxy faults are configured with ``base_url_env="CJ_EVAL_BASE_URL"``
    so that CJ temporarily replaces that variable with the proxy URL when the
    fault starts (and restores it on stop).  ModelClient reads the same
    variable on every request, so it automatically routes through the proxy
    during the fault phase without any additional wiring.

    The ``upstream`` argument points the proxy at the real API so it can
    forward non-faulted portions of requests correctly.
    """
    import os
    import importlib

    spec     = get_fault_spec(name)
    cls_name = spec["cj_class"]
    params   = dict(spec["parameters"])

    mod = importlib.import_module("chaos_jungle.faults.llm")
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise ImportError(f"CJ fault class {cls_name!r} not found in chaos_jungle.faults.llm")

    # Inject routing parameters into every LLM proxy fault so that CJ
    # modifies CJ_EVAL_BASE_URL (the same variable ModelClient reads) rather
    # than the default OPENAI_BASE_URL.
    from chaos_jungle.faults.llm import _LLMProxyFault, _DEFAULT_UPSTREAM
    if issubclass(cls, _LLMProxyFault):
        real_upstream = os.environ.get("CJ_EVAL_BASE_URL", _DEFAULT_UPSTREAM).rstrip("/")
        # Strip /v1 suffix: the CJ proxy appends the full request path
        # (/v1/chat/completions) to upstream, so upstream must NOT end with
        # /v1 to avoid constructing upstream/v1/v1/chat/completions.
        if real_upstream.endswith("/v1"):
            real_upstream = real_upstream[:-3]
        params.setdefault("upstream",     real_upstream)
        params.setdefault("base_url_env", "CJ_EVAL_BASE_URL")

    return cls(**params)


def build_cj_scenario(name: str, scenario_name: str | None = None):
    """Build a CJ Scenario wrapping the named fault."""
    from chaos_jungle import Scenario
    fault = build_cj_fault(name)
    sname = scenario_name or f"eval-{name}"
    return Scenario(sname, [fault])
