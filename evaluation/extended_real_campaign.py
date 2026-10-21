"""Real-agent E8 cross-layer experiment for the additive paper studies.

The earlier :mod:`evaluation.extended_campaign` E8 study is an inexpensive
operational probe.  This module executes actual framework agents, scores their
generated programs inside Docker, and records a matched five-condition design:

* direct baseline;
* CJ pass-through control;
* LLM latency only;
* container CPU pressure only;
* synchronized LLM latency and CPU pressure.

It intentionally remains separate from the canonical publication runner and
never appends to an existing study.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from chaos_jungle.db import SessionDB
from chaos_jungle.distributed import Injection
from chaos_jungle.faults.llm import LLMLatency
from chaos_jungle.faults.resources import CPUStress
from chaos_jungle.inject.group import InjectionGroup, InjectionGroupRunner
from chaos_jungle.targets import DockerTarget, LocalTarget
from evaluation.analysis.statistics import (
    holm_correction,
    mcnemar_exact,
    paired_permutation_test,
    task_clustered_bootstrap_ci,
)
from evaluation.distributed_runner import benchmark_task_to_dict
from evaluation.docker_runner import (
    DockerAgentRunResult,
    DockerAgentRunner,
    DockerExecutionConfig,
    RunContext,
    redact_secrets,
)
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.extended_campaign import _proxy_evidence
from evaluation.extended_studies import compound_interaction_effect
from evaluation.run import _load_task_subset
from evaluation.schema import OUTPUT_SCHEMA_VERSION
from evaluation.study_protocol import (
    PublicationStudyOrchestrator,
    _flatten_docker_result,
    _run_failure_verdicts,
    default_experiment_definition,
    make_pair_id,
)


CONDITIONS = (
    "direct_baseline",
    "cj_control",
    "llm_only",
    "infrastructure_only",
    "compound",
)
FAULT_CONDITIONS = {"llm_only", "infrastructure_only", "compound"}
PROXY_CONDITIONS = {"cj_control", "llm_only", "infrastructure_only", "compound"}
LATENCY_CONDITIONS = {"llm_only", "compound"}
CPU_CONDITIONS = {"infrastructure_only", "compound"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_secrets(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact_secrets(payload), sort_keys=True) + "\n")


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _scored(record: dict[str, Any]) -> bool:
    return (
        str(record.get("scorer_status", "")).lower() == "ok"
        and int(record.get("tests_total") or 0) > 0
        and isinstance(record.get("success"), (int, float))
    )


def _no_solution_failure(record: dict[str, Any]) -> bool:
    """Return whether an agent completed without a candidate program.

    EvalPlus cannot execute zero programs, but a missing candidate is still an
    observable task failure. It must not be confused with evaluator failure.
    """
    return (
        not str(record.get("generated_code") or "").strip()
        and float(record.get("success") or 0.0) == 0.0
        and str(record.get("scorer_error") or "").strip().lower()
        == "agent produced no generated_code"
        and str(record.get("executor_status") or "ok").lower() in {"", "ok"}
        and record.get("executor_timed_out") is not True
    )


def _task_outcome_available(record: dict[str, Any]) -> bool:
    return _scored(record) or _no_solution_failure(record)


def latest_factorial_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the latest append-only attempt for each pair and condition."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for record in records:
        key = (str(record.get("pair_id") or ""), str(record.get("condition") or ""))
        if not all(key):
            continue
        if key not in latest:
            order.append(key)
        latest[key] = record
    return [latest[key] for key in order]


def _proxy_default() -> dict[str, Any]:
    return {
        "intercepted_calls": 0,
        "affected_calls": 0,
        "http_status_counts": {},
        "configured_delay_s": 0.0,
        "observed_injected_delay_s": 0.0,
        "latency_absolute_error_s": None,
        "call_previews": [],
        "call_preview_truncated": False,
    }


def _percent(raw: Any) -> float | None:
    try:
        return float(str(raw).strip().removesuffix("%"))
    except (TypeError, ValueError):
        return None


def summarize_cross_layer(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate strict scoring coverage from complete binary task outcomes."""
    effective_records = latest_factorial_records(records)
    by_pair: dict[str, dict[str, dict[str, Any]]] = {}
    for record in effective_records:
        pair_id = str(record.get("pair_id") or "")
        condition = str(record.get("condition") or "")
        if pair_id and condition:
            by_pair.setdefault(pair_id, {})[condition] = record

    complete = [group for group in by_pair.values() if set(CONDITIONS) <= set(group)]
    scored = [group for group in complete if all(_scored(group[c]) for c in CONDITIONS)]
    outcome_complete = [
        group for group in complete
        if all(_task_outcome_available(group[c]) for c in CONDITIONS)
    ]
    scored_eligible = [
        group for group in scored
        if group["cj_control"].get("validity") == "valid"
        and all(group[c].get("validity") == "valid" for c in FAULT_CONDITIONS)
    ]
    outcome_eligible = [
        group for group in outcome_complete
        if group["cj_control"].get("validity") == "valid"
        and all(group[c].get("validity") == "valid" for c in FAULT_CONDITIONS)
    ]

    def mean(condition: str, metric: str) -> float | None:
        values = [
            float(group[condition][metric])
            for group in outcome_eligible
            if isinstance(group[condition].get(metric), (int, float))
        ]
        return statistics.fmean(values) if values else None

    success = {condition: mean(condition, "success") for condition in CONDITIONS}
    duration = {condition: mean(condition, "duration_s") for condition in CONDITIONS}
    success_interactions = [
        compound_interaction_effect(
            control=float(group["cj_control"]["success"]),
            fault_a=float(group["llm_only"]["success"]),
            fault_b=float(group["infrastructure_only"]["success"]),
            compound=float(group["compound"]["success"]),
        )
        for group in outcome_eligible
    ]
    duration_interactions = [
        float(group["compound"]["duration_s"])
        - float(group["llm_only"]["duration_s"])
        - float(group["infrastructure_only"]["duration_s"])
        + float(group["cj_control"]["duration_s"])
        for group in outcome_eligible
    ]
    return {
        "metric_scope": "real framework agent plus in-container benchmark scoring",
        "raw_records": len(records),
        "effective_records": len(effective_records),
        "requested_factorial_blocks": len(by_pair),
        "complete_factorial_blocks": len(complete),
        "complete_scored_factorial_blocks": len(scored),
        "valid_scored_factorial_blocks": len(scored_eligible),
        "complete_task_outcome_blocks": len(outcome_complete),
        "valid_task_outcome_blocks": len(outcome_eligible),
        "missing_factorial_blocks": len(by_pair) - len(complete),
        "excluded_unscored_blocks": len(complete) - len(scored),
        "excluded_task_outcome_unavailable_blocks": len(complete) - len(outcome_complete),
        "excluded_invalid_scored_blocks": len(scored) - len(scored_eligible),
        "excluded_invalid_task_outcome_blocks": len(outcome_complete) - len(outcome_eligible),
        "no_solution_failure_records": sum(_no_solution_failure(record) for record in effective_records),
        "unique_tasks": len({group["direct_baseline"].get("task_id") for group in outcome_eligible}),
        "success_rate_by_condition": success,
        "mean_duration_s_by_condition": duration,
        "fault_degradation_vs_control": {
            condition: (
                success["cj_control"] - success[condition]
                if success["cj_control"] is not None and success[condition] is not None
                else None
            )
            for condition in FAULT_CONDITIONS
        },
        "direct_to_control_correctness_difference": (
            success["cj_control"] - success["direct_baseline"]
            if success["cj_control"] is not None and success["direct_baseline"] is not None
            else None
        ),
        "compound_success_interaction_mean": (
            statistics.fmean(success_interactions) if success_interactions else None
        ),
        "compound_duration_interaction_s_mean": (
            statistics.fmean(duration_interactions) if duration_interactions else None
        ),
    }


