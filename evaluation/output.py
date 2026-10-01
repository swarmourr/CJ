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
        "study_id", "run_id", "campaign_id", "pair_id", "condition",
        "agent_level", "topology", "fault_target_role",
        "timestamp", "cj_commit",
        "agent_system", "framework", "requested_framework", "framework_native",
        "multi_agent_impl", "benchmark", "task_id",
        "model", "endpoint_type", "config_hash",
        "seed", "fault_type", "phase", "validity",
        "success", "duration_s", "reported_error", "retries",
        "llm_calls", "tool_calls", "turns",
        "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd",
        "tests_passed", "tests_total", "termination_reason",
        "lifecycle.triggered", "lifecycle.manifested", "lifecycle.reverted", "lifecycle.recovered",
        "lifecycle.activated", "lifecycle.verdict",
        "framework_version", "python_version",
        "docker_image", "docker_image_digest",
    ]

    def _flatten(r: dict) -> dict:
        out = dict(r)
        lc = r.get("lifecycle") or {}
        if isinstance(lc, dict):
            out["lifecycle.triggered"]  = lc.get("triggered", "")
            out["lifecycle.manifested"] = lc.get("manifested", "")
            out["lifecycle.reverted"]   = lc.get("reverted", "")
            out["lifecycle.recovered"]  = lc.get("recovered", "")
            out["lifecycle.activated"]  = lc.get("activated", "")
            out["lifecycle.verdict"]    = lc.get("verdict", "")
        return out

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(_flatten(r) for r in records)


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
    baselines  = [r for r in records if r.get("condition") == "direct_baseline"]
    fault_recs = [r for r in records if r.get("phase") == "fault"]

    # Index baselines by pair_id (exact pairing) and by (agent_system, benchmark)
    # for fallback when pair_id is absent (records from older schema).
    baseline_by_pair:  dict[str, dict] = {}
    baseline_by_ab:    dict[tuple, list[dict]] = {}
    for r in baselines:
        pid = r.get("pair_id", "")
        if pid:
            baseline_by_pair[pid] = r
        k = (r.get("agent_system", ""), r.get("benchmark", ""))
        baseline_by_ab.setdefault(k, []).append(r)

    def fault_key(r: dict):
        return (r.get("agent_system", ""), r.get("benchmark", ""), r.get("fault_type", "none"))

    rows: list[dict] = []

    # One row per (agent_system, benchmark, fault_type).
    # For each fault record, find its paired baseline via pair_id (preferred) or
    # via (agent_system, benchmark) match (fallback for records without pair_id).
    sorted_faults = sorted(fault_recs, key=fault_key)
    for (sys, bench, fault), group_recs in groupby(sorted_faults, key=fault_key):
        group_list = list(group_recs)

        paired_baselines: list[dict] = []
        for fr in group_list:
            pid = fr.get("pair_id", "")
            if pid and pid in baseline_by_pair:
                paired_baselines.append(baseline_by_pair[pid])
            # else: no exact match; baseline_by_ab fallback applied below

        # Fall back to all baselines for this (sys, bench) when pair_id is absent
        if not paired_baselines:
            paired_baselines = baseline_by_ab.get((sys, bench), [])

        combined = paired_baselines + group_list
        m   = compute_metrics(combined)
        row = {"agent_system": sys, "benchmark": bench, "fault_type": fault}
        row.update(m.to_dict())
        rows.append(row)

    # Baseline-only summary rows
    for (sys, bench), b_recs in sorted(baseline_by_ab.items()):
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


