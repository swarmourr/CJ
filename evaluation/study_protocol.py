"""Publication-study orchestration primitives for CJ evaluation."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib import request as urlrequest

from chaos_jungle import ChaosRunner, Scenario
from chaos_jungle.db import SessionDB
from chaos_jungle.faults.llm import _LLMProxyFault, _proxy_script_path
from chaos_jungle.targets import LocalTarget
from evaluation.benchmarks.base import BenchmarkTask
from evaluation.docker_runner import (
    DockerAgentRunResult,
    DockerAgentRunner,
    RunContext,
    redact_secrets,
)
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.schema import OUTPUT_SCHEMA_VERSION


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
        container_direct_base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        control_proxy_port = self.proxy_port
        fault_proxy_port = self.proxy_port + 1
        _configure_proxy_fault(fault, model_config, fault_proxy_port)
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
        direct_base_url = (
            container_direct_base_url
            or model_config.get("container_base_url")
            or model_config.get("base_url")
            or os.environ.get("CJ_EVAL_CONTAINER_BASE_URL")
            or os.environ.get("CJ_EVAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or ""
        )
        control_proxy_url = self._docker_reachable_proxy_url(control_proxy_port)
        fault_proxy_url = self._docker_reachable_proxy_url(fault_proxy_port)
        model_provenance = _model_provenance(model_config)
        routing = {
            "host_upstream_base_url": _redact_url(str(model_config.get("base_url", ""))),
            "container_direct_base_url": _redact_url(str(direct_base_url)),
            "container_control_proxy_base_url": _redact_url(control_proxy_url),
            "container_fault_proxy_base_url": _redact_url(fault_proxy_url),
            "control_proxy_port": control_proxy_port,
            "fault_proxy_port": fault_proxy_port,
        }

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
            extra_env=self._base_env(model_config, {"CJ_EVAL_BASE_URL": str(direct_base_url)} if direct_base_url else None),
            definition=definition,
            benchmark_task=task,
            fault_name="none",
            fault_parameters={},
            lifecycle=_no_fault_lifecycle("direct_baseline"),
            validity="valid",
            routing=routing,
            model_provenance=model_provenance,
        ))

        control_lifecycle = _control_lifecycle()
        control_db = SessionDB(os.path.join(self.results_dir, "cj_proxy_sessions.sqlite3"))
        control_session_id = control_db.open_session(
            name=f"eval-control-{pair_id}",
            target_type="http",
            target_addr=f"127.0.0.1:{control_proxy_port}",
        )
        control_lifecycle["session_id"] = control_session_id
        control_lifecycle["evidence_source"] = "cj_passthrough_proxy_database"
        control_proc = self._start_passthrough_proxy(
            model_config,
            port=control_proxy_port,
            session_id=control_session_id,
            db=control_db,
        )
        control_lifecycle["timestamps"]["activated"] = _utc_now()
        control_record: dict[str, Any] | None = None
        try:
            control_record = self._run_condition(
                condition="cj_control",
                agent_system=agent_system,
                topology=topology,
                task=task_dict,
                seed=seed,
                model_config=model_config,
                execution_config=base_exec,
                campaign_id=campaign_id,
                pair_id=pair_id,
                extra_env=self._base_env(model_config, {"CJ_EVAL_BASE_URL": control_proxy_url}),
                definition=definition,
                benchmark_task=task,
                fault_name="passthrough",
                fault_parameters={},
                lifecycle=control_lifecycle,
                validity="valid",
                routing=routing,
                persist=False,
                model_provenance=model_provenance,
            )
        finally:
            self._stop_process(control_proc)
            self._wait_proxy_down(control_proxy_port)
            control_lifecycle["timestamps"]["reverted"] = _utc_now()
            _merge_control_lifecycle(control_lifecycle, control_db, control_session_id)
            try:
                control_db.set_session_verdict(control_session_id, "VALID")
                control_db.close_session(control_session_id, "reverted")
            except Exception:
                pass
            if control_lifecycle.get("recovered") is True:
                control_lifecycle["timestamps"]["recovered"] = _utc_now()
            if control_record is not None:
                control_record["lifecycle"] = control_lifecycle
                records.append(control_record)
                self._append_jsonl(control_record)

        if fault is None:
            raise ValueError("cj_fault condition requires a concrete CJ fault instance")
        scenario = Scenario(f"eval-{fault_name}-{pair_id}", [fault])
        runner = ChaosRunner(
            scenario,
            LocalTarget(),
            db=SessionDB(os.path.join(self.results_dir, "cj_proxy_sessions.sqlite3")),
            auto_preflight=False,
        )
        lifecycle = _fault_lifecycle(configured=True)
        validity = "inconclusive"
        fault_record: dict[str, Any] | None = None
        try:
            self._wait_proxy_down(fault_proxy_port, timeout_s=2.0)
            runner.start()
            lifecycle["activated"] = True
            lifecycle.setdefault("timestamps", {})["activated"] = _utc_now()
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
                extra_env=self._base_env(model_config, {"CJ_EVAL_BASE_URL": fault_proxy_url}),
                definition=definition,
                benchmark_task=task,
                fault_name=fault_name,
                fault_parameters=fault_parameters,
                lifecycle=lifecycle,
                validity=validity,
                persist=False,
                routing=routing,
                model_provenance=model_provenance,
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
                    routing=routing,
                    model_provenance=model_provenance,
                )
        finally:
            try:
                runner.stop()
                lifecycle["reverted"] = True
                lifecycle.setdefault("timestamps", {})["reverted"] = _utc_now()
            except Exception as exc:  # noqa: BLE001
                lifecycle["reverted"] = False
                lifecycle["recovered"] = False
                lifecycle["details"] = f"runner.stop failed: {exc!r}"
            _merge_runner_lifecycle(lifecycle, runner)
            if lifecycle.get("recovered") is True:
                lifecycle.setdefault("timestamps", {})["recovered"] = _utc_now()
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
        routing: dict[str, str] | None = None,
        model_provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = f"{condition}-{uuid.uuid4().hex[:12]}"
        timestamps = lifecycle.setdefault("timestamps", {})
        timestamps.setdefault("configured", _utc_now())
        timestamps["workload_started"] = _utc_now()
        context = RunContext(
            study_id=self.study_id,
            campaign_id=campaign_id,
            pair_id=pair_id,
            run_id=run_id,
            condition=condition,
            output_root=os.path.join(self.results_dir, run_id),
            environment=extra_env,
        )
        try:
            result: DockerAgentRunResult = self.docker_runner.run(
                agent_system=agent_system,
                topology=topology,
                task=task,
                seed=seed,
                model_config=model_config,
                execution_config=execution_config,
                run_context=context,
            )
        finally:
            timestamps["workload_finished"] = _utc_now()
        record = _flatten_docker_result(result)
        _validate_container_scoring(record, benchmark_task)
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
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cj_commit": _CJ_COMMIT,
            **_CJ_SOURCE_PROVENANCE,
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
            "benchmark_subset": task.get("metadata", {}).get("benchmark_subset", ""),
            "task_difficulty": task.get("metadata", {}).get("difficulty", ""),
            "task_complexity": task.get("metadata", {}).get("complexity_proxy", ""),
            "seed": seed,
            "model": model_config.get("name") or model_config.get("model", ""),
            "model_digest": (model_provenance or {}).get("digest", ""),
            "model_provenance": model_provenance or {},
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
            "routing": routing or {},
        })
        record.update(_run_failure_verdicts(record))
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
        routing: dict[str, str] | None = None,
        model_provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = f"{condition}-{uuid.uuid4().hex[:12]}"
        is_reference_multi_agent = agent_level == "multi_agent"
        effective_agent_system = (
            "reference-multi-agent" if is_reference_multi_agent else agent_system
        )
        record = {
            "study_id": self.study_id,
            "campaign_id": campaign_id,
            "pair_id": pair_id,
            "run_id": run_id,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cj_commit": _CJ_COMMIT,
            **_CJ_SOURCE_PROVENANCE,
            "condition": condition,
            "phase": "fault",
            "agent_level": agent_level,
            "agent_system": effective_agent_system,
            "framework": effective_agent_system,
            "requested_framework": agent_system,
            "framework_native": not is_reference_multi_agent,
            "multi_agent_impl": "reference-multi-agent" if is_reference_multi_agent else "",
            "topology": topology,
            "benchmark": task.get("benchmark", ""),
            "task_id": task.get("task_id", ""),
            "benchmark_subset": task.get("metadata", {}).get("benchmark_subset", ""),
            "task_difficulty": task.get("metadata", {}).get("difficulty", ""),
            "task_complexity": task.get("metadata", {}).get("complexity_proxy", ""),
            "seed": seed,
            "model": model_config.get("name") or model_config.get("model", ""),
            "model_digest": (model_provenance or {}).get("digest", ""),
            "model_provenance": model_provenance or {},
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
            "routing": routing or {},
        }
        record.update(_run_failure_verdicts(record))
        return record

    def _start_passthrough_proxy(
        self,
        model_config: dict[str, Any],
        *,
        port: int | None = None,
        session_id: int | None = None,
        db: SessionDB | None = None,
    ) -> subprocess.Popen:
        proxy_port = port or self.proxy_port
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
            str(proxy_port),
            "--upstream",
            str(upstream).removesuffix("/v1"),
            "--fault",
            "passthrough",
        ]
        if db is not None and session_id is not None:
            cmd.extend([
                "--db-path",
                str(db.path),
                "--session-id",
                str(session_id),
                "--phase",
                "control",
            ])
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 5
        last_cfg: dict[str, Any] | None = None
        last_error = ""

        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise RuntimeError(f"CJ pass-through proxy failed to start: {out}")
            try:
                with urlrequest.urlopen(
                    f"http://127.0.0.1:{proxy_port}/_cj/config", timeout=0.3
                ) as resp:
                    cfg = json.loads(resp.read())
                last_cfg = cfg
                session_matches = (
                    session_id is None
                    or str(cfg.get("session_id", "")) == str(session_id)
                )
                phase_matches = db is None or str(cfg.get("phase", "")) == "control"
                if (
                    cfg.get("fault") == "passthrough"
                    and not cfg.get("fault_chain")
                    and session_matches
                    and phase_matches
                ):
                    return proc
            except Exception as exc:
                last_error = repr(exc)
                time.sleep(0.05)
        self._stop_process(proc)
        raise RuntimeError(
            "CJ pass-through proxy did not become ready with the expected "
            f"control config on port {proxy_port}; last_config={last_cfg!r}; "
            f"last_error={last_error}"
        )

    def _docker_reachable_proxy_url(self, port: int | None = None) -> str:
        if platform.system().lower() == "darwin":
            host = "host.docker.internal"
        else:
            host = os.environ.get("CJ_DOCKER_HOST_GATEWAY", "host.docker.internal")
        return f"http://{host}:{port or self.proxy_port}/v1"

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
            proc.wait(timeout=5)

    def _wait_proxy_down(self, port: int, *, timeout_s: float = 5.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                urlrequest.urlopen(f"http://127.0.0.1:{port}/_cj/health", timeout=0.2)
            except Exception:
                return
            time.sleep(0.05)
        raise RuntimeError(f"CJ proxy on port {port} did not stop before next condition")


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


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _config_hash(model_config: dict[str, Any], execution_config: dict[str, Any]) -> str:
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
    metadata = dict(task.metadata)
    metadata.setdefault("prompt_chars", len(task.prompt or ""))
    metadata.setdefault("test_chars", len(task.test_code or ""))
    metadata.setdefault("canonical_solution_chars", len(task.canonical_solution or ""))
    total_chars = metadata["prompt_chars"] + metadata["test_chars"]
    metadata.setdefault(
        "complexity_proxy",
        "small" if total_chars < 1500 else "medium" if total_chars < 4000 else "large",
    )
    metadata.setdefault(
        "benchmark_subset",
        "publication" if metadata.get("source") == "evalplus" else "smoke",
    )
    return {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "prompt": task.agent_prompt(),
        "entry_point": task.entry_point,
        "test_code": task.test_code,
        "canonical_solution": task.canonical_solution,
        "metadata": _jsonable(metadata),
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


def _success_verdicts(record: dict[str, Any]) -> dict[str, str]:
    def as_float(value: Any, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    executor_status = str(record.get("executor_status", "ok")).lower()
    executor_exit_code = int(as_float(record.get("executor_exit_code"), 0.0))
    executor_ok = (
        record.get("executor_timed_out") is not True
        and executor_exit_code == 0
        and executor_status in {"", "ok"}
        and not record.get("executor_error")
    )

    scorer_status = str(record.get("scorer_status", "")).lower()
    tests_total = int(as_float(record.get("tests_total"), 0.0))
    if scorer_status in {"", "not_available", "missing", "skipped"}:
        scorer_verdict = "not_available"
    elif scorer_status == "ok" and tests_total > 0:
        scorer_verdict = "success"
    else:
        scorer_verdict = "failure"

    return {
        "executor_success_verdict": "success" if executor_ok else "failure",
        "scorer_success_verdict": scorer_verdict,
        "task_success_verdict": (
            "success" if as_float(record.get("success"), 0.0) >= 0.5 else "failure"
        ),
    }


def _run_failure_verdicts(record: dict[str, Any]) -> dict[str, Any]:
    success = _success_verdicts(record)
    agent_detected = _agent_detected_fault(record)
    runner_observed, runner_source = _runner_observed_failure(record)
    reported_error = _as_record_float(record.get("reported_error"), 0.0) >= 0.5
    task_failed = success["task_success_verdict"] == "failure"
    operational_success = _agent_operational_success(record)
    continued_operation = operational_success
    if agent_detected and runner_observed:
        source = f"agent_and_{runner_source}"
    elif agent_detected:
        source = "agent"
    elif runner_observed:
        source = runner_source
    elif reported_error:
        source = "reported_error"
    else:
        source = "none"
    return {
        **success,
        "operational_success": operational_success,
        "continued_operation": continued_operation,
        "agent_detected_fault": agent_detected,
        "runner_observed_failure": runner_observed,
        "failure_detection_source": source,
        "silent_failure": bool(task_failed and operational_success and not agent_detected),
    }


def _as_record_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _agent_detected_fault(record: dict[str, Any]) -> bool:
    reason = str(record.get("termination_reason", "")).lower()
    if reason in {"graceful_failure", "controlled_failure", "reported_failure"}:
        return True
    trace = record.get("execution_trace") or (record.get("result") or {}).get("execution_trace", [])
    if isinstance(trace, list):
        return any(
            isinstance(event, dict)
            and event.get("event_type") in {"error_detection", "attempted_recovery"}
            for event in trace
        )
    return False


def _runner_observed_failure(record: dict[str, Any]) -> tuple[bool, str]:
    if record.get("executor_timed_out") is True:
        return True, "runner_timeout"
    if record.get("exception"):
        return True, "runner_exception"
    if record.get("executor_error"):
        return True, "executor_error"
    status = str(record.get("executor_status", "")).lower()
    if status not in {"", "ok"}:
        return True, "executor_error"
    reason = str(record.get("termination_reason", "")).lower()
    if reason in {"timeout"}:
        return True, "runner_timeout"
    if reason in {
        "api_error", "agent_exception", "container_killed",
        "error", "exception", "executor_error", "killed",
    }:
        return True, "runner_exception"
    if "exception" in reason:
        return True, "runner_exception"
    if "error" in reason:
        return True, "executor_error"
    return False, "none"


def _agent_operational_success(record: dict[str, Any]) -> bool:
    if _as_record_float(record.get("reported_error"), 0.0) >= 0.5:
        return False
    reason = str(record.get("termination_reason", "")).lower()
    return reason == "completed"


def _validate_container_scoring(
    record: dict[str, Any],
    task: BenchmarkTask,
) -> None:
    if record.get("scorer_status") == "ok" and int(record.get("tests_total") or 0) > 0:
        evidence = record.setdefault("scoring_evidence", {})
        if isinstance(evidence, dict):
            evidence.setdefault("host_scored", False)
            evidence.setdefault("score_location", "container")
        return
    record["success"] = 0.0
    record.setdefault("tests_passed", 0)
    record.setdefault("tests_total", 0)
    record["scorer_status"] = "error"
    if not record.get("scorer_error"):
        record["scorer_error"] = (
            "container did not produce valid scoring evidence; "
            "host fallback scoring is disabled"
        )
    record["scoring_evidence"] = {
        "host_scored": False,
        "score_location": "container",
        "strict_evalplus": task.metadata.get("source") == "evalplus",
        "valid": False,
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


def _merge_control_lifecycle(
    lifecycle: dict[str, Any],
    db: SessionDB,
    session_id: int,
) -> None:
    lifecycle["session_id"] = session_id
    try:
        calls = db.get_llm_calls(session_id, phase="control")
        lifecycle["proxy_call_count"] = len(calls)
        lifecycle["triggered"] = False
        lifecycle["manifested"] = False
        lifecycle["reverted"] = True
        lifecycle["recovered"] = True
        lifecycle["verdict"] = "valid"
        lifecycle["evidence"] = {
            "faults": [],
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
            "proxy_calls": [
                _proxy_call_evidence(row)
                for row in calls[:20]
            ],
        }
        lifecycle["details"] = (
            "CJ pass-through proxy captured "
            f"{len(calls)} model request(s) with no active fault"
        )
    except Exception as exc:  # noqa: BLE001
        lifecycle["recovered"] = False
        lifecycle["verdict"] = "inconclusive"
        lifecycle["details"] = f"failed to read CJ control evidence: {exc!r}"


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
        export = runner.db.export_session(session_id)
        faults = export.get("faults", [])
        lifecycle["proxy_call_count"] = len(calls)
        lifecycle["triggered"] = len(calls) > 0
        lifecycle["manifested"] = any(
            row.get("was_blocked")
            or row.get("was_modified")
            or bool(json.loads(row.get("triggered_faults_json") or "[]"))
            for row in calls
        )
        lifecycle["evidence"] = {
            "faults": [
                {
                    "kind": f.get("kind"),
                    "status": f.get("status"),
                    "verified_active": bool(f.get("verified_active")),
                    "verified_recovered": bool(f.get("verified_recovered")),
                    "verification_note": f.get("verification_note", ""),
                }
                for f in faults
            ],
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
            "proxy_calls": [
                _proxy_call_evidence(row)
                for row in calls[:20]
            ],
        }
        if faults:
            lifecycle["activated"] = all(bool(f.get("verified_active")) for f in faults)
            lifecycle["recovered"] = all(bool(f.get("verified_recovered")) for f in faults)
            lifecycle["reverted"] = all(str(f.get("status")) == "reverted" for f in faults)
    except Exception as exc:  # noqa: BLE001
        lifecycle["verdict"] = "inconclusive"
        lifecycle["details"] = f"failed to read CJ evidence: {exc!r}"


def _proxy_call_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded, secret-safe subset of a CJ proxy llm_calls row."""
    request_meta = _request_meta_from_fault_evidence(row.get("fault_evidence_json", "{}"))
    safe = {
        "id": row.get("id"),
        "phase": row.get("phase", ""),
        "call_index": row.get("call_index"),
        "timestamp": row.get("timestamp", ""),
        "model": row.get("model", ""),
        "latency_s": row.get("latency_s"),
        "http_status": row.get("http_status"),
        "fault_name": row.get("fault_name", ""),
        "was_blocked": row.get("was_blocked"),
        "was_modified": row.get("was_modified"),
        "fault_triggered": row.get("fault_triggered"),
        "configured_faults_json": row.get("configured_faults_json", "[]"),
        "triggered_faults_json": row.get("triggered_faults_json", "[]"),
        "fault_evidence_json": row.get("fault_evidence_json", "{}"),
        "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "total_tokens": row.get("total_tokens"),
        "finish_reason": row.get("finish_reason", ""),
        "error_type": row.get("error_type", ""),
        "request_size_bytes": row.get("request_size_bytes"),
        "response_size_bytes": row.get("response_size_bytes"),
        "response_length_chars": row.get("response_length_chars"),
        "max_tokens_requested": row.get("max_tokens_requested"),
        "message_count": row.get("message_count"),
        "tool_count": row.get("tool_count"),
        "response_tool_calls": row.get("response_tool_calls"),
        "is_retry": row.get("is_retry"),
        "fault_offset_s": row.get("fault_offset_s"),
        "agent_addr": row.get("agent_addr", ""),
        "run_id": request_meta.get("run_id", ""),
        "agent_role": request_meta.get("agent_role", ""),
        "step": request_meta.get("step", ""),
    }
    safe["prompt_text"] = _text_digest(row.get("prompt_text", ""))
    safe["response_text"] = _text_digest(row.get("response_text", ""))
    safe["full_messages_json"] = _text_digest(row.get("full_messages_json", ""))
    return safe


