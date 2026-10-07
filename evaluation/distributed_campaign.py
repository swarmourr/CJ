"""CLI for the distributed reference multi-agent smoke experiment."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from evaluation.distributed_faults import (
    COMMUNICATION_FAULTS,
    ROLE_LOCAL_FAULTS,
    DistributedFaultPlan,
)
from evaluation.distributed_runner import DistributedReferenceRunner, benchmark_task_to_dict
from evaluation.docker_runner import DockerExecutionConfig, RunContext, redact_secrets
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.output import generate_all_outputs
from evaluation.run import _load_task_subset, _publication_model_config
from evaluation.schema import OUTPUT_SCHEMA_VERSION
from evaluation.study_protocol import default_experiment_definition, make_pair_id
from evaluation.task_complexity import classify_task_complexity


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run a container-per-role distributed reference smoke")
    p.add_argument("--docker-image", required=True)
    p.add_argument("--results-dir", required=True)
    p.add_argument("--benchmark", choices=["humanevalplus", "mbppplus"], default="humanevalplus")
    p.add_argument("--benchmark-subset", choices=["auto", "smoke", "development", "full"], default="smoke")
    p.add_argument("--tasks", type=int, default=1)
    p.add_argument("--seed", type=int, default=301)
    p.add_argument("--topology", choices=["linear", "closed_loop"], default="linear")
    p.add_argument("--model", default=os.environ.get("CJ_EVAL_MODEL") or os.environ.get("LLM_MODEL") or "gpt-4o-mini")
    p.add_argument("--base-url", default=os.environ.get("CJ_EVAL_BASE_URL") or os.environ.get("LLM_BASE_URL") or "")
    p.add_argument("--container-base-url", default=os.environ.get("CJ_EVAL_CONTAINER_BASE_URL") or "")
    p.add_argument("--api-key", default=os.environ.get("CJ_EVAL_API_KEY") or os.environ.get("LLM_API_KEY") or "dummy")
    p.add_argument("--env-file")
    p.add_argument("--container-timeout", type=float, default=420.0)
    p.add_argument("--score-timeout", type=float, default=25.0)
    p.add_argument("--max-turns", type=int, default=8)
    p.add_argument("--cpus", type=float, default=1.0)
    p.add_argument("--memory", default="2g")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--study-id")
    p.add_argument("--triplet", action="store_true", help="Run direct/control/fault triplets")
    p.add_argument(
        "--fault",
        default="none",
        help="Distributed fault name, comma-list, or suite name",
    )
    p.add_argument(
        "--fault-suite",
        choices=["smoke", "communication", "role_local", "all"],
        help="Named distributed fault suite",
    )
    p.add_argument("--fault-severity", default="medium", choices=["low", "medium", "high"])
    p.add_argument("--fault-target-role", default="coder")
    p.add_argument("--fault-from-role", default="planner")
    p.add_argument("--fault-to-role", default="coder")
    return p


DISTRIBUTED_FAULT_SUITES = {
    "smoke": ["message_delay"],
    "communication": sorted(COMMUNICATION_FAULTS),
    "role_local": sorted(ROLE_LOCAL_FAULTS),
    "all": sorted(COMMUNICATION_FAULTS | ROLE_LOCAL_FAULTS),
}


def _distributed_fault_names(args: argparse.Namespace) -> list[str]:
    if args.fault_suite and args.fault not in ("", "none"):
        raise SystemExit("Use either --fault or --fault-suite, not both")
    if args.fault_suite:
        return list(DISTRIBUTED_FAULT_SUITES[args.fault_suite])
    raw = str(args.fault or "none").strip()
    if raw in ("", "none"):
        return ["none"]
    if raw in DISTRIBUTED_FAULT_SUITES:
        return list(DISTRIBUTED_FAULT_SUITES[raw])
    names = [part.strip() for part in raw.split(",") if part.strip()]
    supported = set().union(*DISTRIBUTED_FAULT_SUITES.values())
    unsupported = [name for name in names if name not in supported]
    if unsupported:
        raise SystemExit(f"unsupported distributed fault(s): {', '.join(unsupported)}")
    return names


def _distributed_fault_config(args: argparse.Namespace, fault_name: str) -> dict[str, Any]:
    if fault_name in ("", "none"):
        return {}
    config: dict[str, Any] = {
        "fault_type": fault_name,
        "severity": args.fault_severity,
    }
    if fault_name in COMMUNICATION_FAULTS:
        config.update({
            "from_role": args.fault_from_role,
            "to_role": args.fault_to_role,
        })
        if fault_name == "message_misrouting":
            config["target_role"] = args.fault_target_role
            config.setdefault("misroute_to", args.fault_target_role)
    if fault_name in ROLE_LOCAL_FAULTS:
        config["target_role"] = args.fault_target_role
    return config


def _task_complexity_fields(task: Any) -> dict[str, Any]:
    c = classify_task_complexity(
        benchmark=task.benchmark,
        task_id=task.task_id,
        prompt=task.prompt,
        test_code=task.test_code,
        canonical_solution=task.canonical_solution,
    )
    return {
        "task_complexity": c.complexity_proxy,
        "complexity_proxy": c.complexity_proxy,
        "complexity_proxy_kind": c.complexity_proxy_kind,
        "complexity_score": c.complexity_score,
        "complexity_prompt_chars": c.prompt_chars,
        "complexity_prompt_words": c.prompt_words,
        "complexity_test_chars": c.test_chars,
        "complexity_test_assertions": c.test_assertions,
        "complexity_reference_solution_chars": c.reference_solution_chars,
        "complexity_reference_solution_lines": c.reference_solution_lines,
        "complexity_required_reasoning_steps": c.required_reasoning_steps,
        "complexity_algorithmic_category": c.algorithmic_category,
        "complexity_classification_rule": c.complexity_classification_rule,
    }


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(redact_secrets(record), sort_keys=True) + "\n")


def _record_from_result(
    *,
    result: dict[str, Any],
    task: Any,
    task_data: dict[str, Any],
    study_id: str,
    campaign_id: str,
    pair_id: str,
    run_id: str,
    seed: int,
    model: str,
    docker_image: str,
    topology: str,
    condition: str,
    fault_name: str,
    fault_parameters: dict[str, Any],
) -> dict[str, Any]:
    definition = default_experiment_definition(fault_name)
    lifecycle = (
        result.get("distributed_fault_lifecycle")
        if condition == "cj_fault"
        else {"configured": False, "verdict": "valid"}
    )
    validity = (
        str((lifecycle or {}).get("verdict") or "inconclusive").lower()
        if condition == "cj_fault"
        else "valid"
    )
    fault_plan = DistributedFaultPlan.from_config(fault_parameters)
    record = {
        **result,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "study_id": study_id,
        "campaign_id": campaign_id,
        "pair_id": pair_id,
        "run_id": run_id,
        "condition": condition,
        "phase": "fault" if condition == "cj_fault" else "baseline",
        "agent_level": "multi_agent",
        "agent_system": "distributed-reference-multi-agent",
        "framework": "distributed-reference-multi-agent",
        "framework_native": False,
        "multi_agent_impl": "distributed-reference-multi-agent",
        "deployment_mode": "distributed",
        "topology": topology,
        "benchmark": task_data.get("benchmark", ""),
        "task_id": task_data.get("task_id", ""),
        "seed": seed,
        "model": model,
        "endpoint_type": (
            "distributed_reference_direct"
            if condition == "direct_baseline"
            else "distributed_reference_control"
            if condition == "cj_control"
            else "distributed_reference_fault"
        ),
        "fault_type": fault_name,
        "fault_layer": fault_plan.layer(),
        "fault_parameters": fault_parameters,
        "fault_target_role": fault_parameters.get("target_role", ""),
        "target": "container_per_role",
        "validity": validity,
        "lifecycle": lifecycle,
        "distributed_fault_evidence": result.get("distributed_fault_evidence", []),
        "experiment_definition": definition.to_dict(),
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
        "docker_image": docker_image,
        "artifact_path": result.get("artifact_paths", {}),
    }
    record.update(_task_complexity_fields(task))
    return record


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    model_cfg = _publication_model_config(args, None)
    if args.container_base_url:
        model_cfg["base_url"] = args.container_base_url
    elif args.base_url:
        model_cfg["base_url"] = args.base_url
    if model_cfg.get("base_url"):
        os.environ["CJ_EVAL_BASE_URL"] = str(model_cfg["base_url"])
    if model_cfg.get("name"):
        os.environ["CJ_EVAL_MODEL"] = str(model_cfg["name"])

    tasks = _load_task_subset(
        args.benchmark,
        args.tasks,
        args.seed,
        benchmark_subset=args.benchmark_subset,
        complexity_balanced=True,
    )
    study_id = args.study_id or f"dist-study-{uuid.uuid4().hex[:12]}"
    campaign_id = f"dist-{args.topology}-{args.benchmark}-{uuid.uuid4().hex[:8]}"

    docker_cfg = DockerExecutionConfig(
        image=args.docker_image,
        cpus=args.cpus,
        memory=args.memory,
        timeout_s=args.container_timeout,
        env_file=os.path.abspath(args.env_file) if args.env_file else None,
        preserve_io=True,
    )
    runner = DistributedReferenceRunner(docker_cfg)
    base_execution_cfg = {
        "dry_run": args.dry_run,
        "max_turns": args.max_turns,
        "score_timeout_s": args.score_timeout,
        "agent_level": "multi_agent",
        "max_revisions": 2,
    }

    fault_names = _distributed_fault_names(args)
    if not args.triplet and fault_names != ["none"]:
        args.triplet = True

    print("\n[cj-eval] === Distributed reference multi-agent smoke ===")
    print(f"  study_id  : {study_id}")
    print(f"  image     : {args.docker_image}")
    print(f"  topology  : {args.topology}")
    print(f"  benchmark : {args.benchmark} ({len(tasks)} tasks, subset={args.benchmark_subset})")
    print(f"  faults    : {', '.join(fault_names)}")
    print(f"  triplet   : {args.triplet}")
    print(f"  results   : {results_dir}")

    for idx, task in enumerate(tasks):
        task_data = benchmark_task_to_dict(task)
        for fault_name in fault_names:
            fault_parameters = _distributed_fault_config(args, fault_name)
            pair_id = make_pair_id(
                study_id=study_id,
                framework="distributed-reference-multi-agent",
                topology=args.topology,
                task_id=task.task_id,
                seed=args.seed,
                fault_name=fault_name,
                fault_parameters=fault_parameters,
                repetition=0,
            )
            conditions = (
                ["direct_baseline", "cj_control", "cj_fault"]
                if args.triplet and fault_name not in ("", "none")
                else ["direct_baseline"]
            )
            for condition in conditions:
                run_id = f"dist-{uuid.uuid4().hex[:16]}"
                run_root = results_dir / (
                    f"{idx:03d}-{task.task_id.replace('/', '-')}-{fault_name}-{condition}-{run_id}"
                )
                context = RunContext(
                    study_id=study_id,
                    campaign_id=campaign_id,
                    pair_id=pair_id,
                    run_id=run_id,
                    condition=condition,
                    output_root=str(run_root),
                    environment={},
                )
                execution_cfg = dict(base_execution_cfg)
                if condition == "cj_fault":
                    execution_cfg["distributed_fault"] = fault_parameters
                result = runner.run(
                    topology=args.topology,
                    task=task_data,
                    seed=args.seed,
                    model_config=model_cfg,
                    execution_config=execution_cfg,
                    run_context=context,
                )
                record = _record_from_result(
                    result=result,
                    task=task,
                    task_data=task_data,
                    study_id=study_id,
                    campaign_id=campaign_id,
                    pair_id=pair_id,
                    run_id=run_id,
                    seed=args.seed,
                    model=str(model_cfg.get("name", args.model)),
                    docker_image=args.docker_image,
                    topology=args.topology,
                    condition=condition,
                    fault_name=fault_name,
                    fault_parameters=fault_parameters,
                )
                _append_jsonl(results_dir / "runs.jsonl", record)

    generate_all_outputs(str(results_dir), study_id=study_id)
    print(f"[cj-eval] distributed smoke complete: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
