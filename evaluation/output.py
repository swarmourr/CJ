"""Output: JSONL loader, CSV writer, LaTeX table generator.

Reads ``runs.jsonl`` and produces:
  - task_results.csv
  - condition_summary.csv
  - validity_summary.csv
  - group_summary.csv (placeholder when no group data)
  - paper tables as LaTeX snippets
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any

from evaluation.analysis.metrics import AggregationMetrics, compute_metrics
from evaluation.analysis.validity import classify_records


def load_jsonl(path: str) -> list[dict]:
    """Load all RunRecord dicts from a JSONL file."""
    if not os.path.exists(path):
        return []
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def write_task_results_csv(records: list[dict], path: str) -> None:
    """Write one row per RunRecord to task_results.csv."""
    if not records:
        print(f"[output] No records — skipping {path}")
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = [
        "run_id", "timestamp", "agent_system", "benchmark", "task_id",
        "model", "seed", "fault_type", "phase", "validity",
        "success", "duration_s", "reported_error", "retries",
        "llm_calls", "tool_calls", "turns",
        "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd",
        "tests_passed", "tests_total", "termination_reason",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)


def write_condition_summary_csv(
    records: list[dict],
    path: str,
) -> None:
    """Write aggregated metrics per (agent_system, benchmark, fault_type).

    Baselines (fault_type="none") are paired with each fault condition that
    shares the same (agent_system, benchmark) so that degradation is computable.
    """
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # Separate baselines from fault records
    baselines  = [r for r in records if r.get("phase") == "baseline"]
    fault_recs = [r for r in records if r.get("phase") == "fault"]

    # Index baselines by (agent_system, benchmark)
    baseline_idx: dict[tuple, list[dict]] = {}
    for r in baselines:
        k = (r.get("agent_system", ""), r.get("benchmark", ""))
        baseline_idx.setdefault(k, []).append(r)

    def fault_key(r: dict):
        return (r.get("agent_system", ""), r.get("benchmark", ""), r.get("fault_type", "none"))

    rows: list[dict] = []

    # One row per fault type — combined with matching baselines so degradation computes
    sorted_faults = sorted(fault_recs, key=fault_key)
    for (sys, bench, fault), group_recs in groupby(sorted_faults, key=fault_key):
        paired_baselines = baseline_idx.get((sys, bench), [])
        combined = paired_baselines + list(group_recs)
        m   = compute_metrics(combined)
        row = {"agent_system": sys, "benchmark": bench, "fault_type": fault}
        row.update(m.to_dict())
        rows.append(row)

    # Baseline-only summary rows
    for (sys, bench), b_recs in sorted(baseline_idx.items()):
        m   = compute_metrics(b_recs)
        row = {"agent_system": sys, "benchmark": bench, "fault_type": "none"}
        row.update(m.to_dict())
        rows.append(row)

    if not rows:
        print(f"[output] No condition data — skipping {path}")
        return

    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_validity_summary_csv(records: list[dict], path: str) -> None:
    """Write validity breakdown per (agent_system, benchmark, fault_type)."""
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def key(r):
        return (r.get("agent_system",""), r.get("benchmark",""), r.get("fault_type","none"))

    sorted_recs = sorted(records, key=key)
    rows: list[dict] = []
    for (sys, bench, fault), group_recs in groupby(sorted_recs, key=key):
        recs = list(group_recs)
        filt = classify_records(recs)
        rows.append({
            "agent_system":        sys,
            "benchmark":           bench,
            "fault_type":          fault,
            "n_baseline":          len(filt.baseline),
            "n_valid":             len(filt.valid),
            "n_invalid":           len(filt.invalid),
            "n_inconclusive":      len(filt.inconclusive),
            "n_untriggered":       len(filt.untriggered),
            "trigger_rate":        filt.trigger_rate,
            "manifestation_rate":  filt.manifestation_rate,
            "recovery_rate":       filt.recovery_rate,
        })

    if not rows:
        print(f"[output] No validity data — skipping {path}")
        return

    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_group_summary_csv(records: list[dict], path: str) -> None:
    """Write group injection summary (from records with group evidence)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    group_recs = [
        r for r in records
        if isinstance(r.get("lifecycle"), dict)
        and r["lifecycle"].get("group_verdict") is not None
    ]
    if not group_recs:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# No group injection records in this campaign\n")
        return
    # Summarize group evidence fields
    cols = ["run_id", "task_id", "fault_type", "group_verdict",
            "activation_skew_ms", "synchronization_valid"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in group_recs:
            lc = r.get("lifecycle", {})
            w.writerow({
                "run_id":               r.get("run_id",""),
                "task_id":              r.get("task_id",""),
                "fault_type":           r.get("fault_type",""),
                "group_verdict":        lc.get("group_verdict",""),
                "activation_skew_ms":   lc.get("activation_skew_ms",""),
                "synchronization_valid":lc.get("synchronization_valid",""),
            })


def write_latex_tables(
    condition_summary_path: str,
    output_dir: str = "results/latex",
) -> None:
    """Generate LaTeX table snippets from condition_summary.csv."""
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.exists(condition_summary_path):
        with open(os.path.join(output_dir, "tables_notice.txt"), "w") as f:
            f.write("NO DATA: condition_summary.csv not found\n")
        return

    rows = []
    with open(condition_summary_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        with open(os.path.join(output_dir, "tables_notice.txt"), "w") as f:
            f.write("NO DATA: condition_summary.csv is empty\n")
        return

    latex = [
        r"\begin{table}[h]",
        r"\centering",
        r"\caption{Agent System Resilience under Fault Injection}",
        r"\label{tab:resilience}",
        r"\begin{tabular}{llllrrr}",
        r"\hline",
        r"System & Benchmark & Fault & $n$ & pass@1$_\mathrm{base}$ & "
        r"pass@1$_\mathrm{fault}$ & Degrad. \\",
        r"\hline",
    ]
    for row in rows:
        if row.get("fault_type") == "none":
            continue
        try:
            base  = f"{float(row['pass_at_1_baseline']):.3f}" if row.get('pass_at_1_baseline') not in (None,'','None') else "--"
            fault = f"{float(row['pass_at_1_fault']):.3f}"    if row.get('pass_at_1_fault') not in (None,'','None') else "--"
            degrad= f"{float(row['degradation']):+.3f}"       if row.get('degradation') not in (None,'','None') else "--"
        except (ValueError, TypeError):
            base = fault = degrad = "--"
        n = row.get("n_fault_valid", "--")
        latex.append(
            f"{row.get('agent_system','')} & {row.get('benchmark','')} & "
            f"{row.get('fault_type','')} & {n} & {base} & {fault} & {degrad} \\\\"
        )
    latex += [r"\hline", r"\end{tabular}", r"\end{table}"]

    out = os.path.join(output_dir, "table_resilience.tex")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(latex) + "\n")
    print(f"[output] LaTeX table → {out}")


def generate_all_outputs(
    results_dir: str = "results",
    figures_dir: str | None = None,
    latex_dir:   str | None = None,
) -> None:
    """Load runs.jsonl and generate all output files."""
    from evaluation.analysis.plots import plot_degradation, plot_validity_summary
    from itertools import groupby

    jsonl_path = os.path.join(results_dir, "runs.jsonl")
    records    = load_jsonl(jsonl_path)

    if not records:
        print(f"[output] No records found in {jsonl_path}. Run an experiment first.")
        return

    print(f"[output] Loaded {len(records)} records from {jsonl_path}")

    write_task_results_csv(records, os.path.join(results_dir, "task_results.csv"))
    write_condition_summary_csv(records, os.path.join(results_dir, "condition_summary.csv"))
    write_validity_summary_csv(records, os.path.join(results_dir, "validity_summary.csv"))
    write_group_summary_csv(records, os.path.join(results_dir, "group_summary.csv"))

    fig_dir = figures_dir or os.path.join(results_dir, "figures")
    plot_degradation(
        _load_condition_rows(os.path.join(results_dir, "condition_summary.csv")),
        os.path.join(fig_dir, "degradation.pdf"),
    )
    plot_validity_summary(records, os.path.join(fig_dir, "validity.pdf"))

    latex_out = latex_dir or os.path.join(results_dir, "latex")
    write_latex_tables(os.path.join(results_dir, "condition_summary.csv"), latex_out)


def _load_condition_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))
