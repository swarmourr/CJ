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
    write_cj_evidence_csv,
    write_validity_summary_csv,
    write_group_summary_csv,
    write_scientific_summary_csv,
    write_fault_fidelity_summary_csv,
    write_agent_resilience_summary_csv,
    write_data_quality_summary_csv,
    write_inferential_summary_csv,
)
from evaluation.analysis.plots import plot_degradation
from evaluation.schema import OUTPUT_SCHEMA_VERSION


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
        "termination_reason": "completed",
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
        "termination_reason": "completed",
        "lifecycle": {
            "configured": True,
            "activated": True,
            "triggered": False,
            "manifested": False,
            "reverted": True,
            "recovered": True,
            "verdict": "valid",
            "proxy_call_count": 1,
            "evidence": {
                "proxy_calls": [{
                    "id": 9,
                    "phase": "control",
                    "call_index": 0,
                    "timestamp": "2024-01-01T00:01:00Z",
                    "model": "qwen2.5:latest",
                    "latency_s": 0.1,
                    "http_status": 200,
                    "fault_name": "passthrough",
                    "was_blocked": 0,
                    "was_modified": 0,
                    "fault_triggered": 0,
                    "triggered_faults_json": json.dumps([]),
                }],
            },
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
        "termination_reason": "completed",
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
                        "fault_id": "latency_call0",
                        "target": {"operation": "chat.completions", "call_index": 0},
                        "target_matched": True,
                        "triggered": True,
                        "applied": True,
                        "evidence": {
                            "delay_s": 3.0,
                            "configured_delay_s": 3.0,
                            "observed_injected_delay_s": 3.01,
                            "injection_start_fault_offset_s": 0.2,
                            "injection_end_fault_offset_s": 3.21,
                        },
                        "original_value": "x" * 400 + "full-response-tail",
                        "mutated_value": "y" * 400 + "mutated-tail",
                        "delivered_value": "y" * 400 + "mutated-tail",
                    }])
                ],
                "proxy_calls": [{
                    "id": 10,
                    "phase": "fault",
                    "call_index": 0,
                    "timestamp": "2024-01-01T00:02:00Z",
                    "model": "qwen2.5:latest",
                    "latency_s": 3.1,
                    "http_status": 200,
                    "fault_name": "latency",
                    "was_blocked": 0,
                    "was_modified": 0,
                    "fault_triggered": 1,
                    "fault_offset_s": 3.3,
                    "triggered_faults_json": json.dumps(["latency"]),
                    "fault_evidence_json": json.dumps({"latency": {
                        "delay_s": 3.0,
                        "configured_delay_s": 3.0,
                        "observed_injected_delay_s": 3.01,
                    }}),
                    "response_text": "model response should be summarized" * 20,
                    "prompt_text": "prompt should be summarized" * 20,
                }],
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
        assert rows[0]["output_schema_version"] == OUTPUT_SCHEMA_VERSION

    def test_task_results_separates_success_verdicts_and_call_sources(self, tmp_path):
        path = str(tmp_path / "task_results.csv")
        write_task_results_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        control = next(r for r in rows if r["condition"] == "cj_control")
        fault = next(r for r in rows if r["condition"] == "cj_fault")
        assert control["executor_success_verdict"] == "success"
        assert control["scorer_success_verdict"] == "not_available"
        assert control["task_success_verdict"] == "success"
        assert control["agent_reported_llm_calls"] == "1"
        assert control["proxy_intercepted_calls"] == "1"
        assert control["proxy_calls_affected"] == "0"
        assert fault["task_success_verdict"] == "failure"
        assert fault["operational_success"] == "True"
        assert fault["continued_operation"] == "True"
        assert fault["agent_detected_fault"] == "False"
        assert fault["runner_observed_failure"] == "False"
        assert fault["failure_detection_source"] == "none"
        assert fault["silent_failure"] == "True"
        assert fault["proxy_calls_affected"] == "1"

    def test_token_starvation_like_failure_is_silent_operational_success(self, tmp_path):
        records = _make_exact_triplet_records()
        records[2]["fault_type"] = "token_starvation"
        records[2]["success"] = 0.0
        records[2]["termination_reason"] = "completed"
        records[2]["reported_error"] = 0.0
        records[2].pop("exception", None)
        path = str(tmp_path / "task_results.csv")
        write_task_results_csv(records, path)
        with open(path) as f:
            row = next(r for r in csv.DictReader(f) if r["condition"] == "cj_fault")
        assert row["task_success_verdict"] == "failure"
        assert row["operational_success"] == "True"
        assert row["continued_operation"] == "True"
        assert row["agent_detected_fault"] == "False"
        assert row["runner_observed_failure"] == "False"
        assert row["silent_failure"] == "True"

    def test_task_results_marks_agent_exception_as_operational_failure(self, tmp_path):
        records = _make_exact_triplet_records()
        records[2]["success"] = 1.0
        records[2]["reported_error"] = 1.0
        records[2]["termination_reason"] = "agent_exception"
        records[2]["exception"] = "framework raised while handling tool fault"
        path = str(tmp_path / "task_results.csv")
        write_task_results_csv(records, path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        row = next(r for r in rows if r["condition"] == "cj_fault")
        assert row["task_success_verdict"] == "success"
        assert row["operational_success"] == "False"
        assert row["continued_operation"] == "False"
        assert row["agent_detected_fault"] == "False"
        assert row["runner_observed_failure"] == "True"
        assert row["failure_detection_source"] == "runner_exception"
        assert row["silent_failure"] == "False"

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
        assert row["output_schema_version"] == OUTPUT_SCHEMA_VERSION
        assert row["complete_triplets"] == "1"
        assert row["valid_pairs"] == "1"
        assert row["baseline_eligible_pairs"] == "1"
        assert row["evaluation_scope"] == "agent_resilience"
        assert float(row["baseline_pass_at_1"]) == pytest.approx(1.0)
        assert float(row["control_pass_at_1"]) == pytest.approx(1.0)
        assert float(row["fault_pass_at_1"]) == pytest.approx(0.0)
        assert float(row["fault_specific_degradation"]) == pytest.approx(1.0)
        assert float(row["conditional_robustness"]) == pytest.approx(0.0)
        assert float(row["catastrophic_failure_rate"]) == pytest.approx(1.0)
        assert row["inference_status"] == "insufficient_unique_tasks"
        assert float(row["cj_overhead"]) == pytest.approx(0.2)

    def test_scientific_summary_suppresses_fault_effect_when_control_failed(self, tmp_path):
        records = _make_exact_triplet_records()
        records[1]["success"] = 0.0
        path = str(tmp_path / "scientific_summary.csv")
        write_scientific_summary_csv(records, path)
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert row["control_eligible_pairs"] == "0"
        assert row["fault_specific_degradation"] == ""
        assert row["evaluation_scope"] == "fault_injector_validation"

    def test_fault_fidelity_latency_uses_proxy_sleep_evidence_not_runtime_delta(self, tmp_path):
        records = _make_exact_triplet_records()
        records[2]["duration_s"] = 100.0
        path = str(tmp_path / "fault_fidelity_summary.csv")
        write_fault_fidelity_summary_csv(records, path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        row = rows[0]
        assert row["fault"] == "llm_latency"
        assert int(row["intercepted_calls"]) == 1
        assert int(row["affected_calls"]) == 1
        assert float(row["observed_added_latency_s"]) == pytest.approx(3.01)
        assert float(row["expected_added_latency_s"]) == pytest.approx(3.0)
        assert float(row["latency_error_s"]) == pytest.approx(0.01)
        assert row["latency_fidelity_source"] == "cj_proxy_sleep_evidence"
        assert "start_fault_offset_s" in row["injection_point_timing"]
        assert float(row["affected_call_fraction"]) == pytest.approx(1.0)
        assert float(row["affected_call_precision"]) == pytest.approx(1.0)

    def test_fault_fidelity_latency_missing_proxy_sleep_evidence_has_no_runtime_fallback(self, tmp_path):
        records = _make_exact_triplet_records()
        records[2]["duration_s"] = 100.0
        entries = json.loads(records[2]["lifecycle"]["evidence"]["fault_evidence"][0])
        entries[0]["evidence"] = {"delay_s": 3.0}
        records[2]["lifecycle"]["evidence"]["fault_evidence"] = [json.dumps(entries)]
        path = str(tmp_path / "fault_fidelity_summary.csv")
        write_fault_fidelity_summary_csv(records, path)
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert row["observed_added_latency_s"] == ""
        assert row["latency_error_s"] == ""
        assert row["latency_fidelity_source"] == "missing_proxy_sleep_evidence"

    def test_cj_evidence_csv_preserves_raw_cj_output(self, tmp_path):
        path = str(tmp_path / "cj_evidence.csv")
        write_cj_evidence_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        fault_rows = [r for r in rows if r["condition"] == "cj_fault"]
        control_rows = [r for r in rows if r["condition"] == "cj_control"]
        assert len(control_rows) == 1
        assert control_rows[0]["cj_proxy_calls_intercepted"] == "1"
        assert control_rows[0]["cj_proxy_calls_affected"] == "0"
        assert control_rows[0]["proxy_fault_triggered"] == "0"
        assert len(fault_rows) == 1
        row = fault_rows[0]
        assert row["cj_lifecycle_manifested"] == "True"
        assert row["cj_proxy_calls_intercepted"] == "1"
        assert row["cj_proxy_calls_affected"] == "1"
        assert "delay_s" in row["cj_raw_evidence_json"]
        assert row["evidence_fault_type"] == "latency"
        assert row["proxy_call_index"] == "0"
        assert row["proxy_http_status"] == "200"
        assert "sha256" in row["evidence_original_value"]
        assert "full-response-tail" not in row["evidence_original_value"]
        assert "sha256" in row["proxy_response_text_summary_json"]
        assert "model response should be summarizedmodel response should be summarizedmodel response should be summarizedmodel response should be summarizedmodel response should be summarized" not in row["proxy_response_text_summary_json"]

    def test_fault_fidelity_timeout_uses_proxy_latency_not_runtime_delta(self, tmp_path):
        records = _make_exact_triplet_records()
        records[2]["fault_type"] = "llm_timeout"
        records[2]["fault_parameters"] = {"timeout_s": 5.0}
        records[2]["duration_s"] = 99.0
        records[2]["lifecycle"]["evidence"]["fault_evidence"] = [
            json.dumps([{
                "fault_type": "timeout",
                "fault_id": "timeout_call0",
                "target": {"operation": "chat.completions", "call_index": 0},
                "target_matched": True,
                "triggered": True,
                "applied": True,
                "evidence": {"http_status": 504},
            }])
        ]
        records[2]["lifecycle"]["evidence"]["proxy_calls"] = [{
            "call_index": 0,
            "latency_s": 5.2,
            "http_status": 504,
            "fault_name": "timeout",
            "triggered_faults_json": json.dumps(["timeout"]),
        }]
        path = str(tmp_path / "fault_fidelity_summary.csv")
        write_fault_fidelity_summary_csv(records, path)
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert row["fault"] == "llm_timeout"
        assert float(row["observed_timeout_duration_s"]) == pytest.approx(5.2)
        assert float(row["expected_timeout_duration_s"]) == pytest.approx(5.0)
        assert float(row["timeout_error_s"]) == pytest.approx(0.2)
        assert row["timeout_fidelity_source"] == "cj_proxy_latency_s"

    def test_agent_resilience_silent_failure_is_baseline_conditioned(self, tmp_path):
        path = str(tmp_path / "agent_resilience_summary.csv")
        write_agent_resilience_summary_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        row = rows[0]
        assert float(row["silent_failure_rate"]) == pytest.approx(1.0)
        assert row["baseline_eligible_pairs"] == "1"

    def test_agent_resilience_does_not_treat_exception_as_recovery(self, tmp_path):
        records = _make_exact_triplet_records()
        records[2]["exception"] = "framework exploded"
        records[2]["termination_reason"] = "agent_exception"
        records[2]["reported_error"] = 1.0
        path = str(tmp_path / "agent_resilience_summary.csv")
        write_agent_resilience_summary_csv(records, path)
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert float(row["fault_detection_rate"]) == pytest.approx(0.0)
        assert float(row["continued_operation_rate"]) == pytest.approx(0.0)
        assert float(row["graceful_failure_rate"]) == pytest.approx(0.0)
        assert float(row["silent_failure_rate"]) == pytest.approx(0.0)

    def test_agent_resilience_error_detection_is_not_silent_failure(self, tmp_path):
        records = _make_exact_triplet_records()
        records[2]["execution_trace"] = [{"event_type": "error_detection"}]
        path = str(tmp_path / "agent_resilience_summary.csv")
        write_agent_resilience_summary_csv(records, path)
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert float(row["fault_detection_rate"]) == pytest.approx(1.0)
        assert float(row["silent_failure_rate"]) == pytest.approx(0.0)

    def test_data_quality_summary_counts_triplets_and_provenance(self, tmp_path):
        path = str(tmp_path / "data_quality_summary.csv")
        write_data_quality_summary_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        row = rows[0]
        assert row["output_schema_version"] == OUTPUT_SCHEMA_VERSION
        assert row["complete_triplets"] == "1"
        assert row["raw_fault_records"] == "1"
        assert row["raw_condition_records"] == "3"
        assert row["expected_condition_records_for_complete_triplets"] == "3"
        assert row["missing_pair_count"] == "0"
        assert row["valid_fault_pair_count"] == "1"
        assert row["cj_commit"] == "abc123"
        assert row["docker_image_digest"] == "sha256:test"

    def test_inferential_summary_marks_one_task_as_insufficient(self, tmp_path):
        path = str(tmp_path / "inferential_summary.csv")
        write_inferential_summary_csv(_make_exact_triplet_records(), path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["test"] == "not_run"
        assert rows[0]["inference_status"] == "insufficient_unique_tasks"
        assert rows[0]["p_value"] == ""


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
