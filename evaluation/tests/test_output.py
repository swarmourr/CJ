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
    write_scientific_summary_csv,
    write_fault_fidelity_summary_csv,
    write_agent_resilience_summary_csv,
    write_data_quality_summary_csv,
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


def _make_exact_triplet_records():
    common = {
        "study_id": "study",
        "campaign_id": "campaign",
        "pair_id": "pair-1",
        "agent_level": "individual",
        "agent_system": "autogen-real",
        "framework": "autogen-real",
        "topology": "single",
        "benchmark": "humanevalplus",
        "task_id": "HumanEval/0",
        "model": "qwen2.5:latest",
        "seed": 7,
        "cj_commit": "abc123",
        "docker_image_digest": "sha256:test",
        "framework_versions": {"python": "3.12"},
    }
    direct = {
        **common,
        "run_id": "b",
        "condition": "direct_baseline",
        "phase": "baseline",
        "fault_type": "none",
        "validity": "valid",
        "success": 1.0,
        "duration_s": 10.0,
        "llm_calls": 1,
        "tool_calls": 1,
        "turns": 1,
        "total_tokens": 100,
        "cost_usd": 0.0,
        "reported_error": 0.0,
        "lifecycle": {"configured": False, "verdict": "valid"},
    }
    control = {
        **common,
        "run_id": "c",
        "condition": "cj_control",
        "phase": "baseline",
        "fault_type": "passthrough",
        "validity": "valid",
        "success": 1.0,
        "duration_s": 12.0,
        "llm_calls": 1,
        "tool_calls": 1,
        "turns": 1,
        "total_tokens": 110,
        "cost_usd": 0.0,
        "reported_error": 0.0,
        "lifecycle": {
            "configured": True,
            "activated": True,
            "triggered": False,
            "manifested": False,
            "reverted": True,
            "recovered": True,
            "verdict": "valid",
        },
    }
    fault = {
        **common,
        "run_id": "f",
        "condition": "cj_fault",
        "phase": "fault",
        "fault_type": "llm_latency",
        "fault_parameters": {"delay_s": 3.0},
        "validity": "valid",
        "success": 0.0,
        "duration_s": 15.0,
        "llm_calls": 1,
        "tool_calls": 1,
        "turns": 1,
        "total_tokens": 110,
        "cost_usd": 0.0,
        "reported_error": 0.0,
        "retries": 0,
        "lifecycle": {
            "configured": True,
            "activated": True,
            "triggered": True,
            "manifested": True,
            "reverted": True,
            "recovered": True,
            "verdict": "valid",
            "proxy_call_count": 1,
            "evidence": {
                "fault_evidence": [
                    json.dumps([{
                        "fault_type": "latency",
                        "triggered": True,
                        "applied": True,
                        "evidence": {"delay_s": 3.0},
                    }])
                ]
            },
        },
    }
    return [direct, control, fault]


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

    def test_scientific_summary_has_triplet_metrics(self, tmp_path):
        path = str(tmp_path / "scientific_summary.csv")
        write_scientific_summary_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        row = rows[0]
        assert row["complete_triplets"] == "1"
        assert row["valid_pairs"] == "1"
        assert row["baseline_eligible_pairs"] == "1"
        assert float(row["baseline_pass_at_1"]) == pytest.approx(1.0)
        assert float(row["control_pass_at_1"]) == pytest.approx(1.0)
        assert float(row["fault_pass_at_1"]) == pytest.approx(0.0)
        assert float(row["fault_specific_degradation"]) == pytest.approx(1.0)
        assert float(row["conditional_robustness"]) == pytest.approx(0.0)
        assert float(row["catastrophic_failure_rate"]) == pytest.approx(1.0)
        assert float(row["cj_overhead"]) == pytest.approx(0.2)

    def test_fault_fidelity_latency_uses_control_fault_delta(self, tmp_path):
        path = str(tmp_path / "fault_fidelity_summary.csv")
        write_fault_fidelity_summary_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        row = rows[0]
        assert row["fault"] == "llm_latency"
        assert int(row["intercepted_calls"]) == 1
        assert int(row["affected_calls"]) == 1
        assert float(row["observed_added_latency_s"]) == pytest.approx(3.0)
        assert float(row["expected_added_latency_s"]) == pytest.approx(3.0)
        assert float(row["latency_error_s"]) == pytest.approx(0.0)

    def test_agent_resilience_silent_failure_is_baseline_conditioned(self, tmp_path):
        path = str(tmp_path / "agent_resilience_summary.csv")
        write_agent_resilience_summary_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        row = rows[0]
        assert float(row["silent_failure_rate"]) == pytest.approx(1.0)
        assert row["baseline_eligible_pairs"] == "1"

    def test_data_quality_summary_counts_triplets_and_provenance(self, tmp_path):
        path = str(tmp_path / "data_quality_summary.csv")
        write_data_quality_summary_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        row = rows[0]
        assert row["complete_triplets"] == "1"
        assert row["missing_pair_count"] == "0"
        assert row["valid_fault_pair_count"] == "1"
        assert row["cj_commit"] == "abc123"
        assert row["docker_image_digest"] == "sha256:test"


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