def _request_meta_from_fault_evidence(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return {}
    if not isinstance(parsed, list):
        return {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        evidence = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
        meta = evidence.get("request_meta") if isinstance(evidence.get("request_meta"), dict) else {}
        run_id = target.get("run_id") or meta.get("run_id")
        role = target.get("agent_role") or meta.get("agent_role")
        step = target.get("step") or meta.get("step")
        if run_id or role or step:
            return {
                "run_id": run_id or "",
                "agent_role": role or "",
                "step": step or "",
            }
    return {}


def _text_digest(value: Any, *, preview_chars: int = 160) -> dict[str, Any]:
    text = "" if value is None else str(value)
    preview = text[:preview_chars]
    for pattern in _SECRET_PATTERNS:
        preview = pattern.sub("[REDACTED]", preview)
    return {
        "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        "length": len(text),
        "preview": preview,
    }


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]\s*['\"]?[^,'\"\s}]+"),
]


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


def _model_provenance(model_config: dict[str, Any]) -> dict[str, Any]:
    model = str(model_config.get("name") or model_config.get("model") or "")
    base_url = str(model_config.get("base_url") or "")
    if not model:
        return {"status": "missing_model"}
    if "11434" not in base_url and "ollama" not in base_url.lower():
        return {"status": "not_ollama", "model": model}
    digest = _ollama_model_digest(base_url, model)
    return {
        "status": "resolved" if digest else "unresolved",
        "provider": "ollama",
        "model": model,
        "digest": digest or "",
    }


