"""Layer-specific orchestration for container-scoped infrastructure faults."""

from __future__ import annotations

import re
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
        manifestation_evidence = _collect_manifestation_evidence(target, fault)
        _merge_manifestation_evidence(activation_evidence, manifestation_evidence)
        lifecycle["activated"] = active.verified
        lifecycle["triggered"] = active.verified
        lifecycle["manifested"] = bool(
            active.verified
            and manifestation_evidence.get("manifested", active.verified)
        )
        if not active.verified:
            lifecycle["verdict"] = "invalid"
            lifecycle["details"] = active.reason
        elif not lifecycle["manifested"]:
            lifecycle["verdict"] = "invalid"
            lifecycle["details"] = str(
                manifestation_evidence.get("reason")
                or "fault activated but manifestation probe did not observe the requested effect"
            )

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
            _merge_manifestation_evidence(
                recovery_evidence,
                _collect_recovery_evidence(target, fault),
            )
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


def _merge_manifestation_evidence(
    evidence: dict[str, Any],
    extra: dict[str, Any],
) -> None:
    if not extra:
        return
    observed = evidence.setdefault("observed", {})
    if not isinstance(observed, dict):
        observed = {}
        evidence["observed"] = observed
    observed.update(extra.get("observed", {}))
    for key, value in extra.items():
        if key != "observed":
            evidence[key] = value


def _collect_manifestation_evidence(target: DockerTarget, fault: Fault) -> dict[str, Any]:
    fault_name = fault.__class__.__name__
    if fault_name == "CPUStress":
        return _cpu_manifestation(target, fault)
    if fault_name == "MemoryStress":
        return _memory_manifestation(target)
    if fault_name in {"NetworkDelay", "NetworkLoss"}:
        return _network_manifestation(target, fault_name)
    if fault_name == "DiskFull":
        return _disk_manifestation(target, fault)
    return {}


def _collect_recovery_evidence(target: DockerTarget, fault: Fault) -> dict[str, Any]:
    fault_name = fault.__class__.__name__
    if fault_name in {"NetworkDelay", "NetworkLoss"}:
        code, stdout, stderr = target.sudo("tc qdisc show dev eth0 2>/dev/null || true")
        return {
            "observed": {
                "post_recovery_qdisc": stdout.strip(),
                "post_recovery_qdisc_exit_code": code,
                "post_recovery_qdisc_error": stderr.strip()[:500],
            }
        }
    if fault_name == "DiskFull":
        return {"observed": _df_observed(target, "/tmp", prefix="post_recovery_")}
    return {}


def _cpu_manifestation(target: DockerTarget, fault: Fault) -> dict[str, Any]:
    pid_file = getattr(fault, "_pid_file", "")
    if not pid_file:
        return {}
    code, stdout, stderr = target.run(
        f"sleep 1; pid=$(cat {pid_file} 2>/dev/null); "
        "ps -eo pcpu=,comm=,args= 2>/dev/null | "
        "awk '/stress-ng/ && !/awk/ {sum += $1} END {print sum}'"
    )
    cpu_percent = _first_float(stdout)
    observed = {
        "cpu_probe_exit_code": code,
        "cpu_probe_stdout": stdout.strip()[:500],
        "cpu_probe_stderr": stderr.strip()[:500],
    }
    if cpu_percent is not None:
        observed["cpu_percent"] = cpu_percent
    return {
        "manifested": cpu_percent is not None and cpu_percent > 0.0,
        "reason": "stress process reported CPU usage" if cpu_percent else "CPU usage probe did not observe stress CPU consumption",
        "observed": observed,
    }


def _memory_manifestation(target: DockerTarget) -> dict[str, Any]:
    code, stdout, stderr = target.run(
        "sleep 0.5; ps -eo rss=,comm=,args= 2>/dev/null | "
        "awk '/stress-ng/ && !/awk/ {sum += $1} END {print sum * 1024}'"
    )
    memory_bytes = _first_int(stdout)
    observed = {
        "memory_probe_exit_code": code,
        "memory_probe_stdout": stdout.strip()[:500],
        "memory_probe_stderr": stderr.strip()[:500],
    }
    if memory_bytes is not None:
        observed["memory_bytes"] = memory_bytes
        observed["peak_memory_bytes"] = memory_bytes
    return {
        "manifested": memory_bytes is not None and memory_bytes > 0,
        "reason": "stress process memory allocation observed" if memory_bytes else "memory probe did not observe stress-ng RSS",
        "observed": observed,
    }


