"""Tests for the real-agent E8 cross-layer study gates."""

from __future__ import annotations

import json

from evaluation.extended_real_campaign import (
    CONDITIONS,
    inferential_cross_layer,
    latest_factorial_records,
    summarize_cross_layer,
)
from evaluation.rebuild_extended_outputs import rebuild


def _block(pair_id: str, task_id: str, *, success: float = 1.0) -> list[dict]:
    records = []
    for index, condition in enumerate(CONDITIONS):
        records.append({
            "pair_id": pair_id,
            "task_id": task_id,
            "condition": condition,
            "success": success,
            "duration_s": float(index + 1),
            "scorer_status": "ok",
            "tests_total": 4,
            "validity": "valid",
        })
    return records


def test_real_cross_layer_summary_requires_complete_scored_valid_blocks():
    valid = _block("pair-valid", "task-1")
    unscored = _block("pair-unscored", "task-2")
    unscored[0]["scorer_status"] = "error"
    invalid = _block("pair-invalid", "task-3")
    next(r for r in invalid if r["condition"] == "compound")["validity"] = "invalid"
    incomplete = _block("pair-incomplete", "task-4")[:-1]

    summary = summarize_cross_layer(valid + unscored + invalid + incomplete)

    assert summary["requested_factorial_blocks"] == 4
    assert summary["complete_factorial_blocks"] == 3
    assert summary["complete_scored_factorial_blocks"] == 2
    assert summary["valid_scored_factorial_blocks"] == 1
    assert summary["complete_task_outcome_blocks"] == 2
    assert summary["valid_task_outcome_blocks"] == 1
    assert summary["excluded_unscored_blocks"] == 1
    assert summary["excluded_invalid_scored_blocks"] == 1
    assert summary["missing_factorial_blocks"] == 1


def test_no_solution_is_task_failure_not_evaluator_failure():
    records = _block("pair", "task")
    no_solution = records[0]
    no_solution.update({
        "success": 0.0,
        "scorer_status": "error",
        "tests_total": 0,
        "scorer_error": "agent produced no generated_code",
        "generated_code": "",
        "executor_status": "ok",
        "executor_timed_out": False,
    })

    summary = summarize_cross_layer(records)

    assert summary["complete_scored_factorial_blocks"] == 0
    assert summary["complete_task_outcome_blocks"] == 1
    assert summary["valid_task_outcome_blocks"] == 1
    assert summary["no_solution_failure_records"] == 1
    assert summary["success_rate_by_condition"]["direct_baseline"] == 0.0


def test_latest_factorial_attempt_wins_without_deleting_raw_attempt():
    records = _block("pair", "task")
    replacement = dict(records[0], run_id="replacement", success=0.0)
    effective = latest_factorial_records(records + [replacement])

    assert len(effective) == len(CONDITIONS)
    direct = next(record for record in effective if record["condition"] == "direct_baseline")
    assert direct["run_id"] == "replacement"
    assert direct["success"] == 0.0


def test_real_cross_layer_effects_use_cj_control_not_direct_baseline():
    records = _block("pair", "task")
    by_condition = {record["condition"]: record for record in records}
    by_condition["direct_baseline"]["success"] = 0.0
    by_condition["cj_control"]["success"] = 1.0
    by_condition["llm_only"]["success"] = 0.0
    by_condition["infrastructure_only"]["success"] = 1.0
    by_condition["compound"]["success"] = 0.0

    summary = summarize_cross_layer(records)

    assert summary["fault_degradation_vs_control"]["llm_only"] == 1.0
    assert summary["fault_degradation_vs_control"]["infrastructure_only"] == 0.0
    assert summary["direct_to_control_correctness_difference"] == 1.0


def test_real_cross_layer_inference_is_suppressed_below_three_unique_tasks():
    records = _block("pair-1", "task-1") + _block("pair-2", "task-2")

    rows = inferential_cross_layer(records)

    assert len(rows) == 6
    assert {row["inference_status"] for row in rows} == {"insufficient_unique_tasks"}
    assert all(row["test"] == "not_run" for row in rows)
    assert all(row["p_value"] is None for row in rows)
    assert all(row["p_value_holm"] is None for row in rows)


def test_rebuild_extended_outputs_preserves_raw_jsonl(tmp_path):
    study = tmp_path / "e8-real-cross-layer" / "autogen-real-humanevalplus"
    study.mkdir(parents=True)
    runs = study / "runs.jsonl"
    records = _block("pair", "HumanEval/0")
    runs.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    original = runs.read_bytes()

    report = rebuild(tmp_path)

    assert runs.read_bytes() == original
    assert report["raw_records_modified"] is False
    assert report["e8_study_count"] == 1
    assert (study / "cross_layer_summary.json").exists()
    assert (study / "e8_recovery_audit.json").exists()
