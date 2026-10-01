"""Publication-study orchestration primitives for CJ evaluation."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from chaos_jungle import ChaosRunner, Scenario
from chaos_jungle.faults.llm import _proxy_script_path
from chaos_jungle.targets import LocalTarget
from evaluation.benchmarks.base import BenchmarkTask
from evaluation.docker_runner import (
    DockerAgentRunResult,
    DockerAgentRunner,
    RunContext,
)


CONDITIONS = ("direct_baseline", "cj_control", "cj_fault")


@dataclass
class ChaosExperimentDefinition:
    """Chaos-engineering fields stored with every condition group."""

    steady_state_metric: str
    resilience_hypothesis: str
    fault_variable: str
    expected_activation_evidence: str
    abort_policy: str
    blast_radius_boundary: str
    observation_window: str
    recovery_condition: str
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_experiment_definition(fault_name: str) -> ChaosExperimentDefinition:
    return ChaosExperimentDefinition(
        steady_state_metric="pass@1 within the configured execution timeout",
        resilience_hypothesis=(
            "Compared with the paired direct baseline, CJ-valid executions under "
            f"{fault_name} produce bounded correctness degradation and explicit "
            "failure evidence rather than silent failure."
        ),
        fault_variable=fault_name,
        expected_activation_evidence=(
            "CJ lifecycle verification plus layer-specific evidence: proxy DB rows "
            "for LLM/tool faults, container namespace measurements for network and "
            "resource faults, or Docker state transitions for container faults."
        ),
        abort_policy=(
            "Abort if the target container cannot be positively identified, Docker "
            "resource limits are absent, or the host safety guardrails report unsafe "
            "resource pressure."
        ),
        blast_radius_boundary="one disposable Docker experiment container and its temporary mounts",
        observation_window="from verified activation through workload completion and recovery check",
        recovery_condition=(
            "The injected fault is reverted and post-fault verification confirms the "
            "proxy/namespace/container state has returned to the control condition."
        ),
        references=[
            "arxiv:1702.05843",
            "arxiv:2608.06790",
            "arxiv:2602.19843",
            "ACL-2025-long.421",
            "arxiv:2408.00989",
            "arxiv:2608.24271",
        ],
    )


def make_pair_id(*, study_id: str, framework: str, topology: str, task_id: str, seed: int) -> str:
    blob = json.dumps(
        {
            "study_id": study_id,
            "framework": framework,
            "topology": topology,
            "task_id": task_id,
            "seed": seed,
        },
        sort_keys=True,
    )
    import hashlib

    return hashlib.sha256(blob.encode()).hexdigest()[:24]


class PublicationStudyOrchestrator:
    """Host-side orchestrator for direct/control/fault Docker executions."""

    def __init__(
        self,
        docker_runner: DockerAgentRunner,
        *,
        study_id: str | None = None,
        results_dir: str = "results",
        proxy_port: int = 18000,
    ) -> None:
        self.docker_runner = docker_runner
        self.study_id = study_id or f"study-{uuid.uuid4().hex[:12]}"
        self.results_dir = results_dir
        self.proxy_port = proxy_port
        os.makedirs(results_dir, exist_ok=True)

    def run_pair(
        self,
        *,
        agent_system: str,
        agent_level: str,
        topology: str,
        task: BenchmarkTask,
        seed: int,
        model_config: dict[str, Any],
        execution_config: dict[str, Any],
        fault_name: str,
        fault=None,
    ) -> list[dict[str, Any]]:
        pair_id = make_pair_id(
            study_id=self.study_id,
            framework=agent_system,
            topology=topology,
            task_id=task.task_id,
            seed=seed,
        )
        campaign_id = f"{pair_id}-{fault_name}"
        task_dict = {
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "prompt": task.agent_prompt(),
            "entry_point": task.entry_point,
        }
        base_exec = {**execution_config, "agent_level": agent_level}
        definition = default_experiment_definition(fault_name)
        records: list[dict[str, Any]] = []

        records.append(self._run_condition(
            condition="direct_baseline",
            agent_system=agent_system,
            topology=topology,
            task=task_dict,
            seed=seed,
            model_config=model_config,
            execution_config=base_exec,
            campaign_id=campaign_id,
            pair_id=pair_id,
            extra_env={},
            definition=definition,
        ))

        control_proc = self._start_passthrough_proxy()
        try:
            records.append(self._run_condition(
                condition="cj_control",
                agent_system=agent_system,
                topology=topology,
                task=task_dict,
                seed=seed,
                model_config=model_config,
                execution_config=base_exec,
                campaign_id=campaign_id,
                pair_id=pair_id,
                extra_env={"CJ_EVAL_BASE_URL": self._docker_reachable_proxy_url()},
                definition=definition,
            ))
        finally:
            self._stop_process(control_proc)

        if fault is None:
            raise ValueError("cj_fault condition requires a concrete CJ fault instance")
        scenario = Scenario(f"eval-{fault_name}-{pair_id}", [fault])
        runner = ChaosRunner(scenario, LocalTarget(), auto_preflight=False)
        try:
            runner.start()
            records.append(self._run_condition(
                condition="cj_fault",
                agent_system=agent_system,
                topology=topology,
                task=task_dict,
                seed=seed,
                model_config=model_config,
                execution_config=base_exec,
                campaign_id=campaign_id,
                pair_id=pair_id,
                extra_env={"CJ_EVAL_BASE_URL": self._docker_reachable_proxy_url()},
                definition=definition,
            ))
        finally:
            runner.stop()
        return records

    def _run_condition(
        self,
        *,
        condition: str,
        agent_system: str,
        topology: str,
        task: dict[str, Any],
        seed: int,
        model_config: dict[str, Any],
        execution_config: dict[str, Any],
        campaign_id: str,
        pair_id: str,
        extra_env: dict[str, str],
        definition: ChaosExperimentDefinition,
    ) -> dict[str, Any]:
        run_id = f"{condition}-{uuid.uuid4().hex[:12]}"
        context = RunContext(
            study_id=self.study_id,
            campaign_id=campaign_id,
            pair_id=pair_id,
            run_id=run_id,
            condition=condition,
            output_root=os.path.join(self.results_dir, run_id),
            environment=extra_env,
        )
        result: DockerAgentRunResult = self.docker_runner.run(
            agent_system=agent_system,
            topology=topology,
            task=task,
            seed=seed,
            model_config=model_config,
            execution_config=execution_config,
            run_context=context,
        )
        record = result.to_dict()
        record.update({
            "study_id": self.study_id,
            "campaign_id": campaign_id,
            "pair_id": pair_id,
            "run_id": run_id,
            "condition": condition,
            "agent_level": execution_config.get("agent_level", "individual"),
            "framework": agent_system,
            "topology": topology,
            "benchmark": task.get("benchmark", ""),
            "task_id": task.get("task_id", ""),
            "seed": seed,
            "model": model_config.get("name") or model_config.get("model", ""),
            "experiment_definition": definition.to_dict(),
        })
        return record

    def _start_passthrough_proxy(self) -> subprocess.Popen:
        cmd = [
            sys.executable,
            _proxy_script_path(),
            "--port",
            str(self.proxy_port),
            "--upstream",
            os.environ.get("CJ_EVAL_UPSTREAM", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")).removesuffix("/v1"),
            "--fault",
            "passthrough",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 5
        import urllib.request

        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise RuntimeError(f"CJ pass-through proxy failed to start: {out}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.proxy_port}/_cj/health", timeout=0.3)
                return proc
            except Exception:
                time.sleep(0.05)
        self._stop_process(proc)
        raise RuntimeError("CJ pass-through proxy did not become healthy")

    def _docker_reachable_proxy_url(self) -> str:
        if platform.system().lower() == "darwin":
            host = "host.docker.internal"
        else:
            host = os.environ.get("CJ_DOCKER_HOST_GATEWAY", "host.docker.internal")
        return f"http://{host}:{self.proxy_port}/v1"

    def _stop_process(self, proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