def _network_manifestation(target: DockerTarget, fault_name: str) -> dict[str, Any]:
    code, qdisc, qdisc_err = target.sudo("tc qdisc show dev eth0 2>/dev/null || true")
    gateway = _default_gateway(target)
    ping_count = 40 if fault_name == "NetworkLoss" else 5
    ping_interval = "-i 0.2 " if fault_name == "NetworkLoss" else ""
    ping_cmd = (
        f"if [ -n {gateway!r} ]; then "
        f"ping {ping_interval}-c {ping_count} -W 1 {gateway} 2>&1; "
        "else echo 'no default gateway'; exit 2; fi"
    )
    ping_code, ping_out, ping_err = target.run(ping_cmd)
    observed: dict[str, Any] = {
        "qdisc_exit_code": code,
        "qdisc": qdisc.strip(),
        "qdisc_error": qdisc_err.strip()[:500],
        "default_gateway": gateway,
        "ping_exit_code": ping_code,
        "ping_stdout_preview": ping_out.strip()[:1000],
        "ping_stderr_preview": ping_err.strip()[:500],
    }
    loss = _parse_ping_loss(ping_out)
    rtt = _parse_ping_avg_ms(ping_out)
    if loss is not None:
        observed["packet_loss_rate"] = loss
    if rtt is not None:
        observed["latency_ms"] = rtt
        observed["rtt_avg_ms"] = rtt
    has_netem = "netem" in qdisc.lower()
    return {
        "manifested": has_netem,
        "reason": "tc netem qdisc observed" if has_netem else "tc netem qdisc was not observed",
        "observed": observed,
    }


def _disk_manifestation(target: DockerTarget, fault: Fault) -> dict[str, Any]:
    path = getattr(fault, "path", "/tmp")
    observed = _df_observed(target, path)
    probe = f"{path.rstrip('/')}/.cj_enospc_probe"
    code, stdout, stderr = target.run(
        f"rm -f {probe}; "
        f"dd if=/dev/zero of={probe} bs=1M count=4 conv=fsync 2>&1; "
        "rc=$?; "
        f"rm -f {probe}; "
        "exit $rc"
    )
    combined = f"{stdout}\n{stderr}".lower()
    enospc = code != 0 and ("no space left" in combined or "enospc" in combined)
    observed.update({
        "disk_write_probe_exit_code": code,
        "disk_write_probe_stdout": stdout.strip()[:1000],
        "disk_write_probe_stderr": stderr.strip()[:500],
        "disk_enospc_verified": enospc,
    })
    return {
        "manifested": enospc,
        "reason": "write probe failed with ENOSPC" if enospc else "disk write probe did not fail with ENOSPC",
        "observed": observed,
    }


def _df_observed(target: DockerTarget, path: str, *, prefix: str = "") -> dict[str, Any]:
    code, stdout, stderr = target.run(
        f"df -P -B1 {path} 2>/dev/null | awk 'NR==2 {{print $3, $4, $5}}'"
    )
    observed: dict[str, Any] = {
        f"{prefix}df_exit_code": code,
        f"{prefix}df_stdout": stdout.strip()[:500],
        f"{prefix}df_stderr": stderr.strip()[:500],
    }
    parts = stdout.split()
    if len(parts) >= 3:
        used = _safe_int(parts[0])
        available = _safe_int(parts[1])
        if used is not None:
            observed[f"{prefix}disk_used_bytes"] = used
        if available is not None:
            observed[f"{prefix}disk_available_bytes"] = available
        observed[f"{prefix}disk_used_percent"] = parts[2]
    return observed


def _default_gateway(target: DockerTarget) -> str:
    _, stdout, _ = target.run("ip route | awk '/default/ {print $3; exit}'")
    return stdout.strip().splitlines()[0] if stdout.strip() else ""


def _first_float(value: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else None


def _first_int(value: str) -> int | None:
    match = re.search(r"[-+]?\d+", value or "")
    return int(match.group(0)) if match else None


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_ping_loss(output: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", output or "")
    if not match:
        return None
    return float(match.group(1)) / 100.0


def _parse_ping_avg_ms(output: str) -> float | None:
    match = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/", output or "")
    if not match:
        return None
    return float(match.group(2))
