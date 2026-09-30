"""CLI entry point for the CJ evaluation package.

Usage examples:
    python -m evaluation.run --system autogen --benchmark humanevalplus \
        --fault llm_timeout --tasks 5 --repeats 3 --seed 42

    python -m evaluation.run --system mad --benchmark mbppplus \
        --fault response_truncation --tasks 5 --repeats 3 --seed 42

    python -m evaluation.run --system mapcoder --benchmark humanevalplus \
        --fault tool_failure --tasks 5 --repeats 3 --seed 42

    python -m evaluation.run --dry-run --system autogen \
        --benchmark humanevalplus --fault llm_timeout --tasks 3

    python -m evaluation.run --config evaluation/configs/smoke.yaml

    python -m evaluation.run --generate-outputs --results-dir results/
"""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evaluation.run",
        description="CJ agent system evaluation CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Mode ───────────────────────────────────────────────────────────────────
    p.add_argument("--dry-run", action="store_true",
                   help="Run without a real model API (stub responses, no CJ proxy)")
    p.add_argument("--generate-outputs", action="store_true",
                   help="Load existing runs.jsonl and regenerate all output files")
    p.add_argument("--config", metavar="YAML",
                   help="Load all settings from a YAML config file")

    # ── Experiment parameters ──────────────────────────────────────────────────
    p.add_argument("--system", choices=["autogen", "mad", "mapcoder"],
                   help="Agent system to evaluate")
    p.add_argument("--benchmark", choices=["humanevalplus", "mbppplus"],
                   help="Benchmark to use")
    p.add_argument("--fault",
                   help="Fault name from the catalog (or 'none' for baseline-only)")
    p.add_argument("--tasks", type=int, default=5,
                   help="Number of tasks (smoke subset)")
    p.add_argument("--repeats", type=int, default=1,
                   help="Repetitions per task")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed")

    # ── Output ─────────────────────────────────────────────────────────────────
    p.add_argument("--results-dir", default="results",
                   help="Directory for output files")
    p.add_argument("--exec-timeout", type=float, default=10.0,
                   help="Sandbox execution timeout (seconds)")

    # ── Model config (override env vars) ──────────────────────────────────────
    p.add_argument("--base-url", help="Override CJ_EVAL_BASE_URL")
    p.add_argument("--api-key",  help="Override CJ_EVAL_API_KEY")
    p.add_argument("--model",    help="Override CJ_EVAL_MODEL")

    return p


def _apply_env_overrides(args: argparse.Namespace) -> None:
    if args.base_url:
        os.environ["CJ_EVAL_BASE_URL"] = args.base_url
    if args.api_key:
        os.environ["CJ_EVAL_API_KEY"] = args.api_key
    if args.model:
        os.environ["CJ_EVAL_MODEL"] = args.model

    if args.dry_run:
        # Ensure env is clean for dry-run (no stale base_url)
        os.environ.pop("CJ_EVAL_BASE_URL", None)


def _load_yaml_config(path: str) -> dict:
    import yaml  # type: ignore
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_single_experiment(
    system_name: str,
    benchmark_name: str,
    fault_name: str,
    tasks: int,
    repeats: int,
    seed: int,
    results_dir: str,
    exec_timeout: float,
    dry_run: bool,
) -> None:
    from evaluation.agents import REGISTRY as AGENT_REGISTRY
    from evaluation.agents.base import ModelClient
    from evaluation.benchmarks import REGISTRY as BENCH_REGISTRY
    from evaluation.experiments.protocol import ExperimentProtocol
    from evaluation.output import generate_all_outputs

    print(f"\n[eval] === Experiment ===")
    print(f"  system    : {system_name}")
    print(f"  benchmark : {benchmark_name}")
    print(f"  fault     : {fault_name}")
    print(f"  tasks     : {tasks}  repeats: {repeats}  seed: {seed}")
    print(f"  dry_run   : {dry_run}")
    print()

    # Build agent
    client = ModelClient(dry_run=dry_run)
    agent_cls = AGENT_REGISTRY[system_name]
    agent = agent_cls(client=client, dry_run=dry_run)

    # Load tasks — pick subset based on requested count.
    # The ImportError from evalplus not being installed surfaces inside loader.load()
    # (when _load_all() runs), not during the loader constructor.
    _SMOKE_MAX = 5  # bundled tasks available without evalplus
    if tasks <= _SMOKE_MAX:
        loader = BENCH_REGISTRY[benchmark_name](subset="smoke")
        loader.smoke_n = tasks
        task_list = loader.load(seed=seed)
    else:
        loader = BENCH_REGISTRY[benchmark_name](subset="development")
        loader.development_n = tasks
        try:
            task_list = loader.load(seed=seed)
        except ImportError:
            print(
                f"[eval] Warning: evalplus not installed; cannot load {tasks} tasks. "
                f"Falling back to smoke subset (up to {_SMOKE_MAX} bundled tasks). "
                "Install with: pip install evalplus==0.3.1"
            )
            loader = BENCH_REGISTRY[benchmark_name](subset="smoke")
            loader.smoke_n = _SMOKE_MAX
            task_list = loader.load(seed=seed)
    print(f"[eval] Loaded {len(task_list)} tasks from {benchmark_name}")

    # Run protocol
    proto = ExperimentProtocol(
        agent=agent,
        fault_name=fault_name or "none",
        output_dir=results_dir,
        dry_run=dry_run,
        exec_timeout_s=exec_timeout,
    )
    records = proto.run_campaign(task_list, seed=seed, repeats=repeats)
    print(f"\n[eval] Completed {len(records)} run records → {results_dir}/runs.jsonl")

    # Generate outputs
    generate_all_outputs(results_dir)

    # Print summary
    from evaluation.analysis.metrics import compute_metrics
    from evaluation.output import load_jsonl
    all_recs = load_jsonl(os.path.join(results_dir, "runs.jsonl"))
    m = compute_metrics(all_recs)
    print("\n[eval] Summary:")
    for line in m.summary_lines():
        print(line)


def run_from_yaml(config_path: str, dry_run: bool) -> None:
    cfg = _load_yaml_config(config_path)
    experiments = cfg.get("experiments", [cfg])
    results_dir = cfg.get("results_dir", "results")
    for exp in experiments:
        run_single_experiment(
            system_name=exp.get("system", "autogen"),
            benchmark_name=exp.get("benchmark", "humanevalplus"),
            fault_name=exp.get("fault", "none"),
            tasks=exp.get("tasks", 5),
            repeats=exp.get("repeats", 1),
            seed=exp.get("seed", 42),
            results_dir=exp.get("results_dir", results_dir),
            exec_timeout=exp.get("exec_timeout", 10.0),
            dry_run=dry_run or exp.get("dry_run", False),
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)
    _apply_env_overrides(args)

    if args.generate_outputs:
        from evaluation.output import generate_all_outputs
        generate_all_outputs(args.results_dir)
        return 0

    if args.config:
        run_from_yaml(args.config, dry_run=args.dry_run)
        return 0

    if not args.system:
        parser.error("--system is required (or use --config / --generate-outputs)")
    if not args.benchmark:
        parser.error("--benchmark is required")

    fault = args.fault or "none"
    run_single_experiment(
        system_name=args.system,
        benchmark_name=args.benchmark,
        fault_name=fault,
        tasks=args.tasks,
        repeats=args.repeats,
        seed=args.seed,
        results_dir=args.results_dir,
        exec_timeout=args.exec_timeout,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
