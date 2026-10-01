"""Per-condition statistical summaries.

Reports for every principal condition:
  - sample count, mean, std, median, IQR, 95% CI, Cohen's d (vs baseline).

Uses paired analysis when the same task_id appears in both baseline and
fault records. Missing pairs are documented but not discarded.
"""

from __future__ import annotations

import math
import random
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


def exact_pair_matches(
    baseline_records: list[dict],
    condition_records: list[dict],
    *,
    metric: str,
) -> tuple[list[tuple[dict, dict]], int]:
    """Return exact pair_id matches and missing-pair count for one metric."""
    baseline_by_pair = {
        r.get("pair_id"): r
        for r in baseline_records
        if r.get("pair_id") and isinstance(r.get(metric), (int, float))
    }
    condition_by_pair = {
        r.get("pair_id"): r
        for r in condition_records
        if r.get("pair_id") and isinstance(r.get(metric), (int, float))
    }
    ids = sorted(set(baseline_by_pair) & set(condition_by_pair))
    missing = len(set(baseline_by_pair) ^ set(condition_by_pair))
    return [(baseline_by_pair[i], condition_by_pair[i]) for i in ids], missing


def mcnemar_exact(
    baseline_records: list[dict],
    condition_records: list[dict],
    *,
    metric: str = "success",
) -> dict[str, Any]:
    """McNemar exact test for paired binary correctness."""
    pairs, missing = exact_pair_matches(baseline_records, condition_records, metric=metric)
    b_only = 0
    c_only = 0
    for base, cond in pairs:
        bv = 1 if float(base.get(metric, 0)) >= 0.5 else 0
        cv = 1 if float(cond.get(metric, 0)) >= 0.5 else 0
        if bv == 1 and cv == 0:
            b_only += 1
        elif bv == 0 and cv == 1:
            c_only += 1
    discordant = b_only + c_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, k) * (0.5 ** discordant)
            for k in range(0, min(b_only, c_only) + 1)
        )
        p_value = min(1.0, 2.0 * tail)
    return {
        "test": "mcnemar_exact",
        "n_pairs": len(pairs),
        "baseline_only_success": b_only,
        "condition_only_success": c_only,
        "missing_pairs": missing,
        "p_value": p_value,
    }


def paired_permutation_test(
    baseline_records: list[dict],
    condition_records: list[dict],
    *,
    metric: str,
    iterations: int = 10_000,
    seed: int = 0,
) -> dict[str, Any]:
    """Two-sided sign-flip paired permutation test for continuous metrics."""
    pairs, missing = exact_pair_matches(baseline_records, condition_records, metric=metric)
    diffs = [float(c.get(metric, 0)) - float(b.get(metric, 0)) for b, c in pairs]
    n = len(diffs)
    if n == 0:
        return {"test": "paired_permutation", "n_pairs": 0, "missing_pairs": missing, "p_value": None}
    observed = abs(sum(diffs) / n)
    rng = random.Random(seed)
    extreme = 0
    total = 0
    if n <= 18:
        for mask in range(1 << n):
            signed = [
                d if (mask >> i) & 1 else -d
                for i, d in enumerate(diffs)
            ]
            if abs(sum(signed) / n) >= observed - 1e-12:
                extreme += 1
            total += 1
    else:
        for _ in range(iterations):
            signed = [d if rng.random() < 0.5 else -d for d in diffs]
            if abs(sum(signed) / n) >= observed - 1e-12:
                extreme += 1
            total += 1
    return {
        "test": "paired_permutation",
        "metric": metric,
        "n_pairs": n,
        "observed_mean_diff": sum(diffs) / n,
        "missing_pairs": missing,
        "p_value": extreme / total if total else None,
    }


