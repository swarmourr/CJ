"""Offline rescoring utility for publication-study records.

This recovers campaigns where model executions completed and generated code was
saved, but the in-container scorer failed for environmental reasons such as an
exhausted temporary filesystem. It never calls a model endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from evaluation.benchmarks.base import BenchmarkTask
from evaluation.benchmarks.executor import score_task
from evaluation.output import generate_all_outputs, load_jsonl
from evaluation.study_protocol import _run_failure_verdicts


def _git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parents[1],
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_from_request(record: dict[str, Any]) -> tuple[BenchmarkTask | None, str]:
    artifacts = record.get("artifact_paths") if isinstance(record.get("artifact_paths"), dict) else {}
    input_dir = artifacts.get("input_dir")
    if not input_dir:
        return None, "record has no artifact_paths.input_dir"
    request_path = Path(str(input_dir)) / "request.json"
    if not request_path.exists():
        return None, f"request artifact not found: {request_path}"
    try:
        task_data = _read_json(request_path).get("task") or {}
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read request artifact: {exc!r}"
    if not isinstance(task_data, dict):
        return None, "request artifact task is not an object"
    return BenchmarkTask(
        task_id=str(task_data.get("task_id", record.get("task_id", ""))),
        benchmark=str(task_data.get("benchmark", record.get("benchmark", ""))),
        prompt=str(task_data.get("prompt") or task_data.get("input") or ""),
        entry_point=str(task_data.get("entry_point", "")),
        test_code=str(task_data.get("test_code", "")),
        canonical_solution=str(task_data.get("canonical_solution", "")),
        metadata=dict(task_data.get("metadata") or {}),
    ), ""


def _generated_code(record: dict[str, Any]) -> str:
    code = record.get("generated_code")
    if code:
        return str(code)
    artifacts = record.get("artifact_paths") if isinstance(record.get("artifact_paths"), dict) else {}
    result_json = artifacts.get("result_json")
    if result_json and Path(str(result_json)).exists():
        try:
            return str(_read_json(Path(str(result_json))).get("generated_code") or "")
        except Exception:
            return ""
    return ""


def _rescore_record(
    record: dict[str, Any],
    *,
    timeout_s: float,
    commit: str,
) -> dict[str, Any]:
    record = dict(record)
    code = _generated_code(record)
    task, task_error = _task_from_request(record)
    record["rescored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record["rescoring_commit"] = commit
    record["rescoring_location"] = "offline_host_evalplus"

    if task is None:
        record["offline_rescore_status"] = "missing_task_artifact"
        record["offline_rescore_error"] = task_error
        return record

    if not code.strip():
        record.update({
            "success": 0.0,
            "tests_passed": 0,
            "tests_total": 0,
            "scorer_status": "error",
            "scorer_error": "agent produced no generated_code",
            "offline_rescore_status": "no_code",
            "offline_rescore_error": "",
        })
        record.update(_run_failure_verdicts(record))
        return record

    ok, passed, total, output = score_task(task, code, timeout_s=timeout_s)
    record.update({
        "success": 1.0 if ok else 0.0,
        "tests_passed": passed,
        "tests_total": total,
        "scorer_status": "ok" if total > 0 else "error",
        "scorer_error": "" if total > 0 else output,
        "offline_rescore_status": "ok" if total > 0 else "scorer_error",
        "offline_rescore_error": "" if total > 0 else output,
        "scoring_evidence": {
            **(record.get("scoring_evidence") if isinstance(record.get("scoring_evidence"), dict) else {}),
            "rescored": True,
            "original_score_location": (
                record.get("scoring_evidence", {}).get("score_location", "")
                if isinstance(record.get("scoring_evidence"), dict)
                else ""
            ),
            "score_location": "offline_host_evalplus",
            "strict_evalplus": task.metadata.get("source") == "evalplus",
            "output_preview": output[:2000],
        },
    })
    record.update(_run_failure_verdicts(record))
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rescore saved CJ evaluation outputs offline")
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--study-id")
    parser.add_argument("--score-timeout", type=float, default=25.0)
    parser.add_argument("--no-generate-outputs", action="store_true")
    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir)
    runs_path = results_dir / "runs.jsonl"
    records = load_jsonl(str(runs_path))
    if not records:
        raise SystemExit(f"no records found: {runs_path}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = runs_path.with_suffix(f".jsonl.before-rescore-{stamp}")
    shutil.copy2(runs_path, backup)

    tmp_root = results_dir / "offline-rescore-tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    old_tmp = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(tmp_root)

    commit = _git_commit()
    selected = 0
    rescored = 0
    try:
        out_records = []
        for record in records:
            if args.study_id and record.get("study_id") != args.study_id:
                out_records.append(record)
                continue
            selected += 1
            new_record = _rescore_record(record, timeout_s=args.score_timeout, commit=commit)
            if new_record.get("offline_rescore_status") in {"ok", "no_code", "scorer_error"}:
                rescored += 1
            out_records.append(new_record)
    finally:
        if old_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmp

    tmp_path = runs_path.with_suffix(".jsonl.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        for record in out_records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    tmp_path.replace(runs_path)

    print(f"[rescore] backup   : {backup}")
    print(f"[rescore] selected : {selected}")
    print(f"[rescore] rescored : {rescored}")
    if not args.no_generate_outputs:
        generate_all_outputs(str(results_dir), study_id=args.study_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