def write_cj_overhead_summary_csv(records: list[dict], path: str) -> None:
    """Compare direct_baseline with cj_control by exact pair_id."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    direct = {r.get("pair_id"): r for r in records if r.get("condition") == "direct_baseline"}
    controls = [r for r in records if r.get("condition") == "cj_control"]
    rows = []
    for ctrl in controls:
        base = direct.get(ctrl.get("pair_id"))
        if not base:
            continue
        rows.append({
            "pair_id": ctrl.get("pair_id", ""),
            "agent_level": ctrl.get("agent_level", ""),
            "framework": ctrl.get("framework") or ctrl.get("agent_system", ""),
            "topology": ctrl.get("topology", ""),
            "benchmark": ctrl.get("benchmark", ""),
            "task_id": ctrl.get("task_id", ""),
            "duration_overhead_s": float(ctrl.get("duration_s", 0) or 0) - float(base.get("duration_s", 0) or 0),
            "llm_call_overhead": int(ctrl.get("llm_calls", 0) or 0) - int(base.get("llm_calls", 0) or 0),
            "token_overhead": int(ctrl.get("total_tokens", 0) or 0) - int(base.get("total_tokens", 0) or 0),
            "correctness_difference": float(ctrl.get("success", 0) or 0) - float(base.get("success", 0) or 0),
        })
    fields = list(rows[0].keys()) if rows else [
        "pair_id", "agent_level", "framework", "topology", "benchmark", "task_id",
        "duration_overhead_s", "llm_call_overhead", "token_overhead", "correctness_difference",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_multi_agent_process_summary_csv(records: list[dict], path: str) -> None:
    """Summarize process metrics from multi-agent execution traces."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = []
    for r in records:
        if r.get("agent_level") != "multi_agent":
            continue
        result = r.get("result") if isinstance(r.get("result"), dict) else r
        trace = result.get("execution_trace") or []
        roles = {
            e.get("agent_role")
            for e in trace
            if isinstance(e, dict) and e.get("agent_role")
        }
        revisions = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "revision_request")
        handoffs = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "handoff")
        detections = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "error_detection")
        repairs = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "attempted_recovery")
        rows.append({
            "study_id": r.get("study_id", ""),
            "pair_id": r.get("pair_id", ""),
            "run_id": r.get("run_id", ""),
            "condition": r.get("condition", ""),
            "framework": r.get("framework") or r.get("agent_system", ""),
            "topology": r.get("topology", ""),
            "task_id": r.get("task_id", ""),
            "message_count": len(trace),
            "affected_roles": len(roles),
            "successful_handoff_count": handoffs,
            "revision_rounds": revisions,
            "fault_detection_events": detections,
            "attempted_recovery_events": repairs,
            "final_pass1": result.get("success", r.get("success", "")),
        })
    fields = list(rows[0].keys()) if rows else [
        "study_id", "pair_id", "run_id", "condition", "framework", "topology",
        "task_id", "message_count", "affected_roles", "successful_handoff_count",
        "revision_rounds", "fault_detection_events", "attempted_recovery_events",
        "final_pass1",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_study_manifest(records: list[dict], path: str, study_id: str | None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    manifest = {
        "study_id": study_id or (records[0].get("study_id") if records else ""),
        "record_count": len(records),
        "conditions": sorted({r.get("condition", "") for r in records if r.get("condition")}),
        "campaign_ids": sorted({r.get("campaign_id", "") for r in records if r.get("campaign_id")}),
        "agent_levels": sorted({r.get("agent_level", "") for r in records if r.get("agent_level")}),
        "frameworks": sorted({(r.get("framework") or r.get("agent_system", "")) for r in records}),
        "benchmarks": sorted({r.get("benchmark", "") for r in records if r.get("benchmark")}),
        "task_ids": sorted({r.get("task_id", "") for r in records if r.get("task_id")}),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


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


def write_stats_summary_csv(records: list[dict], path: str) -> None:
    """Write per-condition statistical summary with CIs and Cohen's d_z."""
    from evaluation.analysis.statistics import compute_condition_stats
    from evaluation.analysis.validity import classify_records
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def fault_key(r: dict):
        return (r.get("agent_system", ""), r.get("benchmark", ""), r.get("fault_type", "none"))

    all_fault_recs = [r for r in records if r.get("phase") == "fault"]
    all_base_recs  = [r for r in records if r.get("condition") == "direct_baseline"]

    rows: list[dict] = []
    for (sys, bench, fault), group_recs in groupby(
        sorted(all_fault_recs, key=fault_key), key=fault_key
    ):
        group_list = list(group_recs)
        filt = classify_records(group_list)
        # Paired baselines for this condition
        pair_ids = {r.get("pair_id") for r in group_list if r.get("pair_id")}
        if pair_ids:
            base_recs = [r for r in all_base_recs
                         if r.get("agent_system") == sys
                         and r.get("benchmark") == bench
                         and r.get("pair_id") in pair_ids]
        else:
            base_recs = [r for r in all_base_recs
                         if r.get("agent_system") == sys and r.get("benchmark") == bench]

        stats_list = compute_condition_stats(
            baseline_records=base_recs,
            fault_records=filt.valid,
            condition_name=fault,
        )
        for s in stats_list:
            row = {"agent_system": sys, "benchmark": bench}
            row.update(s.to_dict())
            rows.append(row)

    if not rows:
        print(f"[output] No stats data — skipping {path}")
        return

    cols = ["agent_system", "benchmark"] + list(rows[0].keys())[2:]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[output] Stats summary → {path}")


def write_inferential_summary_csv(records: list[dict], path: str) -> None:
    """Write paired, task-aware inferential tests for valid fault records."""
    from evaluation.analysis.statistics import (
        holm_correction,
        mcnemar_exact,
        paired_permutation_test,
        task_clustered_bootstrap_ci,
    )
    from evaluation.analysis.validity import classify_records
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    direct = [r for r in records if r.get("condition") == "direct_baseline"]
    faults = [r for r in records if r.get("phase") == "fault"]

    def key(r: dict):
        return (
            r.get("agent_level", ""),
            r.get("agent_system", ""),
            r.get("topology", ""),
            r.get("benchmark", ""),
            r.get("fault_type", "none"),
        )

    rows: list[dict[str, Any]] = []
    for (level, system, topology, bench, fault), group_recs in groupby(
        sorted(faults, key=key), key=key
    ):
        valid_faults = classify_records(list(group_recs)).valid
        pair_ids = {r.get("pair_id") for r in valid_faults if r.get("pair_id")}
        base = [
            r for r in direct
            if r.get("pair_id") in pair_ids
            and r.get("agent_system") == system
            and r.get("benchmark") == bench
        ]
        if not valid_faults or not base:
            continue

        mcn = mcnemar_exact(base, valid_faults, metric="success")
        boot = task_clustered_bootstrap_ci(base, valid_faults, metric="success")
        rows.append({
            "agent_level": level,
            "agent_system": system,
            "topology": topology,
            "benchmark": bench,
            "fault_type": fault,
            "metric": "success",
            "test": "mcnemar_exact",
            "n_pairs": mcn.get("n_pairs"),
            "n_tasks": boot.get("n_tasks"),
            "effect": boot.get("mean_diff"),
            "ci_low": boot.get("ci_low"),
            "ci_high": boot.get("ci_high"),
            "p_value": mcn.get("p_value"),
            "missing_pairs": mcn.get("missing_pairs"),
            "baseline_only_success": mcn.get("baseline_only_success"),
            "condition_only_success": mcn.get("condition_only_success"),
        })

        for metric in ("duration_s", "llm_calls", "tool_calls", "turns", "total_tokens"):
            perm = paired_permutation_test(base, valid_faults, metric=metric)
            boot = task_clustered_bootstrap_ci(base, valid_faults, metric=metric)
            rows.append({
                "agent_level": level,
                "agent_system": system,
                "topology": topology,
                "benchmark": bench,
                "fault_type": fault,
                "metric": metric,
                "test": "paired_permutation",
                "n_pairs": perm.get("n_pairs"),
                "n_tasks": boot.get("n_tasks"),
                "effect": boot.get("mean_diff"),
                "ci_low": boot.get("ci_low"),
                "ci_high": boot.get("ci_high"),
                "p_value": perm.get("p_value"),
                "missing_pairs": perm.get("missing_pairs"),
                "baseline_only_success": "",
                "condition_only_success": "",
            })

    p_adjusted = holm_correction([
        float(r["p_value"]) if r.get("p_value") is not None else None
        for r in rows
    ])
    for row, p_holm in zip(rows, p_adjusted):
        row["p_value_holm"] = p_holm

    fields = [
        "agent_level", "agent_system", "topology", "benchmark", "fault_type",
        "metric", "test", "n_pairs", "n_tasks", "effect", "ci_low", "ci_high",
        "p_value", "p_value_holm", "missing_pairs",
        "baseline_only_success", "condition_only_success",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"[output] Inferential summary → {path}")


def generate_all_outputs(
    results_dir: str = "results",
    figures_dir: str | None = None,
    latex_dir:   str | None = None,
    campaign_id: str | None = None,
    study_id: str | None = None,
) -> None:
    """Load runs.jsonl and generate all output files.

    Parameters
    ----------
    study_id : str, optional
        When provided, only records from this exact study are processed. Legacy
        records without a study_id are excluded.
    campaign_id : str, optional
        When provided, only records from this campaign are processed.
        Prevents mixing data from old and new runs in the same JSONL file.
    """
    from evaluation.analysis.plots import plot_degradation, plot_validity_summary
    from itertools import groupby

    jsonl_path = os.path.join(results_dir, "runs.jsonl")
    all_records = load_jsonl(jsonl_path)

    if not all_records:
        print(f"[output] No records found in {jsonl_path}. Run an experiment first.")
        return

    if study_id:
        records = [r for r in all_records if r.get("study_id") == study_id]
        print(f"[output] Loaded {len(records)}/{len(all_records)} records "
              f"for study {study_id} from {jsonl_path}")
    elif campaign_id:
        records = [r for r in all_records if r.get("campaign_id") == campaign_id]
        print(f"[output] Loaded {len(records)}/{len(all_records)} records "
              f"for campaign {campaign_id[:8]} from {jsonl_path}")
    else:
        records = all_records
        print(f"[output] Loaded {len(records)} records from {jsonl_path}")

    write_task_results_csv(records, os.path.join(results_dir, "task_results.csv"))
    write_condition_summary_csv(records, os.path.join(results_dir, "condition_summary.csv"))
    write_validity_summary_csv(records, os.path.join(results_dir, "validity_summary.csv"))
    write_group_summary_csv(records, os.path.join(results_dir, "group_summary.csv"))
    write_cj_overhead_summary_csv(records, os.path.join(results_dir, "cj_overhead_summary.csv"))
    write_multi_agent_process_summary_csv(records, os.path.join(results_dir, "multi_agent_process_summary.csv"))
    write_stats_summary_csv(records, os.path.join(results_dir, "stats_summary.csv"))
    write_inferential_summary_csv(records, os.path.join(results_dir, "inferential_summary.csv"))
    write_study_manifest(records, os.path.join(results_dir, "study_manifest.json"), study_id)

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
