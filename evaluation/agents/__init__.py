from evaluation.agents.base import AgentSystem, AgentRunResult
from evaluation.agents.autogen_style import AutoGenStyleAgent
from evaluation.agents.mad_style import MADStyleAgent
from evaluation.agents.mapcoder_style import MapCoderStyleAgent

__all__ = [
    "AgentSystem",
    "AgentRunResult",
    "AutoGenStyleAgent",
    "MADStyleAgent",
    "MapCoderStyleAgent",
]

# ── Style-agent registry (always available, no optional deps) ──────────────
REGISTRY: dict[str, type[AgentSystem]] = {
    "autogen":   AutoGenStyleAgent,
    "mad":       MADStyleAgent,
    "mapcoder":  MapCoderStyleAgent,
}

# ── Real-framework adapters (optional; require pip install chaos-jungle[evaluation]) ──
# Registered lazily so the core package remains importable without the deps.
def _register_real_adapters() -> None:
    from evaluation.agents.autogen_real import AutoGenRealAgent
    from evaluation.agents.langgraph_real import LangGraphRealAgent
    from evaluation.agents.crewai_real import CrewAIRealAgent
    REGISTRY["autogen-real"]   = AutoGenRealAgent
    REGISTRY["langgraph-real"] = LangGraphRealAgent
    REGISTRY["crewai-real"]    = CrewAIRealAgent


# Register real adapters unconditionally — the ImportError only surfaces when
# the adapter's run() method is actually called and tries to import the SDK.
# This lets the REGISTRY dict be populated for introspection without requiring
# the optional packages at import time.
_register_real_adapters()