def _ollama_model_digest(base_url: str, model: str) -> str:
    root = _strip_openai_v1_suffix(base_url).rstrip("/")
    if not root:
        return ""
    try:
        req = urlrequest.Request(
            f"{root}/api/tags",
            method="GET",
            headers={"Accept": "application/json"},
        )
        with urlrequest.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("models", []):
            if item.get("name") == model or item.get("model") == model:
                return str(item.get("digest") or "")
    except Exception:
        pass
    try:
        body = json.dumps({"name": model}).encode("utf-8")
        req = urlrequest.Request(
            f"{root}/api/show",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urlrequest.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(
            data.get("digest")
            or data.get("details", {}).get("digest")
            or data.get("model_info", {}).get("general.digest")
            or ""
        )
    except Exception:
        return ""


def _redact_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "[invalid-url]"
    host = parsed.hostname or ""
    if not host:
        return url
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _strip_openai_v1_suffix(url: str) -> str:
    stripped = url.rstrip("/")
    if stripped.endswith("/v1"):
        return stripped[:-3]
    return stripped


def _configure_proxy_fault(fault: Any, model_config: dict[str, Any], proxy_port: int) -> None:
    """Align host-side LLM proxy faults with the publication-study proxy route."""
    if not isinstance(fault, _LLMProxyFault):
        return
    base_url = (
        model_config.get("base_url")
        or os.environ.get("CJ_EVAL_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or fault.upstream
    )
    fault.port = int(proxy_port)
    fault.upstream = _strip_openai_v1_suffix(str(base_url))
    fault.base_url_env = "CJ_EVAL_BASE_URL"
