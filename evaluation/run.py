"""CLI entry point for the CJ evaluation package.

Usage examples::

    # Dry-run (no model API required)
    python -m evaluation.run --config evaluation/configs/smoke.yaml --dry-run

    # Real model via YAML config
    cp .env.example .env  # then edit .env with your API key
    python -m evaluation.run --config evaluation/configs/real_smoke.yaml

    # Single experiment (style agent)
    python -m evaluation.run --system autogen --benchmark humanevalplus \\
        --fault llm_timeout --tasks 5 --repeats 3 --seed 42

    # Single experiment (real agent)
    python -m evaluation.run --system autogen-real --benchmark humanevalplus \\
        --fault llm_latency --tasks 3 --repeats 1 --seed 42

    # Regenerate output files from existing runs.jsonl
    python -m evaluation.run --generate-outputs --results-dir results/

Credential precedence (highest → lowest)::

    CLI arguments (--api-key, --base-url, --model)
    > exported environment variables (CJ_EVAL_API_KEY, CJ_EVAL_BASE_URL, …)
    > .env values (loaded with override=False so exported vars always win)
    > YAML model: section values
    > built-in defaults

The API key must never appear in YAML; ``model.api_key_env`` names the env var.
"""

from __future__ import annotations

import argparse
import os
import sys


# ---------------------------------------------------------------------------
# .env loading — must happen before any env-var reads
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load ``.env`` from the repository root.

    Uses ``override=False`` so variables that are already exported in the
    shell environment (highest precedence) are never overwritten.  Silently
    skips if ``python-dotenv`` is not installed (only needed for real runs).

    The `.env` file is located relative to this file's package root so the
    command works from any working directory.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # python-dotenv not installed; acceptable for dry-run

    # Walk up from evaluation/ to the repo root (contains .env.example)
    here     = os.path.dirname(os.path.abspath(__file__))
    pkg_root = os.path.dirname(here)
    env_file = os.path.join(pkg_root, ".env")
    if os.path.isfile(env_file):
        load_dotenv(env_file, override=False)


