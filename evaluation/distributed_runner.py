"""Host-side distributed reference multi-agent runner.

This runner is intentionally separate from the ordinary publication runner.  It
implements a real process/container distribution pattern: Planner, Coder, and
Reviewer each run in a fresh Docker container and communicate only through the
host coordinator's serialized messages.  It is a reference distributed
architecture, not a native AutoGen/LangGraph/CrewAI multi-agent implementation.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from evaluation.agents.base import extract_python_code
from evaluation.docker_runner import (
    DockerAgentRunner,
    DockerExecutionConfig,
    PreparedContainer,
    RunContext,
    redact_secrets,
)
from evaluation.multi_agent import CLOSED_LOOP, LINEAR, MAX_REVISIONS, ROLE_PROMPTS, TraceEvent


ROLES = ("planner", "coder", "reviewer")


def benchmark_task_to_dict(task: Any) -> dict[str, Any]:
    """Serialize a benchmark task dataclass or task-like mapping."""
    if isinstance(task, dict):
        return dict(task)
    return {
        "task_id": str(getattr(task, "task_id", "")),
        "benchmark": str(getattr(task, "benchmark", "")),
        "prompt": str(getattr(task, "agent_prompt", lambda: getattr(task, "prompt", ""))()),
        "entry_point": str(getattr(task, "entry_point", "")),
        "test_code": str(getattr(task, "test_code", "")),
        "canonical_solution": str(getattr(task, "canonical_solution", "")),
        "metadata": dict(getattr(task, "metadata", {}) or {}),
    }


class DistributedReferenceRunner:
    """Run one distributed Planner/Coder/Reviewer workflow in Docker."""

    def __init__(self, config: DockerExecutionConfig) -> None:
        self.config = replace(config, entrypoint_module="evaluation.distributed_entrypoint")
        self.docker_runner = DockerAgentRunner(self.config)

    def run(
        self,
        *,
        topology: str,
        task: dict[str, Any],
        seed: int,
        model_config: dict[str, Any],
        execution_config: dict[str, Any] | None,
        run_context: RunContext,
    ) -> dict[str, Any]:
        if topology not in {LINEAR, CLOSED_LOOP}:
            raise ValueError(f"unsupported distributed topology: {topology!r}")

        t0 = time.time()
        execution_config = dict(execution_config or {})
        max_revisions = int(execution_config.get("max_revisions", MAX_REVISIONS))
        if max_revisions != MAX_REVISIONS:
            raise ValueError("distributed publication smoke requires exactly two possible revisions")

        containers: dict[str, PreparedContainer] = {}
        trace: list[dict[str, Any]] = []
        role_outputs: dict[str, list[dict[str, Any]]] = {role: [] for role in ROLES}
        reported_error = 0.0
        exception = ""
        termination_reason = "completed"
        final_code = ""
        score_result: dict[str, Any] = {}
        trace_id = uuid.uuid4().hex

        try:
            containers = self._prepare_role_containers(
                task=task,
                seed=seed,
                model_config=model_config,
                execution_config=execution_config,
                run_context=run_context,
            )

            step = 0
            plan = self._run_role(
                containers,
                role="planner",
                step=step,
                messages=[
                    {"role": "system", "content": ROLE_PROMPTS["planner"]},
                    {"role": "user", "content": self._task_prompt(task)},
                ],
                task=task,
                seed=seed,
                model_config=model_config,
                execution_config=execution_config,
                run_context=run_context,
                trace_id=trace_id,
            )
            role_outputs["planner"].append(plan)
            trace.extend(self._events_with_container(plan, containers["planner"]))
            trace.append(self._host_event(run_context, "planner", step, trace_id, "handoff", to_role="coder"))
            step += 1

            code_result = self._run_role(
                containers,
                role="coder",
                step=step,
                messages=[
                    {"role": "system", "content": ROLE_PROMPTS["coder"]},
                    {
                        "role": "user",
                        "content": f"Task:\n{self._task_prompt(task)}\n\nPlan:\n{plan.get('content', '')}",
                    },
                ],
                task=task,
                seed=seed,
                model_config=model_config,
                execution_config=execution_config,
                run_context=run_context,
                trace_id=trace_id,
            )
            role_outputs["coder"].append(code_result)
            trace.extend(self._events_with_container(code_result, containers["coder"]))
            final_code = extract_python_code(str(code_result.get("generated_code") or code_result.get("content") or ""))
            trace.append(self._host_event(run_context, "coder", step, trace_id, "handoff", to_role="reviewer"))
            step += 1

            review = self._run_role(
                containers,
                role="reviewer",
                step=step,
                messages=[
                    {"role": "system", "content": ROLE_PROMPTS["reviewer"]},
                    {"role": "user", "content": f"Task:\n{self._task_prompt(task)}\n\nSolution:\n{final_code}"},
                ],
                task=task,
                seed=seed,
                model_config=model_config,
                execution_config=execution_config,
                run_context=run_context,
                trace_id=trace_id,
            )
            role_outputs["reviewer"].append(review)
            trace.extend(self._events_with_container(review, containers["reviewer"]))
            needs_revision = "REVISION_REQUEST" in str(review.get("content", "")).upper()
            trace.append(
                self._host_event(
                    run_context,
                    "reviewer",
                    step,
                    trace_id,
                    "review_decision",
                    accepted=not needs_revision,
                )
            )

            revisions = 0
            while topology == CLOSED_LOOP and needs_revision and revisions < MAX_REVISIONS:
                trace.append(
                    self._host_event(
                        run_context,
                        "reviewer",
                        step,
                        trace_id,
                        "revision_request",
                        revision_round=revisions + 1,
                    )
                )
                step += 1
                code_result = self._run_role(
                    containers,
                    role="coder",
                    step=step,
                    messages=[
                        {"role": "system", "content": ROLE_PROMPTS["coder"]},
                        {
                            "role": "user",
                            "content": (
                                f"Task:\n{self._task_prompt(task)}\n\nPrior solution:\n{final_code}\n\n"
                                f"Reviewer request:\n{review.get('content', '')}"
                            ),
                        },
                    ],
                    task=task,
                    seed=seed,
                    model_config=model_config,
                    execution_config=execution_config,
                    run_context=run_context,
                    trace_id=trace_id,
                )
                role_outputs["coder"].append(code_result)
                trace.extend(self._events_with_container(code_result, containers["coder"]))
                final_code = extract_python_code(str(code_result.get("generated_code") or code_result.get("content") or ""))
                trace.append(
                    self._host_event(
                        run_context,
                        "coder",
                        step,
                        trace_id,
                        "attempted_recovery",
                        revision_round=revisions + 1,
                    )
                )
                step += 1
                review = self._run_role(
                    containers,
                    role="reviewer",
                    step=step,
                    messages=[
                        {"role": "system", "content": ROLE_PROMPTS["reviewer"]},
                        {"role": "user", "content": f"Task:\n{self._task_prompt(task)}\n\nSolution:\n{final_code}"},
                    ],
                    task=task,
                    seed=seed,
                    model_config=model_config,
                    execution_config=execution_config,
                    run_context=run_context,
                    trace_id=trace_id,
                )
                role_outputs["reviewer"].append(review)
                trace.extend(self._events_with_container(review, containers["reviewer"]))
                needs_revision = "REVISION_REQUEST" in str(review.get("content", "")).upper()
                trace.append(
                    self._host_event(
                        run_context,
                        "reviewer",
                        step,
                        trace_id,
                        "review_decision",
                        accepted=not needs_revision,
                    )
                )
                revisions += 1

            score_result = self._run_score(
                containers["coder"],
                task=task,
                generated_code=final_code,
                execution_config=execution_config,
                run_context=run_context,
                trace_id=trace_id,
            )
        except Exception as exc:  # noqa: BLE001
            reported_error = 1.0
            termination_reason = "agent_exception"
            exception = repr(exc)
            trace.append(
                self._host_event(
                    run_context,
                    "coordinator",
                    len(trace),
                    trace_id,
                    "error_detection",
                    exception=exception,
                )
            )
        finally:
            for container in containers.values():
                try:
                    self.docker_runner._sync_staged_io(container)  # noqa: SLF001
                except Exception:
                    pass
                self.docker_runner.cleanup(container)

        total_usage = self._usage(role_outputs)
        success = float(score_result.get("success", 0.0) or 0.0)
        scorer_status = str(score_result.get("scorer_status") or ("error" if reported_error else "not_available"))
        operational_success = termination_reason == "completed" and reported_error == 0.0
        return redact_secrets({
            "executor_status": "ok" if reported_error == 0.0 else "error",
            "deployment_mode": "distributed",
            "agent_system": "distributed-reference-multi-agent",
            "framework": "distributed-reference-multi-agent",
            "framework_native": False,
            "multi_agent_impl": "distributed-reference-multi-agent",
            "agent_level": "multi_agent",
            "topology": topology,
            "success": success,
            "task_success_verdict": "success" if success == 1.0 else "failure",
            "operational_success": operational_success,
            "continued_operation": operational_success,
            "agent_detected_fault": False,
            "runner_observed_failure": bool(reported_error),
            "failure_detection_source": "runner_exception" if reported_error else "",
            "silent_failure": bool(success == 0.0 and operational_success),
            "duration_s": time.time() - t0,
            "reported_error": reported_error,
            "termination_reason": termination_reason,
            "exception": exception,
            "generated_code": final_code,
            "execution_trace": trace,
            "distributed_trace": trace,
            "role_outputs": role_outputs,
            "role_container_ids": {role: c.container_id for role, c in containers.items()},
            "role_container_count": len({c.container_id for c in containers.values()}),
            "llm_calls": total_usage["llm_calls"],
            "agent_reported_llm_calls": total_usage["llm_calls"],
            "tool_calls": 0,
            "turns": total_usage["turns"],
            "prompt_tokens": total_usage["prompt_tokens"],
            "completion_tokens": total_usage["completion_tokens"],
            "total_tokens": total_usage["total_tokens"],
            "token_usage_status": "measured" if total_usage["total_tokens"] else "unavailable",
            "cost_usd": None,
            "cost_status": "unavailable",
            "scorer_status": scorer_status,
            "scorer_error": score_result.get("scorer_error", ""),
            "tests_passed": int(score_result.get("tests_passed", 0) or 0),
            "tests_total": int(score_result.get("tests_total", 0) or 0),
            "scoring_evidence": score_result.get("scoring_evidence", {}),
            "framework_versions": self._framework_versions(role_outputs, score_result),
            "python_version": score_result.get("python_version") or self._python_version(role_outputs),
            "docker_image": self.config.image,
            "docker_image_digest": self._image_digest(containers),
            "container_resource_limits": self.config.resource_limits(),
            "artifact_paths": self._artifact_paths(containers),
        })

    def _prepare_role_containers(
        self,
        *,
        task: dict[str, Any],
        seed: int,
        model_config: dict[str, Any],
        execution_config: dict[str, Any],
        run_context: RunContext,
    ) -> dict[str, PreparedContainer]:
        containers: dict[str, PreparedContainer] = {}
        for role in ROLES:
            role_root = Path(run_context.output_root or ".").resolve() / "role-containers" / role
            role_context = RunContext(
                study_id=run_context.study_id,
                campaign_id=run_context.campaign_id,
                pair_id=run_context.pair_id,
                run_id=run_context.run_id,
                condition=run_context.condition,
                output_root=str(role_root),
                environment=run_context.environment,
            )
            containers[role] = self.docker_runner.prepare(
                agent_system="distributed-reference-multi-agent",
                topology="distributed",
                task=task,
                seed=seed,
                model_config=model_config,
                execution_config=execution_config,
                run_context=role_context,
            )
        return containers

    def _run_role(
        self,
        containers: dict[str, PreparedContainer],
        *,
        role: str,
        step: int,
        messages: list[dict[str, str]],
        task: dict[str, Any],
        seed: int,
        model_config: dict[str, Any],
        execution_config: dict[str, Any],
        run_context: RunContext,
        trace_id: str,
    ) -> dict[str, Any]:
        req = {
            "mode": "role",
            "role": role,
            "step_index": step,
            "trace_id": trace_id,
            "messages": messages,
            "task": task,
            "seed": seed,
            "model_config": redact_secrets(model_config),
            "execution_config": execution_config,
            "run_context": redact_secrets(run_context.__dict__),
        }
        return self._exec_json(containers[role], req, f"{role}_{step}.json", f"{role}_{step}_result.json")

    def _run_score(
        self,
        container: PreparedContainer,
        *,
        task: dict[str, Any],
        generated_code: str,
        execution_config: dict[str, Any],
        run_context: RunContext,
        trace_id: str,
    ) -> dict[str, Any]:
        req = {
            "mode": "score",
            "trace_id": trace_id,
            "task": task,
            "generated_code": generated_code,
            "execution_config": execution_config,
            "run_context": redact_secrets(run_context.__dict__),
        }
        return self._exec_json(container, req, "score_request.json", "score_result.json")

    def _exec_json(
        self,
        container: PreparedContainer,
        request: dict[str, Any],
        request_name: str,
        output_name: str,
    ) -> dict[str, Any]:
        request_path = Path(container.input_dir) / request_name
        request_path.write_text(json.dumps(redact_secrets(request), indent=2, sort_keys=True), encoding="utf-8")
        execution = self.docker_runner.exec_agent(
            container,
            request_path=f"/cj/input/{request_name}",
            output_path=f"/cj/output/{output_name}",
        )
        output_path = Path(container.output_dir) / output_name
        if not output_path.exists():
            payload = {
                "executor_status": "error",
                "executor_error": "distributed role output JSON was not produced",
                "docker_exit_code": execution.exit_code,
                "docker_stdout": execution.stdout,
                "docker_stderr": execution.stderr,
            }
        else:
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                payload = {
                    "executor_status": "error",
                    "executor_error": f"invalid distributed role JSON: {exc}",
                }
        payload["docker_exit_code"] = execution.exit_code
        payload["docker_timed_out"] = execution.timed_out
        payload["docker_stdout"] = execution.stdout
        payload["docker_stderr"] = execution.stderr
        if execution.exit_code != 0 and payload.get("executor_status") == "ok":
            payload["executor_status"] = "error"
            payload["executor_error"] = f"entrypoint exited with {execution.exit_code}"
        if payload.get("executor_status") != "ok":
            raise RuntimeError(str(payload.get("executor_error") or "distributed role execution failed"))
        return payload

    def _events_with_container(self, payload: dict[str, Any], container: PreparedContainer) -> list[dict[str, Any]]:
        events = []
        for event in payload.get("execution_trace") or []:
            if not isinstance(event, dict):
                continue
            event = dict(event)
            event["container_id"] = container.container_id
            event.setdefault("payload", {})
            if isinstance(event["payload"], dict):
                event["payload"]["container_id"] = container.container_id
            events.append(event)
        return events

    def _host_event(
        self,
        run_context: RunContext,
        role: str,
        step: int,
        trace_id: str,
        event_type: str,
        **payload: Any,
    ) -> dict[str, Any]:
        event = TraceEvent(
            study_id=run_context.study_id,
            run_id=run_context.run_id,
            pair_id=run_context.pair_id,
            agent_role=role,
            step_index=step,
            timestamp=time.time(),
            trace_id=trace_id,
            event_type=event_type,
            payload=payload,
        ).to_dict()
        return event

    def _task_prompt(self, task: dict[str, Any]) -> str:
        return str(task.get("prompt") or task.get("input") or "")

    def _usage(self, role_outputs: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        totals = {
            "llm_calls": 0,
            "turns": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        for outputs in role_outputs.values():
            for output in outputs:
                totals["llm_calls"] += int(output.get("llm_calls", 0) or 0)
                totals["turns"] += int(output.get("turns", 0) or 0)
                totals["prompt_tokens"] += int(output.get("prompt_tokens", 0) or 0)
                totals["completion_tokens"] += int(output.get("completion_tokens", 0) or 0)
                totals["total_tokens"] += int(output.get("total_tokens", 0) or 0)
        return totals

    def _framework_versions(
        self,
        role_outputs: dict[str, list[dict[str, Any]]],
        score_result: dict[str, Any],
    ) -> dict[str, Any]:
        versions: dict[str, Any] = {}
        for role, outputs in role_outputs.items():
            if outputs:
                versions[role] = outputs[-1].get("framework_versions", {})
        if score_result.get("framework_versions"):
            versions["score"] = score_result["framework_versions"]
        return versions

    def _python_version(self, role_outputs: dict[str, list[dict[str, Any]]]) -> str:
        for outputs in role_outputs.values():
            for output in outputs:
                if output.get("python_version"):
                    return str(output["python_version"])
        return ""

    def _image_digest(self, containers: dict[str, PreparedContainer]) -> str:
        for container in containers.values():
            if container.image_digest:
                return container.image_digest
        return ""

    def _artifact_paths(self, containers: dict[str, PreparedContainer]) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        for role, container in containers.items():
            artifacts[f"{role}_input_dir"] = container.artifact_input_dir or container.input_dir
            artifacts[f"{role}_output_dir"] = container.artifact_output_dir or container.output_dir
        return artifacts
