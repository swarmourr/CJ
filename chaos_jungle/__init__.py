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

Measure style (auto-records return dict as results)::

    from chaos_jungle.decorators import chaos_measure
    from chaos_jungle.faults import NetworkDelay

    @chaos_measure(NetworkDelay("100ms"), scenario_name="E1")
    def run_experiment():
        run_pipeline()
        return {"retries": 3, "throughput_mbps": 42.1}

    summary = run_experiment()
    print(summary["duration_s"], "s of chaos")
"""

from chaos_jungle.scenario import Scenario
from chaos_jungle.runner import ChaosRunner, MeasurementResult
from chaos_jungle.suite import ExperimentSuite
from chaos_jungle.decorators import chaos, chaos_session, chaos_measure
from chaos_jungle.metrics import Metric, PingLatency, CommandMetric, FileIntegrity, metric, ScriptMetric
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
    LLMLatency,
    LLMRateLimit,
    LLMTimeout,
    LLMResponseCorrupt,
    LLMUnavailable,
    ToolFault,
    LLMHallucination,
    LLMStreamInterrupt,
    LLMTokenStarvation,
    MCPFault,
    SemanticCorrupt,
    RedisStateCorrupt,
    JsonStateCorrupt,
    PostgresStateCorrupt,
)
from chaos_jungle.targets import LocalTarget, SSHTarget, HTTPTarget
from chaos_jungle.fetch import fetch, collect_logs, export_db_to_csv, FetchResult
from chaos_jungle.faults.bpf import iface_for_ip
from chaos_jungle.judge import LLMJudge, JudgeScore, average_scores

__version__ = "0.1.0"

__all__ = [
    # Core
    "Scenario",
    "ChaosRunner",
    "MeasurementResult",
    "ExperimentSuite",
    # Decorators
    "chaos",
    "chaos_session",
    "chaos_measure",
    # Metrics
    "Metric",
    "PingLatency",
    "CommandMetric",
    "FileIntegrity",
    "metric",
    "ScriptMetric",
    # Faults
    "Fault",
    "PreflightError",
    "NetworkDelay",
    "NetworkLoss",
    "NetworkCorrupt",
    "NetworkDuplicate",
    "StorageCorrupt",
    "SilentNetworkCorrupt",
    "LLMLatency",
    "LLMRateLimit",
    "LLMTimeout",
    "LLMResponseCorrupt",
    "LLMUnavailable",
    "ToolFault",
    "LLMHallucination",
    "LLMStreamInterrupt",
    "LLMTokenStarvation",
    "MCPFault",
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
    # Fetch / data collection
    "fetch",
    "collect_logs",
    "export_db_to_csv",
    "FetchResult",
    # Network utilities
    "iface_for_ip",
    # Semantic / state faults
    "SemanticCorrupt",
    "RedisStateCorrupt",
    "JsonStateCorrupt",
    "PostgresStateCorrupt",
    # Judge evaluator
    "LLMJudge",
    "JudgeScore",
    "average_scores",
]