# Load .env immediately at module import so env vars are available for
# everything that follows (YAML parsing, credential checks, etc.).
_load_dotenv()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

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
    p.add_argument("--publication-study", action="store_true",
                   help="Run the Docker publication triplet protocol")
    p.add_argument("--config", metavar="YAML",
                   help="Load all settings from a YAML config file")

    # ── Experiment parameters ──────────────────────────────────────────────────
    p.add_argument("--system",
                   choices=["autogen", "mad", "mapcoder",
                            "autogen-real", "langgraph-real", "crewai-real"],
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
    p.add_argument("--study-id",
                   help="Exact study_id to run or filter when generating outputs")
    p.add_argument("--exec-timeout", type=float, default=10.0,
                   help="Sandbox execution timeout (seconds)")

    # ── Docker publication protocol ───────────────────────────────────────────
    p.add_argument("--docker-image",
                   help="CJ evaluation image for --publication-study")
    p.add_argument("--env-file",
                   help="Docker --env-file containing model secrets; defaults to .env when present")
    p.add_argument("--agent-level", choices=["individual", "multi_agent"], default="individual",
                   help="Publication study level")
    p.add_argument("--topology", default="single",
                   help="single, linear, or closed_loop")
    p.add_argument("--max-turns", type=int, default=10,
                   help="Maximum agent turns inside the container")
    p.add_argument("--score-timeout", type=float, default=10.0,
                   help="Scoring timeout inside the container")
    p.add_argument("--container-timeout", type=float, default=300.0,
                   help="Docker workload timeout in seconds")
    p.add_argument("--cpus", type=float, default=1.0,
                   help="Docker CPU limit")
    p.add_argument("--memory", default="2g",
                   help="Docker memory limit")
    p.add_argument("--proxy-port", type=int, default=18000,
                   help="Host CJ proxy port for publication study runs")

    # ── Model config (override env vars) ──────────────────────────────────────
    p.add_argument("--base-url", help="Override CJ_EVAL_BASE_URL")
    p.add_argument(
        "--container-base-url",
        help=(
            "Container-reachable direct model URL for publication Docker runs. "
            "Defaults to CJ_EVAL_CONTAINER_BASE_URL, then --base-url/CJ_EVAL_BASE_URL."
        ),
    )
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
        os.environ.pop("CJ_EVAL_BASE_URL", None)


def _load_yaml_config(path: str) -> dict:
    import yaml  # type: ignore
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _apply_model_config(cfg: dict, dry_run: bool) -> None:
    """Apply the top-level ``model:`` section from a YAML config to env vars.

    Priority (highest → lowest):
      1. CLI flags set by ``_apply_env_overrides`` (already in os.environ)
      2. ``model:`` section in the YAML config
      3. ``.env`` values (loaded at module import above)
      4. Existing env vars

    ``setdefault`` is used so CLI flags always win.

    Supported keys
    --------------
    base_url     : str  — endpoint base URL (CJ_EVAL_BASE_URL)
    api_key_env  : str  — name of an env var that holds the API key.
                          The key itself is never stored in the config file.
    api_key      : str  — API key literal (discouraged; use api_key_env).
    model / name : str  — model identifier (CJ_EVAL_MODEL)
    temperature  : float — sampling temperature (CJ_EVAL_TEMPERATURE)
    """
    if dry_run:
        return

    model_cfg = cfg.get("model", {})
    if not model_cfg:
        return

    if "base_url" in model_cfg:
        os.environ.setdefault("CJ_EVAL_BASE_URL", str(model_cfg["base_url"]))

    if "api_key_env" in model_cfg:
        key = os.environ.get(str(model_cfg["api_key_env"]), "")
        if key:
            os.environ.setdefault("CJ_EVAL_API_KEY", key)
        else:
            print(
                f"[eval] Warning: model.api_key_env={model_cfg['api_key_env']!r} "
                "is not set in the environment."
            )
    elif "api_key" in model_cfg:
        os.environ.setdefault("CJ_EVAL_API_KEY", str(model_cfg["api_key"]))

    model_name = model_cfg.get("model") or model_cfg.get("name")
    if model_name:
        os.environ.setdefault("CJ_EVAL_MODEL", str(model_name))

    if "temperature" in model_cfg:
        os.environ.setdefault("CJ_EVAL_TEMPERATURE", str(model_cfg["temperature"]))


# ---------------------------------------------------------------------------
# Agent construction — dispatches to style vs. real adapters
# ---------------------------------------------------------------------------

def _build_agent(system_name: str, yaml_model_cfg: dict, dry_run: bool):
    """Construct the appropriate agent for *system_name*.

    Style adapters receive a :class:`~evaluation.agents.base.ModelClient`.
    Real-framework adapters receive a :class:`~evaluation.model_config.ModelConfig`.
    """
    from evaluation.agents import REGISTRY

    agent_cls = REGISTRY[system_name]
    uses_mc   = getattr(agent_cls, "uses_model_config", False)

    if uses_mc:
        from evaluation.model_config import load_model_config, require_api_key
        mc = load_model_config(yaml_model_cfg)
        # CLI --model flag (CJ_EVAL_MODEL) overrides the YAML model name.
        env_model = os.environ.get("CJ_EVAL_MODEL")
        if env_model:
            mc.name = env_model
        if not dry_run:
            require_api_key(mc)
        return agent_cls(model_config=mc, max_turns=10)
    else:
        from evaluation.agents.base import ModelClient
        client = ModelClient(dry_run=dry_run)
        return agent_cls(client=client, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Single experiment
# ---------------------------------------------------------------------------

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
    yaml_model_cfg: dict | None = None,
) -> None:
    from evaluation.benchmarks import REGISTRY as BENCH_REGISTRY
    from evaluation.experiments.protocol import ExperimentProtocol
    from evaluation.output import generate_all_outputs

    yaml_model_cfg = yaml_model_cfg or {}

    print(f"\n[eval] === Experiment ===")
    print(f"  system    : {system_name}")
    print(f"  benchmark : {benchmark_name}")
    print(f"  fault     : {fault_name}")
    print(f"  tasks     : {tasks}  repeats: {repeats}  seed: {seed}")
    print(f"  dry_run   : {dry_run}")
    print()

    # Build agent
    agent = _build_agent(system_name, yaml_model_cfg, dry_run)

    # Load tasks — fail-closed: never silently substitute a smaller task set.
    # Running fewer tasks than requested would invalidate comparisons.
    _SMOKE_MAX = 5
    if tasks <= _SMOKE_MAX:
        loader = BENCH_REGISTRY[benchmark_name](subset="smoke")
        loader.smoke_n = tasks
        task_list = loader.load(seed=seed)
    else:
        loader = BENCH_REGISTRY[benchmark_name](subset="development")
        loader.development_n = tasks
        # Do NOT catch ImportError here: if evalplus is unavailable the run
        # must fail loudly.  Install with: pip install evalplus==0.3.1
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
    jsonl_path = proto._jsonl_path
    print(f"\n[eval] Completed {len(records)} run records → {jsonl_path} "
          f"(campaign {proto.campaign_id[:8]})")

    generate_all_outputs(results_dir, campaign_id=proto.campaign_id)

    from evaluation.analysis.metrics import compute_metrics
    from evaluation.output import load_jsonl
    # Load only records from this campaign to avoid mixing reruns
    all_recs = [r for r in load_jsonl(jsonl_path)
                if r.get("campaign_id") == proto.campaign_id or not r.get("campaign_id")]
    m = compute_metrics(all_recs)
    print("\n[eval] Summary:")
    for line in m.summary_lines():
        print(line)


def run_from_yaml(config_path: str, dry_run: bool) -> None:
    cfg = _load_yaml_config(config_path)
    _apply_model_config(cfg, dry_run)
    yaml_model_cfg = cfg.get("model", {})
    experiments    = cfg.get("experiments", [cfg])
    results_dir    = cfg.get("results_dir", "results")
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
            yaml_model_cfg=yaml_model_cfg,
        )


