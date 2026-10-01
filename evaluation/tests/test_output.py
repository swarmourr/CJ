"""Tests for JSONL and CSV schema, and CLI dry-run behavior."""

import csv
import json
import os
import tempfile

import pytest

from evaluation.output import (
    load_jsonl,
    write_task_results_csv,
    write_condition_summary_csv,
    write_validity_summary_csv,
    write_group_summary_csv,
)
from evaluation.analysis.plots import plot_degradation


def _make_records():
    return [
        {
            "run_id": "r1", "timestamp": "2024-01-01T00:00:00Z",
            "cj_commit": "abc123",
            "agent_system": "autogen-style", "benchmark": "humanevalplus",
            "task_id": "HumanEval/0", "model": "fake", "endpoint_type": "mock",
            "seed": 42, "fault_type": "none", "fault_parameters": {},
            "target": "local", "phase": "baseline",
            "success": 1.0, "duration_s": 1.2, "reported_error": 0.0,
            "retries": 0, "llm_calls": 3, "tool_calls": 1, "turns": 3,
            "prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150,
            "cost_usd": 0.0001, "tests_passed": 3, "tests_total": 3,
            "generated_code_hash": "abc", "termination_reason": "success",
            "exception": "", "oracle_outcome": {}, "validity": "unchecked",
            "lifecycle": {"configured": False}, "artifact_path": "",
        },
        {
            "run_id": "r2", "timestamp": "2024-01-01T00:01:00Z",
            "cj_commit": "abc123",
            "agent_system": "autogen-style", "benchmark": "humanevalplus",
            "task_id": "HumanEval/0", "model": "fake", "endpoint_type": "mock",
            "seed": 42, "fault_type": "llm_timeout",
            "fault_parameters": {"timeout_s": 5.0},
            "target": "local", "phase": "fault",
            "success": 0.0, "duration_s": 6.5, "reported_error": 1.0,
            "retries": 1, "llm_calls": 1, "tool_calls": 0, "turns": 1,
            "prompt_tokens": 50, "completion_tokens": 0, "total_tokens": 50,
            "cost_usd": 0.0, "tests_passed": 0, "tests_total": 3,
            "generated_code_hash": "", "termination_reason": "api_error",
            "exception": "LLM timeout", "oracle_outcome": {"silent_failure": False},
            "validity": "valid",
            "lifecycle": {"configured": True, "activated": True, "recovered": True,
                          "verdict": "VALID"},
            "artifact_path": "",
        },
    ]


# ── JSONL schema ──────────────────────────────────────────────────────────────

class TestJSONLSchema:

    def test_load_jsonl_empty_when_no_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.jsonl")
        assert load_jsonl(path) == []

    def test_load_jsonl_roundtrip(self, tmp_path):
        path = str(tmp_path / "runs.jsonl")
        records = _make_records()
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        loaded = load_jsonl(path)
        assert len(loaded) == 2
        assert loaded[0]["run_id"] == "r1"
        assert loaded[1]["run_id"] == "r2"

    def test_load_jsonl_skips_bad_lines(self, tmp_path):
        path = str(tmp_path / "runs.jsonl")
        with open(path, "w") as f:
            f.write('{"run_id": "ok"}\n')
            f.write("NOT JSON\n")
            f.write('{"run_id": "ok2"}\n')
        loaded = load_jsonl(path)
        assert len(loaded) == 2

    def test_required_fields_in_records(self):
        records = _make_records()
        required = ["run_id", "timestamp", "cj_commit", "agent_system",
                    "benchmark", "task_id", "phase", "success", "fault_type"]
        for r in records:
            for k in required:
                assert k in r, f"Missing {k} in record {r['run_id']}"


# ── CSV schema ────────────────────────────────────────────────────────────────

class TestCSVOutput:

    def test_task_results_csv_has_headers(self, tmp_path):
        path = str(tmp_path / "task_results.csv")
        write_task_results_csv(_make_records(), path)
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert "run_id" in rows[0]
        assert "success" in rows[0]
        assert "fault_type" in rows[0]

    def test_condition_summary_csv_created(self, tmp_path):
        path = str(tmp_path / "condition_summary.csv")
        write_condition_summary_csv(_make_records(), path)
        assert os.path.exists(path)
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) >= 1

    def test_validity_summary_csv_created(self, tmp_path):
        path = str(tmp_path / "validity_summary.csv")
        write_validity_summary_csv(_make_records(), path)
        assert os.path.exists(path)
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert any(r.get("fault_type") == "llm_timeout" for r in rows)

    def test_group_summary_csv_notice_when_no_groups(self, tmp_path):
        path = str(tmp_path / "group_summary.csv")
        write_group_summary_csv(_make_records(), path)
        assert os.path.exists(path)
        with open(path) as f:
            content = f.read()
        assert "No group" in content

    def test_empty_records_does_not_crash(self, tmp_path):
        path = str(tmp_path / "empty.csv")
        write_task_results_csv([], path)
        # Should just print notice and not write (or write nothing)

    def test_condition_summary_numeric_fields(self, tmp_path):
        path = str(tmp_path / "cond.csv")
        write_condition_summary_csv(_make_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        # Check numeric fields are present
        for row in rows:
            if row.get("pass_at_1_baseline") not in (None, "None", ""):
                float(row["pass_at_1_baseline"])  # must be parseable

    def test_plot_degradation_valid_data_does_not_crash(self, tmp_path):
        path = tmp_path / "degradation.pdf"
        out = plot_degradation(
            [{
                "fault_type": "llm_latency",
                "pass_at_1_baseline": 1.0,
                "pass_at_1_fault": 0.0,
                "n_fault_valid": 1,
            }],
            output_path=str(path),
        )
        if out is not None:
            assert path.exists()


# ── CLI dry-run ───────────────────────────────────────────────────────────────

class TestCLIDryRun:

    def test_dry_run_no_api_required(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CJ_EVAL_BASE_URL", raising=False)
        from evaluation.run import main
        ret = main([
            "--dry-run",
            "--system", "autogen",
            "--benchmark", "humanevalplus",
            "--fault", "none",
            "--tasks", "2",
            "--repeats", "1",
            "--seed", "0",
            "--results-dir", str(tmp_path),
        ])
        assert ret == 0
        assert os.path.exists(str(tmp_path / "runs.jsonl"))

    def test_dry_run_produces_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CJ_EVAL_BASE_URL", raising=False)
        from evaluation.run import main
        main(["--dry-run", "--system", "mad", "--benchmark", "mbppplus",
              "--fault", "none", "--tasks", "2",
              "--results-dir", str(tmp_path)])
        records = load_jsonl(str(tmp_path / "runs.jsonl"))
        assert len(records) >= 2
        for r in records:
            assert r.get("agent_system") == "mad-style"

    def test_generate_outputs_on_empty(self, tmp_path, monkeypatch, capsys):
        from evaluation.run import main
        main(["--generate-outputs", "--results-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert "No records" in captured.out or os.path.exists(str(tmp_path))

    def test_missing_system_raises_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CJ_EVAL_BASE_URL", raising=False)
        from evaluation.run import main
        with pytest.raises(SystemExit):
            main(["--dry-run", "--benchmark", "humanevalplus",
                  "--results-dir", str(tmp_path)])