def task_clustered_bootstrap_ci(
    baseline_records: list[dict],
    condition_records: list[dict],
    *,
    metric: str,
    iterations: int = 2_000,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Bootstrap paired mean difference with task_id as the resampling cluster."""
    pairs, missing = exact_pair_matches(baseline_records, condition_records, metric=metric)
    clusters: dict[str, list[float]] = {}
    for base, cond in pairs:
        tid = str(base.get("task_id") or cond.get("task_id") or base.get("pair_id"))
        diff = float(cond.get(metric, 0)) - float(base.get(metric, 0))
        clusters.setdefault(tid, []).append(diff)
    task_ids = sorted(clusters)
    if not task_ids:
        return {
            "metric": metric,
            "n_pairs": 0,
            "n_tasks": 0,
            "missing_pairs": missing,
            "mean_diff": None,
            "ci_low": None,
            "ci_high": None,
        }
    observed_diffs = [d for values in clusters.values() for d in values]
    observed = sum(observed_diffs) / len(observed_diffs)
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        picked = [rng.choice(task_ids) for _ in task_ids]
        vals = [d for tid in picked for d in clusters[tid]]
        samples.append(sum(vals) / len(vals))
    samples.sort()
    low_idx = max(0, int((alpha / 2) * iterations) - 1)
    high_idx = min(iterations - 1, int((1 - alpha / 2) * iterations) - 1)
    return {
        "metric": metric,
        "n_pairs": len(pairs),
        "n_tasks": len(task_ids),
        "missing_pairs": missing,
        "mean_diff": observed,
        "ci_low": samples[low_idx],
        "ci_high": samples[high_idx],
    }


def holm_correction(p_values: list[float | None]) -> list[float | None]:
    """Return Holm-adjusted p-values in the original order."""
    indexed = [(i, p) for i, p in enumerate(p_values) if p is not None]
    m = len(indexed)
    adjusted: list[float | None] = [None] * len(p_values)
    running_max = 0.0
    for rank, (idx, p) in enumerate(sorted(indexed, key=lambda item: item[1]), start=1):
        val = min(1.0, (m - rank + 1) * p)
        running_max = max(running_max, val)
        adjusted[idx] = running_max
    return adjusted


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

    # Build pair_id-indexed lookups for exact (task, repeat) pairing.
    # Fall back to task_id list-order matching for legacy records without pair_id.
    baseline_by_pair: dict[str, dict] = {}
    legacy_baseline_by_task: dict[str, list[dict]] = {}
    for r in baseline_records:
        pid = r.get("pair_id", "")
        if pid:
            baseline_by_pair[pid] = r
        else:
            legacy_baseline_by_task.setdefault(r.get("task_id", ""), []).append(r)

    fault_by_pair: dict[str, dict] = {}
    legacy_fault_by_task: dict[str, list[dict]] = {}
    for r in fault_records:
        pid = r.get("pair_id", "")
        if pid:
            fault_by_pair[pid] = r
        else:
            legacy_fault_by_task.setdefault(r.get("task_id", ""), []).append(r)

    matched_pair_ids = set(baseline_by_pair) & set(fault_by_pair)
    missing_pairs    = len(set(baseline_by_pair) - set(fault_by_pair))

    for metric in metrics:
        # Descriptive stats use all available records (not restricted to pairs)
        b_vals = [r.get(metric, 0.0) for r in baseline_records
                  if isinstance(r.get(metric), (int, float))]
        f_vals = [r.get(metric, 0.0) for r in fault_records
                  if isinstance(r.get(metric), (int, float))]

        b_st = _stats(b_vals)
        f_st = _stats(f_vals)

        # Paired Cohen's d_z — prefer pair_id for exact (task, repeat) matching;
        # fall back to task_id list-order for legacy records without pair_id.
        differences: list[float] = []
        for pid in matched_pair_ids:
            b_r = baseline_by_pair[pid]
            f_r = fault_by_pair[pid]
            bv  = b_r.get(metric)
            fv  = f_r.get(metric)
            if isinstance(bv, (int, float)) and isinstance(fv, (int, float)):
                differences.append(float(fv) - float(bv))
        if not differences:
            # Legacy fallback: pair by task_id list order
            for tid in set(legacy_baseline_by_task) & set(legacy_fault_by_task):
                for b_r, f_r in zip(
                    legacy_baseline_by_task[tid], legacy_fault_by_task[tid]
                ):
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
