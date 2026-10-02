"""Tests for aggregation metrics, validity filtering, and statistical analysis.

Covers:
  - pass@1 and degradation calculations (verified against hand-calculated fixtures)
  - Robustness score calculation
  - Silent failure detection
  - Call amplification calculation
  - Invalid and inconclusive handling
  - Group skew aggregation (placeholder)
"""

import pytest

from evaluation.analysis.metrics import AggregationMetrics, compute_metrics
from evaluation.analysis.validity import ValidityFilter, classify_records
from evaluation.analysis.statistics import ConditionStats, compute_condition_stats


# ── Fixture builders ──────────────────────────────────────────────────────────

def _b(task_id, success=1.0, llm_calls=5, duration_s=1.0, tool_calls=1,
       total_tokens=100, cost_usd=0.001, reported_error=0.0):
    return {
        "run_id": f"b-{task_id}", "phase": "baseline", "validity": "unchecked",
        "task_id": task_id, "fault_type": "none",
        "success": success, "llm_calls": llm_calls, "duration_s": duration_s,
        "tool_calls": tool_calls, "total_tokens": total_tokens,
        "cost_usd": cost_usd, "reported_error": reported_error,
        "retries": 0, "turns": llm_calls,
    }


def _f(task_id, success=0.0, llm_calls=8, duration_s=5.0, validity="valid",
       tool_calls=1, total_tokens=150, cost_usd=0.002, reported_error=0.0,
       recovered=True):
    return {
        "run_id": f"f-{task_id}", "phase": "fault", "validity": validity,
        "task_id": task_id, "fault_type": "llm_timeout",
        "success": success, "llm_calls": llm_calls, "duration_s": duration_s,
        "tool_calls": tool_calls, "total_tokens": total_tokens,
        "cost_usd": cost_usd, "reported_error": reported_error,
        "retries": 1, "turns": llm_calls,
        "lifecycle": {"recovered": recovered},
    }


# ── pass@1 and degradation ────────────────────────────────────────────────────

class TestPassAt1AndDegradation:

    def test_perfect_baseline(self):
        records = [_b("t1", success=1.0), _b("t2", success=1.0)]
        m = compute_metrics(records)
        assert m.pass_at_1_baseline == 1.0

    def test_zero_baseline(self):
        records = [_b("t1", success=0.0), _b("t2", success=0.0)]
        m = compute_metrics(records)
        assert m.pass_at_1_baseline == 0.0

    def test_pass_at_1_fault_uses_valid_only(self):
        records = [
            _b("t1"), _b("t2"),
            _f("t1", success=1.0, validity="valid"),
            _f("t2", success=0.0, validity="invalid"),  # excluded from pass@1_fault
        ]
        m = compute_metrics(records)
        assert m.n_fault_valid == 1
        assert m.pass_at_1_fault == 1.0

    def test_degradation_sign_positive_when_worse(self):
        # baseline=1.0, fault=0.0 → degradation=+1.0
        records = [_b("t1", success=1.0), _f("t1", success=0.0, validity="valid")]
        m = compute_metrics(records)
        assert m.degradation is not None
        assert m.degradation > 0

    def test_degradation_zero_when_same(self):
        records = [_b("t1", success=1.0), _f("t1", success=1.0, validity="valid")]
        m = compute_metrics(records)
        assert m.degradation == pytest.approx(0.0)

    def test_hand_calculated_pass_at_1(self):
        # 4 baseline: 3 succeed → 0.75
        # 3 valid fault: 1 succeeds → 0.333
        # degradation = 0.75 - 0.333 ≈ 0.417
        records = [
            _b("t1", success=1.0), _b("t2", success=1.0),
            _b("t3", success=1.0), _b("t4", success=0.0),
            _f("t1", success=1.0, validity="valid"),
            _f("t2", success=0.0, validity="valid"),
            _f("t3", success=0.0, validity="valid"),
            _f("t4", success=0.0, validity="invalid"),
        ]
        m = compute_metrics(records)
        assert m.pass_at_1_baseline == pytest.approx(0.75)
        assert m.pass_at_1_fault    == pytest.approx(1/3, abs=1e-4)
        assert m.degradation        == pytest.approx(0.75 - 1/3, abs=1e-4)


