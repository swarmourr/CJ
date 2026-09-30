"""Per-condition statistical summaries.

Reports for every principal condition:
  - sample count, mean, std, median, IQR, 95% CI, Cohen's d (vs baseline).

Uses paired analysis when the same task_id appears in both baseline and
fault records. Missing pairs are documented but not discarded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConditionStats:
    """Statistics for one numeric metric in one condition."""
    metric:      str
    condition:   str    # "baseline" or fault name
    n:           int
    mean:        float | None = None
    std:         float | None = None
    median:      float | None = None
    iqr:         float | None = None
    ci95_half:   float | None = None   # margin of error (±)
    cohens_d:    float | None = None   # vs baseline; None for baseline itself
    missing_pairs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric":    self.metric,
            "condition": self.condition,
            "n":         self.n,
            "mean":      self.mean,
            "std":       self.std,
            "median":    self.median,
            "iqr":       self.iqr,
            "ci95_half": self.ci95_half,
            "cohens_d":  self.cohens_d,
            "missing_pairs": self.missing_pairs,
        }


_T_TABLE = {1:12.706,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,7:2.365,
            8:2.306,9:2.262,10:2.228,15:2.131,20:2.086,30:2.042,60:2.000}


def _t_crit(n: int) -> float:
    df = n - 1
    for k in sorted(_T_TABLE, reverse=True):
        if df >= k:
            return _T_TABLE[k]
    return 12.706


def _stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {}
    s = sorted(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / max(n - 1, 1)
    std  = math.sqrt(variance)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    q1 = s[n // 4]
    q3 = s[3 * n // 4] if 3 * n // 4 < n else s[-1]
    iqr  = q3 - q1
    ci95 = _t_crit(n) * std / math.sqrt(n) if n >= 2 else None
    return {"n": n, "mean": mean, "std": std, "median": median, "iqr": iqr, "ci95_half": ci95}


def _cohens_dz(differences: list[float]) -> float | None:
    """Paired Cohen's d_z = mean(diff) / std(diff).

    Uses the within-subject formula appropriate for paired designs where each
    baseline observation is matched to exactly one fault observation.
    """
    n = len(differences)
    if n < 2:
        return None
    mean_d = sum(differences) / n
    std_d  = math.sqrt(sum((x - mean_d) ** 2 for x in differences) / (n - 1))
    return round(mean_d / std_d, 4) if std_d > 0 else None


def compute_condition_stats(
    baseline_records: list[dict],
    fault_records: list[dict],
    metrics: list[str] | None = None,
    condition_name: str = "fault",
) -> list[ConditionStats]:
    """Compute per-metric statistics for baseline and fault conditions.

    Parameters
    ----------
    baseline_records : list[dict]
        Raw RunRecord dicts with phase=="baseline".
    fault_records : list[dict]
        Raw RunRecord dicts with phase=="fault" and validity=="valid".
    metrics : list[str], optional
        Which numeric metrics to analyse. Defaults to standard set.
    condition_name : str
        Label for the fault condition (e.g. ``"llm_timeout"``).
    """
    if metrics is None:
        metrics = [
            "success", "duration_s", "llm_calls", "tool_calls",
            "turns", "total_tokens", "cost_usd", "reported_error", "retries",
        ]

    results: list[ConditionStats] = []

    # Build paired sets — match by task_id
    baseline_by_task: dict[str, list[dict]] = {}
    for r in baseline_records:
        baseline_by_task.setdefault(r.get("task_id", ""), []).append(r)

    fault_by_task: dict[str, list[dict]] = {}
    for r in fault_records:
        fault_by_task.setdefault(r.get("task_id", ""), []).append(r)

    paired_tasks   = set(baseline_by_task) & set(fault_by_task)
    missing_pairs  = len(set(baseline_by_task) - set(fault_by_task))

    for metric in metrics:
        # Descriptive stats use all available records (not restricted to pairs)
        b_vals = [r.get(metric, 0.0) for r in baseline_records
                  if isinstance(r.get(metric), (int, float))]
        f_vals = [r.get(metric, 0.0) for r in fault_records
                  if isinstance(r.get(metric), (int, float))]

        b_st = _stats(b_vals)
        f_st = _stats(f_vals)

        # Paired Cohen's d_z: compute differences for matched (task_id) pairs.
        # When a task has multiple repeats pick the first match per task_id to
        # form one canonical pair (avoids duplicate-counting).
        differences: list[float] = []
        for tid in paired_tasks:
            b_recs = baseline_by_task.get(tid, [])
            f_recs = fault_by_task.get(tid, [])
            for b_r, f_r in zip(b_recs, f_recs):
                bv = b_r.get(metric)
                fv = f_r.get(metric)
                if isinstance(bv, (int, float)) and isinstance(fv, (int, float)):
                    differences.append(float(fv) - float(bv))
        d = _cohens_dz(differences)

        results.append(ConditionStats(
            metric=metric, condition="baseline",
            n=b_st.get("n", 0),
            mean=b_st.get("mean"),
            std=b_st.get("std"),
            median=b_st.get("median"),
            iqr=b_st.get("iqr"),
            ci95_half=b_st.get("ci95_half"),
            cohens_d=None,
        ))
        results.append(ConditionStats(
            metric=metric, condition=condition_name,
            n=f_st.get("n", 0),
            mean=f_st.get("mean"),
            std=f_st.get("std"),
            median=f_st.get("median"),
            iqr=f_st.get("iqr"),
            ci95_half=f_st.get("ci95_half"),
            cohens_d=d,
            missing_pairs=missing_pairs,
        ))

    return results