def _publication_model_config(args: argparse.Namespace, yaml_model_cfg: dict | None = None) -> dict:
    cfg = dict(yaml_model_cfg or {})
    if args.base_url:
        cfg["base_url"] = args.base_url
    elif os.environ.get("CJ_EVAL_BASE_URL"):
        cfg.setdefault("base_url", os.environ["CJ_EVAL_BASE_URL"])
    elif os.environ.get("OPENAI_BASE_URL"):
        cfg.setdefault("base_url", os.environ["OPENAI_BASE_URL"])
    if args.model:
        cfg["name"] = args.model
    elif os.environ.get("CJ_EVAL_MODEL"):
        cfg.setdefault("name", os.environ["CJ_EVAL_MODEL"])
    cfg.setdefault("api_key_env", "CJ_EVAL_API_KEY")
    cfg.setdefault("transport_retries", 0)
    return cfg


def _default_env_file() -> str | None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, ".env")
    return path if os.path.exists(path) else None


def _load_task_subset(benchmark_name: str, tasks: int, seed: int):
    from evaluation.benchmarks import REGISTRY as BENCH_REGISTRY

    if tasks <= 0:
        raise ValueError("--tasks must be positive")
    if tasks <= 5:
        loader = BENCH_REGISTRY[benchmark_name](subset="smoke")
        loader.smoke_n = tasks
    else:
        loader = BENCH_REGISTRY[benchmark_name](subset="development")
        loader.development_n = tasks
    return loader.load(seed=seed)


