"""Aggregation metrics for the CJ evaluation.

All metrics are computed from raw RunRecord dicts (loaded from JSONL).
Only confirmed-valid fault records are used for primary resilience metrics;
all partitions are reported separately.

Metric definitions
------------------
pass@1_baseline   = successful_baseline_tasks / baseline_tasks
pass@1_fault      = successful_manifested_fault_tasks / manifested_fault_tasks
degradation       = pass@1_baseline - pass@1_fault  (NOT CJ's raw delta)
trigger_rate      = triggered / attempted
manifestation_rate= manifested / triggered
recovery_rate     = recovered / manifested
robustness_score  = fault_success_among_baseline_successes / baseline_successes
silent_failure_rate = P(no explicit error | baseline passed, fault failed, valid fault)
llm_call_amplification = fault_llm_calls / baseline_llm_calls
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evaluation.analysis.validity import ValidityFilter, classify_records


@dataclass
class AggregationMetrics:
    """All computed aggregation metrics for one (system, fault, benchmark) condition.

    Fields set to None when there are insufficient records to compute them.
    """
    # ── Primary resilience metrics ─────────────────────────────────────────────
    pass_at_1_baseline:   float | None = None
    pass_at_1_fault:      float | None = None
    degradation:          float | None = None   # baseline - fault (positive = worse)

    # ── Injection rates ────────────────────────────────────────────────────────
    trigger_rate:         float | None = None
    manifestation_rate:   float | None = None
    recovery_rate:        float | None = None

    # ── Robustness + silent failure ────────────────────────────────────────────
    robustness_score:     float | None = None
    catastrophic_failure_rate: float | None = None
    silent_failure_rate:  float | None = None
    baseline_eligible_pairs: int = 0

    # ── Amplification factors ──────────────────────────────────────────────────
    llm_call_amplification:   float | None = None
    tool_call_amplification:  float | None = None
    token_amplification:      float | None = None
    cost_amplification:       float | None = None
    duration_amplification:   float | None = None

    # ── Record counts ──────────────────────────────────────────────────────────
    n_baseline:     int = 0
    n_fault_valid:  int = 0
    n_fault_invalid: int = 0
    n_fault_inconclusive: int = 0
    n_fault_untriggered: int = 0

    # ── Mean telemetry (baseline / fault) ─────────────────────────────────────
    baseline_llm_calls:   float | None = None
    fault_llm_calls:      float | None = None
    baseline_duration_s:  float | None = None
    fault_duration_s:     float | None = None
    baseline_tokens:      float | None = None
    fault_tokens:         float | None = None
    baseline_cost_usd:    float | None = None
    fault_cost_usd:       float | None = None

    # ── CJ overhead ───────────────────────────────────────────────────────────
    # Populated externally when overhead measurements are available
    cj_overhead_pct:      float | None = None  # (cj_baseline - native) / native * 100

    def to_dict(self) -> dict[str, Any]:
        import dataclasses
        return dataclasses.asdict(self)

    def summary_lines(self) -> list[str]:
        lines = []
        if self.pass_at_1_baseline is not None:
            lines.append(f"  pass@1 baseline      : {self.pass_at_1_baseline:.3f}  (n={self.n_baseline})")
        if self.pass_at_1_fault is not None:
            lines.append(f"  pass@1 fault (valid) : {self.pass_at_1_fault:.3f}  (n={self.n_fault_valid})")
        if self.degradation is not None:
            lines.append(f"  degradation          : {self.degradation:+.3f}")
        if self.trigger_rate is not None:
            lines.append(f"  trigger rate         : {self.trigger_rate:.3f}")
        if self.manifestation_rate is not None:
            lines.append(f"  manifestation rate   : {self.manifestation_rate:.3f}")
        if self.recovery_rate is not None:
            lines.append(f"  recovery rate        : {self.recovery_rate:.3f}")
        if self.robustness_score is not None:
            lines.append(f"  robustness score     : {self.robustness_score:.3f}")
        if self.catastrophic_failure_rate is not None:
            lines.append(f"  catastrophic failure : {self.catastrophic_failure_rate:.3f}")
        if self.silent_failure_rate is not None:
            lines.append(f"  silent failure rate  : {self.silent_failure_rate:.3f}")
        if self.llm_call_amplification is not None:
            lines.append(f"  LLM call amplification: {self.llm_call_amplification:.3f}×")
        if self.duration_amplification is not None:
            lines.append(f"  duration amplification: {self.duration_amplification:.3f}×")
        counts = (
            f"  n: baseline={self.n_baseline}  valid={self.n_fault_valid}  "
            f"invalid={self.n_fault_invalid}  "
            f"inconclusive={self.n_fault_inconclusive}  "
            f"untriggered={self.n_fault_untriggered}"
        )
        lines.append(counts)
        return lines


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _amplification(base: float | None, fault: float | None) -> float | None:
    if base is None or fault is None or base == 0:
        return None
    return round(fault / base, 4)


def _bool_field(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    return default


def _trace_has_detected_error(record: dict) -> bool:
    trace = record.get("execution_trace") or (record.get("result") or {}).get("execution_trace", [])
    if not isinstance(trace, list):
        return False
    return any(
        isinstance(event, dict)
        and event.get("event_type") in {"error_detection", "attempted_recovery"}
        for event in trace
    )


def _reported_or_detected_error(record: dict) -> bool:
    try:
        reported_error = float(record.get("reported_error", 0.0) or 0.0)
    except (TypeError, ValueError):
        reported_error = 0.0
    if reported_error >= 0.5:
        return True
    if record.get("exception") or record.get("executor_error"):
        return True
    status = str(record.get("executor_status", "")).lower()
    if status not in {"", "ok"}:
        return True
    reason = str(record.get("termination_reason", "")).lower()
    explicit_failure_reasons = {
        "api_error", "agent_exception", "container_killed",
        "controlled_failure", "error", "exception", "executor_error",
        "graceful_failure", "killed", "reported_failure", "timeout",
    }
    return (
        reason in explicit_failure_reasons
        or "error" in reason
        or "exception" in reason
        or _trace_has_detected_error(record)
    )


def _silent_failure(record: dict) -> bool:
    if "silent_failure" in record:
        return _bool_field(record.get("silent_failure"))
    return not _reported_or_detected_error(record)


def compute_metrics(records: list[dict]) -> AggregationMetrics:
    """Compute all aggregation metrics from a list of RunRecord dicts.

    Parameters
    ----------
    records : list[dict]
        Mixed list of baseline and fault RunRecord.to_dict() outputs.
    """
    filt = classify_records(records)
    m    = AggregationMetrics()

    m.n_baseline           = len(filt.baseline)
    m.n_fault_valid        = len(filt.valid)
    m.n_fault_invalid      = len(filt.invalid)
    m.n_fault_inconclusive = len(filt.inconclusive)
    m.n_fault_untriggered  = len(filt.untriggered)

    # ── pass@1 ─────────────────────────────────────────────────────────────────
    if filt.baseline:
        m.pass_at_1_baseline = sum(r["success"] for r in filt.baseline) / len(filt.baseline)
        m.baseline_llm_calls  = _mean([r.get("llm_calls",0) for r in filt.baseline])
        m.baseline_duration_s = _mean([r.get("duration_s",0.0) for r in filt.baseline])
        m.baseline_tokens     = _mean([r.get("total_tokens",0) for r in filt.baseline])
        m.baseline_cost_usd   = _mean([r.get("cost_usd",0.0) for r in filt.baseline])

    if filt.valid:
        m.pass_at_1_fault    = sum(r["success"] for r in filt.valid) / len(filt.valid)
        m.fault_llm_calls    = _mean([r.get("llm_calls",0) for r in filt.valid])
        m.fault_duration_s   = _mean([r.get("duration_s",0.0) for r in filt.valid])
        m.fault_tokens       = _mean([r.get("total_tokens",0) for r in filt.valid])
        m.fault_cost_usd     = _mean([r.get("cost_usd",0.0) for r in filt.valid])

    # ── degradation: paired-baseline − fault (positive = fault made things worse) ─
    # Only use baselines whose pair_id matches a valid fault record so that
    # orphaned, invalid, or untriggered pairs do not skew the comparison.
    if filt.valid and filt.baseline and m.pass_at_1_fault is not None:
        valid_pair_ids = {r["pair_id"] for r in filt.valid if r.get("pair_id")}
        if valid_pair_ids:
            paired_baselines = [
                r for r in filt.baseline if r.get("pair_id") in valid_pair_ids
            ]
            if paired_baselines:
                paired_base_pass = (
                    sum(r["success"] for r in paired_baselines) / len(paired_baselines)
                )
                m.degradation = round(paired_base_pass - m.pass_at_1_fault, 4)
        elif m.pass_at_1_baseline is not None:
            # Legacy records with no pair_id: fall back to global baseline
            m.degradation = round(m.pass_at_1_baseline - m.pass_at_1_fault, 4)

    # ── Injection rates ────────────────────────────────────────────────────────
    m.trigger_rate       = filt.trigger_rate
    m.manifestation_rate = filt.manifestation_rate
    m.recovery_rate      = filt.recovery_rate

    # ── Robustness score ───────────────────────────────────────────────────────
    # Fault success restricted to tasks that succeeded at baseline.
    # Prefer pair_id for exact (task, repeat) matching; fall back to task_id
    # for legacy records without pair_id.
    baseline_success_faults: list[dict] = []
    if filt.baseline and filt.valid:
        baseline_success_pairs = {
            r["pair_id"] for r in filt.baseline
            if r.get("success", 0.0) >= 0.5 and r.get("pair_id")
        }
        if baseline_success_pairs:
            baseline_success_faults = [
                r for r in filt.valid if r.get("pair_id") in baseline_success_pairs
            ]
        else:
            # Legacy: match by task_id
            baseline_success_tasks = {
                r["task_id"] for r in filt.baseline if r.get("success", 0.0) >= 0.5
            }
            baseline_success_faults = [
                r for r in filt.valid if r.get("task_id") in baseline_success_tasks
            ]
        m.baseline_eligible_pairs = len(baseline_success_faults)
        if baseline_success_faults:
            m.robustness_score = round(
                sum(r["success"] for r in baseline_success_faults)
                / len(baseline_success_faults),
                4,
            )
            m.catastrophic_failure_rate = round(1.0 - m.robustness_score, 4)

    # ── Silent failure rate ────────────────────────────────────────────────────
    # Correct definition:
    # P(no explicit error | B=1, F=0, valid fault).  Fault runs whose direct
    # baseline already failed cannot demonstrate silent degradation caused by CJ.
    if baseline_success_faults:
        denominator = [
            r for r in baseline_success_faults
            if r.get("success", 1.0) < 0.5
        ]
        silent = [
            r for r in denominator
            if _silent_failure(r)
        ]
        if denominator:
            m.silent_failure_rate = round(len(silent) / len(denominator), 4)
        else:
            m.silent_failure_rate = 0.0

    # ── Amplification factors ──────────────────────────────────────────────────
    m.llm_call_amplification  = _amplification(m.baseline_llm_calls,  m.fault_llm_calls)
    m.tool_call_amplification = _amplification(
        _mean([r.get("tool_calls",0) for r in filt.baseline]) if filt.baseline else None,
        _mean([r.get("tool_calls",0) for r in filt.valid])    if filt.valid    else None,
    )
    m.token_amplification     = _amplification(m.baseline_tokens,     m.fault_tokens)
    m.cost_amplification      = _amplification(m.baseline_cost_usd,   m.fault_cost_usd)
    m.duration_amplification  = _amplification(m.baseline_duration_s, m.fault_duration_s)

    return m
