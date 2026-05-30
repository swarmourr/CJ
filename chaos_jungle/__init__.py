"""chaos-jungle — a generic chaos engineering framework.

Quick start::

    from chaos_jungle import Scenario, ChaosRunner
    from chaos_jungle.faults import NetworkDelay, StorageCorrupt
    from chaos_jungle.targets import LocalTarget

    scenario = Scenario("my-test", faults=[NetworkDelay("100ms")])
    runner = ChaosRunner(scenario, LocalTarget())
    runner.start()
    # ... your workload ...
    runner.stop()

Decorator style::

    from chaos_jungle.decorators import chaos
    from chaos_jungle.faults import NetworkDelay

    @chaos(NetworkDelay("100ms"))
    def my_experiment():
        run_pipeline()

Context manager style::

    from chaos_jungle.decorators import chaos_session
    from chaos_jungle.faults import NetworkLoss

    with chaos_session(NetworkLoss("5%")) as session:
        run_pipeline()
"""

from chaos_jungle.scenario import Scenario
from chaos_jungle.runner import ChaosRunner
from chaos_jungle.suite import ExperimentSuite
from chaos_jungle.decorators import chaos, chaos_session
from chaos_jungle.guardrails import ConflictError, ConflictWarning
from chaos_jungle.preflight import detect_pkg_manager, PKG_MAP
from chaos_jungle.faults import (
    Fault,
    PreflightError,
    NetworkDelay,
    NetworkLoss,
    NetworkCorrupt,
    NetworkDuplicate,
    StorageCorrupt,
    SilentNetworkCorrupt,
)
from chaos_jungle.targets import LocalTarget, SSHTarget, HTTPTarget

__version__ = "0.1.0"

__all__ = [
    # Core
    "Scenario",
    "ChaosRunner",
    "ExperimentSuite",
    # Decorators
    "chaos",
    "chaos_session",
    # Faults
    "Fault",
    "PreflightError",
    "NetworkDelay",
    "NetworkLoss",
    "NetworkCorrupt",
    "NetworkDuplicate",
    "StorageCorrupt",
    "SilentNetworkCorrupt",
    # Targets
    "LocalTarget",
    "SSHTarget",
    "HTTPTarget",
    # Guardrails
    "ConflictError",
    "ConflictWarning",
    # Preflight / auto-install
    "detect_pkg_manager",
    "PKG_MAP",
]