# ── Robustness score ──────────────────────────────────────────────────────────

class TestRobustnessScore:

    def test_robustness_only_baseline_successes(self):
        # t1, t2 succeed at baseline; t3 fails. fault: t1 succeeds, t2 fails
        records = [
            _b("t1", success=1.0), _b("t2", success=1.0), _b("t3", success=0.0),
            _f("t1", success=1.0, validity="valid"),
            _f("t2", success=0.0, validity="valid"),
            _f("t3", success=1.0, validity="valid"),  # t3 excluded (failed baseline)
        ]
        m = compute_metrics(records)
        # Among baseline successes (t1, t2): t1 succeeded → 1/2 = 0.5
        assert m.robustness_score == pytest.approx(0.5)

    def test_robustness_none_when_no_valid(self):
        records = [_b("t1", success=1.0), _f("t1", success=0.0, validity="invalid")]
        m = compute_metrics(records)
        assert m.robustness_score is None


# ── Silent failure ────────────────────────────────────────────────────────────

class TestSilentFailure:

    def test_silent_failure_detected(self):
        # tests fail (success=0) but agent didn't report error (reported_error=0)
        records = [
            _b("t1"),
            _f("t1", success=0.0, validity="valid", reported_error=0.0),
        ]
        m = compute_metrics(records)
        assert m.silent_failure_rate == 1.0

    def test_no_silent_failure_when_error_reported(self):
        records = [
            _b("t1"),
            _f("t1", success=0.0, validity="valid", reported_error=1.0),
        ]
        m = compute_metrics(records)
        assert m.silent_failure_rate == 0.0

    def test_no_silent_failure_when_error_detected_in_trace(self):
        fault = _f("t1", success=0.0, validity="valid", reported_error=0.0)
        fault["execution_trace"] = [{"event_type": "error_detection"}]
        records = [_b("t1"), fault]
        m = compute_metrics(records)
        assert m.silent_failure_rate == 0.0

    def test_no_silent_failure_when_executor_reports_exception(self):
        fault = _f("t1", success=0.0, validity="valid", reported_error=0.0)
        fault["exception"] = "framework exploded"
        fault["termination_reason"] = "agent_exception"
        records = [_b("t1"), fault]
        m = compute_metrics(records)
        assert m.silent_failure_rate == 0.0

    def test_success_is_not_silent_failure(self):
        records = [_b("t1"), _f("t1", success=1.0, validity="valid", reported_error=0.0)]
        m = compute_metrics(records)
        assert m.silent_failure_rate == 0.0

    def test_baseline_failure_is_not_silent_failure_denominator(self):
        records = [
            _b("t1", success=0.0),
            _f("t1", success=0.0, validity="valid", reported_error=0.0),
        ]
        m = compute_metrics(records)
        assert m.baseline_eligible_pairs == 0
        assert m.silent_failure_rate is None


# ── Amplification ─────────────────────────────────────────────────────────────

class TestAmplification:

    def test_llm_call_amplification(self):
        records = [
            _b("t1", llm_calls=5), _b("t2", llm_calls=5),
            _f("t1", llm_calls=10, validity="valid"),
            _f("t2", llm_calls=10, validity="valid"),
        ]
        m = compute_metrics(records)
        assert m.llm_call_amplification == pytest.approx(2.0)

    def test_amplification_none_when_baseline_zero(self):
        records = [
            _b("t1", llm_calls=0),
            _f("t1", llm_calls=5, validity="valid"),
        ]
        m = compute_metrics(records)
        # baseline_llm_calls=0 → amplification is None (avoid div by zero)
        assert m.llm_call_amplification is None

    def test_duration_amplification_hand_calc(self):
        records = [
            _b("t1", duration_s=1.0),
            _f("t1", duration_s=3.0, validity="valid"),
        ]
        m = compute_metrics(records)
        assert m.duration_amplification == pytest.approx(3.0)


