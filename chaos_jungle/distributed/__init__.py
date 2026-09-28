"""chaos-jungle distributed injection — coordinated multi-host fault injection.

Quick start::

    from chaos_jungle.distributed import DistributedScenario, Injection
    from chaos_jungle.faults import LLMLatency, NetworkLoss
    from chaos_jungle.targets import HTTPTarget, SSHTarget

    scenario = DistributedScenario(
        name="agent-failure-test",
        members=[
            Injection(
                id="llm-delay",
                target=HTTPTarget("http://agent-node:8080"),
                fault=LLMLatency(delay_s=1.0),
            ),
            Injection(
                id="net-loss",
                target=SSHTarget("tool-node", user="ubuntu"),
                fault=NetworkLoss("5%"),
            ),
        ],
    )

    from chaos_jungle.distributed import DistributedCoordinator
    evidence = DistributedCoordinator(scenario).run()
    print(evidence.verdict)              # valid | invalid | inconclusive
    print(evidence.activation_skew_ms)  # observed skew across all members
"""

from .coordinator import DistributedCoordinator
from .evidence import GroupActivationEvidence, MemberEvidence
from .scenario import (
    AtomicityConfig,
    DistributedSafetyConfig,
    DistributedScenario,
    Injection,
    SyncConfig,
)

__all__ = [
    "AtomicityConfig",
    "DistributedCoordinator",
    "DistributedSafetyConfig",
    "DistributedScenario",
    "GroupActivationEvidence",
    "Injection",
    "MemberEvidence",
    "SyncConfig",
]