def run_publication_study(args: argparse.Namespace, yaml_model_cfg: dict | None = None) -> None:
    """Run direct baseline, CJ control, and CJ fault in Docker for each task."""
    if not args.docker_image:
        raise SystemExit("--docker-image is required with --publication-study")
    if not args.system:
        raise SystemExit("--system is required with --publication-study")
    if not args.benchmark:
        raise SystemExit("--benchmark is required with --publication-study")
    fault_name = args.fault or ""
    if fault_name in ("", "none"):
        raise SystemExit("--publication-study requires a concrete --fault for the triplet")

    from evaluation.docker_runner import DockerAgentRunner, DockerExecutionConfig
    from evaluation.experiments.fault_campaign import build_cj_fault
    from evaluation.output import generate_all_outputs
    from evaluation.study_protocol import PublicationStudyOrchestrator

    model_cfg = _publication_model_config(args, yaml_model_cfg)
    if model_cfg.get("base_url"):
        os.environ["CJ_EVAL_BASE_URL"] = str(model_cfg["base_url"])
    if model_cfg.get("name"):
        os.environ["CJ_EVAL_MODEL"] = str(model_cfg["name"])

    task_list = _load_task_subset(args.benchmark, args.tasks, args.seed)
    env_file = args.env_file if args.env_file is not None else _default_env_file()
    if env_file:
        env_file = os.path.abspath(env_file)

    docker_cfg = DockerExecutionConfig(
        image=args.docker_image,
        cpus=args.cpus,
        memory=args.memory,
        timeout_s=args.container_timeout,
        env_file=env_file,
        preserve_io=True,
    )
    orchestrator = PublicationStudyOrchestrator(
        DockerAgentRunner(docker_cfg),
        study_id=args.study_id,
        results_dir=args.results_dir,
        proxy_port=args.proxy_port,
    )
    execution_cfg = {
        "dry_run": args.dry_run,
        "max_turns": args.max_turns,
        "score_timeout_s": args.score_timeout,
        "agent_level": args.agent_level,
    }

    total = 0
    print("\n[eval] === Docker publication study ===")
    print(f"  study_id  : {orchestrator.study_id}")
    print(f"  image     : {args.docker_image}")
    print(f"  system    : {args.system}")
    print(f"  level     : {args.agent_level}")
    print(f"  topology  : {args.topology}")
    print(f"  benchmark : {args.benchmark} ({len(task_list)} tasks)")
    print(f"  fault     : {fault_name}")
    print(f"  results   : {args.results_dir}")

    for rep in range(args.repeats):
        for task in task_list:
            fault = build_cj_fault(fault_name)
            orchestrator.run_pair(
                agent_system=args.system,
                agent_level=args.agent_level,
                topology=args.topology,
                task=task,
                seed=args.seed + rep,
                model_config=model_cfg,
                execution_config=execution_cfg,
                fault_name=fault_name,
                fault=fault,
                repetition=rep,
                container_direct_base_url=(
                    args.container_base_url
                    or os.environ.get("CJ_EVAL_CONTAINER_BASE_URL")
                    or None
                ),
            )
            total += 3

    print(f"[eval] Wrote {total} paired condition records to {args.results_dir}/runs.jsonl")
    generate_all_outputs(args.results_dir, study_id=orchestrator.study_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)
    _apply_env_overrides(args)

    if args.generate_outputs:
        from evaluation.output import generate_all_outputs
        generate_all_outputs(args.results_dir, study_id=args.study_id)
        return 0

    if args.config:
        cfg = _load_yaml_config(args.config)
        _apply_model_config(cfg, args.dry_run)
        if args.publication_study:
            run_publication_study(args, cfg.get("model", {}))
        else:
            run_from_yaml(args.config, dry_run=args.dry_run)
        return 0

    if args.publication_study:
        run_publication_study(args)
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
        yaml_model_cfg={},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