def inferential_cross_layer(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return paired tests using CJ control as the fault-effect comparator."""
    records = latest_factorial_records(records)
    control = [
        r for r in records
        if r.get("condition") == "cj_control"
        and r.get("validity") == "valid"
        and _task_outcome_available(r)
    ]
    rows: list[dict[str, Any]] = []
    for condition in ("llm_only", "infrastructure_only", "compound"):
        fault = [
            r for r in records
            if r.get("condition") == condition
            and r.get("validity") == "valid"
            and _task_outcome_available(r)
        ]
        mcnemar = mcnemar_exact(control, fault, metric="success")
        success_ci = task_clustered_bootstrap_ci(control, fault, metric="success")
        duration_test = paired_permutation_test(control, fault, metric="duration_s")
        duration_ci = task_clustered_bootstrap_ci(control, fault, metric="duration_s")
        enough_tasks = int(success_ci.get("n_tasks") or 0) >= 3
        rows.extend([
            {
                "comparison": f"cj_control_vs_{condition}",
                "metric": "success",
                "effect": success_ci.get("mean_diff"),
                "ci_low": success_ci.get("ci_low") if enough_tasks else None,
                "ci_high": success_ci.get("ci_high") if enough_tasks else None,
                "p_value": mcnemar.get("p_value") if enough_tasks else None,
                "n_pairs": mcnemar.get("n_pairs"),
                "n_unique_tasks": success_ci.get("n_tasks"),
                "missing_pairs": mcnemar.get("missing_pairs"),
                "test": "mcnemar_exact" if enough_tasks else "not_run",
                "inference_status": "ok" if enough_tasks else "insufficient_unique_tasks",
            },
            {
                "comparison": f"cj_control_vs_{condition}",
                "metric": "duration_s",
                "effect": duration_ci.get("mean_diff"),
                "ci_low": duration_ci.get("ci_low") if enough_tasks else None,
                "ci_high": duration_ci.get("ci_high") if enough_tasks else None,
                "p_value": duration_test.get("p_value") if enough_tasks else None,
                "n_pairs": duration_test.get("n_pairs"),
                "n_unique_tasks": duration_ci.get("n_tasks"),
                "missing_pairs": duration_test.get("missing_pairs"),
                "test": "paired_permutation" if enough_tasks else "not_run",
                "inference_status": "ok" if enough_tasks else "insufficient_unique_tasks",
            },
        ])
    adjusted = holm_correction([row["p_value"] for row in rows])
    for row, corrected in zip(rows, adjusted):
        row["p_value_holm"] = corrected
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class RealCrossLayerCampaign:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results = Path(args.results_dir).resolve()
        self.results.mkdir(parents=True, exist_ok=True)
        env_file = os.path.abspath(args.env_file) if args.env_file else None
        self.docker_runner = DockerAgentRunner(DockerExecutionConfig(
            image=args.docker_image,
            cpus=args.cpus,
            memory=args.memory,
            timeout_s=args.container_timeout,
            env_file=env_file,
            preserve_io=True,
        ))
        self.study_id = args.study_id or f"e8-real-{uuid.uuid4().hex[:12]}"
        self.orchestrator = PublicationStudyOrchestrator(
            self.docker_runner,
            study_id=self.study_id,
            results_dir=str(self.results),
            proxy_port=args.proxy_port,
        )
        self.model_config = {
            "name": args.model,
            "base_url": args.base_url,
            "container_base_url": args.container_base_url,
            "api_key_env": "CJ_EVAL_API_KEY",
            "transport_retries": 0,
        }
        self.execution_config = {
            "agent_level": "individual",
            "dry_run": False,
            "max_turns": args.max_turns,
            "score_timeout_s": args.score_timeout,
            "strict_scoring_preflight": False,
        }
        self.db = SessionDB(path=str(self.results / "cj_evidence.sqlite"))

    def run(self) -> list[dict[str, Any]]:
        tasks = _load_task_subset(
            self.args.benchmark,
            self.args.tasks,
            self.args.task_selection_seed,
            benchmark_subset=self.args.benchmark_subset,
            complexity_balanced=True,
        )
        manifest = {
            "study": "E8-real-cross-layer-chaos",
            "study_id": self.study_id,
            "started_at": _now(),
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "cj_commit": _CJ_COMMIT,
            **_CJ_SOURCE_PROVENANCE,
            "docker_image": self.args.docker_image,
            "agent_system": self.args.system,
            "model": self.args.model,
            "benchmark": self.args.benchmark,
            "benchmark_subset": self.args.benchmark_subset,
            "task_ids": [task.task_id for task in tasks],
            "task_selection_seed": self.args.task_selection_seed,
            "seeds": self.args.seeds,
            "conditions": list(CONDITIONS),
            "fault_parameters": {
                "llm_latency_s": self.args.delay_s,
                "cpu_cores": 1,
                "cpu_duration_s": self.args.stress_duration,
                "maximum_skew_ms": self.args.maximum_skew_ms,
            },
            "resource_limits": self.docker_runner.config.resource_limits(),
            "canonical_studies_modified": False,
        }
        _write_json(self.results / "study_manifest.json", manifest)

        print("[cj-eval] E8-real scoring preflight (no model call)")
        self.orchestrator.run_campaign_preflight(
            agent_system=self.args.system,
            agent_level="individual",
            topology="single",
            task=tasks[0],
            seed=self.args.seeds[0],
            model_config=self.model_config,
            execution_config=self.execution_config,
            container_direct_base_url=self.args.container_base_url,
        )

        records: list[dict[str, Any]] = []
        total = len(tasks) * len(self.args.seeds) * len(CONDITIONS)
        completed = 0
        for seed_index, seed in enumerate(self.args.seeds):
            for task_index, task in enumerate(tasks):
                pair_id = make_pair_id(
                    study_id=self.study_id,
                    framework=self.args.system,
                    topology="single",
                    task_id=task.task_id,
                    seed=seed,
                    fault_name="cross_layer_factorial",
                    fault_parameters={
                        "delay_s": self.args.delay_s,
                        "cpu_duration_s": self.args.stress_duration,
                    },
                    repetition=seed_index,
                )
                order = list(CONDITIONS)
                random.Random(seed * 1009 + task_index).shuffle(order)
                for order_index, condition in enumerate(order):
                    print(
                        f"[cj-eval] E8-real {completed + 1}/{total}: "
                        f"{task.task_id} seed={seed} condition={condition}"
                    )
                    record = self._run_condition(
                        task=task,
                        seed=seed,
                        pair_id=pair_id,
                        condition=condition,
                        execution_order=order_index,
                    )
                    _append_jsonl(self.results / "runs.jsonl", record)
                    records.append(record)
                    completed += 1

        summary = summarize_cross_layer(records)
        inference = inferential_cross_layer(records)
        _write_json(self.results / "cross_layer_summary.json", summary)
        _write_csv(self.results / "inferential_summary.csv", inference)
        _write_csv(self.results / "task_results.csv", [self._flat_row(r) for r in records])
        manifest["finished_at"] = _now()
        manifest["record_count"] = len(records)
        manifest["summary"] = summary
        _write_json(self.results / "study_manifest.json", manifest)
        self.db.close()
        return records

    def _run_condition(
        self,
        *,
        task: Any,
        seed: int,
        pair_id: str,
        condition: str,
        execution_order: int,
    ) -> dict[str, Any]:
        run_id = f"e8-{condition}-{uuid.uuid4().hex[:12]}"
        campaign_id = f"{pair_id}-cross-layer"
        proxy_port = None
        proxy_url = ""
        session_id: int | None = None
        passthrough = None
        group_runner: InjectionGroupRunner | None = None
        container = None
        docker_target: DockerTarget | None = None
        group_payload: dict[str, Any] = {}
        start_error = ""
        cleanup_error = ""
        recovered = condition == "direct_baseline"
        result: DockerAgentRunResult | None = None
        started = time.time()
        proxy_evidence = _proxy_default()
        infrastructure_evidence: dict[str, Any] = {}
        task_dict = benchmark_task_to_dict(task)

        if condition in PROXY_CONDITIONS:
            proxy_port = self.orchestrator._select_proxy_ports()[0]  # noqa: SLF001
            proxy_url = self.orchestrator._docker_reachable_proxy_url(proxy_port)  # noqa: SLF001
            session_id = self.db.open_session(
                name=f"E8-real-{condition}-{pair_id}",
                target_type="cross-layer",
                target_addr=f"127.0.0.1:{proxy_port}",
            )
        endpoint = proxy_url if condition in PROXY_CONDITIONS else self.args.container_base_url
        context = RunContext(
            study_id=self.study_id,
            campaign_id=campaign_id,
            pair_id=pair_id,
            run_id=run_id,
            condition=condition,
            output_root=str(self.results / "artifacts" / run_id),
            environment=self.orchestrator._base_env(  # noqa: SLF001
                self.model_config,
                {"CJ_EVAL_BASE_URL": endpoint},
            ),
        )
        try:
            if condition in {"cj_control", "infrastructure_only"}:
                passthrough = self.orchestrator._start_passthrough_proxy(  # noqa: SLF001
                    self.model_config,
                    port=proxy_port,
                    session_id=session_id,
                    db=self.db,
                )

            container = self.docker_runner.prepare(
                agent_system=self.args.system,
                topology="single",
                task=task_dict,
                seed=seed,
                model_config=self.model_config,
                execution_config=self.execution_config,
                run_context=context,
            )

            injections: list[Injection] = []
            if condition in CPU_CONDITIONS:
                start_proc = self.docker_runner.start_container(container)
                if start_proc.returncode != 0:
                    raise RuntimeError(start_proc.stderr or start_proc.stdout)
                docker_target = DockerTarget(
                    container.container_id,
                    docker_bin=self.docker_runner.config.docker_bin,
                    timeout_s=60.0,
                )
                injections.append(Injection(
                    id="container-cpu-pressure",
                    target=docker_target,
                    fault=CPUStress(cores=1, duration_s=self.args.stress_duration),
                ))
            if condition in LATENCY_CONDITIONS:
                proxy_target = LocalTarget()
                proxy_target._db = self.db  # noqa: SLF001
                proxy_target._session_id = session_id  # noqa: SLF001
                injections.append(Injection(
                    id="llm-latency",
                    target=proxy_target,
                    fault=LLMLatency(
                        delay_s=self.args.delay_s,
                        port=int(proxy_port),
                        upstream=self.args.base_url.removesuffix("/v1"),
                        base_url_env="CJ_EVAL_BASE_URL",
                    ),
                ))
            if injections:
                group_runner = InjectionGroupRunner(InjectionGroup(
                    name=f"E8-real-{condition}-{run_id}",
                    injections=injections,
                    synchronization="scheduled" if len(injections) > 1 else "barrier",
                    atomic=True,
                    maximum_skew_ms=self.args.maximum_skew_ms,
                    start_after=self.args.start_after if len(injections) > 1 else 0.0,
                    on_activation_failure="rollback_all",
                    on_skew_violation="rollback_all",
                    safety_maximum_duration=self.args.safety_duration,
                ))
                group_runner.start()
                if condition in CPU_CONDITIONS:
                    time.sleep(0.25)
                    stats = self.docker_runner._run([  # noqa: SLF001
                        self.docker_runner.config.docker_bin,
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{json .}}",
                        container.container_id,
                    ], timeout=30)
                    stats_payload: dict[str, Any] = {}
                    if stats.returncode == 0:
                        try:
                            stats_payload = json.loads(stats.stdout.strip())
                        except json.JSONDecodeError:
                            stats_payload = {}
                    infrastructure_evidence = {
                        "measurement_source": "docker_stats_during_active_cpu_stress",
                        "container_id": container.container_id,
                        "cpu_percent": _percent(stats_payload.get("CPUPerc")),
                        "memory_usage": stats_payload.get("MemUsage"),
                        "memory_percent": _percent(stats_payload.get("MemPerc")),
                        "network_io": stats_payload.get("NetIO"),
                        "block_io": stats_payload.get("BlockIO"),
                        "pids": stats_payload.get("PIDs"),
                        "measurement_exit_code": stats.returncode,
                        "measurement_error": stats.stderr.strip()[:300],
                    }

            execution = self.docker_runner.exec_agent(container)
            result = self.docker_runner.collect(execution)
        except Exception as exc:  # noqa: BLE001
            start_error = repr(exc)
        finally:
            if group_runner is not None:
                try:
                    evidence = group_runner.stop()
                    group_payload = evidence.to_dict()
                    for member in group_payload.get("members", []):
                        member.pop("host", None)
                        if member.get("injection_id") == "llm-latency":
                            member.update({
                                "target_type": "host_proxy_process",
                                "target_role": "model_gateway",
                                "target_id": f"127.0.0.1:{proxy_port}",
                            })
                        elif member.get("injection_id") == "container-cpu-pressure":
                            member.update({
                                "target_type": "docker",
                                "target_role": "agent",
                                "target_id": container.container_id if container else "",
                            })
                    group_payload["group_valid"] = evidence.group_valid
                    recovered = (
                        all(member.reverted_at is not None for member in evidence.members)
                        and evidence.verdict != "recovery_invalid"
                    )
                    if proxy_port is not None and condition in LATENCY_CONDITIONS:
                        self.orchestrator._wait_proxy_down(int(proxy_port))  # noqa: SLF001
                except Exception as exc:  # noqa: BLE001
                    cleanup_error = repr(exc)
                    recovered = False
            if passthrough is not None:
                try:
                    self.orchestrator._stop_process(passthrough)  # noqa: SLF001
                    self.orchestrator._wait_proxy_down(int(proxy_port))  # noqa: SLF001
                    recovered = True
                except Exception as exc:  # noqa: BLE001
                    cleanup_error = cleanup_error or repr(exc)
                    recovered = False
            if session_id is not None:
                proxy_evidence = _proxy_evidence(self.db.get_llm_calls(session_id))
                try:
                    self.db.close_session(session_id, "reverted" if recovered else "failed")
                except Exception:
                    pass
            if docker_target is not None:
                try:
                    docker_target.disconnect()
                except Exception:
                    pass
            if container is not None:
                self.docker_runner.cleanup(container)

        if result is not None:
            record = _flatten_docker_result(result)
        else:
            record = {
                "executor_status": "error",
                "executor_error": start_error or "condition failed before agent execution",
                "executor_exit_code": 1,
                "executor_timed_out": False,
                "duration_s": time.time() - started,
                "success": None,
                "scorer_status": "not_available",
                "tests_passed": 0,
                "tests_total": 0,
                "reported_error": 1.0,
            }

        intercepted = int(proxy_evidence.get("intercepted_calls") or 0)
        affected = int(proxy_evidence.get("affected_calls") or 0)
        members = group_payload.get("members") if isinstance(group_payload.get("members"), list) else []
        activated = bool(members) and all(
            member.get("active_at") and not member.get("error")
            for member in members
            if isinstance(member, dict)
        )
        group_manifested = bool(members) and all(
            member.get("manifested") is True
            for member in members
            if isinstance(member, dict)
        )
        synchronization_valid = group_payload.get("synchronization_valid") is True
        cpu_observed = (
            condition not in CPU_CONDITIONS
            or (
                isinstance(infrastructure_evidence.get("cpu_percent"), (int, float))
                and float(infrastructure_evidence["cpu_percent"]) > 0.0
            )
        )
        if condition == "direct_baseline":
            validity = "valid"
            triggered = manifested = False
        elif condition == "cj_control":
            validity = "valid" if intercepted > 0 and affected == 0 and recovered else "invalid"
            triggered = intercepted > 0
            manifested = affected == 0 and intercepted > 0
        else:
            llm_reached = condition not in LATENCY_CONDITIONS or affected > 0
            triggered = activated and llm_reached
            manifested = group_manifested and llm_reached and cpu_observed
            if activated and manifested and synchronization_valid and recovered:
                validity = "valid"
            elif activated and condition in LATENCY_CONDITIONS and affected == 0:
                validity = "untriggered"
            elif activated and condition in CPU_CONDITIONS and not cpu_observed:
                validity = "inconclusive"
            else:
                validity = "invalid"

        record.update({
            "study": "E8-real-cross-layer-chaos",
            "study_id": self.study_id,
            "campaign_id": campaign_id,
            "pair_id": pair_id,
            "factorial_id": pair_id,
            "run_id": run_id,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "timestamp": _now(),
            "execution_order": execution_order,
            "condition": condition,
            "phase": "baseline" if condition in {"direct_baseline", "cj_control"} else "fault",
            "agent_level": "individual",
            "agent_system": self.args.system,
            "framework": self.args.system,
            "framework_native": True,
            "topology": "single",
            "benchmark": task.benchmark,
            "benchmark_subset": self.args.benchmark_subset,
            "task_id": task.task_id,
            "seed": seed,
            "task_selection_seed": self.args.task_selection_seed,
            "model": self.args.model,
            "fault_type": condition if condition in FAULT_CONDITIONS else "none",
            "fault_layers": (
                ["llm_proxy", "container_resource"] if condition == "compound"
                else ["llm_proxy"] if condition == "llm_only"
                else ["container_resource"] if condition == "infrastructure_only"
                else []
            ),
            "fault_parameters": {
                "delay_s": self.args.delay_s if condition in LATENCY_CONDITIONS else None,
                "cpu_cores": 1 if condition in CPU_CONDITIONS else None,
                "cpu_duration_s": self.args.stress_duration if condition in CPU_CONDITIONS else None,
                "maximum_skew_ms": self.args.maximum_skew_ms if condition == "compound" else None,
            },
            "validity": validity,
            "lifecycle": {
                "configured": condition in FAULT_CONDITIONS,
                "activated": activated if condition in FAULT_CONDITIONS else condition == "cj_control",
                "triggered": triggered,
                "manifested": manifested,
                "reverted": recovered if condition != "direct_baseline" else True,
                "recovered": recovered,
                "verdict": validity,
                "evidence_source": "cj_injection_group_and_proxy_database",
                "details": start_error or cleanup_error,
            },
            "group_evidence": group_payload,
            "infrastructure_evidence": infrastructure_evidence,
            "proxy_evidence": proxy_evidence,
            "proxy_calls_intercepted": intercepted,
            "proxy_calls_affected": affected,
            "scoring_eligible": _scored(record),
            "task_outcome_available": _task_outcome_available(record),
            "experiment_definition": {
                **default_experiment_definition("cross_layer_factorial").to_dict(),
                "blast_radius_boundary": (
                    "one disposable Docker agent container and one localhost CJ proxy process"
                ),
            },
            "cj_commit": _CJ_COMMIT,
            **_CJ_SOURCE_PROVENANCE,
            "docker_image": self.args.docker_image,
            "docker_image_digest": record.get("image_digest", ""),
            "container_resource_limits": record.get("resource_limits", {}),
            "duration_s": _safe_float(record.get("duration_s")) or (time.time() - started),
            "condition_wall_duration_s": time.time() - started,
            "cleanup_error": cleanup_error,
        })
        record.update(_run_failure_verdicts(record))
        return redact_secrets(record)

    @staticmethod
    def _flat_row(record: dict[str, Any]) -> dict[str, Any]:
        lifecycle = record.get("lifecycle") or {}
        proxy = record.get("proxy_evidence") or {}
        infrastructure = record.get("infrastructure_evidence") or {}
        return {
            "study_id": record.get("study_id"),
            "pair_id": record.get("pair_id"),
            "run_id": record.get("run_id"),
            "condition": record.get("condition"),
            "execution_order": record.get("execution_order"),
            "framework": record.get("framework"),
            "benchmark": record.get("benchmark"),
            "task_id": record.get("task_id"),
            "seed": record.get("seed"),
            "model": record.get("model"),
            "success": record.get("success"),
            "scorer_status": record.get("scorer_status"),
            "tests_passed": record.get("tests_passed"),
            "tests_total": record.get("tests_total"),
            "duration_s": record.get("duration_s"),
            "validity": record.get("validity"),
            "activated": lifecycle.get("activated"),
            "triggered": lifecycle.get("triggered"),
            "manifested": lifecycle.get("manifested"),
            "reverted": lifecycle.get("reverted"),
            "recovered": lifecycle.get("recovered"),
            "proxy_calls_intercepted": proxy.get("intercepted_calls"),
            "proxy_calls_affected": proxy.get("affected_calls"),
            "configured_delay_s": proxy.get("configured_delay_s"),
            "observed_injected_delay_s": proxy.get("observed_injected_delay_s"),
            "observed_cpu_percent": infrastructure.get("cpu_percent"),
            "observed_memory_usage": infrastructure.get("memory_usage"),
            "docker_image_digest": record.get("docker_image_digest"),
            "cj_commit": record.get("cj_commit"),
        }


def _parse_seeds(raw: str) -> list[int]:
    seeds = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not seeds:
        raise argparse.ArgumentTypeError("at least one seed is required")
    if len(seeds) != len(set(seeds)):
        raise argparse.ArgumentTypeError("seeds must be unique")
    return seeds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-image", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--system", choices=("autogen-real", "langgraph-real", "crewai-real"), default="autogen-real")
    parser.add_argument("--benchmark", choices=("humanevalplus", "mbppplus"), default="humanevalplus")
    parser.add_argument("--benchmark-subset", choices=("auto", "smoke", "development", "full"), default="development")
    parser.add_argument("--tasks", type=int, default=1)
    parser.add_argument("--seeds", type=_parse_seeds, default=[17])
    parser.add_argument(
        "--task-selection-seed",
        type=int,
        default=17,
        help="Fixed seed used to select one reproducible task manifest for all execution seeds",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--container-base-url", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--proxy-port", type=int, default=20380)
    parser.add_argument("--delay-s", type=float, default=0.5)
    parser.add_argument("--stress-duration", type=int, default=480)
    parser.add_argument("--maximum-skew-ms", type=float, default=250.0)
    parser.add_argument("--start-after", type=float, default=0.5)
    parser.add_argument("--safety-duration", type=float, default=600.0)
    parser.add_argument("--container-timeout", type=float, default=420.0)
    parser.add_argument("--score-timeout", type=float, default=25.0)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--cpus", type=float, default=1.0)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--study-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.tasks < 1:
        raise SystemExit("--tasks must be at least one")
    if args.delay_s < 0:
        raise SystemExit("--delay-s must be non-negative")
    if args.stress_duration < 1:
        raise SystemExit("--stress-duration must be at least one second")
    campaign = RealCrossLayerCampaign(args)
    records = campaign.run()
    print(f"[cj-eval] E8-real complete: {len(records)} records in {campaign.results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
