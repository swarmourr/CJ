"""CLI for the distributed reference multi-agent smoke experiment."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

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
    return p


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
) -> dict[str, Any]:
    definition = default_experiment_definition("none")
    record = {
        **result,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "study_id": study_id,
        "campaign_id": campaign_id,
        "pair_id": pair_id,
        "run_id": run_id,
        "condition": "direct_baseline",
        "phase": "baseline",
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
        "endpoint_type": "direct_model_endpoint",
        "fault_type": "none",
        "fault_parameters": {},
        "fault_target_role": "",
        "target": "container_per_role",
        "validity": "valid",
        "lifecycle": {"configured": False, "verdict": "valid"},
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
    execution_cfg = {
        "dry_run": args.dry_run,
        "max_turns": args.max_turns,
        "score_timeout_s": args.score_timeout,
        "agent_level": "multi_agent",
        "max_revisions": 2,
    }

    print("\n[cj-eval] === Distributed reference multi-agent smoke ===")
    print(f"  study_id  : {study_id}")
    print(f"  image     : {args.docker_image}")
    print(f"  topology  : {args.topology}")
    print(f"  benchmark : {args.benchmark} ({len(tasks)} tasks, subset={args.benchmark_subset})")
    print(f"  results   : {results_dir}")

    for idx, task in enumerate(tasks):
        task_data = benchmark_task_to_dict(task)
        pair_id = make_pair_id(
            study_id=study_id,
            framework="distributed-reference-multi-agent",
            topology=args.topology,
            task_id=task.task_id,
            seed=args.seed,
            fault_name="none",
            fault_parameters={},
            repetition=0,
        )
        run_id = f"dist-{uuid.uuid4().hex[:16]}"
        run_root = results_dir / f"{idx:03d}-{task.task_id.replace('/', '-')}-{run_id}"
        context = RunContext(
            study_id=study_id,
            campaign_id=campaign_id,
            pair_id=pair_id,
            run_id=run_id,
            condition="direct_baseline",
            output_root=str(run_root),
            environment={},
        )
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
        )
        _append_jsonl(results_dir / "runs.jsonl", record)

    generate_all_outputs(str(results_dir), study_id=study_id)
    print(f"[cj-eval] distributed smoke complete: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
