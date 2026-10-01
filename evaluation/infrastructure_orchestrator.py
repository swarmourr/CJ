"""Layer-specific orchestration for container-scoped infrastructure faults."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from chaos_jungle.faults.base import Fault
from chaos_jungle.targets import DockerTarget
from evaluation.docker_runner import (
    DockerAgentRunResult,
    DockerAgentRunner,
    PreparedContainer,
    RunContext,
)


@dataclass
class InfrastructureFaultResult:
    result: DockerAgentRunResult
    lifecycle: dict[str, Any]
    activation_evidence: dict[str, Any] = field(default_factory=dict)
    recovery_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = self.result.to_dict()
        payload["lifecycle"] = self.lifecycle
        payload["activation_evidence"] = self.activation_evidence
        payload["recovery_evidence"] = self.recovery_evidence
        return payload


def run_container_scoped_fault(
    *,
    docker_runner: DockerAgentRunner,
    fault: Fault,
    agent_system: str,
    topology: str,
    task: dict[str, Any],
    seed: int,
    model_config: dict[str, Any],
    execution_config: dict[str, Any],
    run_context: RunContext,
    target_timeout_s: float = 30.0,
) -> InfrastructureFaultResult:
    """Run one Docker execution under a fault applied inside that container.

    The sequence is prepare container -> start idle container -> create
    DockerTarget -> activate and verify -> execute workload inside the same
    container -> revert and verify -> cleanup. The host is never used as a
    target for network/resource faults.
    """
    container: PreparedContainer = docker_runner.prepare(
        agent_system=agent_system,
        topology=topology,
        task=task,
        seed=seed,
        model_config=model_config,
        execution_config=execution_config,
        run_context=run_context,
    )
    lifecycle = {
        "configured": True,
        "activated": None,
        "triggered": None,
        "manifested": None,
        "reverted": None,
        "recovered": None,
        "verdict": "pending",
        "evidence_source": "docker_target",
        "timestamps": {"configured": time.time()},
        "details": "",
    }
    activation_evidence: dict[str, Any] = {}
    recovery_evidence: dict[str, Any] = {}
    result: DockerAgentRunResult | None = None
    target = DockerTarget(container.container_id, timeout_s=target_timeout_s)
    try:
        start_proc = docker_runner.start_container(container)
        if start_proc.returncode != 0:
            raise RuntimeError(
                "failed to start experiment container before fault activation: "
                f"{start_proc.stderr or start_proc.stdout}"
            )
        target.connect()
        fault.start(target)
        lifecycle["timestamps"]["activated"] = time.time()
        active = fault.verify_active(target)
        activation_evidence = asdict(active)
        lifecycle["activated"] = active.verified
        lifecycle["triggered"] = active.verified
        lifecycle["manifested"] = active.verified
        if not active.verified:
            lifecycle["verdict"] = "invalid"
            lifecycle["details"] = active.reason

        execution = docker_runner.exec_agent(container)
        result = docker_runner.collect(execution)
        if lifecycle["verdict"] == "pending":
            lifecycle["verdict"] = "valid" if active.verified else "invalid"
    finally:
        try:
            fault.stop(target)
            lifecycle["reverted"] = True
            lifecycle["timestamps"]["reverted"] = time.time()
            recovered = fault.verify_recovered(target)
            recovery_evidence = asdict(recovered)
            lifecycle["recovered"] = recovered.verified
            lifecycle["timestamps"]["recovered"] = time.time()
            if not recovered.verified and lifecycle["verdict"] == "valid":
                lifecycle["verdict"] = "invalid"
                lifecycle["details"] = recovered.reason
        except Exception as exc:  # noqa: BLE001
            lifecycle["reverted"] = False
            lifecycle["recovered"] = False
            lifecycle["verdict"] = "invalid"
            lifecycle["details"] = f"recovery failed: {exc!r}"
        finally:
            target.disconnect()
            docker_runner.cleanup(container)
    if result is None:
        raise RuntimeError("container-scoped fault execution did not produce a Docker result")
    return InfrastructureFaultResult(
        result=result,
        lifecycle=lifecycle,
        activation_evidence=activation_evidence,
        recovery_evidence=recovery_evidence,
    )
