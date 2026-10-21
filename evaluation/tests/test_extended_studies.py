from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from evaluation.extended_campaign import _cross_layer_summary, _proxy_evidence
from evaluation.extended_studies import (
    CoordinatedFaultBinding,
    CoordinatedFaultGroup,
    LoadSample,
    RestorationCycle,
    compound_interaction_effect,
    percentile,
    summarize_load,
    summarize_restoration,
)


@dataclass
class _Verification:
    verified: bool
    reason: str = ""


class _Fault:
    def __init__(self, *, activates: bool = True, recovers: bool = True) -> None:
        self.activates = activates
        self.recovers = recovers
        self.started = False
        self.stopped = False

    def start(self, target) -> None:
        target.append("start")
        self.started = True

    def verify_active(self, target):
        return _Verification(self.activates, "active" if self.activates else "inactive")

    def stop(self, target) -> None:
        target.append("stop")
        self.stopped = True

    def verify_recovered(self, target):
        return _Verification(self.recovers, "recovered" if self.recovers else "residual")


def _binding(name: str, fault: _Fault) -> CoordinatedFaultBinding:
    return CoordinatedFaultBinding(
        target_id=name,
        role=name,
        layer="container_resource",
        fault=fault,
        target=[],
    )


def test_coordinated_group_records_valid_multi_target_lifecycle() -> None:
    bindings = [_binding(role, _Fault()) for role in ("planner", "coder", "reviewer")]
    workload_calls = []

    result = CoordinatedFaultGroup(bindings, study="E7").run(
        lambda: workload_calls.append("run") or {"success": True}
    )

    assert result.verdict == "valid"
    assert result.group_valid is True
    assert result.partial_activation is False
    assert result.rollback_success is True
    assert result.activation_skew_ms is not None
    assert result.rollback_skew_ms is not None
    assert workload_calls == ["run"]
    assert all(state.recovered for state in result.target_lifecycles)


def test_partial_activation_aborts_workload_and_rolls_back_every_target() -> None:
    good = _Fault()
    failed = _Fault(activates=False)
    bindings = [_binding("planner", good), _binding("coder", failed)]
    workload_calls = []

    result = CoordinatedFaultGroup(bindings, study="E7").run(
        lambda: workload_calls.append("should-not-run")
    )

    assert result.verdict == "invalid"
    assert result.partial_activation is True
    assert result.group_valid is False
    assert result.rollback_success is True
    assert workload_calls == []
    assert good.stopped and failed.stopped


def test_failed_recovery_invalidates_otherwise_valid_group() -> None:
    result = CoordinatedFaultGroup(
        [_binding("planner", _Fault()), _binding("coder", _Fault(recovers=False))],
        study="E7",
    ).run(lambda: None)

    assert result.group_valid is True
    assert result.rollback_success is False
    assert result.verdict == "invalid"


def test_duplicate_target_identifiers_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        CoordinatedFaultGroup(
            [_binding("planner", _Fault()), _binding("planner", _Fault())],
            study="E7",
        )


def test_compound_interaction_effect_is_difference_in_differences() -> None:
    # A and B each degrade success by 0.1; compound degradation is 0.4.
    assert compound_interaction_effect(
        control=1.0,
        fault_a=0.9,
        fault_b=0.9,
        compound=0.6,
    ) == pytest.approx(0.2)


def test_load_summary_reports_percentiles_throughput_and_errors() -> None:
    samples = [
        LoadSample(0.01, True, 200, 10),
        LoadSample(0.02, True, 200, 20),
        LoadSample(0.03, False, 503, 0, "unavailable"),
        LoadSample(0.04, True, 200, 40),
    ]
    summary = summarize_load(samples, wall_duration_s=0.5)

    assert summary["latency_p50_ms"] == pytest.approx(25.0)
    assert summary["latency_p95_ms"] == pytest.approx(38.5)
    assert summary["throughput_rps"] == 8.0
    assert summary["error_rate"] == 0.25
    assert summary["bytes_received"] == 70


def test_percentile_rejects_out_of_range_quantile() -> None:
    with pytest.raises(ValueError):
        percentile([1.0], 1.1)


def test_restoration_summary_keeps_reversion_and_recovery_separate() -> None:
    cycles = [
        RestorationCycle(0, True, True, True, True, 0.1, 0.2),
        RestorationCycle(1, True, True, True, False, 0.2, 0.4),
    ]

    summary = summarize_restoration(cycles)

    assert summary["reversion_success_rate"] == 1.0
    assert summary["verified_restoration_rate"] == 0.5
    assert summary["cleanup_p95_s"] == pytest.approx(0.39)


def test_proxy_evidence_aggregates_full_rows_but_bounds_previews() -> None:
    calls = [
        {
            "call_index": index,
            "http_status": 200,
            "fault_triggered": 1,
            "fault_evidence_json": json.dumps([{
                "fault_type": "latency",
                "evidence": {
                    "configured_delay_s": 0.5,
                    "observed_injected_delay_s": 0.501,
                }
            }]),
        }
        for index in range(7)
    ]

    evidence = _proxy_evidence(calls)

    assert evidence["intercepted_calls"] == 7
    assert evidence["affected_calls"] == 7
    assert evidence["configured_delay_s"] == pytest.approx(3.5)
    assert evidence["observed_injected_delay_s"] == pytest.approx(3.507)
    assert len(evidence["call_previews"]) == 5
    assert evidence["call_preview_truncated"] is True


def test_cross_layer_summary_uses_complete_factorial_cycles() -> None:
    values = {
        "direct": (True, 1.0),
        "llm_only": (True, 1.5),
        "infrastructure_only": (False, 1.25),
        "compound": (False, 2.0),
    }
    records = [
        {
            "cycle": 0,
            "condition": condition,
            "task_success": success,
            "workload": {"duration_s": duration},
            "group_valid": True,
            "verified_recovery": True,
        }
        for condition, (success, duration) in values.items()
    ]

    summary = _cross_layer_summary(records)

    assert summary["complete_factorial_cycles"] == 1
    assert summary["valid_factorial_cycles"] == 1
    assert summary["success_rate_by_condition"]["direct"] == 1.0
    assert summary["task_success_degradation_vs_direct"]["compound"] == 1.0
    assert summary["compound_success_interaction_mean"] == 0.0
    assert summary["compound_duration_interaction_s_mean"] == pytest.approx(0.25)
    assert summary["valid_compound_cycles"] == 1


def test_cross_layer_summary_excludes_invalid_fault_cycles() -> None:
    records = [
        {
            "cycle": 0,
            "condition": condition,
            "task_success": True,
            "workload": {"duration_s": 1.0},
            "group_valid": False if condition == "compound" else True,
            "verified_recovery": True,
        }
        for condition in ("direct", "llm_only", "infrastructure_only", "compound")
    ]

    summary = _cross_layer_summary(records)

    assert summary["complete_factorial_cycles"] == 1
    assert summary["valid_factorial_cycles"] == 0
    assert summary["excluded_invalid_factorial_cycles"] == 1
    assert summary["compound_success_interaction_mean"] is None
