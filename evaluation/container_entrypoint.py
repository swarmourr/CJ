"""Container entry point for one CJ evaluation execution."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from evaluation.agents import REGISTRY
from evaluation.agents.base import AgentRunResult, ModelClient
from evaluation.benchmarks.base import BenchmarkTask
from evaluation.benchmarks.executor import score_task
from evaluation.model_config import load_model_config
from evaluation.multi_agent import MultiAgentWorkflow


_VERSION_PACKAGES = {
    "autogen": ("autogen-agentchat", "autogen-ext"),
    "langgraph": ("langgraph", "langchain-openai"),
    "crewai": ("crewai", "litellm"),
    "evalplus": ("evalplus",),
}


def _load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _apply_llm_env_aliases() -> None:
    for source, target in (
        ("LLM_API_KEY", "CJ_EVAL_API_KEY"),
        ("LLM_BASE_URL", "CJ_EVAL_BASE_URL"),
        ("LLM_MODEL", "CJ_EVAL_MODEL"),
    ):
        if source in os.environ and target not in os.environ:
            os.environ[target] = os.environ[source]


def _package_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for names in _VERSION_PACKAGES.values():
        for name in names:
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = "not-installed"
    return versions


def _validate_request(req: dict[str, Any]) -> None:
    required = {"agent_system", "topology", "task", "seed", "model_config", "run_context"}
    missing = sorted(required - set(req))
    if missing:
        raise ValueError(f"experiment request missing required fields: {missing}")
    if not isinstance(req["task"], dict):
        raise ValueError("experiment request field 'task' must be an object")
    if "prompt" not in req["task"] and "input" not in req["task"]:
        raise ValueError("task must contain 'prompt' or 'input'")


def _task_prompt(task: dict[str, Any]) -> str:
    return str(task.get("prompt") or task.get("input") or "")


def _score_result(task_data: dict[str, Any], result: AgentRunResult, timeout_s: float) -> dict[str, Any]:
    """Score generated code when benchmark evidence is present in the request."""
    if not result.generated_code:
        return {
            "scorer_status": "error",
            "scorer_error": "agent produced no generated_code",
            "tests_passed": 0,
            "tests_total": 0,
            "success": 0.0,
        }
    if not task_data.get("test_code") and not (
        isinstance(task_data.get("metadata"), dict)
        and task_data["metadata"].get("source") == "evalplus"
    ):
        return {
            "scorer_status": "not_available",
            "scorer_error": "request did not include test_code or EvalPlus metadata",
        }
    task = BenchmarkTask(
        task_id=str(task_data.get("task_id", "")),
        benchmark=str(task_data.get("benchmark", "")),
        prompt=str(task_data.get("prompt") or task_data.get("input") or ""),
        entry_point=str(task_data.get("entry_point", "")),
        test_code=str(task_data.get("test_code", "")),
        canonical_solution=str(task_data.get("canonical_solution", "")),
        metadata=dict(task_data.get("metadata") or {}),
    )
    ok, passed, total, output = score_task(task, result.generated_code, timeout_s=timeout_s)
    return {
        "scorer_status": "ok" if total > 0 else "error",
        "scorer_error": "" if total > 0 else output,
        "success": 1.0 if ok else 0.0,
        "tests_passed": passed,
        "tests_total": total,
        "scoring_evidence": {
            "output_preview": output[:2000],
            "strict_evalplus": task.metadata.get("source") == "evalplus",
        },
    }


def _write_artifacts(output_path: Path, result: dict[str, Any]) -> dict[str, str]:
    output_dir = output_path.parent
    artifacts: dict[str, str] = {}
    code = result.get("generated_code") or ""
    if code:
        code_path = output_dir / "generated_solution.py"
        code_path.write_text(str(code), encoding="utf-8")
        artifacts["generated_solution"] = str(code_path)
    trace = result.get("execution_trace") or []
    trace_path = output_dir / "execution_trace.json"
    trace_path.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
    artifacts["execution_trace"] = str(trace_path)
    return artifacts


def _run_request(req: dict[str, Any]) -> dict[str, Any]:
    _validate_request(req)
    _load_dotenv()

    run_context = req.get("run_context") or {}
    execution_config = req.get("execution_config") or {}
    model_cfg_raw = dict(req.get("model_config") or {})
    if model_cfg_raw.get("base_url") and not os.environ.get("CJ_EVAL_BASE_URL"):
        os.environ["CJ_EVAL_BASE_URL"] = str(model_cfg_raw["base_url"]).rstrip("/")
    _apply_llm_env_aliases()
    model_config = load_model_config(model_cfg_raw)

    os.environ["CJ_EVAL_MODEL"] = model_config.name
    os.environ["CJ_EVAL_TEMPERATURE"] = str(model_config.temperature)
    os.environ.setdefault("CJ_RUN_ID", str(run_context.get("run_id", "")))

    task = req["task"]
    prompt = _task_prompt(task)
    seed = int(req.get("seed", 0))
    agent_level = str(execution_config.get("agent_level", "individual"))
    max_turns = int(execution_config.get("max_turns", 10))
    dry_run = bool(execution_config.get("dry_run", False))
    score_timeout_s = float(execution_config.get("score_timeout_s", 10.0))

    t0 = time.time()
    if agent_level == "multi_agent":
        workflow = MultiAgentWorkflow(
            ModelClient(dry_run=dry_run),
            topology=str(req.get("topology") or "linear"),
            dry_run=dry_run,
            study_id=str(run_context.get("study_id", "")),
            pair_id=str(run_context.get("pair_id", "")),
            run_id=str(run_context.get("run_id", "")),
        )
        result = workflow.run(prompt, seed=seed)
    else:
        name = str(req["agent_system"])
        if name not in REGISTRY:
            raise ValueError(f"unknown agent system: {name!r}")
        agent_cls = REGISTRY[name]
        if getattr(agent_cls, "uses_model_config", False):
            agent = agent_cls(model_config=model_config, max_turns=max_turns)
        else:
            agent = agent_cls(max_turns=max_turns, dry_run=dry_run)
        result = agent.run(prompt, seed=seed)

    payload = result.to_dict() if isinstance(result, AgentRunResult) else dict(result)
    if isinstance(result, AgentRunResult):
        payload.update(_score_result(task, result, timeout_s=score_timeout_s))
    payload.update({
        "executor_status": "ok",
        "executor_duration_s": time.time() - t0,
        "framework_versions": _package_versions(),
        "python_version": platform.python_version(),
        "agent_system": req["agent_system"],
        "topology": req.get("topology"),
        "agent_level": agent_level,
        "requested_framework": req["agent_system"],
        "framework_native": agent_level != "multi_agent",
        "multi_agent_impl": "reference-multi-agent" if agent_level == "multi_agent" else "",
    })
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run exactly one CJ agent experiment")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        req = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = _run_request(req)
        result["artifact_paths"] = _write_artifacts(output_path, result)
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        # Ordinary task failures are represented inside AgentRunResult and still
        # return zero; nonzero is reserved for executor/infrastructure failures.
        return 0
    except Exception as exc:  # noqa: BLE001
        result = {
            "executor_status": "error",
            "executor_error": repr(exc),
            "success": 0.0,
            "reported_error": 1.0,
            "duration_s": 0.0,
            "framework_versions": _package_versions(),
            "python_version": platform.python_version(),
        }
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(repr(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
