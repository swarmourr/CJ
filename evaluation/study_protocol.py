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
from evaluation.benchmarks.executor import score_task
from evaluation.docker_runner import (
    DockerAgentRunResult,
    DockerAgentRunner,
    RunContext,
    redact_secrets,
)
from evaluation.experiments.protocol import _CJ_COMMIT


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


def make_pair_id(
    *,
    study_id: str,
    framework: str,
    topology: str,
    task_id: str,
    seed: int,
    fault_name: str = "",
    fault_parameters: dict[str, Any] | None = None,
    repetition: int = 0,
) -> str:
    blob = json.dumps(
        {
            "study_id": study_id,
            "framework": framework,
            "topology": topology,
            "task_id": task_id,
            "seed": seed,
            "fault_name": fault_name,
            "fault_parameters": fault_parameters or {},
            "repetition": repetition,
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
        self._jsonl_path = os.path.join(results_dir, "runs.jsonl")
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
        repetition: int = 0,
    ) -> list[dict[str, Any]]:
        pair_id = make_pair_id(
            study_id=self.study_id,
            framework=agent_system,
            topology=topology,
            task_id=task.task_id,
            seed=seed,
            fault_name=fault_name,
            fault_parameters=getattr(fault, "_parameters", lambda: {})(),
            repetition=repetition,
        )
        campaign_id = f"{pair_id}-{fault_name}"
        fault_parameters = getattr(fault, "_parameters", lambda: {})() if fault is not None else {}
        task_dict = _task_to_request_dict(task)
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
            extra_env=self._base_env(model_config),
            definition=definition,
            benchmark_task=task,
            fault_name="none",
            fault_parameters={},
            lifecycle=_no_fault_lifecycle("direct_baseline"),
            validity="valid",
        ))

        control_proc = self._start_passthrough_proxy(model_config)
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
                extra_env=self._base_env(model_config, {"CJ_EVAL_BASE_URL": self._docker_reachable_proxy_url()}),
                definition=definition,
                benchmark_task=task,
                fault_name="passthrough",
                fault_parameters={},
                lifecycle=_control_lifecycle(),
                validity="valid",
            ))
        finally:
            self._stop_process(control_proc)

        if fault is None:
            raise ValueError("cj_fault condition requires a concrete CJ fault instance")
        scenario = Scenario(f"eval-{fault_name}-{pair_id}", [fault])
        runner = ChaosRunner(scenario, LocalTarget(), auto_preflight=False)
        lifecycle = _fault_lifecycle(configured=True)
        validity = "inconclusive"
        fault_record: dict[str, Any] | None = None
        try:
            runner.start()
            lifecycle["activated"] = True
            lifecycle["session_id"] = runner._session_id
            fault_record = self._run_condition(
                condition="cj_fault",
                agent_system=agent_system,
                topology=topology,
                task=task_dict,
                seed=seed,
                model_config=model_config,
                execution_config=base_exec,
                campaign_id=campaign_id,
                pair_id=pair_id,
                extra_env=self._base_env(model_config, {"CJ_EVAL_BASE_URL": self._docker_reachable_proxy_url()}),
                definition=definition,
                benchmark_task=task,
                fault_name=fault_name,
                fault_parameters=fault_parameters,
                lifecycle=lifecycle,
                validity=validity,
                persist=False,
            )
        except Exception as exc:
            lifecycle["details"] = f"fault execution failed: {exc!r}"
            if fault_record is None:
                fault_record = self._failed_fault_record(
                    condition="cj_fault",
                    agent_system=agent_system,
                    topology=topology,
                    task=task_dict,
                    seed=seed,
                    agent_level=agent_level,
                    model_config=model_config,
                    campaign_id=campaign_id,
                    pair_id=pair_id,
                    fault_name=fault_name,
                    fault_parameters=fault_parameters,
                    lifecycle=lifecycle,
                    definition=definition,
                    error=repr(exc),
                )
        finally:
            try:
                runner.stop()
                lifecycle["reverted"] = True
                lifecycle["recovered"] = True
            except Exception as exc:  # noqa: BLE001
                lifecycle["reverted"] = False
                lifecycle["recovered"] = False
                lifecycle["details"] = f"runner.stop failed: {exc!r}"
            _merge_runner_lifecycle(lifecycle, runner)
            validity = _classify_lifecycle(lifecycle)
            lifecycle["verdict"] = validity
        fault_record["lifecycle"] = lifecycle
        fault_record["validity"] = validity
        records.append(fault_record)
        self._append_jsonl(fault_record)
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
        benchmark_task: BenchmarkTask,
        fault_name: str,
        fault_parameters: dict[str, Any],
        lifecycle: dict[str, Any],
        validity: str,
        persist: bool = True,
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
        record = _flatten_docker_result(result)
        _apply_host_scoring(record, benchmark_task, execution_config)
        effective_agent_system = _effective_agent_system(
            requested=agent_system,
            agent_level=str(execution_config.get("agent_level", "individual")),
            record=record,
        )
        record.update({
            "study_id": self.study_id,
            "campaign_id": campaign_id,
            "pair_id": pair_id,
            "run_id": run_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cj_commit": _CJ_COMMIT,
            "condition": condition,
            "phase": "fault" if condition == "cj_fault" else "baseline",
            "agent_level": execution_config.get("agent_level", "individual"),
            "agent_system": effective_agent_system,
            "framework": effective_agent_system,
            "requested_framework": agent_system,
            "framework_native": record.get("framework_native", True),
            "multi_agent_impl": record.get("multi_agent_impl", ""),
            "topology": topology,
            "benchmark": task.get("benchmark", ""),
            "task_id": task.get("task_id", ""),
            "seed": seed,
            "model": model_config.get("name") or model_config.get("model", ""),
            "endpoint_type": _endpoint_type(extra_env.get("CJ_EVAL_BASE_URL") or model_config.get("base_url", "")),
            "config_hash": _config_hash(model_config, execution_config),
            "fault_type": fault_name,
            "fault_parameters": fault_parameters,
            "fault_target_role": _fault_target_role(fault_parameters),
            "target": "docker",
            "validity": validity,
            "lifecycle": lifecycle,
            "docker_image": result.image,
            "docker_image_digest": result.image_digest,
            "container_resource_limits": result.resource_limits,
            "experiment_definition": definition.to_dict(),
        })
        if persist:
            self._append_jsonl(record)
        return record

    def _failed_fault_record(
        self,
        *,
        condition: str,
        agent_system: str,
        topology: str,
        task: dict[str, Any],
        seed: int,
        agent_level: str,
        model_config: dict[str, Any],
        campaign_id: str,
        pair_id: str,
        fault_name: str,
        fault_parameters: dict[str, Any],
        lifecycle: dict[str, Any],
        definition: ChaosExperimentDefinition,
        error: str,
    ) -> dict[str, Any]:
        run_id = f"{condition}-{uuid.uuid4().hex[:12]}"
        return {
            "study_id": self.study_id,
            "campaign_id": campaign_id,
            "pair_id": pair_id,
            "run_id": run_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cj_commit": _CJ_COMMIT,
            "condition": condition,
            "phase": "fault",
            "agent_level": agent_level,
            "agent_system": agent_system,
            "framework": agent_system,
            "requested_framework": agent_system,
            "framework_native": True,
            "multi_agent_impl": "",
            "topology": topology,
            "benchmark": task.get("benchmark", ""),
            "task_id": task.get("task_id", ""),
            "seed": seed,
            "model": model_config.get("name") or model_config.get("model", ""),
            "endpoint_type": _endpoint_type(model_config.get("base_url", "")),
            "config_hash": _config_hash(model_config, {}),
            "fault_type": fault_name,
            "fault_parameters": fault_parameters,
            "fault_target_role": _fault_target_role(fault_parameters),
            "target": "docker",
            "validity": "invalid",
            "lifecycle": lifecycle,
            "success": 0.0,
            "reported_error": 1.0,
            "executor_status": "error",
            "executor_error": error,
            "duration_s": 0.0,
            "tests_passed": 0,
            "tests_total": 0,
            "docker_image": self.docker_runner.config.image,
            "docker_image_digest": "",
            "container_resource_limits": self.docker_runner.config.resource_limits(),
            "experiment_definition": definition.to_dict(),
        }

    def _start_passthrough_proxy(self, model_config: dict[str, Any]) -> subprocess.Popen:
        upstream = (
            model_config.get("base_url")
            or os.environ.get("CJ_EVAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com"
        )
        cmd = [
            sys.executable,
            _proxy_script_path(),
            "--port",
            str(self.proxy_port),
            "--upstream",
            str(upstream).removesuffix("/v1"),
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

    def _base_env(
        self,
        model_config: dict[str, Any],
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        base_url = (
            model_config.get("base_url")
            or os.environ.get("CJ_EVAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or ""
        )
        if base_url:
            env["CJ_EVAL_BASE_URL"] = str(base_url)
        if model_config.get("name") or model_config.get("model"):
            env["CJ_EVAL_MODEL"] = str(model_config.get("name") or model_config.get("model"))
        if "temperature" in model_config:
            env["CJ_EVAL_TEMPERATURE"] = str(model_config["temperature"])
        if extra:
            env.update(extra)
        return env

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def _stop_process(self, proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return repr(value)


def _config_hash(model_config: dict[str, Any], execution_config: dict[str, Any]) -> str:
    import hashlib

    safe = {
        "model_config": redact_secrets(model_config),
        "execution_config": redact_secrets(execution_config),
    }
    blob = json.dumps(_jsonable(safe), sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def _fault_target_role(fault_parameters: dict[str, Any]) -> str:
    selector = fault_parameters.get("selector") if isinstance(fault_parameters, dict) else None
    if isinstance(selector, dict):
        role = selector.get("agent_role")
        if role:
            return str(role)
    return ""


def _task_to_request_dict(task: BenchmarkTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "prompt": task.agent_prompt(),
        "entry_point": task.entry_point,
        "test_code": task.test_code,
        "canonical_solution": task.canonical_solution,
        "metadata": _jsonable(task.metadata),
    }


def _flatten_docker_result(result: DockerAgentRunResult) -> dict[str, Any]:
    payload = result.to_dict()
    agent_result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for key, value in agent_result.items():
        payload.setdefault(key, value)
    payload["executor_exit_code"] = payload.pop("exit_code", 0)
    payload["executor_timed_out"] = payload.pop("timed_out", False)
    payload["executor_duration_s"] = payload.get("duration_s", 0.0)
    return payload


def _apply_host_scoring(
    record: dict[str, Any],
    task: BenchmarkTask,
    execution_config: dict[str, Any],
) -> None:
    if record.get("scorer_status") == "ok" and int(record.get("tests_total") or 0) > 0:
        evidence = record.setdefault("scoring_evidence", {})
        if isinstance(evidence, dict):
            evidence.setdefault("host_scored", False)
        return
    code = str(record.get("generated_code") or "")
    if not code.strip():
        record.setdefault("scorer_status", "error")
        record.setdefault("scorer_error", "agent produced no generated_code")
        record.setdefault("success", 0.0)
        record.setdefault("tests_passed", 0)
        record.setdefault("tests_total", 0)
        return
    ok, passed, total, output = score_task(
        task,
        code,
        timeout_s=float(execution_config.get("score_timeout_s", 10.0)),
    )
    record["success"] = 1.0 if ok else 0.0
    record["tests_passed"] = passed
    record["tests_total"] = total
    record["scorer_status"] = "ok" if total > 0 else "error"
    record["scorer_error"] = "" if total > 0 else output
    record["scoring_evidence"] = {
        "host_scored": True,
        "strict_evalplus": task.metadata.get("source") == "evalplus",
        "output_preview": output[:2000],
    }


def _effective_agent_system(*, requested: str, agent_level: str, record: dict[str, Any]) -> str:
    if agent_level == "multi_agent" and record.get("framework_native") is False:
        return str(record.get("multi_agent_impl") or "reference-multi-agent")
    return requested


def _no_fault_lifecycle(condition: str) -> dict[str, Any]:
    return {
        "configured": False,
        "activated": None,
        "triggered": None,
        "manifested": None,
        "reverted": None,
        "recovered": None,
        "verdict": "valid",
        "evidence_source": condition,
        "timestamps": {},
        "details": "no CJ fault configured",
    }


def _control_lifecycle() -> dict[str, Any]:
    return {
        "configured": True,
        "activated": True,
        "triggered": False,
        "manifested": False,
        "reverted": True,
        "recovered": True,
        "verdict": "valid",
        "evidence_source": "cj_passthrough_proxy",
        "timestamps": {},
        "details": "CJ pass-through proxy active with no fault",
    }


def _fault_lifecycle(*, configured: bool) -> dict[str, Any]:
    return {
        "configured": configured,
        "activated": None,
        "triggered": None,
        "manifested": None,
        "reverted": None,
        "recovered": None,
        "verdict": "inconclusive",
        "evidence_source": "cj_proxy_database",
        "timestamps": {"configured": time.time()},
        "details": "",
    }


def _merge_runner_lifecycle(lifecycle: dict[str, Any], runner: ChaosRunner) -> None:
    session_id = getattr(runner, "_session_id", None)
    if session_id is None:
        lifecycle["verdict"] = "inconclusive"
        lifecycle["details"] = lifecycle.get("details") or "runner did not create a session"
        return
    lifecycle["session_id"] = session_id
    try:
        sess = runner.db.get_session(session_id)
        if sess and "verdict" in sess.keys():
            lifecycle["verdict"] = str(sess["verdict"]).lower()
        calls = runner.db.get_llm_calls(session_id, phase="fault")
        lifecycle["proxy_call_count"] = len(calls)
        lifecycle["triggered"] = len(calls) > 0
        lifecycle["manifested"] = any(
            row.get("was_blocked")
            or row.get("was_modified")
            or bool(json.loads(row.get("triggered_faults_json") or "[]"))
            for row in calls
        )
        lifecycle["evidence"] = {
            "configured_faults": [
                row.get("configured_faults_json", "[]")
                for row in calls[:5]
            ],
            "triggered_faults": [
                row.get("triggered_faults_json", "[]")
                for row in calls[:5]
            ],
            "fault_evidence": [
                row.get("fault_evidence_json", "{}")
                for row in calls[:5]
            ],
        }
    except Exception as exc:  # noqa: BLE001
        lifecycle["verdict"] = "inconclusive"
        lifecycle["details"] = f"failed to read CJ evidence: {exc!r}"


def _classify_lifecycle(lifecycle: dict[str, Any]) -> str:
    if not lifecycle.get("activated"):
        return "invalid"
    if lifecycle.get("activated") and not lifecycle.get("triggered"):
        return "untriggered"
    if lifecycle.get("triggered") and not lifecycle.get("manifested"):
        return "invalid"
    verdict = str(lifecycle.get("verdict", "")).lower()
    if verdict in {"valid", "invalid", "inconclusive"}:
        return verdict
    return "valid" if lifecycle.get("manifested") else "inconclusive"


def _endpoint_type(url: str) -> str:
    if not url:
        return "unknown"
    if "127.0.0.1" in url or "localhost" in url or "host.docker.internal" in url:
        return "local"
    if "openai.com" in url:
        return "openai"
    return "openai_compat"
