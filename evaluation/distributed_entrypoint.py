"""Container entry point for one distributed multi-agent role.

Each invocation performs exactly one role action or one in-container scoring
action.  The host-side distributed runner coordinates message passing between
separate role containers; this module intentionally has no global coordinator
state.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from evaluation.agents.base import ModelClient, extract_python_code
from evaluation.benchmarks.executor import score_task
from evaluation.container_entrypoint import (
    _apply_llm_env_aliases,
    _load_dotenv,
    _package_versions,
    _prepare_writable_runtime_dirs,
    _task_from_request,
    _write_artifacts,
)
from evaluation.model_config import load_model_config
from evaluation.multi_agent import ROLE_PROMPTS, TraceEvent, role_metadata, validate_event_schema


def _content_from_response(response: dict[str, Any]) -> str:
    try:
        return str(response["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _apply_model_env(req: dict[str, Any]) -> None:
    """Apply non-secret model settings from the request to process env."""
    _load_dotenv()
    model_cfg_raw = dict(req.get("model_config") or {})
    if model_cfg_raw.get("base_url") and not os.environ.get("CJ_EVAL_BASE_URL"):
        os.environ["CJ_EVAL_BASE_URL"] = str(model_cfg_raw["base_url"]).rstrip("/")
    _apply_llm_env_aliases()
    model_config = load_model_config(model_cfg_raw)
    os.environ["CJ_EVAL_MODEL"] = model_config.name
    os.environ["CJ_EVAL_TEMPERATURE"] = str(model_config.temperature)


def _base_event(
    *,
    run_context: dict[str, Any],
    role: str,
    step_index: int,
    trace_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = TraceEvent(
        study_id=str(run_context.get("study_id", "")),
        run_id=str(run_context.get("run_id", "")),
        pair_id=str(run_context.get("pair_id", "")),
        agent_role=role,
        step_index=step_index,
        timestamp=time.time(),
        trace_id=trace_id,
        event_type=event_type,
        payload={
            **(payload or {}),
            "container_hostname": os.environ.get("HOSTNAME", ""),
        },
    ).to_dict()
    validate_event_schema(event)
    return event


def _dry_role_content(role: str, messages: list[dict[str, str]]) -> str:
    if role == "planner":
        return "Plan: identify the required function and implement the simplest correct algorithm."
    if role == "coder":
        return (
            "```python\n"
            "def solution(*args, **kwargs):\n"
            "    return None\n"
            "```"
        )
    if role == "reviewer":
        return "ACCEPT"
    return str(messages[-1].get("content", "")) if messages else ""


def run_role_request(req: dict[str, Any]) -> dict[str, Any]:
    """Execute one LLM role call inside the current container."""
    _apply_model_env(req)
    execution_config = dict(req.get("execution_config") or {})
    run_context = dict(req.get("run_context") or {})
    role = str(req.get("role", ""))
    if role not in ROLE_PROMPTS:
        raise ValueError(f"unsupported distributed role: {role!r}")
    step_index = int(req.get("step_index", 0))
    trace_id = str(req.get("trace_id") or uuid.uuid4().hex)
    seed = int(req.get("seed", 0))
    dry_run = bool(execution_config.get("dry_run", False))
    messages = list(req.get("messages") or [])
    if not messages:
        raise ValueError("distributed role request requires non-empty messages")

    events = [
        _base_event(
            run_context=run_context,
            role=role,
            step_index=step_index,
            trace_id=trace_id,
            event_type="agent_activation",
            payload={"message_count": len(messages)},
        ),
        _base_event(
            run_context=run_context,
            role=role,
            step_index=step_index,
            trace_id=trace_id,
            event_type="llm_invocation",
            payload={"message_count": len(messages)},
        ),
    ]

    t0 = time.time()
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if dry_run:
        content = _dry_role_content(role, messages)
    else:
        client = ModelClient(dry_run=False)
        with role_metadata(str(run_context.get("run_id", "")), role, step_index):
            response = client.chat(messages, seed=seed)
        content = _content_from_response(response)
        usage = client.usage(response)

    event_type = "review_decision" if role == "reviewer" else "sent_message"
    if role == "coder":
        event_type = "final_answer"
    events.append(
        _base_event(
            run_context=run_context,
            role=role,
            step_index=step_index,
            trace_id=trace_id,
            event_type=event_type,
            payload={"content_preview": content[:500]},
        )
    )

    return {
        "executor_status": "ok",
        "mode": "role",
        "role": role,
        "step_index": step_index,
        "trace_id": trace_id,
        "content": content,
        "generated_code": extract_python_code(content) if role == "coder" else "",
        "duration_s": time.time() - t0,
        "llm_calls": 1,
        "tool_calls": 0,
        "turns": 1,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "execution_trace": events,
        "framework_versions": _package_versions(),
        "python_version": platform.python_version(),
    }


def run_score_request(req: dict[str, Any]) -> dict[str, Any]:
    """Score final generated code inside the current container."""
    task = _task_from_request(dict(req.get("task") or {}))
    generated_code = extract_python_code(str(req.get("generated_code") or ""))
    timeout_s = float((req.get("execution_config") or {}).get("score_timeout_s", 10.0))
    t0 = time.time()
    if not generated_code:
        return {
            "executor_status": "ok",
            "mode": "score",
            "scorer_status": "error",
            "scorer_error": "no generated_code provided for distributed scoring",
            "tests_passed": 0,
            "tests_total": 0,
            "success": 0.0,
            "duration_s": time.time() - t0,
            "generated_code": "",
            "framework_versions": _package_versions(),
            "python_version": platform.python_version(),
        }
    ok, passed, total, output = score_task(task, generated_code, timeout_s=timeout_s)
    return {
        "executor_status": "ok",
        "mode": "score",
        "scorer_status": "ok" if total > 0 else "error",
        "scorer_error": "" if total > 0 else output,
        "success": 1.0 if ok else 0.0,
        "tests_passed": passed,
        "tests_total": total,
        "duration_s": time.time() - t0,
        "generated_code": generated_code,
        "scoring_evidence": {
            "output_preview": output[:2000],
            "strict_evalplus": task.metadata.get("source") == "evalplus",
        },
        "framework_versions": _package_versions(),
        "python_version": platform.python_version(),
    }


def run_request(req: dict[str, Any]) -> dict[str, Any]:
    mode = str(req.get("mode", "role"))
    if mode == "role":
        return run_role_request(req)
    if mode == "score":
        return run_score_request(req)
    raise ValueError(f"unsupported distributed entrypoint mode: {mode!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one distributed CJ role or score action")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CJ_EVAL_OUTPUT_DIR", str(output_path.parent))

    try:
        _prepare_writable_runtime_dirs()
        req = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = run_request(req)
        result["artifact_paths"] = _write_artifacts(output_path, result)
        output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001
        result = {
            "executor_status": "error",
            "executor_error": repr(exc),
            "mode": "distributed",
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
