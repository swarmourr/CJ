"""Rebuild E7/E8 derived outputs without mutating immutable experiment records."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from evaluation.distributed_campaign import write_distributed_coordination_outputs
from evaluation.extended_real_campaign import (
    RealCrossLayerCampaign,
    _no_solution_failure,
    _scored,
    _write_csv,
    _write_json,
    inferential_cross_layer,
    latest_factorial_records,
    summarize_cross_layer,
)
from evaluation.output import load_jsonl


DERIVED_E7 = (
    "distributed_coordination_summary.json",
    "distributed_coordination_runs.csv",
)
DERIVED_E8 = (
    "cross_layer_summary.json",
    "inferential_summary.csv",
    "task_results.csv",
    "e8_recovery_audit.json",
)


def _backup(path: Path, *, root: Path, backup_root: Path) -> None:
    if not path.exists():
        return
    relative = path.relative_to(root)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def _e8_audit(records: list[dict[str, Any]]) -> dict[str, Any]:
    effective = latest_factorial_records(records)
    no_solution = [record for record in effective if _no_solution_failure(record)]
    scorer_failures_with_code = [
        record for record in effective
        if not _scored(record)
        and str(record.get("generated_code") or "").strip()
    ]
    unavailable = [
        record for record in effective
        if not _scored(record)
        and not _no_solution_failure(record)
        and not str(record.get("generated_code") or "").strip()
    ]
    return {
        "raw_record_count": len(records),
        "effective_record_count": len(effective),
        "strictly_scored_record_count": sum(_scored(record) for record in effective),
        "no_solution_task_failure_count": len(no_solution),
        "offline_rescore_candidate_count": len(scorer_failures_with_code),
        "task_outcome_unavailable_count": len(unavailable),
        "offline_rescore_candidates": [
            {
                "pair_id": record.get("pair_id"),
                "run_id": record.get("run_id"),
                "task_id": record.get("task_id"),
                "condition": record.get("condition"),
                "scorer_error": record.get("scorer_error"),
            }
            for record in scorer_failures_with_code
        ],
        "interpretation": (
            "no candidate program is an observed task failure; it is not an evaluator failure "
            "and must not be selectively rerun based on outcome"
        ),
    }


def rebuild(root: Path) -> dict[str, Any]:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_root = root / "derived-output-backups" / stamp
    e7_studies: list[dict[str, Any]] = []
    e8_studies: list[dict[str, Any]] = []

    e7_root = root / "distributed-native-multi-agent-smoke"
    for runs_path in sorted(e7_root.glob("*/runs.jsonl")) if e7_root.exists() else []:
        study_dir = runs_path.parent
        records = load_jsonl(str(runs_path))
        for name in DERIVED_E7:
            _backup(study_dir / name, root=root, backup_root=backup_root)
        write_distributed_coordination_outputs(records, study_dir)
        summary = json.loads((study_dir / DERIVED_E7[0]).read_text(encoding="utf-8"))
        e7_studies.append({"path": str(study_dir), **summary})

    e8_root = root / "e8-real-cross-layer"
    for runs_path in sorted(e8_root.glob("*/runs.jsonl")) if e8_root.exists() else []:
        study_dir = runs_path.parent
        records = load_jsonl(str(runs_path))
        effective = latest_factorial_records(records)
        for name in DERIVED_E8:
            _backup(study_dir / name, root=root, backup_root=backup_root)
        summary = summarize_cross_layer(records)
        inference = inferential_cross_layer(records)
        audit = _e8_audit(records)
        _write_json(study_dir / "cross_layer_summary.json", summary)
        _write_csv(study_dir / "inferential_summary.csv", inference)
        _write_csv(
            study_dir / "task_results.csv",
            [RealCrossLayerCampaign._flat_row(record) for record in effective],
        )
        _write_json(study_dir / "e8_recovery_audit.json", audit)
        e8_studies.append({"path": str(study_dir), **summary, "recovery_audit": audit})

    report = {
        "schema": "cj-extended-derived-rebuild-v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results_root": str(root),
        "raw_records_modified": False,
        "backup_root": str(backup_root) if backup_root.exists() else None,
        "e7_study_count": len(e7_studies),
        "e8_study_count": len(e8_studies),
        "e7": e7_studies,
        "e8": e8_studies,
    }
    _write_json(root / "extended_derived_rebuild_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.results_root).resolve()
    if not root.exists():
        raise SystemExit(f"results root does not exist: {root}")
    report = rebuild(root)
    print(f"[cj-eval] rebuilt E7 summaries: {report['e7_study_count']}")
    print(f"[cj-eval] rebuilt E8 summaries: {report['e8_study_count']}")
    print(f"[cj-eval] raw records modified: {report['raw_records_modified']}")
    print(f"[cj-eval] report: {root / 'extended_derived_rebuild_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
