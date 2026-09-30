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

REGISTRY: dict[str, type[AgentSystem]] = {
    "autogen":   AutoGenStyleAgent,
    "mad":       MADStyleAgent,
    "mapcoder":  MapCoderStyleAgent,
}
