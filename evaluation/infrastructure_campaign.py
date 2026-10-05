"""Container-scoped infrastructure fault smoke runner.

This module is intentionally small and additive.  It reuses DockerAgentRunner,
DockerTarget, and run_container_scoped_fault to produce the same three-condition
record shape as the publication study, but marks the records as infrastructure
validation rather than LLM/API resilience evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from chaos_jungle.faults.network import NetworkDelay, NetworkLoss
from chaos_jungle.faults.resources import CPUStress, DiskFull, MemoryStress
from evaluation.benchmarks.base import BenchmarkTask
from evaluation.docker_runner import (
    DockerAgentRunResult,
    DockerAgentRunner,
    DockerExecutionConfig,
    RunContext,
)
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.infrastructure_orchestrator import run_container_scoped_fault
from evaluation.output import generate_all_outputs
from evaluation.schema import OUTPUT_SCHEMA_VERSION
from evaluation.study_protocol import (
    default_experiment_definition,
    make_pair_id,
    _task_to_request_dict,
)


INFRA_FAULTS = {
    "cpu_pressure": lambda: CPUStress(cores=1, duration_s=30),
    "memory_pressure": lambda: MemoryStress(mb=64, duration_s=30),
    "network_latency": lambda: NetworkDelay("100ms", iface="eth0"),
    "packet_loss": lambda: NetworkLoss("5%", iface="eth0"),
    "disk_full": lambda: DiskFull(path="/tmp", size_mb=63),
}

NETWORK_FAULTS = {"network_latency", "packet_loss"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-image", required=True)
    parser.add_argument("--results-dir", default="results/infra-smoke")
    parser.add_argument("--study-id", default=None)
    parser.add_argument("--fault", choices=sorted(INFRA_FAULTS), default="cpu_pressure")
    parser.add_argument("--benchmark", choices=["toy", "humanevalplus", "mbppplus"], default="toy")
    parser.add_argument("--tasks", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--system", default="autogen-real")
    parser.add_argument("--topology", default="single")
    parser.add_argument("--model", default=os.environ.get("CJ_EVAL_MODEL", "dry-run"))
    parser.add_argument("--base-url", default=os.environ.get("CJ_EVAL_BASE_URL", ""))
    parser.add_argument("--container-base-url", default=os.environ.get("CJ_EVAL_CONTAINER_BASE_URL", ""))
    parser.add_argument("--env-file", default=os.environ.get("CJ_EVAL_ENV_FILE"))
    parser.add_argument("--cpus", type=float, default=1.0)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--container-timeout", type=float, default=180.0)
    parser.add_argument("--score-timeout", type=float, default=10.0)
    parser.add_argument("--max-turns", type=int, default=2)
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    study_id = args.study_id or f"infra-study-{uuid.uuid4().hex[:12]}"
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    records_path = results_dir / "runs.jsonl"
    fault = INFRA_FAULTS[args.fault]()
    docker_cfg = DockerExecutionConfig(
        image=args.docker_image,
        cpus=args.cpus,
        memory=args.memory,
        timeout_s=args.container_timeout,
        env_file=args.env_file,
        preserve_io=True,
        capabilities=["NET_ADMIN", "NET_RAW"] if args.fault in NETWORK_FAULTS else [],
    )
    runner = DockerAgentRunner(docker_cfg)
    model_cfg = {
        "name": args.model,
        "base_url": args.base_url,
        "container_base_url": args.container_base_url or args.base_url,
        "api_key_env": "CJ_EVAL_API_KEY",
        "transport_retries": 0,
    }
    execution_cfg = {
        "dry_run": args.dry_run,
        "max_turns": args.max_turns,
        "score_timeout_s": args.score_timeout,
        "agent_level": "individual",
    }
    tasks = _load_tasks(args.benchmark, args.tasks, args.seed)
    print("\n[cj-eval] === Infrastructure smoke ===")
    print(f"  study_id : {study_id}")
    print(f"  image    : {args.docker_image}")
    print(f"  fault    : {args.fault}")
    print(f"  results  : {results_dir}")
    for task in tasks:
        pair_id = make_pair_id(
            study_id=study_id,
            framework=args.system,
            topology=args.topology,
            task_id=task.task_id,
            seed=args.seed,
            fault_name=args.fault,
            fault_parameters=getattr(fault, "_parameters", lambda: {})(),
            repetition=0,
        )
        campaign_id = f"{pair_id}-{args.fault}"
        task_dict = _task_to_request_dict(task)
        common = {
            "study_id": study_id,
            "campaign_id": campaign_id,
            "pair_id": pair_id,
            "agent_system": args.system,
            "agent_level": "individual",
            "topology": args.topology,
            "task": task_dict,
            "seed": args.seed,
            "model_config": model_cfg,
            "execution_config": execution_cfg,
            "fault_name": args.fault,
            "fault_parameters": getattr(fault, "_parameters", lambda: {})(),
        }
        direct = runner.run(
            agent_system=args.system,
            topology=args.topology,
            task=task_dict,
            seed=args.seed,
            model_config=model_cfg,
            execution_config=execution_cfg,
            run_context=_context(
                results_dir,
                study_id,
                campaign_id,
                pair_id,
                "direct_baseline",
            ),
        )
        control = runner.run(
            agent_system=args.system,
            topology=args.topology,
            task=task_dict,
            seed=args.seed,
            model_config=model_cfg,
            execution_config=execution_cfg,
            run_context=_context(
                results_dir,
                study_id,
                campaign_id,
                pair_id,
                "cj_control",
            ),
        )
        fault_result = run_container_scoped_fault(
            docker_runner=runner,
            fault=fault,
            agent_system=args.system,
            topology=args.topology,
            task=task_dict,
            seed=args.seed,
            model_config=model_cfg,
            execution_config=execution_cfg,
            run_context=_context(
                results_dir,
                study_id,
                campaign_id,
                pair_id,
                "cj_fault",
            ),
        )
        records = [
            _record_from_result(
                result=direct,
                condition="direct_baseline",
                lifecycle=_infra_control_lifecycle("direct_baseline"),
                validity="valid",
                fault_type="none",
                activation_evidence={},
                recovery_evidence={},
                **common,
            ),
            _record_from_result(
                result=control,
                condition="cj_control",
                lifecycle=_infra_control_lifecycle("cj_control"),
                validity="valid",
                fault_type="infrastructure_control",
                activation_evidence={},
                recovery_evidence={},
                **common,
            ),
            _record_from_result(
                result=fault_result.result,
                condition="cj_fault",
                lifecycle=fault_result.lifecycle,
                validity=str(fault_result.lifecycle.get("verdict", "inconclusive")).lower(),
                fault_type=args.fault,
                activation_evidence=fault_result.activation_evidence,
                recovery_evidence=fault_result.recovery_evidence,
                **common,
            ),
        ]
        with records_path.open("a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    generate_all_outputs(str(results_dir), study_id=study_id)
    return 0


def _load_tasks(benchmark: str, tasks: int, seed: int) -> list[BenchmarkTask]:
    if benchmark == "toy":
        return [
            BenchmarkTask(
                task_id="toy/solution",
                benchmark="toy",
                prompt="Write a function named solution that returns None.",
                entry_point="solution",
                test_code="assert solution() is None\n",
                canonical_solution="def solution():\n    return None\n",
                metadata={"source": "infrastructure_smoke", "benchmark_subset": "validation"},
            )
        ][:tasks]
    from evaluation.benchmarks import REGISTRY
    loader = REGISTRY[benchmark](subset="smoke")
    loader.smoke_n = tasks
    return loader.load(seed=seed)


def _context(
    results_dir: Path,
    study_id: str,
    campaign_id: str,
    pair_id: str,
    condition: str,
) -> RunContext:
    run_id = uuid.uuid4().hex
    return RunContext(
        study_id=study_id,
        campaign_id=campaign_id,
        pair_id=pair_id,
        run_id=run_id,
        condition=condition,
        output_root=str(results_dir / f"{condition}-{run_id[:12]}"),
    )


def _infra_control_lifecycle(condition: str) -> dict[str, Any]:
    now = time.time()
    return {
        "configured": condition == "cj_control",
        "activated": condition == "cj_control",
        "triggered": False,
        "manifested": False,
        "reverted": True,
        "recovered": True,
        "infrastructure_recovered": True,
        "agent_recovered": "",
        "verdict": "valid",
        "evidence_source": "docker_target_control" if condition == "cj_control" else "none",
        "timestamps": {"configured": now},
    }


def _record_from_result(
    *,
    result: DockerAgentRunResult,
    condition: str,
    lifecycle: dict[str, Any],
    validity: str,
    fault_type: str,
    activation_evidence: dict[str, Any],
    recovery_evidence: dict[str, Any],
    study_id: str,
    campaign_id: str,
    pair_id: str,
    agent_system: str,
    agent_level: str,
    topology: str,
    task: dict[str, Any],
    seed: int,
    model_config: dict[str, Any],
    execution_config: dict[str, Any],
    fault_name: str,
    fault_parameters: dict[str, Any],
) -> dict[str, Any]:
    run_result = result.result if isinstance(result.result, dict) else {}
    run_id = str(run_result.get("run_id") or uuid.uuid4().hex)
    record = {
        "study_id": study_id,
        "campaign_id": campaign_id,
        "pair_id": pair_id,
        "run_id": run_id,
        "condition": condition,
        "phase": "fault" if condition == "cj_fault" else "baseline",
        "agent_level": agent_level,
        "agent_system": agent_system,
        "framework": run_result.get("framework", agent_system),
        "topology": topology,
        "benchmark": task.get("benchmark", ""),
        "benchmark_subset": task.get("metadata", {}).get("benchmark_subset", ""),
        "task_id": task.get("task_id", ""),
        "task_complexity": task.get("metadata", {}).get("complexity_proxy", ""),
        "model": model_config.get("name") or model_config.get("model", ""),
        "endpoint_type": "dry_run" if execution_config.get("dry_run") else "openai_compat",
        "seed": seed,
        "fault_type": fault_type,
        "fault_layer": "infrastructure",
        "fault_parameters": {"name": fault_name, **(fault_parameters or {})},
        "validity": validity,
        "success": run_result.get("success", 0.0),
        "duration_s": run_result.get("duration_s", result.duration_s),
        "reported_error": run_result.get("reported_error", 0.0),
        "retries": run_result.get("retries", 0),
        "llm_calls": run_result.get("llm_calls", 0),
        "tool_calls": run_result.get("tool_calls", 0),
        "turns": run_result.get("turns", 0),
        "prompt_tokens": run_result.get("prompt_tokens"),
        "completion_tokens": run_result.get("completion_tokens"),
        "total_tokens": run_result.get("total_tokens"),
        "cost_usd": run_result.get("cost_usd"),
        "token_usage_status": run_result.get("token_usage_status", "unavailable"),
        "cost_status": run_result.get("cost_status", "unavailable"),
        "tests_passed": run_result.get("tests_passed", 0),
        "tests_total": run_result.get("tests_total", 0),
        "termination_reason": run_result.get("termination_reason", ""),
        "exception": run_result.get("exception", ""),
        "executor_status": run_result.get("executor_status", "ok"),
        "executor_exit_code": result.exit_code,
        "executor_timed_out": result.timed_out,
        "executor_error": run_result.get("executor_error", ""),
        "docker_image": result.image,
        "docker_image_digest": result.image_digest,
        "container_resource_limits": result.resource_limits,
        "artifact_paths": result.artifact_paths,
        "lifecycle": lifecycle,
        "activation_evidence": activation_evidence,
        "recovery_evidence": recovery_evidence,
        "experiment_definition": default_experiment_definition(fault_name).to_dict(),
        "cj_commit": _CJ_COMMIT,
        "cj_source_version": _CJ_SOURCE_PROVENANCE.get("cj_source_version", _CJ_COMMIT),
        "cj_source_state": _CJ_SOURCE_PROVENANCE.get("cj_source_state", "unknown"),
        "cj_source_dirty": _CJ_SOURCE_PROVENANCE.get("cj_source_dirty"),
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
    }
    metadata = task.get("metadata", {}) if isinstance(task.get("metadata"), dict) else {}
    for key, value in metadata.items():
        if key.startswith("complexity_") or key in {
            "complexity_proxy",
            "complexity_proxy_kind",
            "complexity_score",
        }:
            record[key] = value
    return record


if __name__ == "__main__":
    raise SystemExit(main())