# ── Validity filtering ────────────────────────────────────────────────────────

class TestValidityFilter:

    def test_partitions_correctly(self):
        records = [
            _b("t1"), _b("t2"),
            _f("t1", validity="valid"),
            _f("t2", validity="invalid"),
            _f("t3", validity="inconclusive"),
            _f("t4", validity="untriggered"),
        ]
        filt = classify_records(records)
        assert len(filt.baseline)    == 2
        assert len(filt.valid)       == 1
        assert len(filt.invalid)     == 1
        assert len(filt.inconclusive)== 1
        assert len(filt.untriggered) == 1

    def test_trigger_rate_calculation(self):
        records = [
            _b("t1"),
            _f("t1", validity="valid"),
            _f("t2", validity="valid"),
            _f("t3", validity="untriggered"),
            _f("t4", validity="untriggered"),
        ]
        filt = classify_records(records)
        # 2 valid (triggered) out of 4 fault records → 0.5
        assert filt.trigger_rate == pytest.approx(0.5)

    def test_invalid_inconclusive_excluded_from_primary(self):
        records = [
            _b("t1", success=1.0),
            _f("t1", success=0.0, validity="invalid"),
            _f("t2", success=0.0, validity="inconclusive"),
        ]
        m = compute_metrics(records)
        # No valid fault records → pass_at_1_fault is None
        assert m.pass_at_1_fault is None

    def test_recovery_rate(self):
        records = [
            _b("t1"), _b("t2"),
            _f("t1", validity="valid", recovered=True),
            _f("t2", validity="valid", recovered=False),
        ]
        filt = classify_records(records)
        assert filt.recovery_rate == pytest.approx(0.5)


# ── Statistical analysis ──────────────────────────────────────────────────────

class TestStatisticalAnalysis:

    def test_returns_condition_stats_list(self):
        b_recs = [_b("t1", success=1.0, duration_s=1.0),
                  _b("t2", success=0.0, duration_s=2.0)]
        f_recs = [_f("t1", success=0.0, duration_s=5.0),
                  _f("t2", success=0.0, duration_s=6.0)]
        stats = compute_condition_stats(b_recs, f_recs)
        assert isinstance(stats, list)
        assert len(stats) > 0
        assert all(isinstance(s, ConditionStats) for s in stats)

    def test_cohens_d_computed(self):
        # Differences must be non-constant so std(diff) > 0 and d_z is defined.
        # b=[1,2,3,4,5], f=[3,5,7,9,11] → diffs=[2,3,4,5,6] → std>0
        b_recs = [_b(f"t{i}", duration_s=float(i+1)) for i in range(5)]
        f_recs = [_f(f"t{i}", duration_s=float(i*2+3), validity="valid") for i in range(5)]
        stats = compute_condition_stats(b_recs, f_recs)
        duration_fault = next((s for s in stats if s.metric == "duration_s" and s.condition == "fault"), None)
        assert duration_fault is not None
        assert duration_fault.cohens_d is not None

    def test_to_dict_has_all_keys(self):
        stats = compute_condition_stats(
            [_b("t1")], [_f("t1", validity="valid")]
        )
        for s in stats:
            d = s.to_dict()
            assert "metric" in d
            assert "n" in d
            assert "mean" in d
            assert "cohens_d" in d


# ── Group summary (placeholder) ───────────────────────────────────────────────

class TestGroupSummary:

    def test_no_group_data_returns_empty_valid(self):
        records = [_b("t1"), _f("t1", validity="valid")]
        # No group lifecycle fields present
        filt = classify_records(records)
        # Group metrics not applicable — just verify no crash
        assert filt is not None
